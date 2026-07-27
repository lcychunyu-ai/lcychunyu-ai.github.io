"""
全台股 FactSet 修正追蹤 - 資料抓取與解析（v3）

v2 基礎上補齊：
- TARGET_PRICE 文章：目標價最高/最低估值、相關產業5日漲跌%、大盤(加權指數)5日漲跌%
- EPS 文章：完整多年度(2026~2029) EPS / 營收 預估表，展開成 estimates 列表
  （寫入時對應 factset_estimates 子表，一篇文章最多 8 列：EPS×4年 + REVENUE×4年）

EPS 類型文章天生沒有：目標價高低、產業/大盤比較、分析師評等分布、收盤價
TARGET_PRICE 類型文章天生沒有：多年度 EPS/營收預估表
上述兩種「NULL」都是資料本質如此，不是漏抓。

用法：
    python3 factset_scraper_v3.py --start 2026-05-01 --end 2026-07-21 --out factset_data/may_jul_revisions.json
"""

import argparse
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

API_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_forecast"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TZ_TW = timezone(timedelta(hours=8))

TITLE_RE = re.compile(r"Factset\s*最新調查[：:]\s*(?P<name>[^\(]+)\((?P<code>\d{4,6})-TW\)")
EPS_TITLE_RE = re.compile(r"EPS預估(?P<dir>上修|下修)至(?P<eps_new>-?[\d.]+)元，預估目標價為(?P<target>-?[\d.]+)元")
# 2023年文章格式：中位數表格欄位不是"新值(舊值)"括號格式，舊值改寫在內文開頭一句話裡，
# 例如「中位數由21.01元下修至20.76元」。2026-07-24發現，之前誤判成2023年原文沒寫舊值。
EPS_OLD_MEDIAN_RE = re.compile(r"中位數由(-?[\d.]+)元(?:上修|下修)至(-?[\d.]+)元")
TP_TITLE_RE = re.compile(r"目標價調(?P<dir>升|降)至(?P<target>-?[\d.]+)元，幅度約(?P<pct>-?[\d.]+)%")
RATING_RE = re.compile(r"積極樂觀(?P<bull>\d+)位、保持中立(?P<neutral>\d+)位、保守悲觀(?P<bear>\d+)位")
PRICE_RE = re.compile(
    r"今\((?P<day>\d+)日\)收盤價為(?P<price>-?[\d.]+)元。"
    r"近5日股價(?P<stock_dir>上漲|下跌)(?P<stock_chg>-?[\d.]+)%"
)
INDUSTRY_MARKET_RE = re.compile(
    r"相關(?P<industry>[^近]+?)近5日(?P<idir>上漲|下跌)(?P<ipct>-?[\d.]+)%，"
    r"(?:集中市場加權指數|櫃買市場加權指數|櫃買指數|台灣加權指數)(?P<mdir>上漲|下跌)(?P<mpct>-?[\d.]+)%"
)
TP_RANGE_RE = re.compile(r"其中最高估值(?P<high>-?[\d.]+)元，最低估值(?P<low>-?[\d.]+)元")
COUNT_RE = re.compile(r"共(\d+)位分析師")

ROW_KEY_TO_STAT = {"最高值": "high", "最低值": "low", "平均值": "avg", "中位數": "median"}


def fetch_page(start_ts, end_ts, page, limit=30):
    params = {"page": page, "limit": limit, "startAt": start_ts, "endAt": end_ts}
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()["items"]


def fetch_all_in_range(start_dt, end_dt, sleep_sec=0.15):
    articles = []
    window = timedelta(days=30)
    cur_start = start_dt
    while cur_start < end_dt:
        cur_end = min(cur_start + window, end_dt)
        s_ts, e_ts = int(cur_start.timestamp()), int(cur_end.timestamp())
        page = 1
        while True:
            items = fetch_page(s_ts, e_ts, page=page)
            articles.extend(items["data"])
            if page >= items["last_page"]:
                break
            page += 1
            time.sleep(sleep_sec)
        cur_start = cur_end
        time.sleep(sleep_sec)
    return articles


