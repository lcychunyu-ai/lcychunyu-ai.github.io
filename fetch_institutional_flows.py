"""
三大法人(外資/投信/自營商)每日買賣超回補——免費公開資料源，不需要任何公司/付費帳號:
- TWSE(上市)：www.twse.com.tw/rwd/zh/fund/T86，逐日查詢，回溯到2023年初驗證過可用
- TPEx(上櫃)：www.tpex.org.tw/www/zh-tw/insti/dailyTrade，逐日查詢

只抓「目前策略回測有在追蹤的216檔股票」，不是全市場，資料量可控。寫入Supabase的
institutional_flows表(ticker, date, foreign_net, trust_net, dealer_net, total_net)。
只做讀取用途，用anon key即可(RLS開了public read policy)，但寫入還是需要service_role
權限，這支腳本用Supabase MCP的execute_sql路徑寫入(不透過REST API)，本機執行、
不進GitHub Actions(之後要排程更新再考慮要不要搬過去)。

用法：
    python3 fetch_institutional_flows.py --start 2023-01-03 --end 2026-07-30 --out flows.json
    (先抓成本機json，用另一支腳本或手動批次upsert進Supabase，避免這支腳本混入DB憑證)
"""
import argparse
import json
import time
from datetime import datetime, timedelta

import requests

SUPABASE_URL = "https://kiiwaojcetxmeycyupvn.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtpaXdhb2pjZXR4bWV5Y3l1cHZuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ1Mjk2NzAsImV4cCI6MjEwMDEwNTY3MH0.QPnEenJ8OtgWm1q3zhstinsAzXJAD6bunPp6JhrL4PU"
HEADERS = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}
UA = {"User-Agent": "Mozilla/5.0"}


def get_tracked_tickers():
    out, offset, page_size = set(), 0, 5000
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/factset_revisions", headers=HEADERS,
                          params={"select": "ticker", "offset": str(offset), "limit": str(page_size)}, timeout=60)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.update(row["ticker"] for row in page if row.get("ticker"))
        offset += page_size
    return out


def fetch_twse_day(date_str, tickers_set):
    """date_str: YYYYMMDD"""
    r = requests.get("https://www.twse.com.tw/rwd/zh/fund/T86",
                      params={"response": "json", "date": date_str, "selectType": "ALL"},
                      headers=UA, timeout=20)
    if r.status_code != 200:
        return []
    try:
        d = r.json()
    except ValueError:
        return []
    if d.get("stat") != "OK":
        return []
    out = []
    for row in d.get("data", []):
        ticker = row[0].strip()
        if ticker not in tickers_set:
            continue
        try:
            foreign_net = int(row[4].replace(",", "")) + int(row[7].replace(",", ""))
            trust_net = int(row[10].replace(",", ""))
            dealer_net = int(row[11].replace(",", ""))
            total_net = int(row[18].replace(",", ""))
        except (ValueError, IndexError):
            continue
        out.append({"ticker": ticker, "foreign_net": foreign_net, "trust_net": trust_net,
                     "dealer_net": dealer_net, "total_net": total_net})
    return out


def fetch_tpex_day(date_roc_str, tickers_set):
    """date_roc_str: YYYY/MM/DD(西元年，TPEx這隻API吃西元年格式的date參數，內部回傳民國年)"""
    r = requests.get("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                      params={"type": "Daily", "sect": "EW", "date": date_roc_str, "id": "", "response": "json"},
                      headers=UA, timeout=20)
    if r.status_code != 200:
        return []
    try:
        d = r.json()
    except ValueError:
        return []
    tables = d.get("tables", [])
    if not tables:
        return []
    out = []
    for row in tables[0].get("data", []):
        ticker = row[0].strip()
        if ticker not in tickers_set:
            continue
        # TPEx這隻API的fields只給"買進/賣出/買賣超"重複7組，沒有標明每組對應哪個法人，
        # 不像TWSE T86有明確欄位名稱可以對照，硬猜個別分項風險太高。只取最後一欄
        # "三大法人買賣超股數合計"(欄位數固定是最後一個，語意無歧義)，foreign/trust/dealer
        # 對TPEx股票留null，之後如果真的需要上櫃股票的分項，再回頭查TPEx官方文件補上。
        try:
            total_net = int(row[-1].replace(",", ""))
        except (ValueError, IndexError):
            continue
        out.append({"ticker": ticker, "foreign_net": None, "trust_net": None,
                     "dealer_net": None, "total_net": total_net})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tickers = get_tracked_tickers()
    print(f"追蹤股票數: {len(tickers)}")

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    all_rows = []
    day = start
    n_days = 0
    while day <= end:
        if day.weekday() < 5:  # 只抓平日，週末一定沒交易(國定假日打空就跳過，不特別處理行事曆)
            date_str = day.strftime("%Y%m%d")
            date_slash = day.strftime("%Y/%m/%d")
            twse_rows, tpex_rows = [], []
            for attempt in range(3):
                try:
                    twse_rows = fetch_twse_day(date_str, tickers)
                    break
                except requests.RequestException:
                    time.sleep(1.0)
            for attempt in range(3):
                try:
                    tpex_rows = fetch_tpex_day(date_slash, tickers)
                    break
                except requests.RequestException:
                    time.sleep(1.0)
            for row in twse_rows + tpex_rows:
                row["date"] = day.strftime("%Y-%m-%d")
                all_rows.append(row)
            n_days += 1
            if n_days % 20 == 0:
                print(f"  進度: {day.date()}，累積筆數 {len(all_rows)}")
            time.sleep(0.3)
        day += timedelta(days=1)

    print(f"完成，共{len(all_rows)}筆，寫入 {args.out}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
