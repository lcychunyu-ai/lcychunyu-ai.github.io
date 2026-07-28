"""
診斷用途，一次性腳本：把同事factset.sqlite的原始資料(articles/prices_daily/benchmarks_daily)
原封不動搬進我們Supabase的colleague_revisions/colleague_stock_prices/colleague_taiex_index
這三張獨立診斷表(跟正式網站在用的factset_revisions/stock_prices/taiex_index完全分開，
v_unified_target_events視圖跟get_strategy_price_bundle() RPC都不會讀到，不影響strategy.html)。

目的：讓strategy_backtest.py可以指定讀這三張診斷表而不是正式表，跑出「我們的方法 x 同事的
資料」這個象限的結果，拿來跟「我們的方法 x 我們的資料」對照，藉此判斷剩餘的數字差異到底是
資料來源不同、還是方法邏輯不同造成的。

用service_role key(跟update_stock_prices.py同一把key)，不能放進前端網頁，只在本機跑一次。

用法：
    export SUPABASE_SERVICE_ROLE_KEY="你的service_role key"
    python3 load_colleague_data_to_supabase.py
"""
import os
import sqlite3

import requests

SUPABASE_URL = "https://kiiwaojcetxmeycyupvn.supabase.co"
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}
SQLITE_PATH = "/Users/USER/Desktop/Matthias Agent/factset/data/factset.sqlite"


def upsert_batch(table, rows, on_conflict, batch_size=3000):
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=HEADERS, json=chunk, timeout=120,
        )
        if resp.status_code >= 300:
            print(f"  [警告] {table} 批次{i}: {resp.status_code} {resp.text[:300]}")
        resp.raise_for_status()
        print(f"  {table} 已寫入 {min(i + batch_size, len(rows))}/{len(rows)}")


def main():
    con = sqlite3.connect(SQLITE_PATH)
    cur = con.cursor()

    print("讀取articles(所有news_type，之後篩TARGET_PRICE由查詢端自己決定)...")
    cur.execute("""SELECT news_id, published_at_taipei, published_date, news_type, ticker, direction,
                          analyst_count, old_target, new_target, reported_revision_pct, calculated_revision_pct
                   FROM articles WHERE ticker IS NOT NULL""")
    cols = ["news_id", "published_at_taipei", "published_date", "news_type", "ticker", "direction",
            "analyst_count", "old_target", "new_target", "reported_revision_pct", "calculated_revision_pct"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} 筆")
    upsert_batch("colleague_revisions", rows, "news_id")

    print("讀取prices_daily...")
    cur.execute("SELECT ticker, trade_date, open, close, adj_close FROM prices_daily WHERE ticker IS NOT NULL")
    cols = ["ticker", "trade_date", "open", "close", "adj_close"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} 筆")
    upsert_batch("colleague_stock_prices", rows, "ticker,trade_date")

    print("讀取benchmarks_daily(TAIEX)...")
    cur.execute("SELECT trade_date, open, close FROM benchmarks_daily WHERE benchmark='TAIEX'")
    cols = ["trade_date", "open", "close"]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"  {len(rows)} 筆")
    upsert_batch("colleague_taiex_index", rows, "trade_date")

    print("完成。")


if __name__ == "__main__":
    main()
