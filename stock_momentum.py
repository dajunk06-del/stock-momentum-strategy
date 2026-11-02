"""
Individual Stock Momentum Strategy - monthly signal + live tracking.

Each month-end:
  1. Take every stock in the S&P 500.
  2. Measure its "12-1" momentum: the return from 12 months ago to 1 month ago.
     The most recent month is skipped because last month's winners tend to
     give some back the next month (short-term reversal).
  3. Hold the top 20, equal weight (5% each), for the next month.
  4. Trend filter: if SPY closes the month below its 10-month moving average,
     hold T-bills instead. This is the guard against momentum crashes.

Run once a month (GitHub Actions does it automatically). It:
  - writes the new picks to stock_signal.md
  - appends them to stock_live_log.csv
  - fills in last month's realized return vs. SPY for the previous picks
"""

import os
import sys
import time
from datetime import date

import pandas as pd

TOP_N = 20
LOOKBACK = 12        # months
SKIP = 1             # skip the most recent month
TREND_MONTHS = 10    # SPY moving-average length for the trend filter
BENCHMARK = "SPY"
TBILL = "^IRX"       # 13-week T-bill yield, used as the cash return
LOG_FILE = "stock_live_log.csv"
SIGNAL_FILE = "stock_signal.md"

DATA_REPO = "https://raw.githubusercontent.com/fja05680/sp500/master/"
CURRENT_URL = DATA_REPO + "sp500.csv"
HISTORY_URL = DATA_REPO + "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"


# ---------------------------------------------------------------- data

def to_yahoo(ticker):
    """S&P lists use BRK.B; Yahoo uses BRK-B."""
    return ticker.replace(".", "-")


def load_current_members():
    """Today's S&P 500 list with company names and sectors."""
    df = pd.read_csv(os.environ.get("CURRENT_CSV", CURRENT_URL))
    df["Symbol"] = df["Symbol"].map(to_yahoo)
    return df.set_index("Symbol")[["Security", "GICS Sector"]]


def load_member_history():
    """Point-in-time S&P 500 membership since 1996, one row per change date.
    Returns {date: set of tickers}. Used by the backtest so each month only
    picks from stocks that were actually in the index at that time."""
    df = pd.read_csv(os.environ.get("MEMBERS_CSV", HISTORY_URL), parse_dates=["date"])
    return pd.Series({d: {to_yahoo(t) for t in s.split(",")}
                      for d, s in zip(df["date"], df["tickers"])}).sort_index()


def members_at(history, month):
    """Index members as of the last trading day of `month` (a pandas Period)."""
    return history.loc[:month.to_timestamp(how="end")].iloc[-1]


def load_monthly_prices(tickers, start="1999-01-01"):
    """Month-end dividend-adjusted prices from Yahoo Finance, plus SPY and
    the T-bill yield. Downloads in chunks to stay under Yahoo's rate limits.
    Drops the current month if it isn't finished yet."""
    local = os.environ.get("PRICES_CSV")  # optional: test with a local file
    if local:
        px = pd.read_csv(local, index_col=0)
        px.index = pd.PeriodIndex(px.index, freq="M")
        return px

    import yfinance as yf
    tickers = sorted(set(tickers) | {BENCHMARK, TBILL})
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        daily = yf.download(chunk, start=start, auto_adjust=True,
                            progress=False, threads=True)["Close"]
        if isinstance(daily, pd.Series):
            daily = daily.to_frame(chunk[0])
        frames.append(daily)
        time.sleep(1)
    daily = pd.concat(frames, axis=1).sort_index()
    daily = daily.loc[:, ~daily.columns.duplicated()]

    px = daily.resample("ME").last()
    last_day = daily.index[-1]
    today = pd.Timestamp(date.today())
    if last_day.month == today.month and last_day.year == today.year:
        px = px.iloc[:-1]  # current month still in progress
    px.index = px.index.to_period("M")
    return px.dropna(how="all", axis=1)


# ---------------------------------------------------------------- signal

def momentum_scores(px):
    """12-1 momentum for every stock at every month-end: price 1 month ago
    divided by price 12 months ago, minus 1. A stock needs a price this month
    too, so names that stopped trading drop out."""
    mom = px.shift(SKIP) / px.shift(LOOKBACK) - 1
    return mom.where(px.notna())


def trend_is_up(px):
    """True for each month-end where SPY is at or above its 10-month average."""
    spy = px[BENCHMARK]
    return spy >= spy.rolling(TREND_MONTHS).mean()


