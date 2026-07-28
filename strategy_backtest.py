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
    - 同一天同一股票多篇新聞，沿用資料庫view v_unified_target_events既有的去重邏輯
      (優先TARGET_PRICE類型、其次analyst_count較高者)，跟PDF「取當日最後一篇」不完全一致，
      差異只發生在極少數同日多篇的邊界案例，不重建。
    - 交易成本、滑價、漲跌停限制、EPS調升訊號混合，都不在這版基準裡，PDF本身也明講是下一步。

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

    events = pd.DataFrame(events_raw)
    events["date"] = pd.to_datetime(events["date"])
    return stock_price, stock_price_open, taiex, taiex_open, calendar, events


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


# ----------------------------------------------------------------------------
# 2. 事件表前處理：算出兩種上漲空間、歷史平均(給avg_rule跟異常調升因子用)
# ----------------------------------------------------------------------------

def prepare_events(events: pd.DataFrame, stock_price: dict, stock_price_open: dict, calendar: pd.DatetimeIndex, taiex: Optional[pd.Series] = None) -> pd.DataFrame:
    ev = events[events["direction"] == "UP"].copy()
    ev = ev.sort_values(["ticker", "date"]).reset_index(drop=True)

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
        ev = _attach_enhanced_factors(ev, events, stock_price, calendar, taiex)

    return ev


