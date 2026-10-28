"""抓取 funds.json 列出的基金 / ETF 歷史淨值，產生 report.html 績效比較報告。

用法: python build_report.py [funds.json] [report.html]

資料來源:
  nomura - 野村投信官網 API (fundId 見基金頁網址的 gradeId)
  yahoo  - Yahoo Finance 含息調整價 (symbol 例如 0050.TW、00878.TW)
所有序列都轉成「總報酬指數」(配息再投入)，績效才可以互相比較。
"""
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

# 野村官網憑證缺少 Subject Key Identifier，Python 3.13+ 的嚴格模式會拒絕；
# 只關閉 X509 嚴格檢查，憑證鏈與主機名稱驗證照常進行。
SSL_CTX = ssl.create_default_context()
SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT


def http(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        return json.load(r)


def fetch_nomura(inst):
    res = http("https://www.nomurafunds.com.tw/API/Content/Fund/NetWorths",
               {"fundIdList": [inst["fundId"]]})
    if res["responseHeader"]["returnCode"] != "C0000":
        raise RuntimeError(res["responseHeader"]["returnMsg"])
    fund = res["responseBody"]["data"][0]
    series, navs, idx, prev = [], {}, None, None
    for row in fund["dayAchievements"]:
        nav = row["netWorth"]
        if not nav:
            continue
        div = row.get("amountOfInterest") or 0
        idx = nav if idx is None else idx * (nav + div) / prev
        prev = nav
        day = row["day"].replace("/", "-")
        navs[day] = nav
        series.append((day, idx))

    # MoneyDJ 通常比野村官網早公布當天淨值：官網還沒有的最新幾天用 MoneyDJ 補上
    if inst.get("moneydj"):
        try:
            recent = fetch_moneydj(inst["moneydj"])
        except Exception as e:
            print(f"  {inst['name']}: MoneyDJ 查詢失敗，只用野村官網資料: {e}")
            recent = []
        overlap = [(d, v) for d, v in recent if d in navs]
        mismatch = [(d, v, navs[d]) for d, v in overlap if abs(v - navs[d]) > 0.005]
        if not overlap or mismatch:
            print(f"  {inst['name']}: MoneyDJ 淨值與官網對不上，不採用 {mismatch[:3]}")
        else:
            for d, v in recent:
                if d > series[-1][0]:
                    series.append((d, series[-1][1] * v / prev))
                    prev = v
                    print(f"  {inst['name']}: {d} 官網尚未公布，採用 MoneyDJ 淨值 {v}")
    return series, fund["fundName"]


def fetch_moneydj(code):
    """MoneyDJ 電腦版淨值頁的「最近30日淨值」：[(YYYY-MM-DD, 淨值)]，日期由舊到新。
    手機版 (m.moneydj.com) 更新較慢（2026-10-19 傍晚手機版優質還停在 10/16），所以用電腦版。
    表格只有月/日，年份從「淨值日期 YYYY/MM/DD」往回推（跨年時年份減一）。"""
    req = urllib.request.Request(f"https://www.moneydj.com/funddj/ya/yp010000.djhtm?a={code}",
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        page = r.read().decode("cp950", errors="replace")   # 頁面為 Big5 編碼
    latest = re.search(r">(\d{4})/(\d{2})/(\d{2})</td>", page)
    rows = re.findall(r'<td class="t3n0c1(?:_rev)?">(\d{2})/(\d{2})</td>\s*'
                      r'<td class="t3n1(?:_rev)?">([\d,]+\.\d+)</td>', page)
    if not latest or not rows:
        raise RuntimeError("頁面格式不符，找不到淨值表")
    year, out, last_month = int(latest.group(1)), [], None
    for mm, dd, nav in rows:   # 表格由新到舊
        if last_month is not None and int(mm) > last_month:
            year -= 1
        last_month = int(mm)
        out.append((f"{year}-{mm}-{dd}", float(nav.replace(",", ""))))
    return sorted(out)


def fetch_yahoo(inst):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{inst['symbol']}"
           f"?period1=0&period2={int(datetime.now().timestamp())}&interval=1d&events=div%7Csplit")
    res = http(url)["chart"]["result"][0]
    tz = timezone(timedelta(seconds=res["meta"]["gmtoffset"]))
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    close = res["indicators"]["quote"][0]["close"]
    dates = [datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d") for ts in res["timestamp"]]
    # 盤中或剛收盤時，當天的價格還在變動：收盤 30 分鐘後才採用當天資料
    close_ts = res["meta"].get("currentTradingPeriod", {}).get("regular", {}).get("end")
    if close_ts and datetime.now().timestamp() < close_ts + 1800:
        today = datetime.fromtimestamp(close_ts, tz).strftime("%Y-%m-%d")
        if dates and dates[-1] == today:
            dates = dates[:-1]   # 後面的 zip() 會一併截掉 adj / close 的最後一筆
    series, factor = {}, {}
    for d, v, c in zip(dates, adj, close):
        if v:
            series[d] = v
            if c:
                factor[d] = v / c   # 調整價 / 收盤價，用來把補來的收盤價換成調整價
    series = sorted(series.items())
    if inst["symbol"].upper().endswith(".TW"):
        close_by_date = {d: c for d, c in zip(dates, close) if c}
        series = dict(fix_dividends(inst, series, close_by_date))
        factor = {d: v / close_by_date[d] for d, v in series.items() if d in close_by_date}
        reconcile_twse(inst, series, factor)
        series = sorted(series.items())
    elif inst["symbol"].upper() == "^TWII":
        # 加權指數沒有除息調整，調整係數固定為 1（2026-10-20 Yahoo 缺加權指數，卡住整份報告的結束日）
        series = dict(series)
        reconcile_twse(inst, series, {d: 1.0 for d in series})
        series = sorted(series.items())
    # 台股有漲跌幅限制（2015 年前 7%、之後 10%），超過的單日變動必為資料錯誤才能清理；
    # 美股等無漲跌幅限制的市場可能真的單日暴跌 50%（如 AAPL 2000-09-29），不可套用
    if inst["symbol"].upper().endswith((".TW", ".TWO")):
        series = clean_yahoo(series, inst["name"])
    return series, res["meta"].get("longName") or inst["symbol"]


CACHE = ROOT / "cache"
_EXDIV = None


def twse_cash_dividends():
    """證交所「除權除息計算結果表」(TWT49U)：{代號: [(除息日, 除息前收盤價, 除息參考價)]}，只取純現金除息。
    已結束的年份不會再變動，存在 cache/ 下次直接讀取。"""
    global _EXDIV
    if _EXDIV is not None:
        return _EXDIV
    _EXDIV = {}
    CACHE.mkdir(exist_ok=True)
    this_year = datetime.now().year
    for y in range(2004, this_year + 1):   # TWT49U 資料從 2003-05 開始
        f = CACHE / f"twt49u_{y}.json"
        if f.exists() and y < this_year:
            rows = json.loads(f.read_text(encoding="utf-8"))
        else:
            res = http(f"https://www.twse.com.tw/exchangeReport/TWT49U?response=json"
                       f"&startDate={y}0101&endDate={y}1231")
            rows = res.get("data") or []
            f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            time.sleep(0.5)   # 避免查詢太頻繁被證交所暫時封鎖
        for r in rows:
            if r[6] != "息":
                continue
            yy, rest = r[0].split("年")
            mm, dd = rest.replace("日", "").split("月")
            _EXDIV.setdefault(r[1], []).append((
                f"{int(yy) + 1911}-{int(mm):02d}-{int(dd):02d}",
                float(r[3].replace(",", "")), float(r[4].replace(",", ""))))
    return _EXDIV


def fix_dividends(inst, series, close):
    """以證交所除息參考價校正 Yahoo 含息價：
    Yahoo 缺 2013 年以前的配息（0050、0056），也有調整錯誤的情況（0050 的 2014-10-24 多算一次配息）。
    在每個除息日，正確的含息報酬 = 收盤價報酬 ÷ (除息參考價 / 除息前收盤價)；
    Yahoo 的含息報酬不符時，把該日之前的所有資料等比例縮放。"""
    try:
        events = twse_cash_dividends().get(inst["symbol"].split(".")[0], [])
    except Exception as e:
        print(f"  {inst['name']}: 證交所除息資料查詢失敗，略過配息校正: {e}")
        return series
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    pos = {d: i for i, d in enumerate(dates)}
    for d, prev_close, ref in events:
        i = pos.get(d)
        if not i or dates[i - 1] not in close or d not in close:
            continue
        expected = close[d] / close[dates[i - 1]] / (ref / prev_close)
        actual = vals[i] / vals[i - 1]
        corr = actual / expected
        if abs(corr - 1) > 0.002 and abs(corr - 1) < 0.2:
            vals[:i] = [v * corr for v in vals[:i]]
            print(f"  {inst['name']}: 校正 {d} 除息（Yahoo 含息報酬差 {corr - 1:+.2%}）")
    return list(zip(dates, vals))


_TWSE_MONTH = {}


def twse_month(inst, month):
    """證交所某月的每日收盤：{YYYY-MM-DD: 收盤}。加權指數用 FMTQIK（每日市場成交資訊），個股 / ETF 用 STOCK_DAY。"""
    code = inst["symbol"].split(".")[0]
    ym = month.replace("-", "") + "01"
    if code == "^TWII":
        url, col = f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={ym}", 4
    else:
        url, col = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}&stockNo={code}", 6
    if url not in _TWSE_MONTH:
        out = {}
        for row in http(url).get("data") or []:
            y, m, d = row[0].split("/")
            try:
                out[f"{int(y) + 1911}-{m}-{d}"] = float(row[col].replace(",", ""))
            except ValueError:
                pass   # 停牌等無收盤價的日子
        _TWSE_MONTH[url] = out
        time.sleep(0.3)
    return _TWSE_MONTH[url]


def reconcile_twse(inst, series, factor):
    """以證交所交易日為準，校正 Yahoo 最近 90 天的日期：
    - Yahoo 多出休市日（2026-10-23 休市，Yahoo 卻有一筆複製前一天價格的資料）→ 刪除
    - Yahoo 漏掉交易日（2026-10-27 的 0050 等 ETF 沒有資料、加權指數 2026-10-20 為空值）→ 用證交所收盤補上
    調整係數取前一個有資料交易日的值（加權指數固定為 1）。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    months, m = [], cutoff[:7]
    while m <= today[:7]:
        months.append(m)
        y, mm = int(m[:4]), int(m[5:]) + 1
        m = f"{y + (mm > 12)}-{(mm - 1) % 12 + 1:02d}"
    calendar, closes = set(), {}
    try:
        for m in months:
            calendar |= set(twse_month({"symbol": "^TWII"}, m))   # 大盤有資料的日子 = 交易日
            closes.update(twse_month(inst, m))
    except Exception as e:
        print(f"  {inst['name']}: 證交所資料查詢失敗，略過交易日校正: {e}")
        return
    # 當天證交所可能還沒公布，不刪除當天的資料
    for d in [d for d in series if cutoff <= d < today and d not in calendar]:
        del series[d]
        print(f"  {inst['name']}: 刪除 {d}（證交所休市，Yahoo 多出的資料）")
    for d in sorted(x for x in calendar if x >= cutoff and x not in series and x in closes):
        prev = [x for x in factor if x < d]
        if prev and d > min(series):
            series[d] = closes[d] * factor[max(prev)]
            factor[d] = factor[max(prev)]
            print(f"  {inst['name']}: {d} Yahoo 缺資料，以證交所收盤價 {closes[d]} 補上")


def clean_yahoo(series, name):
    """修正 Yahoo 調整價常見瑕疵：
    1. 分割只調整了一部分歷史 → 單日比例接近 1/N 或 N 倍，將之前的資料等比例縮放
    2. 單筆錯誤報價 → 單日 >10% 且隔天反向 >10%，刪除該筆
    """
    out = list(series)
    k = 1
    while k < len(out) - 1:
        r1 = out[k][1] / out[k - 1][1] - 1
        r2 = out[k + 1][1] / out[k][1] - 1
        if abs(r1) > 0.10 and abs(r2) > 0.10 and r1 * r2 < 0:
            print(f"  {name}: 移除異常報價 {out[k][0]} ({r1:+.1%})")
            del out[k]
            k = max(k - 1, 1)
        else:
            k += 1
    for k in range(1, len(out)):
        ratio = out[k][1] / out[k - 1][1]
        for n in range(2, 11):
            for f in (n, 1 / n):
                if abs(ratio / f - 1) < 0.08:
                    print(f"  {name}: 修正 {out[k][0]} 疑似未調整的分割 (×{f:.3g})")
                    out[:k] = [(d, v * ratio) for d, v in out[:k]]
                    break
            else:
                continue
            break

    # 3. 超過漲跌幅限制的單日變動不可能發生（除息已由 fix_dividends 校正）。
    #    資料開頭一年內若有這種跳動（如 0050 在 2009 年 1～3 月），代表 Yahoo 早期資料損壞，
    #    直接把起始日移到最後一次異常之後，不去猜正確價格。
    def limit(d):
        return 0.07 if d < "2015-06-01" else 0.10

    # 連續數天的錯誤報價（如台積電 2012-01-02～04 被記成 69.8 元，實際約 75 元）：
    # 異常跳動後 5 個交易日內回到原水準，就移除中間這幾天
    k = 1
    while k < len(out):
        jump = abs(out[k][1] / out[k - 1][1] - 1)
        if jump > limit(out[k][0]) + 0.005:
            for m in range(1, 6):
                if k + m < len(out) and abs(out[k + m][1] / out[k - 1][1] - 1) < jump / 2:
                    print(f"  {name}: 移除 {out[k][0]}～{out[k + m - 1][0]} 共 {m} 筆錯誤報價")
                    del out[k:k + m]
                    break
        k += 1

    bad = [k for k in range(1, len(out))
           if abs(out[k][1] / out[k - 1][1] - 1) > limit(out[k][0]) + 0.005]
    early = [k for k in bad if k < 250]
    if early:
        print(f"  {name}: 早期資料有 {len(early)} 筆超過漲跌幅限制的跳動，起始日改為 {out[early[-1]][0]}")
        out = out[early[-1]:]
    for k in bad:
        if k >= 250:
            print(f"  {name}: 警告 {out[k - early[-1] if early else k][0]} 單日變動超過漲跌幅限制，請留意")
    return out


SOURCES = {"nomura": fetch_nomura, "yahoo": fetch_yahoo}


def build_payload(cfg):
    payload = []
    for inst in cfg["instruments"]:
        try:
            series, full_name = SOURCES[inst["source"]](inst)
        except Exception as e:  # 單一標的失敗不影響整份報告
            print(f"[失敗] {inst['name']}: {e}")
            continue
        print(f"[完成] {inst['name']}: {len(series)} 筆, {series[0][0]} ~ {series[-1][0]}")
        payload.append({
            "name": inst["name"],
            "fullName": full_name,
            "source": inst["source"],
            "dates": [d for d, _ in series],
            "values": [round(v, 6) for _, v in series],
        })
    return payload


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "funds.json"
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "report.html"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    payload = build_payload(cfg)

    template = (ROOT / "report_template.html").read_text(encoding="utf-8")
    html = (template
            .replace("__TITLE__", cfg.get("title", "績效比較"))
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    out_path.write_text(html, encoding="utf-8")
    print(f"已產生 {out_path}")


if __name__ == "__main__":
    main()
