from factset_scraper_v3 import fetch_all_in_range, parse_article
from datetime import datetime
import pytz, json, sys

TZ = pytz.timezone("Asia/Taipei")
updates = []
for month in range(1, 13):
    start = TZ.localize(datetime(2023, month, 1, 0, 0))
    end_month = month + 1 if month < 12 else 1
    end_year = 2023 if month < 12 else 2024
    end = TZ.localize(datetime(end_year, end_month, 1, 0, 0))
    articles = fetch_all_in_range(start, end)
    month_updates = 0
    for a in articles:
        row = parse_article(a)
        if row and row.get("event_type") == "EPS" and row.get("old_eps") is not None:
            updates.append({"source_url": row["source_url"], "old_eps": row["old_eps"]})
            month_updates += 1
    print(f"2023-{month:02d}: 文章{len(articles)}篇, 補到old_eps {month_updates}筆", flush=True)

json.dump(updates, open("/Users/USER/Desktop/Matthias Agent/factset_data/eps_2023_old_eps_backfill.json", "w"), ensure_ascii=False)
print(f"總計補到: {len(updates)}筆", flush=True)
