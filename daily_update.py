"""
每日排程用：抓最近幾天的FactSet修正快訊，upsert寫入Supabase。
用service_role key(不受RLS限制)，只在GitHub Actions這種受信任的後端環境使用，絕不能放進前端網頁。

用法：
    python3 daily_update.py --days 5
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from factset_scraper_v3 import fetch_all_in_range, parse_article, TZ_TW

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=minimal",
}


def get_last_known_targets(tickers):
    """2026-07-28新增：查每檔股票目前資料庫裡最新一筆TARGET_PRICE事件的new_target，當作
    「真正的前次目標價」基準。原本target_change_pct/old_target是從新聞標題自己講的
    「幅度約X%」反推回去算的(new_target/(1+X%))，但標題講的百分比偶爾跟「相對上一篇
    報告」對不起來(尤其是「維持目標價」的重申文章，標題還是會寫一個舊的、過期的百分比)，
    導致target_change_pct被灌水，進而讓abnormal_revision異常升高、排進不該進的候選。
    改成用「資料庫裡這檔股票最近一筆已知目標價」當基準重新計算，不信任新聞標題自報的
    百分比——這是查證同事資料庫欄位calculated_revision_pct的真實算法後採用的方式。
    """
    if not tickers:
        return {}
    out = {}
    chunk = list(tickers)
    for i in range(0, len(chunk), 50):
        batch = chunk[i:i + 50]
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/factset_revisions",
            headers=HEADERS,
            params={
                "select": "ticker,date,news_time_taipei,new_target",
                "ticker": f"in.({','.join(batch)})",
                "event_type": "eq.TARGET_PRICE",
                "direction": "in.(UP,DOWN)",
                "order": "ticker.asc,date.asc,news_time_taipei.asc",
                "limit": "50000",
            },
            timeout=30,
        )
        r.raise_for_status()
        for row in r.json():
            if row.get("new_target") is not None:
                out[row["ticker"]] = float(row["new_target"])  # 依序覆蓋，留下最後一筆(最新)
    return out


def recompute_revision_pct(rows):
    """依「資料庫裡最近已知目標價」重新計算target_change_pct/old_target，不信任新聞標題
    自報的百分比。同一批次內同一檔股票有多篇，依時間序依序鏈接(後一篇用前一篇剛算出的
    new_target當基準)。沒有任何已知歷史(這檔股票第一次出現)才保留原本從標題反推的值。
    """
    tp_rows = [r for r in rows if r.get("event_type") == "TARGET_PRICE" and r.get("direction") in ("UP", "DOWN")]
    tickers = {r["ticker"] for r in tp_rows if r.get("ticker")}
    last_known = get_last_known_targets(tickers)
    tp_rows.sort(key=lambda r: (r.get("ticker") or "", r.get("news_time") or ""))
    for r in tp_rows:
        ticker = r.get("ticker")
        new_target = r.get("new_target")
        if ticker is None or new_target is None:
            continue
        prev = last_known.get(ticker)
        if prev is not None and prev != 0:
            r["old_target"] = round(prev, 4)
            r["target_change_pct"] = round((new_target / prev - 1) * 100, 4)
        last_known[ticker] = new_target


def row_for_revisions(r):
    return {
        "date": r["news_time"][:10] if r.get("news_time") else None,
        "ticker": r.get("ticker"), "company": r.get("company"), "direction": r.get("direction"),
        "old_target": r.get("old_target"), "new_target": r.get("new_target"),
        "target_high": r.get("target_high"), "target_low": r.get("target_low"),
        "analyst_count": r.get("analyst_count"), "old_eps": r.get("old_eps"), "new_eps": r.get("new_eps"),
        "event_close_price": r.get("close_price"), "concept": r.get("concept"), "source_url": r.get("source_url"),
        "news_time": r.get("news_time"), "news_time_taipei": r.get("news_time_taipei"),
        "market": r.get("market"), "event_type": r.get("event_type"),
        "target_change_pct": r.get("target_change_pct"), "eps_year": r.get("eps_year"),
        "highest_eps": r.get("highest_eps"), "lowest_eps": r.get("lowest_eps"),
        "rating_bullish": r.get("rating_bullish"), "rating_neutral": r.get("rating_neutral"),
        "rating_bearish": r.get("rating_bearish"), "price_5d_pct": r.get("price_5d_pct"),
        "industry_name": r.get("industry_name"), "industry_5d_pct": r.get("industry_5d_pct"),
        "market_5d_pct": r.get("market_5d_pct"), "raw_title": r.get("raw_title"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="往前抓幾天(有重疊也沒關係,靠source_url去重)")
    args = ap.parse_args()

    end_dt = datetime.now(TZ_TW) + timedelta(days=1)
    start_dt = end_dt - timedelta(days=args.days + 1)

    print(f"抓取區間：{start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d}")
    articles = fetch_all_in_range(start_dt, end_dt)
    print(f"tw_forecast 分類總筆數：{len(articles)}")

    rows, seen = [], set()
    for item in articles:
        row = parse_article(item)
        if row is None or row["source_url"] in seen:
            continue
        seen.add(row["source_url"])
        rows.append(row)
    print(f"成功解析：{len(rows)} 筆")

    if not rows:
        print("無新資料，結束")
        return

    recompute_revision_pct(rows)
    revision_rows = [row_for_revisions(r) for r in rows]
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/factset_revisions?on_conflict=source_url",
        headers=HEADERS, json=revision_rows, timeout=60,
    )
    print("revisions upsert:", resp.status_code, resp.text[:300] if resp.status_code >= 300 else "OK")
    resp.raise_for_status()

    # 取回剛寫入(或本來就有)的id，補estimates子表
    urls = [r["source_url"] for r in rows]
    id_map = {}
    for i in range(0, len(urls), 100):
        chunk = urls[i:i+100]
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/factset_revisions",
            headers=HEADERS,
            params={"select": "id,source_url", "source_url": f"in.({','.join(chunk)})"},
            timeout=30,
        )
        for row in r.json():
            id_map[row["source_url"]] = row["id"]

    est_rows = []
    for r in rows:
        rid = id_map.get(r["source_url"])
        if rid is None:
            continue
        for e in r.get("estimates", []):
            est_rows.append({
                "revision_id": rid, "metric": e["metric"], "fiscal_year": e["fiscal_year"],
                "high": e["high"], "low": e["low"], "avg": e["avg"], "median": e["median"],
            })

    if est_rows:
        resp2 = requests.post(
            f"{SUPABASE_URL}/rest/v1/factset_estimates?on_conflict=revision_id,metric,fiscal_year",
            headers=HEADERS, json=est_rows, timeout=60,
        )
        print("estimates upsert:", resp2.status_code, resp2.text[:300] if resp2.status_code >= 300 else "OK")

    print(f"完成：{len(revision_rows)} revisions, {len(est_rows)} estimates")


if __name__ == "__main__":
    main()
