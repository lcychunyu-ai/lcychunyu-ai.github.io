"""
每日排程用：抓個股開盤價/收盤價+台股加權指數開盤/收盤，upsert寫入Supabase的stock_prices/taiex_index。
用service_role key(不受RLS限制)，只在GitHub Actions這種受信任的後端環境使用，絕不能放進前端網頁。

2026-07-28更新：改用yfinance的auto_adjust=False，open/close是原始未調整價格，不是還原股價。
原本用還原股價(auto_adjust=True)理論上比較「正確」(內含除權息調整，不會製造假跳空)，但為了
讓strategy.html/strategy_backtest.py回測結果能跟同事獨立實作(Node.js版本，用原始股價)的數字
完全對齊、方便交叉驗證，改成跟他一致的原始股價。這是資料口徑的選擇，不是誰對誰錯。

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

import pandas as pd
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
    # 2026-07-28修正：原本沒分頁，Supabase REST API預設一次最多回傳1000筆，但
    # factset_revisions有9000多筆，只查得到前1000筆、算出的distinct ticker因此少了10檔——
    # 凡是「第一次出現的新聞」剛好落在第1000筆之後的股票，永遠不會被列入追蹤清單，股價
    # 也就永遠抓不到(查證發現的12檔股價全空，其中10檔根因就在這裡，不是yfinance代碼問題)。
    # 改成分頁抓全部。
    all_rows, offset, page_size = [], 0, 5000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/factset_revisions",
            headers=HEADERS, params={"select": "ticker", "offset": str(offset), "limit": str(page_size)}, timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        all_rows.extend(page)
        offset += page_size
    tickers = sorted({row["ticker"] for row in all_rows if row.get("ticker")})
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
        # 2026-07-28修正：原本market_type=上市/上櫃時只試「對應的那一個」代碼，市場別記錄
        # 一旦過期或漏登，就永久抓不到那檔股票，資料庫會整批缺該股票的股價(查證發現12檔
        # 股票發生這個狀況：新聞事件有抓到、股價卻完全沒有)。改成「有記錄market_type時優先
        # 試對應代碼，但失敗一律再試另一個代碼」，不管市場別記錄準不準，都還有機會抓到。
        suffixes = [".TW", ".TWO"] if mt == "上市" else [".TWO", ".TW"] if mt == "上櫃" else [".TW", ".TWO"]
        ok = False
        for suf in suffixes:
            # 2026-07-28新增：yfinance偶爾因為短暫網路/流量限制單次抓取失敗(不是真的沒資料)，
            # 原本失敗一次就直接放棄換下一個代碼，這裡加2次重試(短暫等待後重來)，避免把
            # 「暫時抓不到」誤判成「這檔股票沒資料」而永久漏掉。
            merged = None
            for attempt in range(3):
                try:
                    df = yf.download(f"{t}{suf}", start=start_date, end=end_date, progress=False, auto_adjust=False)
                    if df.empty:
                        break
                    close, open_ = df["Close"], df["Open"]
                    if hasattr(close, "columns"):
                        close = close.iloc[:, 0]
                    if hasattr(open_, "columns"):
                        open_ = open_.iloc[:, 0]
                    # 2026-07-28修正：原本用dropna(how="all")，只要open/close其中一個有值就會寫進資料庫，
                    # 等於允許close=null的殘缺列被upsert進stock_prices/taiex_index——這種列一旦進了
                    # taiex_index，因為taiex_index本身就是回測引擎拿來當交易日曆基準的表，就會讓那個
                    # 交易日「看起來存在」但close是null，我們自己的引擎有fillna(0)防呆所以沒事，但
                    # 同事的引擎沒防呆，close/前收-1直接把null當0算，炸出離譜的單日-100%。改成
                    # dropna(how="any")，open/close只要缺一個就整列不寫，寧可那天暫時沒資料(等下次
                    # 排程重跑再補)，也不要寫一筆看似存在、實際上殘缺的資料進資料庫。
                    merged = close.to_frame("close").join(open_.to_frame("open")).dropna(how="any")
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(1.0)
                    continue
            if merged is None or len(merged) == 0:
                continue
            for d, row in merged.iterrows():
                price_rows.append({
                    "ticker": t, "date": d.strftime("%Y-%m-%d"),
                    "close": float(row["close"]),
                    "open": float(row["open"]),
                })
            ok = True
            break
        if not ok:
            failed.append(t)
        if i % 50 == 0:
            print(f"  進度 {i}/{len(tickers)}")
        time.sleep(0.05)

    print(f"股價筆數: {len(price_rows)}；抓不到的股票({len(failed)}檔): {failed}")
    if price_rows:
        upsert_batch("stock_prices", price_rows, "ticker,date")
        print("stock_prices upsert 完成")

    taiex_df = yf.download("^TWII", start=start_date, end=end_date, progress=False, auto_adjust=False)
    tclose, topen = taiex_df["Close"], taiex_df["Open"]
    if hasattr(tclose, "columns"):
        tclose = tclose.iloc[:, 0]
    if hasattr(topen, "columns"):
        topen = topen.iloc[:, 0]
    # 同上：taiex_index是交易日曆基準表，close絕對不能寫null進去，open/close缺一個就整列跳過。
    tmerged = tclose.to_frame("close").join(topen.to_frame("open")).dropna(how="any")
    taiex_rows = [{
        "date": d.strftime("%Y-%m-%d"),
        "close": float(row["close"]),
        "open": float(row["open"]),
    } for d, row in tmerged.iterrows()]
    print(f"TAIEX筆數: {len(taiex_rows)}")
    if taiex_rows:
        upsert_batch("taiex_index", taiex_rows, "date")
        print("taiex_index upsert 完成")


if __name__ == "__main__":
    main()
