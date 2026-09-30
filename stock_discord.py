"""上市＋上櫃盤後強勢股前五名，推播至 Discord。Python 3.11+，無第三方套件。"""
import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TWSE = 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'
TWSE_DATED = 'https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999'
TPEX = 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'
TZ = ZoneInfo('Asia/Taipei')


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'TaiwanStockDiscord/1.0', 'Accept': 'application/json'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.load(resp)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def num(value):
    try:
        x = float(str(value).strip().replace(',', ''))
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def iso_date(raw):
    s = re.sub(r'\D', '', str(raw))
    if len(s) == 7:  # 民國年，例如 1150930
        return f'{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:]}'
    if len(s) == 8:
        return f'{s[:4]}-{s[4:6]}-{s[6:]}'
    return ''


def normalized(row, market):
    if market == '上市':
        code = str(row.get('Code', '')).strip()
        name = str(row.get('Name', '')).strip()
        date = iso_date(row.get('Date', ''))
        op, hi, lo, cl = [num(row.get(k)) for k in ('OpeningPrice', 'HighestPrice', 'LowestPrice', 'ClosingPrice')]
        shares = num(row.get('TradeVolume'))
        change = num(row.get('Change'))
    else:
        code = str(row.get('SecuritiesCompanyCode', '')).strip()
        name = str(row.get('CompanyName', row.get('SecuritiesCompanyName', ''))).strip()
        date = iso_date(row.get('Date', ''))
        op = num(row.get('Open', row.get('OpeningPrice')))
        hi = num(row.get('High', row.get('HighestPrice')))
        lo = num(row.get('Low', row.get('LowestPrice')))
        cl = num(row.get('Close', row.get('ClosingPrice')))
        shares = num(row.get('TradingShares', row.get('TradeVolume')))
        change = num(row.get('Change', row.get('PriceChange')))
    if not re.fullmatch(r'\d{4}', code) or min(op, hi, lo, cl) <= 0 or shares <= 0 or hi < lo:
        return None
    return dict(code=code, name=name, market=market, date=date, open=op, high=hi, low=lo, close=cl,
                lots=shares / 1000, change=change)


def listed_for_date(today):
    """先讀上市快照；若尚停在前一日，改查證交所指定日期的行情。"""
    snapshot = get_json(TWSE)
    rows = [s for row in snapshot if (s := normalized(row, '上市')) and s['date'] == today]
    if rows:
        print(f'上市快照：{today}，{len(rows)} 檔')
        return rows

    last_date = iso_date(snapshot[0].get('Date', '')) if snapshot else '無資料'
    print(f'上市快照日期 {last_date}，改查指定日期 {today}')
    data = get_json(TWSE_DATED.format(date=today.replace('-', '')))
    if data.get('stat') != 'OK':
        print(f'證交所指定日期尚無資料：{data.get("stat", "未知狀態")}')
        return []
    reported = iso_date(data.get('date', ''))
    if reported and reported != today:
        print(f'證交所指定日期回傳 {reported}，略過')
        return []
    # 證交所可能調整表格編號；以欄名尋找每日個股行情表。
    tables = [(data.get(f'fields{i}', []), data.get(f'data{i}', [])) for i in range(1, 20)]
    tables += [(table.get('fields', []), table.get('data', []))
               for table in data.get('tables', []) if isinstance(table, dict)]
    required = ('證券代號', '證券名稱', '成交股數', '開盤價', '最高價', '最低價', '收盤價', '漲跌價差')
    fields, records = next(((f, r) for f, r in tables
                            if r and all(any(k in str(col) for col in f) for k in required)), ([], []))
    if not records:
        preview = [(str(k), len(v)) for k, v in data.items() if k.startswith(('fields', 'data')) and isinstance(v, list)]
        raise RuntimeError(f'證交所無個股行情表；收到的表格：{preview}，請檢查資料來源')
    idx = {key: next(i for i, col in enumerate(fields) if key in str(col)) for key in required}
    sign_idx = next((i for i, col in enumerate(fields) if '漲跌(+/-)' in str(col)), None)
    results = []
    for row in records:
        if len(row) < len(fields):
            continue
        code = str(row[idx['證券代號']]).strip()
        if not re.fullmatch(r'\d{4}', code):
            continue
        op, hi, lo, cl = (num(row[idx[key]]) for key in ('開盤價', '最高價', '最低價', '收盤價'))
        shares = num(row[idx['成交股數']])
        if min(op, hi, lo, cl) <= 0 or shares <= 0 or hi < lo:
            continue
        sign = str(row[sign_idx]).strip() if sign_idx is not None else ''
        change = num(row[idx['漲跌價差']]) * (-1 if '-' in sign or '－' in sign else 1)
        results.append(dict(code=code, name=str(row[idx['證券名稱']]).strip(), market='上市',
                            date=today, open=op, high=hi, low=lo, close=cl,
                            lots=shares / 1000, change=change))
    print(f'上市指定日期：{today}，{len(results)} 檔')
    return results