def pick_stocks(scores_row, universe, top_n=TOP_N):
    """Top `top_n` stocks in `universe` by momentum score."""
    s = scores_row.reindex(sorted(universe)).dropna()
    return s.sort_values(ascending=False).head(top_n)


# ---------------------------------------------------------------- live log

def update_log(px, picks, trend_up):
    month = str(px.index[-1])
    cols = ["signal_month", "trend", "holdings", "next_month",
            "strategy_return", "spy_return", "excess_return"]
    log = pd.read_csv(LOG_FILE, dtype=str) if os.path.exists(LOG_FILE) else pd.DataFrame(columns=cols)

    if month in set(log["signal_month"]):
        print(f"Signal for {month} already logged - nothing to do.")
        return log

    # Fill in the realized return for last month's picks.
    rets = px.pct_change(fill_method=None).iloc[-1]
    prev_month = str(px.index[-2])
    mask = log["signal_month"] == prev_month
    if mask.any():
        held = log.loc[mask, "holdings"].iloc[0].split()
        if held == ["CASH"]:
            strat = px[TBILL].iloc[-2] / 100 / 12
        else:
            strat = rets.reindex(held).fillna(0).mean()  # missing = sold flat
        spy = rets[BENCHMARK]
        log.loc[mask, ["strategy_return", "spy_return", "excess_return"]] = [
            f"{strat:.4f}", f"{spy:.4f}", f"{strat - spy:.4f}"]

    holdings = " ".join(picks.index) if trend_up else "CASH"
    new = pd.DataFrame([[month, "up" if trend_up else "down", holdings,
                         str(px.index[-1] + 1), "", "", ""]], columns=cols)
    log = pd.concat([log, new], ignore_index=True)
    log.to_csv(LOG_FILE, index=False)
    return log


def write_signal(px, picks, trend_up, info, log):
    month = px.index[-1]
    spy = px[BENCHMARK]
    ma = spy.rolling(TREND_MONTHS).mean().iloc[-1]

    lines = [f"# Stock momentum signal for {(month + 1).strftime('%B %Y')}", "",
             f"Based on prices at the {month.strftime('%B %Y')} close.", "",
             f"**Trend filter:** SPY {spy.iloc[-1]:.2f} vs. 10-month average {ma:.2f} - "
             + ("**up, hold stocks.**" if trend_up else "**down, hold T-bills.**"), ""]

    prev = log["holdings"].iloc[-2].split() if len(log) > 1 else []
    if trend_up:
        now = list(picks.index)
        if prev:
            buys = [t for t in now if t not in prev]
            sells = [t for t in prev if t not in now]
            lines += [f"**Changes from last month:** {len(buys)} buys, {len(sells)} sells"
                      + (f" (sell {', '.join(sells)}; buy {', '.join(buys)})" if buys or sells else ""), ""]
        lines += [f"**Hold (5% each):**", "",
                  "| Rank | Ticker | Company | Sector | 12-1 month return |",
                  "| --- | --- | --- | --- | --- |"]
        for i, (t, r) in enumerate(picks.items(), 1):
            name = info["Security"].get(t, "")
            sector = info["GICS Sector"].get(t, "")
            lines.append(f"| {i} | {t} | {name} | {sector} | {r:+.1%} |")
        sectors = info["GICS Sector"].reindex(picks.index).value_counts()
        lines += ["", "**Sector mix:** " + ", ".join(f"{s} {n}" for s, n in sectors.items())]
    else:
        lines += ["**Hold:** T-bills (cash) until SPY closes back above its 10-month average."]

    done = log[log["strategy_return"].fillna("") != ""]
    if len(done):
        s = (1 + done["strategy_return"].astype(float)).prod() - 1
        b = (1 + done["spy_return"].astype(float)).prod() - 1
        n = len(done)
        lines += ["", f"**Live track record ({n} month{'s' if n != 1 else ''}):** "
                      f"strategy {s:+.1%} vs. SPY {b:+.1%}"]

    lines += ["", "_Educational project, not investment advice._"]
    text = "\n".join(lines) + "\n"
    with open(SIGNAL_FILE, "w") as f:
        f.write(text)
    return text


def main():
    info = load_current_members()
    px = load_monthly_prices(info.index, start=str(date.today().year - 3) + "-01-01")
    if len(px) <= LOOKBACK:
        sys.exit("Not enough price history.")
    scores = momentum_scores(px)
    picks = pick_stocks(scores.iloc[-1], set(info.index) & set(px.columns))
    trend_up = bool(trend_is_up(px).iloc[-1])
    log = update_log(px, picks, trend_up)
    print(write_signal(px, picks, trend_up, info, log))


if __name__ == "__main__":
    main()