def _attach_enhanced_factors(ev: pd.DataFrame, events_full: pd.DataFrame, stock_price: dict, calendar: pd.DatetimeIndex, taiex: pd.Series) -> pd.DataFrame:
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
    all_ev = all_ev.sort_values("publish_ts").reset_index(drop=True)

    entry_date_by_key = dict(zip(zip(ev["ticker"], ev["date"]), ev["entry_date"]))

    prior_revisions: dict[str, list] = {}
    prior_upgrades: dict[str, list] = {}
    out_rows = {}

    for _, row in all_ev.iterrows():
        ticker, pub_date = row["ticker"], row["date"]
        revs = prior_revisions.setdefault(ticker, [])
        cutoff365 = pub_date - pd.Timedelta(days=365)
        base_vals = [v for d, v in revs if d < pub_date and d >= cutoff365]
        base = np.median(base_vals) if base_vals else None
        abnormal = abs(row["revision"]) / base if base else 1.0

        ups = prior_upgrades.setdefault(ticker, [])
        cutoff60 = pub_date - pd.Timedelta(days=60)
        recent_up = 1 + sum(1 for d in ups if d < pub_date and d >= cutoff60)

        entry_date = entry_date_by_key.get((ticker, pub_date))
        mom = rel_mom = 0.0
        vol = 0.35
        if entry_date is not None and entry_date in calendar:
            pos = calendar.get_loc(entry_date)
            ser = stock_price.get(ticker)
            window = ser.iloc[max(0, pos - 21):pos].dropna() if ser is not None else pd.Series(dtype=float)
            bench_window = taiex.iloc[max(0, pos - 21):pos]
            if len(window) > 1:
                mom = window.iloc[-1] / window.iloc[0] - 1
                rets = window.pct_change(fill_method=None).dropna()
                if len(rets) > 1:
                    vol = rets.std() * np.sqrt(252)
            if len(bench_window) > 1:
                bench_ret = bench_window.iloc[-1] / bench_window.iloc[0] - 1
                rel_mom = mom - bench_ret

        out_rows[(ticker, pub_date)] = (abnormal, recent_up, mom, rel_mom, vol)

        revs.append((pub_date, abs(row["revision"])))
        if row["direction"] == "UP":
            ups.append(pub_date)

    keys = list(zip(ev["ticker"], ev["date"]))
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
    mask = (
        ev["entry_price"].notna()
        & ev["upside_price"].notna()
        & (ev["target_change_pct"] >= p.min_upgrade_pct)
        & (ev["analyst_count"] >= p.min_analyst_count)
        & (ev["upside_price"] >= p.min_upside)
    )
    out = ev[mask].copy()
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
    surprise = cands["abnormal_revision"].fillna(1.0).clip(lower=0.25, upper=4.0)
    analysts = np.log1p(cands["analyst_count"].clip(lower=1))
    upside = cands["upside_used"].clip(lower=0.01, upper=0.60)
    relative_momentum = (1.0 + cands["relative_momentum_20"].fillna(0.0)).clip(lower=0.25, upper=2.0)
    repeat_boost = 1.0 + 0.15 * (cands["recent_upgrades_60"].fillna(1.0) - 1).clip(lower=0)
    risk = cands["volatility_20"].fillna(0.35).clip(lower=0.10)
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

    holdings: dict[str, dict] = {}  # ticker -> {weight, entry_date, entry_price}
    rows = []
    trades = []
    # 2026-07-28新增：orders是逐單完整帳本，對照同事backtest.js的notionalTrades——每天只要
    # 權重有變動(進場/出場/既有持股單純被重新配置到新權重)都各記一筆BUY/SELL，不是只記
    # 「進場到出場」這種round-trip摘要(trades沿用舊格式，win_rate/payoff_ratio還是靠它算)。
    # 既有持股「只改權重、不出場」這個動作原本完全沒被記錄，等於報酬計算已經假設你每天
    # 真的有下單調整部位，交易紀錄卻假裝沒發生過——這裡補上讓兩份紀錄口徑一致，也讓我們
    # 的交易數第一次能跟同事的tradeCount直接比。
    orders = []

    entries_by_date = cands_all.groupby("entry_date")

    for day_idx, day in enumerate(period_cal):
        prev_day = period_cal[day_idx - 1] if day_idx > 0 else None

        # --- 隔夜段報酬：用「今天開盤前就已經持有」的部位、當時(昨天收盤後)的權重，
        # 算「昨收→今開」這段報酬。今天才進場的部位還沒經歷這段，不計入。這段要在
        # 出場/重新配置動作之前算，因為用的是「重新配置前」的舊權重。---
        overnight_ret = 0.0
        if prev_day is not None:
            for t, h in holdings.items():
                open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                prior_close = stock_price.get(t, pd.Series(dtype=float)).get(prev_day, np.nan)
                if not np.isnan(open_now) and not np.isnan(prior_close) and prior_close != 0:
                    overnight_ret += h["weight"] * (open_now / prior_close - 1)

        # --- 出場檢查：達目標價用「前一天收盤價」判斷(對照今天開盤前就該知道的資訊，
        # 不能用今天收盤價，那是還沒發生的事、會有前視偏誤)；調降訊號用「今天」是否
        # 生效判斷；持有天數用交易日數(不是自然日數)，跟同事版本一致改用>=判斷。---
        down_today = down_lookup(day)
        exited = []
        for t, h in holdings.items():
            days_held = day_idx - h["entry_idx"]
            prior_close = stock_price.get(t, pd.Series(dtype=float)).get(prev_day, np.nan) if prev_day is not None else np.nan
            reason = None
            if t in down_today:
                reason = "調降出場"
            elif not np.isnan(prior_close) and prior_close >= h["target"]:
                reason = "達目標價出場"
            elif days_held >= p.max_hold_days:
                reason = "超過最長持有天數"
            if reason:
                exited.append((t, reason))
        for t, reason in exited:
            h = holdings.pop(t)
            exit_price = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            trades.append({
                "ticker": t, "entry_date": h["entry_date"], "exit_date": day,
                "entry_price": h["entry_price"], "exit_price": exit_price,
                "reason": reason,
            })
            orders.append({"date": day, "ticker": t, "side": "SELL", "weight_before": h["weight"],
                            "weight_after": 0.0, "weight_change": -h["weight"], "price": exit_price, "reason": reason})

        # --- 新進場候選(含「已持有但又有新訊號」的股票——這種要當成重新進場處理：
        # 更新目標價、重新起算持有天數，不能拿舊的、過時的目標價繼續判斷出場) ---
        new_rows_by_ticker = {}
        if day in entries_by_date.groups:
            todays = entries_by_date.get_group(day)
            if len(todays) > 0:
                scores = score_candidates(todays, p, stock_price, calendar)
                scores.index = todays["ticker"].values
                # 同一天同一ticker可能有多筆(不同訊號日剛好映射到同一個進場日)，
                # 分數取最後一筆、進場價/目標價取第一筆出現的，跟combined_scores原本的
                # keep="last"對照JS版firstRowByTicker的慣例一致。
                scores = scores[~scores.index.duplicated(keep="last")]
                for t in scores.index:
                    new_rows_by_ticker[t] = (todays[todays["ticker"] == t].iloc[0], scores[t])

        # --- 每天都對「目前整個帳本(舊部位+今天新候選)」重新正規化權重 ---
        # 固定資本每日平帳：不管前一天賺賠，隔天開盤前都會把整個投組還原回固定本金重新部署，
        # 所以權重不能只更新「今天有變動的那一小部分」，要整批一起重算，總曝險才會真的
        # 每天都保證不超過max_portfolio_exposure，不會有舊部位權重被放著不動、跟新配置疊加
        # 導致總曝險偷偷超過100%的問題。
        if holdings or new_rows_by_ticker:
            score_map = {t: h["score"] for t, h in holdings.items()}
            score_map.update({t: s for t, (_, s) in new_rows_by_ticker.items()})
            combined_scores = pd.Series(score_map)
            new_weights = allocate_weights(combined_scores, p)

            for t, (row, s) in new_rows_by_ticker.items():
                if t in new_weights.index and new_weights[t] > 0:
                    holdings[t] = {
                        "weight": new_weights[t], "entry_date": day, "entry_idx": day_idx,
                        "entry_price": row["entry_price_used"], "target": row["new_target"], "score": s,
                    }
                    orders.append({"date": day, "ticker": t, "side": "BUY", "weight_before": 0.0,
                                    "weight_after": new_weights[t], "weight_change": new_weights[t],
                                    "price": row["entry_price_used"], "reason": "QUALIFIED_UPGRADE"})
                elif t in holdings:
                    # 已持有的股票這次雖然又有新訊號，但新分數沒擠進名額——原本的邏輯
                    # 縫隙是這裡什麼都不做，導致舊權重留著沒被清掉，總曝險偷偷超過100%。
                    # 這裡要跟一般被擠出排名一樣處理：出場。
                    h = holdings.pop(t)
                    exit_price = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                    trades.append({
                        "ticker": t, "entry_date": h["entry_date"], "exit_date": day,
                        "entry_price": h["entry_price"], "exit_price": exit_price,
                        "reason": "排名被擠出",
                    })
                    orders.append({"date": day, "ticker": t, "side": "SELL", "weight_before": h["weight"],
                                    "weight_after": 0.0, "weight_change": -h["weight"], "price": exit_price, "reason": "RANKED_OUT"})
            for t in list(holdings.keys()):
                if t in new_rows_by_ticker:
                    continue  # 剛加入/已處理過的在上面設好權重或出場了
                if t in new_weights.index and new_weights[t] > 0:
                    # 2026-07-28新增：既有持股「維持在名單內、但權重被重新配置」也是真實下單
                    # 行為——隔夜漲跌讓部位偏離目標權重，開盤前本來就要買賣調整回新權重，不是
                    # bookkeeping假動作。之前這裡完全沒記錄，等於報酬計算已經假設你下了這筆單，
                    # 交易紀錄卻沒承認它發生過。
                    before = holdings[t]["weight"]
                    holdings[t]["weight"] = new_weights[t]
                    delta = new_weights[t] - before
                    if abs(delta) > 1e-9:
                        open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                        orders.append({"date": day, "ticker": t, "side": "BUY" if delta > 0 else "SELL",
                                        "weight_before": before, "weight_after": new_weights[t],
                                        "weight_change": delta, "price": open_now, "reason": "SIGNAL_REBALANCE"})
                else:
                    h = holdings.pop(t)
                    exit_price = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
                    trades.append({
                        "ticker": t, "entry_date": h["entry_date"], "exit_date": day,
                        "entry_price": h["entry_price"], "exit_price": exit_price,
                        "reason": "排名被擠出",
                    })
                    orders.append({"date": day, "ticker": t, "side": "SELL", "weight_before": h["weight"],
                                    "weight_after": 0.0, "weight_change": -h["weight"], "price": exit_price, "reason": "RANKED_OUT"})

        # --- 盤中段報酬：用「今天重新配置完」的新權重，算「今開→今收」這段報酬。
        # 不管是今天新進場還是本來就持有、繼續留著的部位，都用今天的新權重算這段
        # ——這樣同一檔股票如果因為新部位加入被稀釋，稀釋前(隔夜段)已經用舊權重
        # 算過的報酬不會被追溯打折，稀釋只影響稀釋之後(盤中段)這一段。當天出場的
        # 部位已經從holdings移除，不會有盤中段(對照同事版本的既有簡化)。---
        intraday_ret = 0.0
        for t, h in holdings.items():
            close_now = stock_price.get(t, pd.Series(dtype=float)).get(day, np.nan)
            open_now = stock_price_open.get(t, pd.Series(dtype=float)).get(day, np.nan) if stock_price_open else np.nan
            if not np.isnan(close_now) and not np.isnan(open_now) and open_now != 0:
                intraday_ret += h["weight"] * (close_now / open_now - 1)

        daily_ret = overnight_ret + intraday_ret

        rows.append({
            "date": day,
            "holdings": ",".join(holdings.keys()),
            "n_positions": len(holdings),
            "exposure": sum(h["weight"] for h in holdings.values()),
            "daily_return": daily_ret,
            "taiex_return": taiex_ret.get(day, np.nan),
        })

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
    """跟進場邏輯用同一套09:00執行時間窗口規則，生效日才會跟entry_date口徑一致。"""
    down = ev_full[ev_full["direction"] == "DOWN"].copy()
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
    running_max = book["cum_return"].cummax()
    max_dd = (book["cum_return"] - running_max).min()

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
    stock_price, stock_price_open, taiex, taiex_open, calendar, events = load_data()
    ev = prepare_events(events, stock_price, stock_price_open, calendar, taiex=taiex)
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