def evaluate(s):
    op, hi, lo, cl = s['open'], s['high'], s['low'], s['close']
    previous = cl - s['change'] if s['change'] else 0
    pct = 100 * s['change'] / previous if previous > 0 else 0
    span = max(hi - lo, 0.001)
    # 僅使用單日可驗證的 5 項；不假裝具備歷史均量、法人或 5 分鐘資料。
    checks = [
        ('收紅', cl > op),
        ('較前收上漲', 0 < pct < 9.8),
        ('收近高點', (cl - lo) / span >= 0.7),
        ('實體明顯', (cl - op) / span >= 0.35),
        ('有成交量', s['lots'] >= 1000),
    ]
    s.update(score=20 * sum(ok for _, ok in checks), passed=sum(ok for _, ok in checks),
             pct=pct, checks=checks)
    return s


def fmt(x):
    return f'{x:,.2f}'.rstrip('0').rstrip('.')


def message(stocks, date):
    embeds = []
    for rank, s in enumerate(stocks, 1):
        cl, hi, lo = s['close'], s['high'], s['low']
        # 參考區間來自當日 K 棒，隔日會變動；不是委託價格或獲利承諾。
        entry_lo = max(s['open'], lo)
        entry_hi = cl
        if entry_lo > entry_hi:
            entry_lo = lo
        conditions = '、'.join(label for label, ok in s['checks'] if ok)
        embeds.append({
            'title': f'#{rank}｜{s["code"]} {s["name"]}｜{s["market"]}｜{s["score"]} 分',
            'color': 0xFA476B if s['score'] >= 80 else 0x7289DA,
            'description': (f'**收盤 {fmt(cl)}　漲跌 {s["pct"]:+.2f}%　成交 {s["lots"]:,.0f} 張**\n'
                            f'強勢條件 {s["passed"]}/5：{conditions}\n\n'
                            f'**① 開盤觀察**：留意是否跳空過大，不以盤後資料預判開盤。\n'
                            f'**② 盤中劇本**：先觀察前 5 分鐘是否守住開盤價。\n'
                            f'**③ 第一個動作**：等待前 5 分鐘結束，再評估。\n\n'
                            f'**隔日觀察區**：{fmt(entry_lo)}～{fmt(entry_hi)}（前日開盤至收盤）\n'
                            f'**參考防守**：前日低點 {fmt(lo)}　**參考壓力**：前日高點 {fmt(hi)}\n'
                            f'開高但量價未同步時不追價；跌破防守價應重新評估。'),
            'footer': {'text': f'資料日 {date}｜盤後資料；價位僅為前日 K 棒參考，非即時訊號或投資建議'}
        })
    return {'content': f'📊 **台股盤後做多強勢股 TOP {len(stocks)}｜{date}**\n上市＋上櫃｜單日 5 條件評分，滿分 100；同分依漲幅、成交量排序。',
            'embeds': embeds, 'allowed_mentions': {'parse': []}}


def send(webhook, payload):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(webhook, data=data, headers={'Content-Type': 'application/json', 'User-Agent': 'TaiwanStockDiscord/1.0'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f'Discord HTTP {resp.status}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='預覽 JSON，不送 Discord')
    parser.add_argument('--allow-old-data', action='store_true', help='僅供資料格式測試；允許舊交易日')
    args = parser.parse_args()
    now = datetime.now(TZ)
    if not args.allow_old_data and now.weekday() >= 5:
        print('非交易日，略過。')
        return
    today = now.date().isoformat()
    listed = listed_for_date(today)
    otc_raw = get_json(TPEX)
    otc = [s for row in otc_raw if (s := normalized(row, '上櫃'))]
    print(f'上櫃資料日期：{sorted({s["date"] for s in otc})[-3:]}，有效 {len(otc)} 檔')
    stocks = listed + otc
    current = [evaluate(s) for s in stocks if s['date'] == today]
    if not current and not args.allow_old_data:
        print(f'{today} 兩市場資料尚未齊全或休市，略過推播。')
        return
    if args.allow_old_data and not current:
        latest = max((s['date'] for s in stocks), default='')
        current = [evaluate(s) for s in stocks if s['date'] == latest]
    markets = {s['market'] for s in current}
    if markets != {'上市', '上櫃'}:
        raise RuntimeError(f'資料不同步或缺少市場：{markets}；不發送不完整排行')
    selected = sorted((s for s in current if s['score'] >= 60 and s['pct'] > 0),
                      key=lambda s: (s['score'], s['pct'], s['lots']), reverse=True)[:5]
    if not selected:
        print('今天沒有達標股票。')
        return
    payload = message(selected, current[0]['date'])
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
        if not webhook.startswith('https://discord.com/api/webhooks/'):
            raise RuntimeError('請設定 DISCORD_WEBHOOK_URL（GitHub Actions Secret）')
        send(webhook, payload)
        print(f'已推播 {len(selected)} 檔；資料日 {current[0]["date"]}')


if __name__ == '__main__':
    main()
