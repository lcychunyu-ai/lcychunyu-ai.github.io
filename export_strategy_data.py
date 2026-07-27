"""
把prices_full.json/taiex_full.json重新打包成給strategy.html(瀏覽器端JS回測引擎)用的精簡格式。

原本的prices_full.json是「每檔股票各自一份{日期:價格}」，214檔股票各自重複存了1344次日期字串，
9.2MB；網站是靜態部署(GitHub Pages)，這份資料要整包送到瀏覽器裡跑，用不到的重複日期字串沒有意義。

改成「共用一份日期陣列，每檔股票只存價格陣列(用同一個index對齊日期)」，體積小很多，
也方便JS直接用陣列index做逐日回測迴圈。
"""
import json

with open("factset_data/prices_full.json", encoding="utf-8") as f:
    prices_raw = json.load(f)
with open("factset_data/taiex_full.json", encoding="utf-8") as f:
    taiex_raw = json.load(f)

# 用大盤有報價的日子當交易日曆基準(跟strategy_backtest.py的load_data()邏輯一致)
dates = sorted(taiex_raw.keys())

taiex_close = [taiex_raw.get(d) for d in dates]

tickers_out = {}
for ticker, d in prices_raw.items():
    p = d["prices"]
    tickers_out[ticker] = [p.get(date) for date in dates]

strategy_prices = {"dates": dates, "tickers": tickers_out}
strategy_taiex = {"dates": dates, "close": taiex_close}

with open("factset_data/strategy_prices.json", "w", encoding="utf-8") as f:
    json.dump(strategy_prices, f, separators=(",", ":"))
with open("factset_data/strategy_taiex.json", "w", encoding="utf-8") as f:
    json.dump(strategy_taiex, f, separators=(",", ":"))

import os
print(f"交易日數: {len(dates)}，範圍 {dates[0]} ~ {dates[-1]}")
print(f"股票數: {len(tickers_out)}")
print(f"strategy_prices.json 大小: {os.path.getsize('factset_data/strategy_prices.json')/1e6:.2f} MB")
print(f"strategy_taiex.json 大小: {os.path.getsize('factset_data/strategy_taiex.json')/1e6:.2f} MB")
