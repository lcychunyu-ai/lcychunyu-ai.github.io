"""
概念股分類：抓台新投信官網ETF成分股頁面，解析成concept_map資料
來源：https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/{代號}（台新投信官方揭露，最權威）

用法：手動維護一份 ETF_CONFIG（代號、對應concept名稱、tier層級），
之後要加新的細分類，直接在這裡加一筆設定即可。
"""
import re
import json
import requests

FIRECRAWL_API_KEY = None  # 用MCP firecrawl_scrape抓好的markdown直接貼進來，這裡先示範手動流程

ETF_CONFIG = [
    {"ticker": "00904", "concept": "半導體", "tier": 1},
    {"ticker": "00947", "concept": "IC設計", "tier": 2},
    {"ticker": "00962", "concept": "AI", "tier": 2,
     "note": "台新投信，https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00962"},
    {"ticker": "00901", "concept": "智能車供應鏈", "tier": 2,
     "note": "永豐投信，表格格式不同(欄位是「證券代碼/證券名稱/股數/佔基金淨資產之權重(%)」)，"
             "頁面在 https://sitc.sinopac.com/SinopacEtfs/Etfs/Pcf/00901，"
             "不是ETFSeriesDetail格式，parse_holdings_table()目前只認台新投信的表格格式，"
             "這筆是手動解析後直接寫入concept_insert.sql，之後要重跑得另外寫parser"},
]

# 2026-07-24 有查過但沒找到可用的官方權重來源，先不做，之後有更好的資料源再補：
# - 資安：台股唯一資安主題ETF是00875國泰網路資安，但成分股是全球資安公司(CrowdStrike等)，
#   幾乎不含台股，對我們205檔追蹤股票沒有覆蓋率，勉強做只能用非官方的概念股清單(如MoneyDJ/CMoney)，
#   跟現有「官方ETF權重揭露」的方法論不一致，等於降低整體資料品質，先不做。
# - 核能/鈾礦：台灣沒有掛牌的核能/鈾礦主題ETF(這是美股/加拿大礦業股的主題，台股裡沒有真正的鈾礦公司)，
#   概念本身就不太適用在台股205檔追蹤清單上，跳過。


def parse_holdings_table(markdown_text):
    """從台新投信頁面的markdown抓「股票」表格區塊，解析成 [{ticker, name, weight}]"""
    m = re.search(r"股票\s*\n+\s*\|\s*代號\s*\|\s*名稱\s*\|\s*股數\s*\|\s*持股權重\s*\|\s*\n(.*?)\n股票合計", markdown_text, re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).strip().split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        code_raw, name, shares, weight = cells[0], cells[1], cells[2], cells[3]
        ticker = code_raw.replace("TT", "").strip()
        if not re.match(r"^\d{4,6}$", ticker):
            continue
        weight_val = float(weight.replace("%", "").replace(",", ""))
        rows.append({"ticker": ticker, "name": name, "weight": weight_val})
    return rows


if __name__ == "__main__":
    print("這支腳本的parse_holdings_table()函式，會在主流程裡被呼叫，搭配firecrawl抓到的markdown使用")
