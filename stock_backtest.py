"""
Backtest of the individual stock momentum strategy, Jan 2001 to the latest month-end.

Each month it only picks from stocks that were in the S&P 500 at that time
(point-in-time membership), not today's list, to limit survivorship bias.

Outputs:
  stock_backtest_results.md  - performance vs. SPY and the sector strategy,
                               year-by-year returns, and a robustness check
  stock_backtest_chart.png   - growth of $10,000 and drawdowns
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from stock_momentum import (TOP_N, BENCHMARK, TBILL, load_member_history, members_at,
                            load_monthly_prices, momentum_scores, trend_is_up, pick_stocks)

COST_BPS = 10        # trading cost per $ traded
RISK_FREE = 0.02     # assumed average T-bill yield, for the Sharpe ratio
START = "2001-01"    # membership data is most complete from 2001 on

# For comparison: the sector momentum strategy from
# github.com/dajunk06-del/sector-momentum-strategy (top 3 of 9 sector ETFs by 12-month return).
SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]


def run_sector(px, lookback=12, top_n=3, cost_bps=COST_BPS):
    rets = px[SECTORS].pct_change()
    mom = px[SECTORS] / px[SECTORS].shift(lookback) - 1
    rank = mom.rank(axis=1, ascending=False, method="first")
    held = ((rank <= top_n).astype(float) / top_n).shift(1)
    gross = (held * rets).sum(axis=1)
    turnover = (held - held.shift(1).fillna(0)).abs().sum(axis=1)
    return gross - turnover * cost_bps / 10_000


def stats(r):
    growth = (1 + r).cumprod()
    years = len(r) / 12
    vol = r.std() * np.sqrt(12)
    return {
        "Annual return (CAGR)": f"{growth.iloc[-1] ** (1 / years) - 1:.1%}",
        "Volatility": f"{vol:.1%}",
        "Sharpe ratio": f"{(r.mean() * 12 - RISK_FREE) / vol:.2f}",
        "Max drawdown": f"{(growth / growth.cummax() - 1).min():.1%}",
        "$10,000 grows to": f"${10_000 * growth.iloc[-1]:,.0f}",
    }


def run(px, history, top_n=TOP_N, trend_filter=True, cost_bps=COST_BPS):
    """Monthly returns of the strategy. Picks made at month-end t earn month t+1."""
    rets = px.pct_change(fill_method=None)
    scores = momentum_scores(px)
    trend = trend_is_up(px)
    cash_rate = px[TBILL] / 100 / 12          # T-bill yield -> monthly return

    months = px.loc[START:].index
    out, coverage = {}, []
    prev_w = pd.Series(dtype=float)
    for t, nxt in zip(months[:-1], months[1:]):
        universe = members_at(history, t)
        coverage.append(scores.loc[t].reindex(sorted(universe)).notna().mean())

        if trend_filter and not trend[t]:
            w = pd.Series(dtype=float)        # all in T-bills
        else:
            picks = pick_stocks(scores.loc[t], universe & set(px.columns), top_n)
            w = pd.Series(1 / len(picks), index=picks.index)

        cash = 1 - w.sum()
        # A stock with no price next month (delisted, bought out) is treated
        # as sold flat at its last price.
        gross = (w * rets.loc[nxt].reindex(w.index).fillna(0)).sum() + cash * cash_rate[t]
        turnover = w.sub(prev_w, fill_value=0).abs().sum()
        out[nxt] = gross - turnover * cost_bps / 10_000
        prev_w = w

    return pd.Series(out), float(np.mean(coverage))


def yearly(series):
    df = pd.DataFrame(series)
    return df.groupby(df.index.year).apply(lambda g: (1 + g).prod() - 1)


def main():
    history = load_member_history()
    every_member = set().union(*history.loc[pd.Timestamp("1999-12-31"):])
    px = load_monthly_prices(every_member | set(SECTORS), start="1999-01-01")

    stock, coverage = run(px, history)
    no_filter, _ = run(px, history, trend_filter=False)
    idx = stock.index
    sector = run_sector(px).reindex(idx)
    spy = px[BENCHMARK].pct_change().reindex(idx)

    series = {"Stock momentum + trend filter": stock,
              "Stock momentum, no filter": no_filter,
              "Sector momentum": sector,
              "S&P 500 (SPY)": spy}

    table = pd.DataFrame({k: stats(v.dropna()) for k, v in series.items()})
    period = f"{idx[0].strftime('%b %Y')} - {idx[-1].strftime('%b %Y')}"

    years = yearly(pd.DataFrame(series)).map(lambda x: f"{x:+.1%}")

    robust = {}
    for n in (10, 20, 30, 50):
        for filt in (True, False):
            r, _ = run(px, history, top_n=n, trend_filter=filt)
            s = stats(r)
            robust[(f"Top {n}", "Filter on" if filt else "Filter off")] = {
                k: s[k] for k in ("Annual return (CAGR)", "Sharpe ratio", "Max drawdown")}
    robust = pd.DataFrame(robust).T
    robust.index = [f"{a}, {b.lower()}" for a, b in robust.index]

    in_cash = (~trend_is_up(px)).loc[START:].mean()

    with open("stock_backtest_results.md", "w") as f:
        f.write(f"## Stock momentum backtest ({period})\n\n")
        f.write(table.to_markdown() + "\n\n")
        f.write(f"Trend filter had the strategy in T-bills {in_cash:.0%} of months. "
                f"Price data was found for {coverage:.0%} of index members on average "
                f"(the rest are mostly delisted companies Yahoo no longer carries).\n\n")
        f.write("### Robustness: number of stocks held\n\n")
        f.write(robust.to_markdown() + "\n\n")
        f.write("### Year by year\n\n")
        f.write(years.to_markdown() + "\n")
    print(period)
    print(table)
    print(robust)
    print(f"coverage {coverage:.1%}, months in cash {in_cash:.1%}")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    colors = {"Stock momentum + trend filter": "#1F3864",
              "Stock momentum, no filter": "#8FAADC",
              "Sector momentum": "#ED7D31",
              "S&P 500 (SPY)": "#A5A5A5"}
    for name, r in series.items():
        g = 10_000 * (1 + r.fillna(0)).cumprod()
        ts = g.index.to_timestamp()
        ax1.plot(ts, g, label=name, color=colors[name], lw=1.8)
        if name in ("Stock momentum + trend filter", "Stock momentum, no filter", "S&P 500 (SPY)"):
            ax2.plot(ts, g / g.cummax() - 1, color=colors[name], lw=1.4)
    ax1.set_title("Stock momentum strategy: growth of $10,000 (log scale)")
    ax1.set_yscale("log")
    ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    ax1.legend(frameon=False)
    ax2.set_title("Drawdown from prior peak")
    ax2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    for ax in (ax1, ax2):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig("stock_backtest_chart.png", dpi=120)


if __name__ == "__main__":
    main()
