"""
概念/產業鏈分類：改用「產業價值鏈資訊平台」(證券櫃檯買賣中心 + 臺灣證券交易所 官方共同維運)
https://ic.tpex.org.tw/company_chain.php?stk_code={代號}

這是公司自行申報、交易所對外公布的官方資料，不是我自己主觀判斷、也不是爬蟲網站的
群眾標籤——每家公司可能同時列在好幾個「產業別 > 產業鏈位置」，不強制湊滿1~3個，
公司填了幾個就是幾個。

用法：
    python3 fetch_industry_chain.py
輸出：
    factset_data/industry_chain_map.json
"""
import html
import json
import re
import time
import requests

TRACKED = """1101 1102 1216 1301 1303 1305 1308 1319 1326 1476 1477 1504 1513 1519 1536 1560 1565 1590 1707 1795
2002 2006 2014 2027 2049 2059 2105 2301 2303 2308 2313 2317 2324 2327 2330 2337 2344 2345 2347 2351 2352 2353
2355 2356 2357 2360 2368 2376 2377 2379 2382 2383 2385 2395 2408 2409 2412 2421 2439 2449 2454 2455 2458 2467
2474 2481 2603 2605 2606 2609 2610 2615 2618 2634 2637 2645 2723 2727 2881 2882 2884 2885 2886 2887 2888 2891
2892 2912 3008 3017 3023 3034 3035 3036 3037 3042 3044 3045 3081 3105 3131 3189 3211 3218 3231 3324 3376 3406
3413 3443 3481 3491 3515 3529 3533 3587 3592 3596 3653 3661 3665 3673 3680 3702 3706 3708 3711 3714 3715 4129
4137 4147 4551 4572 4749 4770 4771 4772 4904 4915 4938 4958 4961 4966 4968 5269 5274 5306 5347 5371 5388 5871
5904 6147 6176 6182 6187 6196 6213 6223 6239 6269 6271 6274 6279 6285 6288 6409 6412 6414 6415 6446 6456 6469
6472 6488 6491 6505 6510 6515 6526 6533 6561 6643 6669 6670 6689 6719 6741 6768 6770 6781 6782 6799 6805 6873
6890 7769 8046 8069 8081 8086 8150 8210 8299 8436 8454 8464 8996 9802 9910 9914 9921 9933 9938 9958""".split()

BASE = "https://ic.tpex.org.tw/company_chain.php?stk_code={}"
HEADERS = {"User-Agent": "Mozilla/5.0"}

ROW_RE = re.compile(
    r'<a href="introduce\.php\?ic=[^"]*">([^<]+)</a>&nbsp;&gt;&nbsp;([^<]+)</h4>'
)
NO_DATA_HINTS = ("查無", "尚未")


def fetch_one(ticker):
    url = BASE.format(ticker)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    r.encoding = "utf-8"
    page_html = r.text
    rows = ROW_RE.findall(page_html)
    return [{"industry": html.unescape(ind.strip()), "position": html.unescape(pos.strip())} for ind, pos in rows]


if __name__ == "__main__":
    result = {}
    empty = []
    for i, t in enumerate(TRACKED, 1):
        try:
            chains = fetch_one(t)
        except Exception as e:
            print(f"  {t} 抓取失敗: {e}")
            chains = []
        if chains:
            result[t] = chains
        else:
            empty.append(t)
        if i % 20 == 0:
            print(f"進度 {i}/{len(TRACKED)}")
        time.sleep(0.3)

    with open("factset_data/industry_chain_map.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n有分類資料: {len(result)} 檔")
    print(f"平台上查無資料(公司沒填): {len(empty)} 檔")
    print(f"沒填的代號: {empty}")
    total_rows = sum(len(v) for v in result.values())
    print(f"總筆數(一檔可能對應多筆): {total_rows}")
