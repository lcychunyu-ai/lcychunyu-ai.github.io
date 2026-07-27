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
這樣這支研究腳本跟網站的strategy.html用同一份資料來源，不會兩邊對不起來。開盤價也已經支援：
fill_price="open"時，進場當天用「開盤買進→收盤」的報酬，不是完整一天的收盤對收盤；
出場當天(不論收盤/開盤版本)沿用既有簡化，當天不計入報酬(部位直接從當日報酬迴圈移除)。
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
                "select": "ticker,date,direction,prev_target,new_target,target_change_pct,analyst_count",
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


# ----------------------------------------------------------------------------
# 2. 事件表前處理：算出兩種上漲空間、歷史平均(給avg_rule跟異常調升因子用)
# ----------------------------------------------------------------------------

def prepare_events(events: pd.DataFrame, stock_price: dict, stock_price_open: dict, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    ev = events[events["direction"] == "UP"].copy()
    ev = ev.sort_values(["ticker", "date"]).reset_index(drop=True)

    ev["entry_date"] = ev["date"].apply(lambda d: next_trading_day(d, calendar))
    ev = ev.dropna(subset=["entry_date"]).copy()

    def entry_price_close(row):
        ser = stock_price.get(row["ticker"])
        if ser is None:
            return np.nan
        return ser.get(row["entry_date"], np.nan)

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

    ev["entry_price_close"] = ev.apply(entry_price_close, axis=1)
    ev = ev.dropna(subset=["entry_price_close"]).copy()
    ev["entry_price_open"] = ev.apply(entry_price_open, axis=1)
    ev["entry_prev_close"] = ev.apply(entry_prev_close, axis=1)

    # 上漲空間固定用進場前一天收盤價當基準，跟實際成交用開盤/收盤無關
    ev["upside_price"] = (ev["new_target"] - ev["entry_prev_close"]) / ev["entry_prev_close"]

    # 歷史平均：只用「這筆事件之前」該股票的調升幅度/分析師數(expanding shift(1))，
    # 避免用到當下這筆事件本身或未來事件，這是point-in-time紀律，不是可省略的細節。
    grp = ev.groupby("ticker")
    ev["hist_avg_change"] = grp["target_change_pct"].transform(lambda s: s.shift(1).expanding().mean())
    ev["hist_avg_analyst"] = grp["analyst_count"].transform(lambda s: s.shift(1).expanding().mean())
    ev["hist_std_change"] = grp["target_change_pct"].transform(lambda s: s.shift(1).expanding().std())
    ev["streak"] = grp.cumcount()  # 這是第幾次調升(從0開始)，越大代表過去連續調升次數越多

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

    sizing_mode: Literal["equal", "by_upgrade", "composite", "multifactor"] = "equal"
    composite_alpha: float = 1.0
    composite_beta: float = 1.0
    composite_gamma: float = 1.0

    avg_rule: Literal["none", "tier3"] = "none"
    avg_boost_mult: float = 1.5             # tier3高於平均時的加碼倍數

    fill_price: Literal["open", "close"] = "close"
    enable_rank_eviction: bool = False      # 被更高分數股票擠出排名(PDF有，本版預設關閉)


# ----------------------------------------------------------------------------
# 4. 篩選 + 權重分配
# ----------------------------------------------------------------------------

def filter_candidates(ev: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    entry_col = "entry_price_open" if p.fill_price == "open" else "entry_price_close"
    mask = (
        ev[entry_col].notna()
        & ev["upside_price"].notna()
        & (ev["target_change_pct"] >= p.min_upgrade_pct)
        & (ev["analyst_count"] >= p.min_analyst_count)
        & (ev["upside_price"] >= p.min_upside)
    )
    out = ev[mask].copy()
    out["upside_used"] = out["upside_price"]
    out["entry_price_used"] = out[entry_col]
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


def allocate_weights(scores: pd.Series, p: StrategyParams) -> pd.Series:
    if scores.sum() <= 0:
        return pd.Series(dtype=float)
    if p.max_positions > 0 and len(scores) > p.max_positions:
        # kind='stable'一定要指定：預設quicksort不保證同分數(常見於equal權重)的排序順序，
        # 會讓「哪些既有持股被擠出排名」在每次執行時不可預期，也讓JS版本無法逐日對出一樣的結果。
        scores = scores.sort_values(ascending=False, kind="stable").head(p.max_positions)
    weights = scores / scores.sum()
    weights = weights.clip(upper=p.max_weight_per_stock)
    total = weights.sum()
    if total > 0:
        weights = weights / total * min(total, p.max_portfolio_exposure)
    return weights


# ----------------------------------------------------------------------------
# 5. 逐日模擬(進出場、固定資本100每日平帳)
# ----------------------------------------------------------------------------

def run_backtest(p: StrategyParams, ev: pd.DataFrame, stock_price: dict, taiex: pd.Series,
                  calendar: pd.DatetimeIndex, start: str, end: str, down_lookup=None,
                  stock_price_open: Optional[dict] = None) -> dict:
    period_cal = calendar[(calendar >= start) & (calendar <= end)]
    if len(period_cal) == 0:
        return {"daily_book": pd.DataFrame(), "summary": {}, "trades": pd.DataFrame()}

    if down_lookup is None:
        down_lookup = lambda d: set()

    # 預先算好每檔股票的日報酬序列，不要在逐日迴圈裡對整條序列重算pct_change()，
    # 200多檔股票、900多個交易日，迴圈裡重算會慢上好幾個量級。
    stock_ret = {t: ser.pct_change(fill_method=None) for t, ser in stock_price.items()}
    # 開盤成交版本：進場當天用「開盤買進→收盤」的報酬，不是完整一天的收盤對收盤
    # (訊號隔日開盤才成交，當天開盤前的變動沒有參與到)；進場後第二天起跟收盤版一樣用stock_ret。
    stock_ret_open_entry = {}
    if p.fill_price == "open" and stock_price_open:
        for t, close_ser in stock_price.items():
            open_ser = stock_price_open.get(t)
            if open_ser is None:
                continue
            stock_ret_open_entry[t] = (close_ser - open_ser) / open_ser

    cands_all = filter_candidates(ev, p)
    cands_all = cands_all[(cands_all["entry_date"] >= period_cal[0]) & (cands_all["entry_date"] <= period_cal[-1])]

    taiex_ret = taiex.pct_change()

    holdings: dict[str, dict] = {}  # ticker -> {weight, entry_date, entry_price}
    rows = []
    trades = []

    entries_by_date = cands_all.groupby("entry_date")

    for day in period_cal:
        # --- 出場檢查(用「今天」是否有這檔股票的DOWN訊號來判斷，DOWN訊號來自完整events表) ---
        down_today = down_lookup(day)
        exited = []
        for t, h in holdings.items():
            days_held = (day - h["entry_date"]).days
            price_now = stock_price.get(t, pd.Series(dtype=float)).get(day, np.nan)
            reason = None
            if t in down_today:
                reason = "調降出場"
            elif not np.isnan(price_now) and price_now >= h["target"]:
                reason = "達目標價出場"
            elif days_held > p.max_hold_days:
                reason = "超過最長持有天數"
            if reason:
                exited.append((t, reason))
        for t, reason in exited:
            h = holdings.pop(t)
            trades.append({
                "ticker": t, "entry_date": h["entry_date"], "exit_date": day,
                "entry_price": h["entry_price"], "exit_price": stock_price.get(t, pd.Series(dtype=float)).get(day, np.nan),
                "reason": reason,
            })

        # --- 新進場候選 ---
        if day in entries_by_date.groups:
            todays = entries_by_date.get_group(day)
            todays = todays[~todays["ticker"].isin(holdings.keys())]
            if len(todays) > 0:
                scores = score_candidates(todays, p, stock_price, calendar)
                scores.index = todays["ticker"].values
                existing_scores = pd.Series(0.0, index=list(holdings.keys()))
                combined_scores = pd.concat([existing_scores, scores])
                combined_scores = combined_scores[~combined_scores.index.duplicated(keep="last")]
                new_weights = allocate_weights(combined_scores, p)
                for t in scores.index:
                    if t in new_weights.index and new_weights[t] > 0:
                        row = todays[todays["ticker"] == t].iloc[0]
                        holdings[t] = {
                            "weight": new_weights[t], "entry_date": day,
                            "entry_price": row["entry_price_used"], "target": row["new_target"],
                        }
                for t in list(holdings.keys()):
                    if t in new_weights.index:
                        holdings[t]["weight"] = new_weights[t]

        # --- 當日報酬(固定資本100，權重在持有期間不因股價變動而複利滾動) ---
        # 收盤成交版本進場當天是收盤才成交，當天完全沒有部位曝險，報酬算0，隔天才開始計入
        # (跟出場當天不計報酬是同一個道理，兩者都要排除)。
        daily_ret = 0.0
        for t, h in holdings.items():
            is_entry_day = h["entry_date"] == day
            if is_entry_day and p.fill_price == "close":
                continue
            rser = stock_ret_open_entry.get(t) if (is_entry_day and p.fill_price == "open") else stock_ret.get(t)
            if rser is None:
                continue
            r = rser.get(day, np.nan)
            if not np.isnan(r):
                daily_ret += h["weight"] * r

        rows.append({
            "date": day,
            "holdings": ",".join(holdings.keys()),
            "n_positions": len(holdings),
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
    summary = summarize(book, trades_df)
    return {"daily_book": book, "summary": summary, "trades": trades_df}


def down_tickers_on_factory(ev_full: pd.DataFrame, calendar: pd.DatetimeIndex):
    """跟進場邏輯用同一套紀律：調降訊號當天不能反應，用下一交易日當生效日。"""
    down = ev_full[ev_full["direction"] == "DOWN"].copy()
    down["effective_date"] = down["date"].apply(lambda d: next_trading_day(d, calendar))
    down = down.dropna(subset=["effective_date"])
    by_date = down.groupby("effective_date")["ticker"].apply(set).to_dict()
    return lambda d: by_date.get(d, set())


# ----------------------------------------------------------------------------
# 6. 績效摘要
# ----------------------------------------------------------------------------

def summarize(book: pd.DataFrame, trades: pd.DataFrame) -> dict:
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
        "win_rate_pct": round(win_rate * 100, 2) if not np.isnan(win_rate) else None,
        "payoff_ratio": round(payoff, 2) if payoff and not np.isnan(payoff) else None,
        "trading_days": len(book),
    }


# ----------------------------------------------------------------------------
# 7. 主程式：baseline驗證 + 訓練/驗證/測試三段 + 開關組合檢查
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    stock_price, stock_price_open, taiex, taiex_open, calendar, events = load_data()
    ev = prepare_events(events, stock_price, stock_price_open, calendar)
    down_lookup = down_tickers_on_factory(events, calendar)

    print(f"UP事件(訊號)總數: {len(ev)}")
    print(f"日曆交易日數: {len(calendar)}，範圍 {calendar[0].date()} ~ {calendar[-1].date()}")

    # --- 前瞻偏差檢查 ---
    bad = ev[ev["entry_date"] <= ev["date"]]
    assert len(bad) == 0, f"發現{len(bad)}筆進場日沒有晚於訊號日，前瞻偏差！"
    print("前瞻偏差檢查通過：所有進場日都嚴格晚於訊號日\n")

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
