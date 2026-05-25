"""Analyze portfolio equity curve: daily returns, drawdowns, Sharpe, SPY comparison."""
from __future__ import annotations
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent


def _load_history(fname):
    data = json.loads((OUT / fname).read_text())
    ts = data["timestamp"]
    eq = data["equity"]
    base = data.get("base_value")
    return ts, eq, base


def daily_table():
    ts, eq, base = _load_history("portfolio_history_90d.json")
    rows = []
    prev_eq = None
    peak = -1e18
    for t, e in zip(ts, eq):
        d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        if e is None or e == 0:
            continue
        daily_ret = (e / prev_eq - 1.0) if prev_eq else 0.0
        peak = max(peak, e)
        dd = (e / peak - 1.0)
        rows.append({"date": d, "equity_eod": round(e, 2), "daily_ret_pct": round(daily_ret * 100, 3),
                     "peak": round(peak, 2), "dd_from_peak_pct": round(dd * 100, 3)})
        prev_eq = e
    return rows, base


def intraday_dd():
    """Max intraday DD using 1H data."""
    data = json.loads((OUT / "portfolio_history_30d_1h.json").read_text())
    ts, eq = data["timestamp"], data["equity"]
    max_dd = 0.0
    max_dd_from = None
    max_dd_to = None
    peak = -1e18
    peak_t = None
    for t, e in zip(ts, eq):
        if e is None or e == 0:
            continue
        if e > peak:
            peak = e
            peak_t = t
        dd = (e / peak - 1.0) if peak else 0.0
        if dd < max_dd:
            max_dd = dd
            max_dd_from = peak_t
            max_dd_to = t
    if max_dd_from and max_dd_to:
        return {
            "max_intraday_dd_pct": round(max_dd * 100, 3),
            "from": datetime.fromtimestamp(max_dd_from, tz=timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(max_dd_to, tz=timezone.utc).isoformat(),
        }
    return None


def metrics(rows):
    returns = [r["daily_ret_pct"] / 100.0 for r in rows[1:]]
    if not returns:
        return {}
    n = len(returns)
    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / max(1, n - 1)
    vol = math.sqrt(var)
    ann_vol = vol * math.sqrt(252)
    sharpe = (mean / vol) * math.sqrt(252) if vol else 0.0

    equity0 = rows[0]["equity_eod"]
    equityN = rows[-1]["equity_eod"]
    days = n  # approx trading days
    years = days / 252.0
    cagr = (equityN / equity0) ** (1 / years) - 1 if years > 0 and equity0 > 0 else 0.0

    pos_days = sum(1 for r in returns if r > 0)

    peak = -1e18
    max_dd = 0.0
    for r in rows:
        peak = max(peak, r["equity_eod"])
        dd = (r["equity_eod"] / peak - 1.0)
        max_dd = min(max_dd, dd)

    return {
        "days": n,
        "start_equity": equity0,
        "end_equity": equityN,
        "total_return_pct": round((equityN / equity0 - 1) * 100, 3),
        "cagr_ann_pct": round(cagr * 100, 3),
        "vol_ann_pct": round(ann_vol * 100, 3),
        "sharpe_rf0": round(sharpe, 3),
        "max_dd_pct": round(max_dd * 100, 3),
        "pct_positive_days": round(pos_days / n * 100, 2),
        "mean_daily_pct": round(mean * 100, 4),
        "median_daily_pct": round(sorted(returns)[n // 2] * 100, 4),
        "worst_day_pct": round(min(returns) * 100, 3),
        "best_day_pct": round(max(returns) * 100, 3),
    }


def main():
    rows, base = daily_table()
    # Save CSV
    with (OUT / "equity_curve.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    m = metrics(rows)
    (OUT / "equity_metrics.json").write_text(json.dumps(m, indent=2))

    print("## Equity curve — last 30 trading days")
    print("| date | equity_eod | daily_ret_% | peak | dd_from_peak_% |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for r in rows[-30:]:
        print(f"| {r['date']} | {r['equity_eod']:,.2f} | {r['daily_ret_pct']:+.3f} | "
              f"{r['peak']:,.2f} | {r['dd_from_peak_pct']:+.3f} |")

    print("\n## Days with daily_return < -1.5%")
    worst = [r for r in rows if r["daily_ret_pct"] < -1.5]
    for r in worst:
        print(f"  {r['date']}: {r['daily_ret_pct']:+.3f}%  equity={r['equity_eod']:,.2f}")
    if not worst:
        print("  (none in window)")

    print("\n## Metrics (full 90D window)")
    for k, v in m.items():
        print(f"  {k}: {v}")

    idd = intraday_dd()
    if idd:
        print(f"\n## Max intraday DD (30d, 1H): {idd['max_intraday_dd_pct']}%  "
              f"from={idd['from']}  to={idd['to']}")


if __name__ == "__main__":
    main()
