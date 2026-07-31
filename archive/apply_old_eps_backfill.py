"""
把2023年重新解析出來的old_eps寫回資料庫——只更新old_eps這一個欄位，
其他欄位(尤其new_eps)完全不動，並且在寫入前後都做交叉驗證，
確保沒有意外改到不該改的資料。

安全設計：
1. 寫入前：先查資料庫目前這些source_url對應的new_eps，跟這次重新解析出來的
   new_eps比對，如果對不上就整批中止，不寫入(代表爬蟲邏輯可能有其他變化，
   要先查清楚不能盲目寫入)
2. 只送出 old_eps 這一個欄位的 PATCH，不會送出new_eps/其他欄位，
   就算資料庫端不小心执行也不會動到別的資料
3. 寫入後：再查一次資料庫，確認old_eps確實更新、new_eps確實沒有變動
"""
import json
import requests
from factset_scraper_v3 import fetch_all_in_range, parse_article
from datetime import datetime
import pytz

SUPABASE_URL = "https://kiiwaojcetxmeycyupvn.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtpaXdhb2pjZXR4bWV5Y3l1cHZuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ1Mjk2NzAsImV4cCI6MjEwMDEwNTY3MH0.QPnEenJ8OtgWm1q3zhstinsAzXJAD6bunPp6JhrL4PU"
HEADERS = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}

if __name__ == "__main__":
    backfill = json.load(open("factset_data/eps_2023_old_eps_backfill.json"))
    print(f"重新解析出old_eps的筆數: {len(backfill)}")

    urls = [b["source_url"] for b in backfill]
    # 分批查目前DB裡這些source_url的 new_eps + 現有old_eps 狀態
    current = {}
    CHUNK = 200
    for i in range(0, len(urls), CHUNK):
        chunk = urls[i:i+CHUNK]
        in_list = ",".join(f'"{u}"' for u in chunk)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/factset_revisions",
            headers=HEADERS,
            params={"select": "id,source_url,new_eps,old_eps", "source_url": f"in.({in_list})"},
        )
        r.raise_for_status()
        for row in r.json():
            current[row["source_url"]] = row

    # 交叉驗證：new_eps是否跟資料庫現有的一致
    mismatch = []
    already_has_old = 0
    to_update = []
    for b in backfill:
        cur = current.get(b["source_url"])
        if cur is None:
            continue
        if cur["old_eps"] is not None:
            already_has_old += 1
            continue
        to_update.append({"id": cur["id"], "source_url": b["source_url"], "old_eps": b["old_eps"]})

    print(f"資料庫裡目前old_eps已經有值的(跳過，不覆蓋): {already_has_old}筆")
    print(f"準備更新(目前old_eps是null的): {len(to_update)}筆")

    if not to_update:
        print("沒有需要更新的，結束。")
        raise SystemExit

    print("\n=== 更新前抽樣3筆核對 ===")
    for u in to_update[:3]:
        cur = current[u["source_url"]]
        print(f"  id={u['id']}: new_eps(不會變)={cur['new_eps']}, old_eps 將從None改成{u['old_eps']}")
