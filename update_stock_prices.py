"""
每日排程用：抓個股收盤價+台股加權指數，upsert寫入Supabase的stock_prices/taiex_index。
用service_role key(不受RLS限制)，只在GitHub Actions這種受信任的後端環境使用，絕不能放進前端網頁。

股票清單不是寫死的名單，是直接查Supabase的factset_revisions撈「歷史上出現過目標價/EPS修正新聞的所有ticker」，
這樣就算換一台電腦、換一個人接手，只要有這組service_role key，重跑這支腳本就能拿到完整正確的追蹤清單，
不依賴本機任何檔案。

用法：
    python3 update_stock_prices.py --days 10      # 日常增量更新(預設)
    python3 update_stock_prices.py --full          # 第一次全量回補(2021-01-01至今)
"""
import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}
TZ_TW = timezone(timedelta(hours=8))


def get_tracked_tickers():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/factset_revisions",
        headers=HEADERS, params={"select": "ticker"}, timeout=60,
    )
    r.raise_for_status()
    tickers = sorted({row["ticker"] for row in r.json() if row.get("ticker")})
    print(f"追蹤股票數(來自factset_revisions distinct ticker): {len(tickers)}")
    return tickers


def get_market_type_map():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/ticker_industry_official",
        headers=HEADERS, params={"select": "ticker,market_type", "limit": "5000"}, timeout=30,
    )
    r.raise_for_status()
    return {row["ticker"]: row["market_type"] for row in r.json()}


def upsert_batch(table, rows, on_conflict, batch_size=3000):
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=HEADERS, json=chunk, timeout=60,
        )
        if resp.status_code >= 300:
            print(f"  [警告] {table} 批次{i}: {resp.status_code} {resp.text[:300]}")
        resp.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="增量模式：往前抓幾天(有重疊也沒關係，upsert會覆蓋)")
    ap.add_argument("--full", action="store_true", help="全量回補：從2021-01-01抓到今天，第一次建表用")
    args = ap.parse_args()

    end_date = (datetime.now(TZ_TW) + timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = "2021-01-01" if args.full else (datetime.now(TZ_TW) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"抓取區間：{start_date} ~ {end_date}（{'全量回補' if args.full else '增量更新'}）")

    tickers = get_tracked_tickers()
    market_map = get_market_type_map()

    price_rows = []
    failed = []
    for i, t in enumerate(tickers, 1):
        mt = market_map.get(t)
        suffixes = [".TW"] if mt == "上市" else [".TWO"] if mt == "上櫃" else [".TW", ".TWO"]
        ok = False
        for suf in suffixes:
            try:
                df = yf.download(f"{t}{suf}", start=start_date, end=end_date, progress=False, auto_adjust=True)
                if df.empty:
                    continue
                close = df["Close"]
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
                close = close.dropna()
                if len(close) == 0:
                    continue
                for d, v in close.items():
                    price_rows.append({"ticker": t, "date": d.strftime("%Y-%m-%d"), "close": float(v)})
                ok = True
                break
            except Exception:
                continue
        if not ok:
            failed.append(t)
        if i % 50 == 0:
            print(f"  進度 {i}/{len(tickers)}")
        time.sleep(0.05)

    print(f"股價筆數: {len(price_rows)}；抓不到的股票({len(failed)}檔): {failed}")
    if price_rows:
        upsert_batch("stock_prices", price_rows, "ticker,date")
        print("stock_prices upsert 完成")

    taiex_df = yf.download("^TWII", start=start_date, end=end_date, progress=False, auto_adjust=True)
    tclose = taiex_df["Close"]
    if hasattr(tclose, "columns"):
        tclose = tclose.iloc[:, 0]
    tclose = tclose.dropna()
    taiex_rows = [{"date": d.strftime("%Y-%m-%d"), "close": float(v)} for d, v in tclose.items()]
    print(f"TAIEX筆數: {len(taiex_rows)}")
    if taiex_rows:
        upsert_batch("taiex_index", taiex_rows, "date")
        print("taiex_index upsert 完成")


if __name__ == "__main__":
    main()