def parse_table_after(soup, marker_text):
    marker = soup.find(string=re.compile(marker_text))
    if not marker:
        return None, None
    table = marker.find_next("table")
    if not table:
        return None, None
    rows = table.find_all("tr")
    years = [td.get_text(strip=True) for td in rows[0].find_all("td")][1:]
    data = {}
    for tr in rows[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            data[cells[0]] = cells[1:]
    return years, data


def split_new_old(cell):
    cell = cell.replace(",", "")
    m = re.match(r"(-?[\d.]+)\((-?[\d.]+)\)", cell)
    if m:
        return float(m.group(1)), float(m.group(2))
    try:
        return float(cell), None
    except ValueError:
        return None, None


def table_to_estimates(years, table, metric):
    """把 parse_table_after 的結果展開成 [{metric, fiscal_year, high, low, avg, median}, ...]"""
    if not years or not table:
        return []
    clean_years = [y.replace("(前值)", "").strip() for y in years]
    per_year = [{} for _ in clean_years]
    for row_key, stat in ROW_KEY_TO_STAT.items():
        cells = table.get(row_key)
        if not cells:
            continue
        for i, cell in enumerate(cells):
            if i >= len(per_year):
                continue
            new_val, _old_val = split_new_old(cell)
            per_year[i][stat] = new_val
    estimates = []
    for year_label, stats in zip(clean_years, per_year):
        if not stats:
            continue
        estimates.append({
            "metric": metric,
            "fiscal_year": year_label,
            "high": stats.get("high"),
            "low": stats.get("low"),
            "avg": stats.get("avg"),
            "median": stats.get("median"),
        })
    return estimates


def parse_article(item):
    title = html.unescape(item["title"])
    m_target = TITLE_RE.search(title)
    if not m_target:
        return None  # 不是台股(-TW)的Factset個股快訊，跳過（例如ADR或美股）

    content_html = html.unescape(item["content"])
    soup = BeautifulSoup(content_html, "html.parser")
    full_text = soup.get_text()

    news_dt = datetime.fromtimestamp(item["publishAt"], tz=TZ_TW)
    row = {
        "source_url": f"https://news.cnyes.com/news/id/{item['newsId']}",
        "event_type": None,
        "news_time": news_dt.isoformat(),
        # news_time_taipei：news_time拆出來的台北時區時分秒(date欄位已經是拆出來的日期部分)，
        # 這樣下游不用每次都重新做時區轉換就能直接拿到「幾點幾分」。
        "news_time_taipei": news_dt.strftime("%H:%M:%S"),
        "market": "TW",
        "ticker": m_target.group("code"),
        "company": m_target.group("name"),
        "direction": None,
        "old_target": None,
        "new_target": None,
        "target_high": None,
        "target_low": None,
        "target_change_pct": None,
        "eps_year": None,
        "old_eps": None,
        "new_eps": None,
        "highest_eps": None,
        "lowest_eps": None,
        "analyst_count": None,
        "rating_bullish": None,
        "rating_neutral": None,
        "rating_bearish": None,
        "close_price": None,
        "price_5d_pct": None,
        "industry_name": None,
        "industry_5d_pct": None,
        "market_5d_pct": None,
        "concept": None,
        "raw_title": title,
        "estimates": [],
    }

    m_count = COUNT_RE.search(full_text)
    if m_count:
        row["analyst_count"] = int(m_count.group(1))

    m_eps = EPS_TITLE_RE.search(title)
    m_tp = TP_TITLE_RE.search(title)

    if m_eps:
        row["event_type"] = "EPS"
        row["direction"] = "UP" if m_eps.group("dir") == "上修" else "DOWN"
        row["new_target"] = float(m_eps.group("target"))

        eps_years, eps_table = parse_table_after(soup, "市場預估EPS")
        if eps_years and eps_table:
            row["eps_year"] = eps_years[0].replace("(前值)", "").strip()
            new_h, _ = split_new_old(eps_table.get("最高值", [""])[0])
            new_l, _ = split_new_old(eps_table.get("最低值", [""])[0])
            new_m, old_m = split_new_old(eps_table.get("中位數", [""])[0])
            if old_m is None:
                m_old_median = EPS_OLD_MEDIAN_RE.search(full_text)
                if m_old_median:
                    old_m = float(m_old_median.group(1))
            row["highest_eps"], row["lowest_eps"] = new_h, new_l
            row["new_eps"], row["old_eps"] = new_m, old_m
            row["estimates"].extend(table_to_estimates(eps_years, eps_table, "EPS"))

        rev_years, rev_table = parse_table_after(soup, "市場預估營收")
        if rev_years and rev_table:
            row["estimates"].extend(table_to_estimates(rev_years, rev_table, "REVENUE"))

    elif m_tp:
        row["event_type"] = "TARGET_PRICE"
        row["direction"] = "UP" if m_tp.group("dir") == "升" else "DOWN"
        row["new_target"] = float(m_tp.group("target"))
        pct = float(m_tp.group("pct"))
        row["target_change_pct"] = pct if m_tp.group("dir") == "升" else -pct
        if row["target_change_pct"] is not None and row["new_target"] is not None:
            row["old_target"] = round(row["new_target"] / (1 + row["target_change_pct"] / 100), 4)

        m_range = TP_RANGE_RE.search(full_text)
        if m_range:
            row["target_high"] = float(m_range.group("high"))
            row["target_low"] = float(m_range.group("low"))

        m_im = INDUSTRY_MARKET_RE.search(full_text)
        if m_im:
            row["industry_name"] = m_im.group("industry").strip()
            ipct = float(m_im.group("ipct"))
            row["industry_5d_pct"] = ipct if m_im.group("idir") == "上漲" else -ipct
            mpct = float(m_im.group("mpct"))
            row["market_5d_pct"] = mpct if m_im.group("mdir") == "上漲" else -mpct
    else:
        return None  # 標題格式不符預期，跳過避免存進髒資料

    m_rating = RATING_RE.search(full_text)
    if m_rating:
        row["rating_bullish"] = int(m_rating.group("bull"))
        row["rating_neutral"] = int(m_rating.group("neutral"))
        row["rating_bearish"] = int(m_rating.group("bear"))

    m_price = PRICE_RE.search(full_text)
    if m_price:
        row["close_price"] = float(m_price.group("price"))
        chg = float(m_price.group("stock_chg"))
        row["price_5d_pct"] = chg if m_price.group("stock_dir") == "上漲" else -chg

    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=TZ_TW)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=TZ_TW) + timedelta(days=1)

    print(f"抓取區間：{start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d}")
    articles = fetch_all_in_range(start_dt, end_dt)
    print(f"tw_forecast 分類總筆數：{len(articles)}")

    rows = []
    skipped = 0
    for item in articles:
        row = parse_article(item)
        if row is None:
            skipped += 1
            continue
        rows.append(row)

    print(f"成功解析：{len(rows)} 筆，跳過（非台股個股快訊或格式不符）：{skipped} 筆")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"已輸出：{args.out}")


if __name__ == "__main__":
    main()
