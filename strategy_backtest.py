"""
目標價調升交易策略回測引擎——臨摹主管同事「FactSet目標價交易策略」PDF說明書的方法論，
用我們自己的資料庫重建一次基準，之後才能在這個基準上細調參數。

方法論對照PDF說明書(/Users/USER/Downloads/FactSet目標價交易策略_主管說明.pdf)：
    ① 訊號：分析師調升目標價(direction=UP)，新聞當天只形成訊號，最早下一交易日執行
    ② 篩選：調升幅度、分析師數、上漲空間門檻
    ③ 排名配置：等權／依調升幅度／原始綜合分數／第二代多因子
    ④ 出場：出現調降、股價達目標價、超過最長持有天數
    ⑤ 資金：固定資本100，每日平帳(獲利抽回、虧損補入)，策略與大盤都用「每日報酬率算術加總」
    ⑥ 訓練(2023-2024)/驗證(2025)/測試(2026)三段式，避免用同一批資料選參數又宣稱有效

使用者在PDF基礎上額外要求、本檔案也做的部分：
    - 上漲空間只用股價版：(新目標價-昨日收盤價)/昨日收盤價(昨日=訊號當天，進場前一天，
      不用進場價本身，因為進場價已經反映訊號公布後市場的反應，拿它當分母會低估真正的上漲空間)。
      2026-07-28：原本另外還有一個不用股價的"change"版((新目標價-舊目標價)/舊目標價)，
      經使用者確認不需要，已移除，只留股價版這一種算法。
    - 成交價格：2026-07-28確認固定用「下一交易日開盤價」進場，不分新聞公布時間點、不做收盤/
      開盤兩版對照——一天只在開盤時買進一次，收盤後再依固定資本(例如10,000,000)做多退少補的
      重新平衡，不會有「這則訊號用收盤價、那則用開盤價」的情況。原本按新聞時間(13:30前後)分流
      收盤/開盤的設計已廢棄不用。
    - avg_rule="tier3"：本次調升幅度或分析師數「高於」該股歷史平均→加碼；
      「低於」平均→維持原權重，不加碼也不出場(不是PDF的連續分數排名式，是使用者要的三段式邏輯)
    - max_portfolio_exposure：投組層級總曝險上限(PDF只有單檔上限，沒有整體曝險上限)

已知限制(誠實揭露，不是之後才發現)：
    - 交易成本、滑價、漲跌停限制、EPS調升訊號混合，都不在這版基準裡，PDF本身也明講是下一步。

2026-07-28修正：資料庫view v_unified_target_events原本用`DISTINCT ON (ticker,date)
ORDER BY analyst_count DESC`去重，同一天同一股票有多篇新聞時會直接刪掉analyst_count較低
的那篇，不管發布時間先後——這在同一個09:00執行窗口內有多篇獨立新聞時，會讓「發布時間較晚
但分析師數較少」的真正有效訊號被資料庫層直接濾掉，永遠傳不到我們自己(prepare_events/
dedupeByExecutionWindow)已經寫對的「同一執行窗口內取發布時間最晚者」邏輯手上。已改成
view不做任何去重，把全部原始新聞都傳出來，去重完全交給應用層處理。

2026-07-28更新：股價/大盤資料改直接從Supabase讀(stock_prices/taiex_index表，透過
get_strategy_price_bundle() RPC一次拉回)，不再讀本機的factset_data/prices_full.json——
這樣這支研究腳本跟網站的strategy.html用同一份資料來源，不會兩邊對不起來。進場固定用開盤價：
進場當天只計算「開盤買進→收盤」的報酬，不是完整一天的收盤對收盤；出場當天沿用既有簡化，
當天不計入報酬(部位直接從當日報酬迴圈移除)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd
import requests

SUPABASE_URL = "https://kiiwaojcetxmeycyupvn.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtpaXdhb2pjZXR4bWV5Y3l1cHZuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ1Mjk2NzAsImV4cCI6MjEwMDEwNTY3MH0.QPnEenJ8OtgWm1q3zhstinsAzXJAD6bunPp6JhrL4PU"
SB_HEADERS = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}

# 同事的新聞/股價資料庫起點，動能/波動率回看窗口的下限對齊這一天(見_attach_enhanced_factors)。
DATA_FLOOR_DATE = pd.Timestamp("2023-01-03")


# ----------------------------------------------------------------------------
# 1. 資料載入(改自Supabase，跟strategy.html同一個RPC/同一份資料)
# ----------------------------------------------------------------------------

def _fetch_all_events():
    out, offset, page_size = [], 0, 5000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/v_unified_target_events",
            headers=SB_HEADERS,
            params={
                "select": "ticker,date,direction,prev_target,new_target,target_change_pct,analyst_count,news_time_taipei",
                "direction": "in.(UP,DOWN)", "order": "date.asc,id.asc",
                "offset": str(offset), "limit": str(page_size),
            },
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


def _fetch_raw_stock_prices(table: str = "stock_prices"):
    """2026-07-28新增：直接查stock_prices原始表，不透過get_strategy_price_bundle()這種
    「先跟大盤交易日曆對齊、對不上的日期直接丟掉」的RPC。個股偶爾會有大盤加權指數當天
    沒有報價、但個股自己有報價的日子(例如某檔股票剛好在這天有資料、大盤那天缺值)，
    這種日子如果透過對齊過的bundle資料，會被整筆丟掉，動能/波動率的回看窗口因此少算
    一天，跟同事backtest.js的makeEvents()「每檔股票各自獨立掃自己的價格紀錄，不受大盤
    日曆限制」對不起來。這裡回傳的是「每檔股票自己的、依日期排序、沒有NaN空缺」的
    原始序列，只給動能/波動率這類回看窗口計算用，不影響逐日模擬本身(逐日模擬本來就該
    用大盤交易日曆，這是正確的、不需要改)。
    """
    out, offset, page_size = [], 0, 10000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS,
            params={"select": "ticker,date,close", "order": "ticker.asc,date.asc",
                    "offset": str(offset), "limit": str(page_size)},
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        offset += page_size
    df = pd.DataFrame(out)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    raw = {}
    for t, g in df.groupby("ticker"):
        g = g.sort_values("date")
        raw[t] = pd.Series(g["close"].values, index=pd.DatetimeIndex(g["date"]))
    return raw


def _dedupe_by_content_signature(events: pd.DataFrame) -> pd.DataFrame:
    """2026-07-29新增：內容重複去重(對照同事articles表的duplicate_signature/duplicate_rank
    欄位)——同一檔股票同一天，同一篇新聞被系統重複發布兩次(例如08:10跟10:10各發一篇，
    old_target/new_target/analyst_count完全相同)，是資料源本身的重複，不是兩則獨立訊號。
    這種重複_dedupe_by_execution_window(用執行窗口當key)抓不到，因為09:00是分界點，
    08:10發布算當天執行、10:10發布算隔天執行，兩篇重複新聞剛好落在不同執行窗口，於是
    被當成兩個獨立的合格候選，entryIdx先被第一篇設到「當天」，馬上又被第二篇(其實是
    同一則新聞的重複發布)蓋成「隔天」——單一個股1天的持有天數差異，經過max_hold_days
    週期性出場/進場的排名資格競爭一路累積下去，是驗證期/測試期超額報酬跟同事對不齊的
    根因。我們自己的Supabase資料庫沒有同事那套duplicate_signature欄位，這裡用內容特徵
    (股票+日期+方向+舊目標價+新目標價+分析師數)重建同樣的去重邏輯：同一組特徵只留發布
    時間最早的一篇，其餘視為重複新聞直接丟棄。"""
    events = events.copy()
    events["_sig"] = (events["ticker"].astype(str) + "|" + events["date"].dt.strftime("%Y-%m-%d") + "|"
                       + events["direction"].astype(str) + "|" + events["prev_target"].astype(str) + "|"
                       + events["new_target"].astype(str) + "|" + events["analyst_count"].astype(str))
    events = events.sort_values("news_time_taipei", na_position="last")
    events = events[~events.duplicated(subset=["_sig"], keep="first")]
    return events.drop(columns=["_sig"]).sort_values(["date", "ticker"]).reset_index(drop=True)


def load_data():
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/get_strategy_price_bundle", headers=SB_HEADERS, json={}, timeout=60)
    r.raise_for_status()
    bundle = r.json()
    events_raw = _fetch_all_events()

    taiex = pd.Series(bundle["taiex_close"], index=pd.to_datetime(bundle["dates"]), dtype=float)
    taiex_open = pd.Series(bundle["taiex_open"], index=pd.to_datetime(bundle["dates"]), dtype=float)
    calendar = taiex.index  # 用大盤有報價的日子當交易日曆基準

    stock_price = {}
    stock_price_open = {}
    for t, arr in bundle["tickers_close"].items():
        stock_price[t] = pd.Series(arr, index=calendar, dtype=float)
    for t, arr in bundle["tickers_open"].items():
        stock_price_open[t] = pd.Series(arr, index=calendar, dtype=float)

    stock_price_raw = _fetch_raw_stock_prices("stock_prices")

    events = pd.DataFrame(events_raw)
    events["date"] = pd.to_datetime(events["date"])
    events = _dedupe_by_content_signature(events)
    return stock_price, stock_price_open, taiex, taiex_open, calendar, events, stock_price_raw


def next_trading_day(d: pd.Timestamp, calendar: pd.DatetimeIndex) -> Optional[pd.Timestamp]:
    """訊號當天不能成交，回傳日曆上第一個嚴格晚於d的交易日；沒有的話回傳None(資料尾端)。"""
    pos = calendar.searchsorted(d, side="right")
    if pos >= len(calendar):
        return None
    return calendar[pos]


def resolve_execution_date(d: pd.Timestamp, time_taipei, calendar: pd.DatetimeIndex) -> Optional[pd.Timestamp]:
    """執行時間窗口規則：交易日以「09:00開盤」當分界，不是午夜。新聞在當天09:00以前
    公布，當天開盤還來得及反應，用當天開盤價；09:00以後公布(或公布在非交易日)，
    最早只能等下一個交易日開盤。這樣盤前新聞不會被多耽誤一整天。"""
    if time_taipei is not None and time_taipei < "09:00:00" and d in calendar:
        return d
    return next_trading_day(d, calendar)


def _dedupe_by_execution_window(events: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """2026-07-28新增：對照同事backtest.js的latestByWindow——同一檔股票、同一個09:00執行
    時間窗口，不管UP還是DOWN，只留發布時間最晚的那篇。原本UP/DOWN是兩條獨立處理的
    資料流(UP進prepare_events決定進場候選、DOWN進down_tickers_on_factory決定出場)，
    完全沒有互相去重——如果同一檔股票同一個執行窗口裡「先有一篇UP、後有一篇DOWN」，
    同事的引擎會讓時間較晚的DOWN直接蓋掉UP(UP從未成為候選)，我們原本兩條資料流互不
    知情，UP還是會被當成候選處理，導致明明應該被蓋掉的進場訊號還是進了場。這裡在UP/DOWN
    分流之前，先合併去重一次，兩邊都用這份「跨方向去重後」的乾淨事件流。"""
    all_ev = events.copy()
    all_ev["execute_date"] = all_ev.apply(
        lambda r: resolve_execution_date(r["date"], r.get("news_time_taipei"), calendar), axis=1
    )
    all_ev = all_ev.dropna(subset=["execute_date"])
    all_ev["publish_ts"] = pd.to_datetime(
        all_ev["date"].dt.strftime("%Y-%m-%d") + " " + all_ev["news_time_taipei"].fillna("00:00:00")
    )
    all_ev = all_ev.sort_values("publish_ts")
    all_ev = all_ev[~all_ev.duplicated(subset=["ticker", "execute_date"], keep="last")]
    return all_ev.drop(columns=["execute_date", "publish_ts"]).sort_values(["ticker", "date"]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# 2. 事件表前處理：算出兩種上漲空間、歷史平均(給avg_rule跟異常調升因子用)
# ----------------------------------------------------------------------------

def prepare_events(events: pd.DataFrame, stock_price: dict, stock_price_open: dict, calendar: pd.DatetimeIndex, taiex: Optional[pd.Series] = None, stock_price_raw: Optional[dict] = None) -> pd.DataFrame:
    # 先跨UP/DOWN方向去重(同一檔股票同一個執行窗口只留發布時間最晚的那篇)，避免明明應該
    # 被較晚的DOWN蓋掉的UP訊號，因為UP/DOWN分開處理而漏網、還是被當成候選進場。
    deduped = _dedupe_by_execution_window(events, calendar)
    ev = deduped[deduped["direction"] == "UP"].copy()
    ev = ev.sort_values(["ticker", "date", "news_time_taipei"]).reset_index(drop=True)

    # 執行日依09:00開盤時間窗口規則決定，不再固定用下一交易日。
    ev["entry_date"] = ev.apply(lambda r: resolve_execution_date(r["date"], r.get("news_time_taipei"), calendar), axis=1)
    ev = ev.dropna(subset=["entry_date"]).copy()

    def entry_price_open(row):
        ser = stock_price_open.get(row["ticker"])
        if ser is None:
            return np.nan
        return ser.get(row["entry_date"], np.nan)

    def entry_prev_close(row):
        """進場前一天(訊號當天)收盤價，上漲空間(股價版)的基準——不用進場價本身，
        因為進場價已經反映訊號公布後市場的反應，拿它當分母會低估真正的上漲空間。"""
        ser = stock_price.get(row["ticker"])
        if ser is None:
            return np.nan
        pos = calendar.get_loc(row["entry_date"])
        if pos == 0:
            return np.nan
        return ser.get(calendar[pos - 1], np.nan)

    ev["entry_price"] = ev.apply(entry_price_open, axis=1)
    ev = ev.dropna(subset=["entry_price"]).copy()
    ev["entry_prev_close"] = ev.apply(entry_prev_close, axis=1)

    # 上漲空間固定用進場前一天收盤價當基準
    ev["upside_price"] = (ev["new_target"] - ev["entry_prev_close"]) / ev["entry_prev_close"]

    # 歷史平均：只用「這筆事件之前」該股票的調升幅度/分析師數(expanding shift(1))，
    # 避免用到當下這筆事件本身或未來事件，這是point-in-time紀律，不是可省略的細節。
    grp = ev.groupby("ticker")
    ev["hist_avg_change"] = grp["target_change_pct"].transform(lambda s: s.shift(1).expanding().mean())
    ev["hist_avg_analyst"] = grp["analyst_count"].transform(lambda s: s.shift(1).expanding().mean())
    ev["hist_std_change"] = grp["target_change_pct"].transform(lambda s: s.shift(1).expanding().std())
    ev["streak"] = grp.cumcount()  # 這是第幾次調升(從0開始)，越大代表過去連續調升次數越多

    if taiex is not None:
        ev = _attach_enhanced_factors(ev, events, stock_price, calendar, taiex, stock_price_raw)

    return ev


def _attach_enhanced_factors(ev: pd.DataFrame, events_full: pd.DataFrame, stock_price: dict, calendar: pd.DatetimeIndex, taiex: pd.Series, stock_price_raw: Optional[dict] = None) -> pd.DataFrame:
    """直接對照同事backtest.js的makeEvents()，算出enhanced配置公式要用的輔助欄位：
    abnormal_revision(異常調升，相對該股過去365天|調升幅度|中位數)、
    recent_upgrades_60(過去60天內調升次數+1)、momentum_20/relative_momentum_20(進場前
    21個價格點、頭尾比的動能，相對加權指數)、volatility_20(同一段期間年化波動率)。
    基準值(abnormal_revision/recent_upgrades_60)用「全部方向」(UP+DOWN)的歷史事件計算，
    跟同事版本一致；動能/波動率只在UP事件(有entry_date)才算得出來。
    """
    all_ev = events_full.copy()
    all_ev["revision"] = all_ev["target_change_pct"] / 100.0
    all_ev["publish_ts"] = pd.to_datetime(
        all_ev["date"].dt.strftime("%Y-%m-%d") + " " + all_ev["news_time_taipei"].fillna("00:00:00")
    )

    # 2026-07-28修正：對照同事backtest.js的latestByWindow——同一檔股票、同一個09:00執行
    # 時間窗口如果有兩篇以上獨立新聞(例如鉅亨網同一次調升重複發了兩天，各自都通過他資料庫
    # 自己的查重判定、不算彼此重複)，同事的引擎會先去重成「只留發布時間最晚的那篇」，
    # 之後才拿這份已去重的新聞流去算abnormal_revision/recent_upgrades_60這些歷史因子。
    # 我們原本是拿「還沒去重的原始新聞流」去算，導致被丟棄的那篇重複新聞還是被算進了
    # 「過去調升次數」的歷史統計裡，把recent_upgrades_60灌水——早期(2023)資料量少時，
    # 一篇灌水的影響被放大，這正是訓練期超額報酬比同事官方數字低一大截的根因。改成
    # 先按(ticker,execute_date)去重(發布時間最晚者留下)，再用這份跟同事口徑一致的乾淨
    # 新聞流去算歷史因子。
    all_ev["execute_date"] = all_ev.apply(
        lambda r: resolve_execution_date(r["date"], r.get("news_time_taipei"), calendar), axis=1
    )
    all_ev = all_ev.dropna(subset=["execute_date"])
    all_ev = all_ev.sort_values("publish_ts")
    all_ev = all_ev[~all_ev.duplicated(subset=["ticker", "execute_date"], keep="last")]
    all_ev = all_ev.sort_values("publish_ts").reset_index(drop=True)

    # 2026-07-28修正：out_rows原本用(ticker,發布日期)當key——如果同一檔股票同一天發布兩篇
    #「執行窗口不同」的獨立新聞(例如一篇09:00前發布當天執行、另一篇09:00後發布隔天執行，
    # 兩篇都合法存活過跨方向去重)，這兩篇的pub_date(只到日期，不含時間)剛好相同，字典
    # key就會撞在一起，後處理的那篇會把前一篇算好的abnormal_revision/recent_upgrades_60
    # 直接覆蓋掉，導致其中一篇拿到完全錯誤(通常是另一篇的)基準值。改用(ticker,execute_date)
    # 當key——這正是_dedupe_by_execution_window保證彼此不重複的欄位，不會再撞。動能/波動率
    # 的進場價位窗口也直接用這一列自己算出來的execute_date，不用另外查表(順便去掉一個
    # 一樣有撞key風險的entry_date_by_key)。
    prior_revisions: dict[str, list] = {}
    prior_upgrades: dict[str, list] = {}
    out_rows = {}

    for _, row in all_ev.iterrows():
        ticker, pub_date, execute_date = row["ticker"], row["date"], row["execute_date"]
        revs = prior_revisions.setdefault(ticker, [])
        cutoff365 = pub_date - pd.Timedelta(days=365)
        base_vals = [v for d, v in revs if d < pub_date and d >= cutoff365]
        base = np.median(base_vals) if base_vals else None
        abnormal = abs(row["revision"]) / base if base else 1.0

        ups = prior_upgrades.setdefault(ticker, [])
        cutoff60 = pub_date - pd.Timedelta(days=60)
        recent_up = 1 + sum(1 for d in ups if d < pub_date and d >= cutoff60)

        mom = rel_mom = 0.0
        vol = 0.35
        if execute_date is not None and execute_date in calendar:
            # 2026-07-28修正：對照同事backtest.js的priorSeries()——動能/波動率的回看窗口是
            # 「這檔股票自己的價格紀錄裡，執行日之前最近21個有效收盤價」，不受大盤交易日曆
            # 限制。個股偶爾會有大盤加權指數當天沒有報價、但個股自己有報價的日子，這種日子
            # 如果用「對齊大盤日曆的序列」去切窗口，會被整筆跳過，回看窗口因此少算一天、
            # 往前多推一天，動能/波動率算出來就會跟同事版本對不上。改成優先用stock_price_raw
            # (每檔股票自己排序、沒有NaN空缺的原始序列)做日期比對，沒有raw資料才退回用
            # 對齊過的calendar序列(兼容舊呼叫方式)。
            # 2026-07-28再修正：同事的新聞/股價資料庫本來就從2023-01-03才開始(FactSet/鉅亨網
            # 的樣本起點)，我們自己的stock_price_raw因為另外回補過2021年至今的歷史，回看窗口
            # 在資料最早期(2023年1月)會比他湊得到更多天數，動能/波動率因此算不一樣——這段
            # 2021-2022的歷史反正也沒有新聞訊號可以回測，沒有實質意義，用戶決定跟同事版本
            # 對齊，回看窗口下限鎖在跟他資料一樣的起點(DATA_FLOOR_DATE)，不用更早的歷史。
            raw_ser = stock_price_raw.get(ticker) if stock_price_raw else None
            if raw_ser is not None and len(raw_ser) > 0:
                cut = raw_ser.index.searchsorted(execute_date, side="left")
                floor_cut = raw_ser.index.searchsorted(DATA_FLOOR_DATE, side="left")
                window = raw_ser.iloc[max(floor_cut, cut - 21):cut]
            else:
                ser = stock_price.get(ticker)
                pos = calendar.get_loc(execute_date)
                window = ser.iloc[max(0, pos - 21):pos].dropna() if ser is not None else pd.Series(dtype=float)
            # 大盤動能窗口下限也要對齊DATA_FLOOR_DATE，理由同上——calendar回溯到2021年，
            # 同事的benchmarkMap只從2023-01-03開始，不鎖下限的話早期訊號的相對動能對不上。
            pos = calendar.get_loc(execute_date)
            floor_pos = calendar.get_loc(DATA_FLOOR_DATE) if DATA_FLOOR_DATE in calendar else 0
            bench_window = taiex.iloc[max(floor_pos, pos - 21):pos]
            if len(window) > 1:
                mom = window.iloc[-1] / window.iloc[0] - 1
                rets = window.pct_change(fill_method=None).dropna()
                if len(rets) > 1:
                    vol = rets.std() * np.sqrt(252)
            if len(bench_window) > 1:
                bench_ret = bench_window.iloc[-1] / bench_window.iloc[0] - 1
                rel_mom = mom - bench_ret

        out_rows[(ticker, execute_date)] = (abnormal, recent_up, mom, rel_mom, vol)

        revs.append((pub_date, abs(row["revision"])))
        if row["direction"] == "UP":
            ups.append(pub_date)

    keys = list(zip(ev["ticker"], ev["entry_date"]))
    ev["abnormal_revision"] = [out_rows.get(k, (1.0, 1, 0.0, 0.0, 0.35))[0] for k in keys]
    ev["recent_upgrades_60"] = [out_rows.get(k, (1.0, 1, 0.0, 0.0, 0.35))[1] for k in keys]
    ev["momentum_20"] = [out_rows.get(k, (1.0, 1, 0.0, 0.0, 0.35))[2] for k in keys]
    ev["relative_momentum_20"] = [out_rows.get(k, (1.0, 1, 0.0, 0.0, 0.35))[3] for k in keys]
    ev["volatility_20"] = [out_rows.get(k, (1.0, 1, 0.0, 0.0, 0.35))[4] for k in keys]
    return ev


# ----------------------------------------------------------------------------
# 3. 參數
# ----------------------------------------------------------------------------

@dataclass
class StrategyParams:
    min_upgrade_pct: float = 3.0            # 最低目標價調升幅度(%)
    min_analyst_count: int = 3              # 最低分析師數
    min_upside: float = 0.0                 # 最低上漲空間門檻(小數，例如0.05=5%)

    max_hold_days: int = 60
    max_weight_per_stock: float = 0.2
    max_positions: int = 10                 # 0=不限
    max_portfolio_exposure: float = 1.0      # 使用者新增：投組總曝險上限，1.0=可滿倉

    sizing_mode: Literal["equal", "by_upgrade", "composite", "multifactor", "enhanced"] = "equal"
    composite_alpha: float = 1.0
    composite_beta: float = 1.0
    composite_gamma: float = 1.0

    avg_rule: Literal["none", "tier3"] = "none"
    avg_boost_mult: float = 1.5             # tier3高於平均時的加碼倍數


# ----------------------------------------------------------------------------
# 4. 篩選 + 權重分配
# ----------------------------------------------------------------------------

def filter_candidates(ev: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    # 2026-07-28修正：原本直接把沒通過門檻的事件整批丟掉，但同事backtest.js的applyEvents()
    # 對「沒通過門檻、但這檔股票已經持有中」的事件不是整篇忽略——會把目標價/分析師數更新
    # 到既有部位上(entryIndex跟分數不變，不算重新進場)，只有「沒通過門檻、且這檔股票目前
    # 沒持有」才會真的整篇忽略。同一天同一股票常有好幾篇文章，後面那篇如果用「自己的」
    # 參考價算出來的上漲空間不夠格，不代表這則新聞完全沒有資訊價值——它至少更新了最新的
    # 目標價，可能讓既有部位提早觸發「達目標價出場」。改成保留所有有效價格資料的事件、
    # 加一個qualifies欄位，篩選判斷留到run_backtest的逐日迴圈依「這檔股票今天在不在
    # active裡」分流處理，不在這裡就整批濾掉。
    valid = ev["entry_price"].notna() & ev["upside_price"].notna()
    out = ev[valid].copy()
    out["qualifies"] = (
        (out["target_change_pct"] >= p.min_upgrade_pct)
        & (out["analyst_count"] >= p.min_analyst_count)
        & (out["upside_price"] >= p.min_upside)
    )
    out["upside_used"] = out["upside_price"]
    out["entry_price_used"] = out["entry_price"]
    return out


def score_candidates(cands: pd.DataFrame, p: StrategyParams, stock_price: dict, calendar: pd.DatetimeIndex) -> pd.Series:
    """回傳每個候選事件(以其DataFrame index為鍵)的原始分數，尚未正規化成權重。"""
    if p.sizing_mode == "equal":
        base = pd.Series(1.0, index=cands.index)
    elif p.sizing_mode == "by_upgrade":
        base = cands["target_change_pct"].clip(lower=0.01)
    elif p.sizing_mode == "composite":
        base = (
            cands["target_change_pct"].clip(lower=0.01) ** p.composite_alpha
            * cands["analyst_count"].clip(lower=1) ** p.composite_beta
            * cands["upside_used"].clip(lower=0.001) ** p.composite_gamma
        )
    elif p.sizing_mode == "multifactor":
        base = _multifactor_score(cands, stock_price, calendar)
    elif p.sizing_mode == "enhanced":
        base = _enhanced_score(cands)
    else:
        raise ValueError(f"未知sizing_mode: {p.sizing_mode}")

    if p.avg_rule == "tier3":
        change_boost = cands["target_change_pct"] > cands["hist_avg_change"]
        analyst_boost = cands["analyst_count"] > cands["hist_avg_analyst"]
        boost = (change_boost | analyst_boost).fillna(False)
        base = base * np.where(boost, p.avg_boost_mult, 1.0)
        # 低於平均：維持base不變(不加碼也不排除)，這裡的np.where已經隱含這個行為

    return base.clip(lower=0)


def _multifactor_score(cands: pd.DataFrame, stock_price: dict, calendar: pd.DatetimeIndex) -> pd.Series:
    """PDF第二代多因子：異常調升 × log(1+分析師) × 上漲空間 × 相對動能 × 連續上修 ÷ 波動率。"""
    z_change = (cands["target_change_pct"] - cands["hist_avg_change"]) / cands["hist_std_change"].replace(0, np.nan)
    z_change = z_change.fillna(0).clip(lower=-3, upper=3) + 3.1  # 平移成正值才能相乘

    log_analyst = np.log1p(cands["analyst_count"])
    upside = cands["upside_used"].clip(lower=0.001)
    streak_factor = 1.0 + cands["streak"].clip(upper=10) * 0.05

    mom = []
    vol = []
    for _, row in cands.iterrows():
        ser = stock_price.get(row["ticker"])
        d = row["entry_date"]
        if ser is None or d not in calendar:
            mom.append(0.0)
            vol.append(np.nan)
            continue
        pos = calendar.get_loc(d)
        window = ser.iloc[max(0, pos - 20):pos]
        rets = window.pct_change(fill_method=None).dropna()
        mom.append(rets.sum() if len(rets) else 0.0)
        vol.append(rets.std() if len(rets) > 1 else np.nan)
    mom = pd.Series(mom, index=cands.index)
    vol = pd.Series(vol, index=cands.index).fillna(pd.Series(vol, index=cands.index).median())
    vol = vol.replace(0, vol[vol > 0].min() if (vol > 0).any() else 0.01)
    mom_factor = mom - mom.median()  # 相對動能：相對同一批候選的中位數，不是跟大盤比(簡化版)
    mom_factor = mom_factor.clip(lower=-0.5) + 0.51  # 平移成正值

    score = z_change * log_analyst * upside * mom_factor * streak_factor / vol
    return score.clip(lower=0)


def _enhanced_score(cands: pd.DataFrame) -> pd.Series:
    """直接對照同事backtest.js的signalScore()『enhanced』分支，逐項複製，不是重新設計：
    surprise=異常調升(相對該股過去365天|調升幅度|中位數，clip[0.25,4])
    analysts=log(1+分析師數)
    upside=上漲空間(clip[0.01,0.60])
    relativeMomentum=1+相對大盤動能(clip[0.25,2])
    repeatBoost=1+0.15×max(0,過去60天調升次數-1)
    risk=年化波動率(clip下限0.10，缺值預設0.35)
    score = surprise × analysts × upside × relativeMomentum × repeatBoost ÷ risk
    """
    # 2026-07-28修正：對照同事backtest.js的`event.abnormalRevision||1`——JS的||對0是falsy，
    # 這其實是他HANDOVER.md自己記錄的已知瑕疵(「abnormalRevision=0目前會因缺值保護被當成1」)，
    # 但這是他小主管已經審過、寫進正式參數的行為，我們要複製這個瑕疵才能跟他的數字對上，
    # 不能自己「修正」成更合理但跟他不一致的版本。abnormal_revision算出來剛好是0.0
    # (revision本身是0，例如目標價沒變動但還是被算成一次調升事件)時，改成當作缺值處理，
    # 不能只用fillna(NaN專用)、讓0繼續留著被clip到0.25——volatility_20同理保守處理。
    surprise = cands["abnormal_revision"].mask(cands["abnormal_revision"] == 0).fillna(1.0).clip(lower=0.25, upper=4.0)
    analysts = np.log1p(cands["analyst_count"].clip(lower=1))
    upside = cands["upside_used"].clip(lower=0.01, upper=0.60)
    relative_momentum = (1.0 + cands["relative_momentum_20"].fillna(0.0)).clip(lower=0.25, upper=2.0)
    repeat_boost = 1.0 + 0.15 * (cands["recent_upgrades_60"].fillna(1.0) - 1).clip(lower=0)
    risk = cands["volatility_20"].mask(cands["volatility_20"] == 0).fillna(0.35).clip(lower=0.10)
    return (surprise * analysts * upside * relative_momentum * repeat_boost / risk).clip(lower=0)


def allocate_weights(scores: pd.Series, p: StrategyParams) -> pd.Series:
    if scores.sum() <= 0:
        return pd.Series(dtype=float)
    if p.max_positions > 0 and len(scores) > p.max_positions:
        # kind='stable'一定要指定：預設quicksort不保證同分數(常見於equal權重)的排序順序，
        # 會讓「哪些既有持股被擠出排名」在每次執行時不可預期，也讓JS版本無法逐日對出一樣的結果。
        scores = scores.sort_values(ascending=False, kind="stable").head(p.max_positions)

    # 水位填充(water-filling)：單檔壓到上限後，省下來的額度要分給還沒被壓到上限的
    # 其他股票，讓總曝險真的能填滿到max_portfolio_exposure，不是壓完就讓那筆錢閒置
    # (原本clip後只整批等比例縮放，資金沒被壓到上限的股票分不到「被壓掉」的那份額度，
    # 曝險常常填不滿)。跟同事版本的cappedWeights()對照，反覆做「這輪按分數比例分配，
    # 誰超過上限就先鎖在上限、退出這輪、剩下的預算留給還沒鎖住的股票」，直到沒有人
    # 再超過上限為止。
    remaining = dict(scores)
    result: dict[str, float] = {}
    budget = p.max_portfolio_exposure
    while remaining and budget > 1e-12:
        total = sum(remaining.values()) or len(remaining)
        capped_any = False
        for ticker in list(remaining.keys()):
            score = remaining[ticker]
            proposed = budget * (score / total if total else 1.0 / len(remaining))
            if proposed > p.max_weight_per_stock + 1e-12:
                result[ticker] = p.max_weight_per_stock
                budget -= p.max_weight_per_stock
                del remaining[ticker]
                capped_any = True
        if not capped_any:
            denom = sum(remaining.values()) or len(remaining)
            for ticker, score in remaining.items():
                result[ticker] = budget * (score / denom if denom else 1.0 / len(remaining))
            break
    return pd.Series(result)


# ----------------------------------------------------------------------------
# 5. 逐日模擬(進出場、固定資本100每日平帳)
# ----------------------------------------------------------------------------

def run_backtest(p: StrategyParams, ev: pd.DataFrame, stock_price: dict, taiex: pd.Series,
                  calendar: pd.DatetimeIndex, start: str, end: str, down_lookup=None,
                  stock_price_open: Optional[dict] = None) -> dict:
    period_cal = calendar[(calendar >= start) & (calendar <= end)]
    if len(period_cal) == 0:
        return {"daily_book": pd.DataFrame(), "summary": {}, "trades": pd.DataFrame(), "orders": pd.DataFrame()}

    if down_lookup is None:
        down_lookup = lambda d: set()

    cands_all = filter_candidates(ev, p)
    cands_all = cands_all[(cands_all["entry_date"] >= period_cal[0]) & (cands_all["entry_date"] <= period_cal[-1])]

    taiex_ret = taiex.pct_change()

    # 2026-07-28重寫：對照同事backtest.js的active/weights雙軌設計。原本「排名被擠出」直接
    # 把股票從holdings刪掉，之後要再進場一定得等一則全新的新聞訊號——但同事的引擎不是這樣：
    # 「排名被擠出」只是那天權重被壓到0(active裡的資格保留)，不是真的離開。只有真正達目標價/
    # 調降/超過最長持有天數，才會把它從active名單踢出。這代表一檔被擠出的股票，隔幾天如果
    # 競爭對手自己出場、名額空出來，同事的引擎會讓它「不需要任何新新聞」就自動用原本(可能已經
    # 是好幾天前)的舊分數重新拿回權重。這是造成訓練期(早期資料筆數少、名額競爭常態性地緊繃)
    # 落差的第二個根因——active(資格池)跟weight_now(今天實際權重)拆成兩個獨立狀態才對得起來。
    active: dict[str, dict] = {}       # ticker -> {entry_idx, target, score}：資格池，只有真出場才刪除
    weight_now: dict[str, float] = {}  # ticker -> 今天實際權重，只放weight>0的
    trade_entry: dict[str, dict] = {}  # ticker -> {entry_date, entry_price}：這次active任期內第一次拿到權重時鎖定，用來組round-trip trades
    rows = []
    trades = []
    # orders是逐單完整帳本，對照同事backtest.js的notionalTrades——每天只要權重有變動(進場/
    # 出場/被擠出/重新配置)都各記一筆BUY/SELL，不是只記「進場到出場」這種round-trip摘要
    # (trades沿用舊格式，win_rate/payoff_ratio還是靠它算)。
    orders = []

    entries_by_date = cands_all.groupby("entry_date")

    for day_idx, day in enumerate(period_cal):
        prev_day = period_cal[day_idx - 1] if day_idx > 0 else None

        # --- 隔夜段報酬：用「今天開盤前實際持有」的部位、當時(昨天收盤後)的權重，
        # 算「昨收→今開」這段報酬。這段要在出場/重新配置動作之前算，因為用的是
        # 「重新配置前」的舊權重。---
        overnight_ret = 0.0
        if prev_day is not None:
            for t, w in weight_now.items():
                open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                prior_close = stock_price.get(t, pd.Series(dtype=float)).get(prev_day, np.nan)
                if not np.isnan(open_now) and not np.isnan(prior_close) and prior_close != 0:
                    overnight_ret += w * (open_now / prior_close - 1)

        # --- 隔夜段結束、出場/重新配置動作之前：先算「自然漂移後」的權重——不重新配置的話，
        # 權重會因為股價漲跌從最後一次配置的目標值移動，公式跟同事backtest.js的
        # positionReturns一樣：weight_t = weight_{t-1} * (1+個股報酬)。注意這裡「不」除以
        # 整體報酬做重新正規化——同事的權重是「相對固定名目本金」的算術權重，不是每天都
        # 强制加總回1的複利權重，這也是為什麼他的exposure/cashWeight每天會偏離1(用
        # DAILY_PROFIT_WITHDRAWAL/DAILY_LOSS_TOPUP把損益結算回現金，不是隱性重新分配到
        # 還持有的部位上)。這個漂移後的權重才是「今天開盤當下實際持有」的權重，出場/重新
        # 配置的weight_before要用這個，不能繼續用「昨天配置完就再也不變」的舊weight_now——
        # 不然SIGNAL_REBALANCE的weight_before/weight_after永遠一樣，跟同事真實交易紀錄
        # (weight_before是漂移後的非整數值)對不起來，這就是主管要求的「每天不管有沒有訊號
        # 都轉回目標權重」的前提。---
        drifted_at_open: dict[str, float] = {}
        for t, w in weight_now.items():
            open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            prior_close = stock_price.get(t, pd.Series(dtype=float)).get(prev_day, np.nan) if prev_day is not None else np.nan
            if not np.isnan(open_now) and not np.isnan(prior_close) and prior_close != 0:
                drifted_at_open[t] = w * (open_now / prior_close)
            else:
                drifted_at_open[t] = w

        # --- 真出場檢查(對active資格池、不是只看今天有沒有權重)：達目標價用「前一天收盤價」
        # 判斷(對照今天開盤前就該知道的資訊，不能用今天收盤價，會有前視偏誤)；調降訊號用
        # 「今天」是否生效判斷；持有天數用交易日數，從active設定的entry_idx算起。---
        down_today = down_lookup(day)
        exited = []
        for t, a in active.items():
            days_held = day_idx - a["entry_idx"]
            prior_close = stock_price.get(t, pd.Series(dtype=float)).get(prev_day, np.nan) if prev_day is not None else np.nan
            reason = None
            if t in down_today:
                reason = "調降出場"
            elif not np.isnan(prior_close) and prior_close >= a["target"]:
                reason = "達目標價出場"
            elif days_held >= p.max_hold_days:
                reason = "超過最長持有天數"
            if reason:
                exited.append((t, reason))
        for t, reason in exited:
            active.pop(t)
            exit_price = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            old_w = drifted_at_open.pop(t, 0.0)
            weight_now.pop(t, None)
            if t in trade_entry:
                te = trade_entry.pop(t)
                trades.append({
                    "ticker": t, "entry_date": te["entry_date"], "exit_date": day,
                    "entry_price": te["entry_price"], "exit_price": exit_price,
                    "reason": reason,
                })
            if old_w > 1e-9:
                orders.append({"date": day, "ticker": t, "side": "SELL", "weight_before": old_w,
                                "weight_after": 0.0, "weight_change": -old_w, "price": exit_price, "reason": reason})

        # --- 新訊號：合格的更新/加入active資格池，重設entry_idx跟目標價，不管這檔股票今天
        # 在不在active裡都要覆蓋——新訊號代表重新起算持有天數，不能拿舊訊號殘留的資訊繼續
        # 判斷。不合格的訊號(例如用自己的參考價算出來上漲空間不夠)如果這檔股票「已經在
        # active裡」，對照同事backtest.js的applyEvents()「else if(active.has(ticker))」分支，
        # 只更新目標價，entry_idx/score/進場價都不動——不是整篇忽略，這則新聞至少讓既有
        # 部位的出場門檻(達目標價出場)用到最新的價位；如果這檔股票目前沒持有，才真的整篇
        # 忽略，不會無中生有創造一筆持股。---
        new_rows_by_ticker = {}
        update_only_by_ticker = {}
        if day in entries_by_date.groups:
            todays = entries_by_date.get_group(day)
            if len(todays) > 0:
                # 分數只算合格的那些——multifactor這類看「同一批候選相對排名」的算法，
                # 分母/中位數統計不該被不合格的候選(它們不會真的進場，只用來更新目標價)
                # 汙染，維持跟filter_candidates還在整批過濾時同樣的統計母體。
                qualifying = todays[todays["qualifies"]]
                if len(qualifying) > 0:
                    scores = score_candidates(qualifying, p, stock_price, calendar)
                    scores.index = qualifying["ticker"].values
                    qualifying_by_ticker = {t: row for t, row in zip(qualifying["ticker"].values, qualifying.to_dict("records"))}
                    scores = scores[~scores.index.duplicated(keep="last")]
                    for t in scores.index:
                        new_rows_by_ticker[t] = (qualifying_by_ticker[t], scores[t])
                # 同一天同一ticker可能有多筆，取最後一筆(對照JS版Map.set的慣例)：不合格
                # 的那篇如果這檔股票已經合格進場(上面已經處理過)，就不算update-only，
                # 只有「不合格、且沒有其他合格筆數把它蓋過去」時才走update-only分支。
                non_qualifying = todays[~todays["qualifies"]]
                for t, row in zip(non_qualifying["ticker"].values, non_qualifying.to_dict("records")):
                    if t in new_rows_by_ticker:
                        continue
                    if t in active:
                        update_only_by_ticker[t] = row
        for t, (row, s) in new_rows_by_ticker.items():
            active[t] = {"entry_idx": day_idx, "target": row["new_target"], "score": s, "entry_price_today": row["entry_price_used"]}
        for t, row in update_only_by_ticker.items():
            active[t]["target"] = row["new_target"]

        # --- 每天都對「整個active資格池」重新正規化權重(不是只有今天有變動的那一小部分)，
        # 固定資本每日平帳：總曝險才會真的每天都保證不超過max_portfolio_exposure。今天沒被
        # 分到權重的active成員，只是「被擠出」暫時領0權重，不會離開active，明天名額空出來
        # 隨時可能不需要新訊號就拿回權重——這是跟同事引擎對齊的關鍵行為，不是bug。---
        if active:
            combined_scores = pd.Series({t: a["score"] for t, a in active.items()})
            new_weights = allocate_weights(combined_scores, p)
            for t, a in active.items():
                w = float(new_weights.get(t, 0.0))
                old_w = drifted_at_open.get(t, 0.0)
                is_fresh_signal = t in new_rows_by_ticker
                if w > 1e-9 and t not in trade_entry:
                    entry_price = a.get("entry_price_today") if is_fresh_signal else (
                        stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan)
                    trade_entry[t] = {"entry_date": day, "entry_price": entry_price}
                delta = w - old_w
                if abs(delta) > 1e-9:
                    price_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                    reason = "QUALIFIED_UPGRADE" if (is_fresh_signal and old_w <= 1e-9) else ("RANKED_OUT" if w <= 1e-9 else "SIGNAL_REBALANCE")
                    orders.append({"date": day, "ticker": t, "side": "BUY" if delta > 0 else "SELL",
                                    "weight_before": old_w, "weight_after": w, "weight_change": delta,
                                    "price": price_now, "reason": reason})
                if w > 1e-9:
                    weight_now[t] = w
                else:
                    weight_now.pop(t, None)

        # --- 盤中段報酬：用「今天重新配置完」的新權重，算「今開→今收」這段報酬。今天出場
        # 或被擠出的部位權重已經是0，不會有盤中段貢獻(對照同事版本的既有簡化)。---
        intraday_ret = 0.0
        for t, w in weight_now.items():
            close_now = stock_price.get(t, pd.Series(dtype=float)).get(day, np.nan)
            open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            if not np.isnan(close_now) and not np.isnan(open_now) and open_now != 0:
                intraday_ret += w * (close_now / open_now - 1)

        daily_ret = overnight_ret + intraday_ret

        # 2026-07-28修正：taiex_ret是對「全部歷史」(calendar，2021年至今)一次算好的
        # pct_change()全域序列，不是針對這次回測視窗算的。如果視窗起始日不是calendar裡
        # 最早的那天，taiex_ret.get(day)在視窗第一天算的是「視窗外前一天到視窗第一天」這段
        # 報酬，會被誤算進這次回測的大盤累積報酬——同事backtest.js用區域變數previousBenchmark
        # (每次回測重新歸零)天生沒這個問題，這裡用視窗起點強制歸零對齊：視窗第一天的大盤
        # 報酬永遠算0，不看taiex_ret.get(day)實際值是多少。
        taiex_ret_today = 0.0 if day_idx == 0 else taiex_ret.get(day, np.nan)

        # --- exposure欄位對照同事的定義：不是「今天重新配置完」的乾淨目標權重總和(那個
        # 恆等於1)，是「今天盤中段自然漂移『後』」的權重總和——同事的exposure/cashWeight
        # 每天會偏離1，是因為他用DAILY_PROFIT_WITHDRAWAL/DAILY_LOSS_TOPUP把損益結算回
        # 現金，不是隱性重新分配到持倉上。drifted_at_close同時也是明天隔夜段的起始權重。---
        drifted_at_close: dict[str, float] = {}
        for t, w in weight_now.items():
            close_now = stock_price.get(t, pd.Series(dtype=float)).get(day, np.nan)
            open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            if not np.isnan(close_now) and not np.isnan(open_now) and open_now != 0:
                drifted_at_close[t] = w * (close_now / open_now)
            else:
                drifted_at_close[t] = w

        rows.append({
            "date": day,
            "holdings": ",".join(weight_now.keys()),
            "n_positions": len(weight_now),
            "exposure": sum(drifted_at_close.values()),
            "daily_return": daily_ret,
            "taiex_return": taiex_ret_today,
        })

        # --- 收盤後把weight_now換成剛剛算好的drifted_at_close，作為明天隔夜段的起始
        # 權重——今天的holdings/exposure顯示仍然是「今天重新配置完」的乾淨目標權重，
        # 漂移只影響「明天開盤前」的drifted_at_open計算。---
        weight_now.update(drifted_at_close)

    book = pd.DataFrame(rows).set_index("date")
    book["daily_return"] = book["daily_return"].fillna(0.0)
    book["taiex_return"] = book["taiex_return"].fillna(0.0)
    book["cum_return"] = book["daily_return"].cumsum()
    book["cum_taiex"] = book["taiex_return"].cumsum()
    book["excess"] = book["cum_return"] - book["cum_taiex"]

    trades_df = pd.DataFrame(trades)
    orders_df = pd.DataFrame(orders)
    summary = summarize(book, trades_df, orders_df)
    return {"daily_book": book, "summary": summary, "trades": trades_df, "orders": orders_df}


def down_tickers_on_factory(ev_full: pd.DataFrame, calendar: pd.DatetimeIndex):
    """跟進場邏輯用同一套09:00執行時間窗口規則，生效日才會跟entry_date口徑一致；也要用
    同一份跨UP/DOWN方向去重過的事件流(_dedupe_by_execution_window)，才會跟prepare_events
    對同一個(ticker,執行窗口)判斷出同樣的贏家——避免這裡看到的DOWN，跟entries那邊看到的
    UP，其實是同一個窗口裡「應該只留一篇」但兩邊各自漏判的情況。"""
    deduped = _dedupe_by_execution_window(ev_full, calendar)
    down = deduped[deduped["direction"] == "DOWN"].copy()
    down["effective_date"] = down.apply(lambda r: resolve_execution_date(r["date"], r.get("news_time_taipei"), calendar), axis=1)
    down = down.dropna(subset=["effective_date"])
    by_date = down.groupby("effective_date")["ticker"].apply(set).to_dict()
    return lambda d: by_date.get(d, set())


# ----------------------------------------------------------------------------
# 6. 績效摘要
# ----------------------------------------------------------------------------

def summarize(book: pd.DataFrame, trades: pd.DataFrame, orders: Optional[pd.DataFrame] = None) -> dict:
    if len(book) == 0:
        return {}
    daily = book["daily_return"]
    total_return = book["cum_return"].iloc[-1]
    ann_factor = 252 / max(len(book), 1)
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else np.nan
    # 2026-07-28修正：對照同事backtest.js的stats()——回撤要用「權益比峰值」算百分比
    # ((1+cum)/(1+peak)-1)，不是直接拿累積報酬點數相減。算術累加報酬會一直往上疊加，
    # 峰值(1+peak_cum)通常遠大於1，不除以這個峰值基期，算出來的回撤百分比會被系統性
    # 高估(絕對值變大)——這正是這幾輪比對下來，我們的回撤數字一直比同事的大上不少、
    # 但超額報酬跟Sharpe都已經對得很準的根因：excess/Sharpe沒受影響，因為它們的公式
    # 本來就沒有這個「除以峰值基期」的步驟，只有回撤這個指標算錯。
    running_max = book["cum_return"].cummax()
    max_dd = ((1 + book["cum_return"]) / (1 + running_max) - 1).min()

    if len(trades) > 0:
        trades = trades.copy()
        trades["ret"] = (trades["exit_price"] - trades["entry_price"]) / trades["entry_price"]
        win_rate = (trades["ret"] > 0).mean()
        avg_win = trades.loc[trades["ret"] > 0, "ret"].mean()
        avg_loss = trades.loc[trades["ret"] <= 0, "ret"].mean()
        payoff = abs(avg_win / avg_loss) if avg_loss and not np.isnan(avg_loss) and avg_loss != 0 else np.nan
    else:
        win_rate = np.nan
        payoff = np.nan

    return {
        "total_return_pct": round(total_return * 100, 2),
        "total_taiex_return_pct": round(book["cum_taiex"].iloc[-1] * 100, 2),
        "excess_return_pct": round(book["excess"].iloc[-1] * 100, 2),
        "annualized_return_pct": round(total_return * ann_factor * 100, 2),
        "sharpe": round(sharpe, 2) if not np.isnan(sharpe) else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "n_trades": len(trades),
        # n_orders：對照同事backtest.js的tradeCount，逐單完整計數(進場+出場+既有持股的權重
        # 再平衡)，才是能跟同事「交易數」直接比較的口徑；n_trades是round-trip(一筆完整進出場)。
        "n_orders": len(orders) if orders is not None else None,
        "win_rate_pct": round(win_rate * 100, 2) if not np.isnan(win_rate) else None,
        "payoff_ratio": round(payoff, 2) if payoff and not np.isnan(payoff) else None,
        "trading_days": len(book),
    }


# ----------------------------------------------------------------------------
# 7. 主程式：baseline驗證 + 訓練/驗證/測試三段 + 開關組合檢查
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    stock_price, stock_price_open, taiex, taiex_open, calendar, events, stock_price_raw = load_data()
    ev = prepare_events(events, stock_price, stock_price_open, calendar, taiex=taiex, stock_price_raw=stock_price_raw)
    down_lookup = down_tickers_on_factory(events, calendar)

    print(f"UP事件(訊號)總數: {len(ev)}")
    print(f"日曆交易日數: {len(calendar)}，範圍 {calendar[0].date()} ~ {calendar[-1].date()}")

    # --- 前瞻偏差檢查 ---
    # 2026-07-28修正：這裡原本寫entry_date<=date整批當作前瞻偏差，是09:00執行時間窗口
    # 規則上線前留下的舊檢查，沒有跟著更新。09:00規則刻意允許entry_date==date(新聞在
    # 當天09:00前公布，當天開盤還來得及反應，用當天開盤價進場)，這不是前瞻偏差，是合理
    # 規則；真正的前瞻偏差只有entry_date<date(進場日還早於訊號公布日，不可能發生)。
    # 這個舊assert因為從09:00規則上線後就沒被重新跑過(平常都是直接呼叫run_backtest或
    # 網站JS版)，一直沒發現自己已經過期——直到用同事的資料庫重跑這個腳本才觸發，證明
    # 自己資料的352筆當日進場案例其實一直是對的，錯的是這行舊檢查本身。
    strict_lookahead = ev[ev["entry_date"] < ev["date"]]
    assert len(strict_lookahead) == 0, f"發現{len(strict_lookahead)}筆進場日早於訊號日，真的前瞻偏差！"
    same_day = ev[ev["entry_date"] == ev["date"]]
    bad_same_day = same_day[same_day["news_time_taipei"].isna() | (same_day["news_time_taipei"] >= "09:00:00")]
    assert len(bad_same_day) == 0, f"發現{len(bad_same_day)}筆當日進場但新聞是09:00後才公布，前瞻偏差！"
    print(f"前瞻偏差檢查通過：所有進場日都不早於訊號日，其中{len(same_day)}筆是09:00前公布、"
          f"當天開盤進場(合理)，其餘{len(ev) - len(same_day)}筆是隔一交易日以後才進場\n")

    baseline = StrategyParams()
    periods = [("訓練期2023-2024", "2023-01-01", "2024-12-31"),
               ("驗證期2025", "2025-01-01", "2025-12-31"),
               ("測試期2026", "2026-01-01", "2026-12-31")]

    print("=== Baseline(equal權重) 三段式結果 ===")
    for name, s, e in periods:
        res = run_backtest(baseline, ev, stock_price, taiex, calendar, s, e, down_lookup=down_lookup,
                            stock_price_open=stock_price_open)
        print(f"{name}: {res['summary']}")

    print("\n=== sizing_mode / avg_rule 開關組合檢查(全期間2023-2026) ===")
    for sizing in ["equal", "by_upgrade", "composite", "multifactor"]:
        for avg_rule in ["none", "tier3"]:
            p = StrategyParams(sizing_mode=sizing, avg_rule=avg_rule)
            res = run_backtest(p, ev, stock_price, taiex, calendar, "2023-01-01", "2026-12-31", down_lookup=down_lookup,
                                stock_price_open=stock_price_open)
            s = res["summary"]
            print(f"sizing={sizing:11s} avg_rule={avg_rule:5s} -> "
                  f"總報酬={s.get('total_return_pct')}% 超額={s.get('excess_return_pct')}% "
                  f"交易數={s.get('n_trades')} 勝率={s.get('win_rate_pct')}%")
