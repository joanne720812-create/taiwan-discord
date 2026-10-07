"""盤後候選／通知復盤。只使用當日官方行情及事前留存紀錄，不回填選股。"""
import argparse
import hashlib
import http.client
import json
import math
import os
import re
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import short_discord as base
import stock_discord as ranking

GROUPS = (
    ('long', '🔴 盤後做多', 'long10_candidates.json', 'stocks', 'long'),
    ('short', '🟢 盤後做空', 'shortlist.json', 'stocks', 'short'),
    ('rising', '📈 起漲股', 'rising5.json', 'top10', 'long'),
    ('rsi', 'RSI強勢觀察', 'rsi10_candidates.json', 'stocks', 'long'),
    ('swing', '📈 波段買點（首日觀察）', 'swing_shortlist.json', 'stocks', 'long'),
    ('fib', '📐 斐波起漲股', 'fibonacci10.json', 'stocks', 'long'),
    ('3374', '精材3374專案', 'stock3374.json', None, 'observation'),
)


def finite(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def documents(root):
    """Archive timestamps determine provenance/order; extraction cannot traverse paths."""
    result = []
    for archive in sorted(Path(root).glob('*.zip')):
        try:
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    if name.endswith('.json') and z.getinfo(name).file_size < 20_000_000:
                        obj = json.loads(z.read(name))
                        if isinstance(obj, dict):
                            result.append((archive.name, Path(name).name, obj))
        except (OSError, ValueError, zipfile.BadZipFile):
            print('略過無效封存檔：', archive.name)
    return result


def select_candidates(docs, previous_day):
    result = {}
    for key, label, filename, field, direction in GROUPS:
        options = [(origin, obj) for origin, name, obj in docs
                   if name == filename and obj.get('asof', obj.get('levels_date')) == previous_day]
        # 首次設定前，做多名單取當日監控實際載入快照；不重跑昨日選股。
        if key == 'long' and not options:
            options = [(origin, {**obj, 'stocks': [s for s in obj.get('stocks', [])
                                                  if s.get('direction') == 'long']})
                       for origin, name, obj in docs if name == 'monitor_candidates_5m.json'
                       and obj.get('asof') == previous_day]
        if not options:
            result[key] = dict(label=label, direction=direction, stocks=[], status='缺少前一交易日留存名單')
            continue
        origin, report = max(options, key=lambda pair: pair[0])
        stocks = report.get(field, []) if field else [report]
        result[key] = dict(label=label, direction=direction, stocks=stocks, origin=origin,
                          status='已留存' if stocks else '留存名單為空（沒有合格候選）')
    return result


def otc_json():
    # 部分櫃買回應的Content-Length與實際JSON大小不同；逐塊讀至EOF，
    # 仍須完整通過JSON解碼及交易日核對，截斷內容絕不作行情使用。
    for attempt in range(3):
        try:
            req = urllib.request.Request(ranking.TPEX, headers={'User-Agent': 'Mozilla/5.0',
                                          'Accept': 'application/json', 'Accept-Encoding': 'identity'})
            with urllib.request.urlopen(req, timeout=35) as response:
                chunks, size = [], 0
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > 20_000_000:
                        raise ValueError('官方行情回應過大')
                    chunks.append(chunk)
            result = json.loads(b''.join(chunks))
            if not isinstance(result, list):
                raise ValueError('官方上櫃行情格式錯誤')
            return result
        except (OSError, ValueError, http.client.IncompleteRead):
            if attempt == 2:
                raise RuntimeError('上櫃官方行情不完整，不發送錯誤復盤') from None
            time.sleep(2 ** attempt)


def quotes_for(day):
    listed = ranking.listed_for_date(day)
    otc = [s for row in otc_json() if (s := ranking.normalized(row, '上櫃'))
           and s['date'] == day]
    if not listed or not otc:
        raise RuntimeError('兩市場當日官方資料未齊，不發送舊日行情復盤')
    return {s['code']: s for s in listed + otc}


def candidate_row(stock, quote, direction):
    if quote is None:
        return dict(code=stock.get('code', '?'), name=stock.get('name', ''), status='缺少當日官方行情')
    op, hi, lo, cl = (finite(quote.get(k)) for k in ('open', 'high', 'low', 'close'))
    reference = finite(stock.get('close'))
    if any(x is None or x <= 0 for x in (op, hi, lo, cl, reference)) or not lo <= min(op, cl) <= max(op, cl) <= hi:
        return dict(code=stock.get('code', '?'), name=stock.get('name', ''), status='行情無效')
    # 候選參考价必須與官方前收核對，避免錯日、除權息或參考價調整混算。
    official_previous = cl - quote['change']
    if abs(reference - official_previous) > max(0.05, reference * 0.001):
        return dict(code=stock['code'], name=stock.get('name', ''), status='前收不一致／可能價格調整，不計表現')
    change = 100 * (cl / reference - 1)
    sign = -1 if direction == 'short' else 1
    touches = []
    for label, fields in (('壓', ('cdp_resistance', 'resistance')), ('界', ('pivot',)), ('撐', ('support',))):
        level = next((finite(stock.get(f)) for f in fields if finite(stock.get(f)) is not None), None)
        if level is not None and lo <= level <= hi:
            touches.append(label)
    return dict(code=stock['code'], name=stock.get('name', ''), previous=reference,
                open=op, high=hi, low=lo, close=cl, change_pct=change,
                direction_pct=sign * change if direction != 'observation' else None,
                touches='、'.join(touches) or '無', status='有效')


def parsed_events(docs, day):
    """事件記錄可能先存再送；因此稱為『通知紀錄』，不是交割／成交紀錄。"""
    events = {}
    notices = {}
    for origin, name, obj in sorted(docs):
        if name in ('events_5m.json', 'stock3374_events.json'):
            entries = obj.get('events', {}) if name.startswith('stock3374') else obj
            for key, hit in entries.items():
                m = re.fullmatch(r'(\d{4}-\d{2}-\d{2}):([^:]+):(long|short):(.+)', key)
                if m and m[1] == day and isinstance(hit, dict):
                    try:
                        end = datetime.fromisoformat(hit.get('bar_at', m[4])) + timedelta(minutes=5)
                    except (ValueError, TypeError):
                        continue
                    group = '精材5分K' if name.startswith('stock3374') else '多空5分K'
                    events[(group, key)] = dict(group=group, ticker=m[2], direction=m[3],
                                               end=end.isoformat(), price=hit.get('price'),
                                               stop=hit.get('stop'), age=hit.get('age_minutes'))
        elif name in ('rising5_events.json', 'rsi10_events.json') and obj.get('date') == day:
            group = '起漲5分K' if name.startswith('rising') else 'RSI5分K'
            for key in obj.get('sent', []):
                if not isinstance(key, str):
                    continue
                ticker, sep, end = key.partition(':')
                try:
                    stamp = datetime.fromisoformat(end)
                except ValueError:
                    continue
                if sep and stamp.date().isoformat() == day:
                    events[(group, key)] = dict(group=group, ticker=ticker, direction='long',
                                               end=end, price=None, stop=None, age=None)
            notices[group] = len([e for e in events.values() if e['group'] == group])
        elif name == 'crossings_5m.json':
            for key, hit in obj.items():
                if key.startswith(day + ':') and isinstance(hit, dict):
                    confirmed = key.startswith(day + ':confirmed:')
                    ticker = key.split(':')[2 if confirmed else 1]
                    notices[key] = dict(ticker=ticker, kind='收盤穿越' if confirmed else '觸价',
                                        price=finite(hit.get('price')), bar_at=hit.get('bar_at', ''),
                                        levels=hit.get('crossed', []))
    return list(events.values()), [v for v in notices.values() if isinstance(v, dict)]


def signal_rows(events, quotes, bars):
    groups = {}
    for e in events:
        groups.setdefault((e['group'], e['ticker'], e['direction']), []).append(e)
    result = []
    for (group, ticker, direction), entries in sorted(groups.items()):
        entries.sort(key=lambda e: e['end'])
        first = entries[0]
        end = datetime.fromisoformat(first['end'])
        price = finite(first['price'])
        reconstructed = False
        if price is None:
            frame = bars.get(ticker)
            stamp = end - timedelta(minutes=5)
            if frame is not None and stamp in frame.index:
                price = finite(frame.loc[stamp, 'Close'])
                reconstructed = True
        quote = quotes.get(ticker.split('.')[0])
        closing = finite(quote.get('close')) if quote else None
        pct = (100 * (closing / price - 1) * (-1 if direction == 'short' else 1)
               if price and price > 0 and closing and closing > 0 else None)
        result.append(dict(group=group, code=ticker.split('.')[0], name=quote.get('name', '') if quote else '',
                           direction=direction, count=len(entries), end=first['end'], price=price,
                           direction_pct=pct, stop=finite(first['stop']), age=finite(first['age']),
                           reconstructed=reconstructed))
    return result


def payloads(report):
    day = report['day']
    summary = [f'📊 **每日全策略復盤｜{day}**', f"比較基準：{report['previous_day']}留存候選 → {day}官方收盤",
               '候選方向表現＝多單漲跌幅／空單反向漲跌幅；不是實際交易損益或勝率。',
               '波段僅列首日觀察；當日盤後新名單供下一交易日使用，不回填今天績效。']
    for group in report['groups'].values():
        valid = [r for r in group['rows'] if r['status'] == '有效' and r.get('direction_pct') is not None]
        positive = sum(r['direction_pct'] > 0 for r in valid)
        negative = sum(r['direction_pct'] < 0 for r in valid)
        avg = sum(r['direction_pct'] for r in valid) / len(valid) if valid else None
        summary.append(f"{group['label']}｜候選{len(group['rows'])}｜有效{len(valid)}｜順向{positive}／逆向{negative}"
                       + (f'｜平均方向表現{avg:+.2f}%' if avg is not None else f"｜{group['status']}"))
    summary.append(f"盤中通知紀錄{len(report['events'])}筆；每檔方向只用最早訊號作收盤比較，避免重複計績效。")
    notices = report.get('notices', [])
    summary.append(f"觸價／收盤穿越紀錄{len(notices)}筆；只表示價格事件，不算進場。")
    output = [{'content': '\n'.join(summary), 'allowed_mentions': {'parse': []}}]
    for group in report['groups'].values():
        embeds = []
        for row in group['rows']:
            if row['status'] != '有效':
                description = row['status']
            else:
                description = (f"前收{row['previous']:.2f}｜開{row['open']:.2f}｜高{row['high']:.2f}｜低{row['low']:.2f}｜收{row['close']:.2f}\n"
                               f"當日漲跌{row['change_pct']:+.2f}%｜日K範圍觸及：{row['touches']}（不判定先後）")
                if row['direction_pct'] is not None:
                    description += f"\n候選方向表現 **{row['direction_pct']:+.2f}%**｜僅名單觀察，未確認進場"
            embeds.append(dict(title=f"{row['code']} {row['name']}", description=description,
                               color=0x00B875 if group['direction'] == 'short' else 0xFF253A))
        if not embeds:
            embeds = [dict(title=group['label'], description=group['status'], color=0x999999)]
        for start in range(0, len(embeds), 5):
            output.append(dict(content=f"**{group['label']}｜候選復盤表｜{day}**", embeds=embeds[start:start+5],
                               allowed_mentions={'parse': []}))
    for category in ('多空5分K', '起漲5分K', 'RSI5分K', '精材5分K'):
        rows = [r for r in report['signals'] if r['group'] == category]
        embeds = []
        for row in rows:
            price = f"{row['price']:.2f}" if row['price'] else '資料不足'
            performance = f"{row['direction_pct']:+.2f}%" if row['direction_pct'] is not None else '資料不足'
            stamp = datetime.fromisoformat(row['end'])
            description = (f"通知紀錄{row['count']}次｜最早訊號K收盤{stamp:%H:%M}\n"
                           f"訊號參考{price}｜截至收盤方向表現 **{performance}**\n"
                           + ('參考價由該訊號5分K歷史收盤重建；' if row['reconstructed'] else '')
                           + '未收到券商成交回報；缺少完整出場紀錄，不計已實現損益。')
            if row['age'] is not None:
                description += f"\n訊號當時行情落後{row['age']:.1f}分"
            if row['stop'] is not None:
                description += f"｜初始失效參考{row['stop']:.2f}（未推定出場）"
            embeds.append(dict(title=f"{'🟢 做空' if row['direction'] == 'short' else '🔴 做多'}｜{row['code']} {row['name']}",
                               description=description, color=0x00B875 if row['direction'] == 'short' else 0xFF253A))
        if not embeds:
            embeds = [dict(title=category, description='沒有當日可用通知紀錄；可能未觸發或紀錄不足，不能視為已完整監控。', color=0x999999)]
        for start in range(0, len(embeds), 5):
            output.append(dict(content=f'**{category}｜訊號復盤表｜{day}**', embeds=embeds[start:start+5],
                               allowed_mentions={'parse': []}))
    counts = {}
    for notice in notices:
        counter = counts.setdefault(notice['ticker'], {'觸价': 0, '收盤穿越': 0})
        counter[notice['kind']] += 1
    notice_embeds = [dict(title=ticker.split('.')[0], color=0x999999,
                         description=f"觸價{c['觸价']}次｜5分K收盤穿越{c['收盤穿越']}次\n價格通知紀錄，不是交易進場或成交。")
                     for ticker, c in sorted(counts.items())]
    if not notice_embeds:
        notice_embeds = [dict(title='到價與收盤穿越', description='沒有當日留存紀錄，未推定完整監控。', color=0x999999)]
    for start in range(0, len(notice_embeds), 5):
        output.append(dict(content=f'**到價／穿越復盤表｜{day}**', embeds=notice_embeds[start:start+5],
                           allowed_mentions={'parse': []}))
    return output


def deliver(report, state_dir, webhook, resend=False):
    if not re.fullmatch(r'https://discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]+', webhook):
        raise RuntimeError('請設定獨立復盤頻道的DISCORD_REVIEW_WEBHOOK_URL')
    path = Path(state_dir) / ('delivery_' + report['day'] + '.json')
    fingerprint = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    saved = json.loads(path.read_text()) if path.exists() else {}
    parts = payloads(report)
    # 只要當日已完整送出就不自動重送；補發需明確resend，允許資料補齊後重發。
    if saved.get('complete') and not resend:
        print('當日復盤已推播，略過重送')
        return
    next_part = saved.get('next_part', 0) if saved.get('fingerprint') == fingerprint and not resend else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(next_part, len(parts)):
        body = json.dumps(parts[i], ensure_ascii=False).encode()
        req = urllib.request.Request(webhook + '?wait=true', data=body,
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            accepted = json.load(response)
        if not accepted.get('id') or len(accepted.get('embeds', [])) != len(parts[i].get('embeds', [])):
            raise RuntimeError('Discord未確認復盤卡片，不標記完成')
        saved = dict(fingerprint=fingerprint, next_part=i+1, complete=i+1 == len(parts))
        path.write_text(json.dumps(saved), encoding='utf-8')
        time.sleep(1)
    print(f"Discord已確認收到{len(parts)}組復盤訊息")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=base.now_tw().date().isoformat())
    parser.add_argument('--archives', default='review_inputs')
    parser.add_argument('--state', default='review_state')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--resend', action='store_true')
    args = parser.parse_args()
    day = date.fromisoformat(args.date)
    if day > base.now_tw().date() or (day == base.now_tw().date() and base.now_tw().hour < 15):
        raise RuntimeError('復盤必須在盤後，不能使用未來日或盤中資料')
    if day.weekday() >= 5:
        print('週末略過')
        return
    quotes = quotes_for(args.date)
    market = base.frame_for(base.download(['^TWII'], '1d', '1mo'), '^TWII')
    prior = [s.date() for s in market.index if s.date() < day]
    if not prior or (day-max(prior)).days > 7:
        raise RuntimeError('無法確認前一交易日，不猜測復盤基準')
    previous = max(prior).isoformat()
    docs = documents(args.archives)
    groups = select_candidates(docs, previous)
    for g in groups.values():
        g['rows'] = [candidate_row(s, quotes.get(s.get('code')), g['direction']) for s in g.pop('stocks')]
    events, notices = parsed_events(docs, args.date)
    tickers = sorted({e['ticker'] for e in events if e['price'] is None})
    bars = {}
    for start in range(0, len(tickers), 20):
        data = base.download(tickers[start:start+20], '5m', '5d')
        for ticker in tickers[start:start+20]:
            bars[ticker] = base.completed_bars(base.frame_for(data, ticker), datetime.combine(day, datetime.min.time(), base.TZ)+timedelta(hours=14))
    report = dict(day=args.date, previous_day=previous, groups=groups, events=events, notices=notices,
                  signals=signal_rows(events, quotes, bars))
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    (state / ('review_' + args.date + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.dry_run:
        print(json.dumps(payloads(report), ensure_ascii=False, indent=2))
    else:
        deliver(report, state, os.environ.get('DISCORD_REVIEW_WEBHOOK_URL', ''), args.resend)


if __name__ == '__main__':
    main()
