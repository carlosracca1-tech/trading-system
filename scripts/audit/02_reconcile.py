"""Reconcile local DBs (trading_paper.db / mrev_paper.db) vs Alpaca live state."""
from __future__ import annotations
import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent


def _fetch_alpaca_positions():
    data = json.loads((OUT / "positions.json").read_text())
    out = {}
    for p in data:
        out[p["symbol"]] = {
            "qty": float(p["qty"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "unrealized_pl": float(p["unrealized_pl"]),
        }
    return out


def _fetch_rftm_db():
    con = sqlite3.connect(str(ROOT / "trading_paper.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol,status,qty,entry_price,stop_loss,partial_tp_taken,initial_qty,close_reason,opened_at,closed_at "
        "FROM positions WHERE status='open' ORDER BY symbol"
    ).fetchall()
    con.close()
    return {r["symbol"]: dict(r) for r in rows}


def _fetch_mrev_db():
    con = sqlite3.connect(str(ROOT / "mrev_paper.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol,status,qty,entry_price,stop_loss,partial_tp_taken,initial_qty,exit_reason,entry_dt,exit_dt "
        "FROM mrev_positions WHERE status='OPEN' ORDER BY symbol"
    ).fetchall()
    con.close()
    # Map MREV symbols to Alpaca position symbols. Alpaca uses BTCUSD (no slash) for crypto positions.
    out = {}
    for r in rows:
        d = dict(r)
        alpaca_sym = d["symbol"].replace("/", "")  # LINK/USD -> LINKUSD
        out[alpaca_sym] = d
        out[alpaca_sym]["db_symbol"] = d["symbol"]
    return out


def verdict(db_row, ap_row, db_entry, ap_entry, db_qty, ap_qty):
    if db_row is None:
        return "ONLY_IN_ALPACA"
    if ap_row is None:
        return "ONLY_IN_DB"
    if abs(db_qty - ap_qty) > max(1e-6, 0.01 * max(abs(db_qty), abs(ap_qty))):
        return "QTY_DRIFT"
    if db_entry and ap_entry and abs(db_entry - ap_entry) / max(ap_entry, 1e-9) > 0.005:
        return "ENTRY_DRIFT"
    return "IN_SYNC"


def main():
    alpaca = _fetch_alpaca_positions()
    rftm_db = _fetch_rftm_db()
    mrev_db = _fetch_mrev_db()

    ETFS = {"ARGT", "ECH", "EWJ", "FLBR", "GLD", "IWM", "PAVE", "QQQ", "SLV", "SPY", "XLE", "XLK", "ARKK", "XLF", "BITO"}
    CRYPTO_SUFFIX = ("USD",)

    rows = []
    all_syms = set(alpaca) | set(rftm_db) | set(mrev_db)
    for sym in sorted(all_syms):
        is_crypto = sym.endswith("USD") and not sym in ETFS
        # Primary DB candidate is based on universe assignment
        if is_crypto:
            db = mrev_db.get(sym)
            bot = "MREV"
        else:
            db = rftm_db.get(sym)
            bot = "RFTM"
        # BUT also detect crossover: if present in wrong DB, flag
        other_db = rftm_db.get(sym) if is_crypto else mrev_db.get(sym)

        ap = alpaca.get(sym)
        db_qty = float(db["qty"]) if db else 0.0
        ap_qty = float(ap["qty"]) if ap else 0.0
        db_entry = float(db["entry_price"]) if db else None
        ap_entry = float(ap["avg_entry_price"]) if ap else None
        v = verdict(db, ap, db_entry, ap_entry, db_qty, ap_qty)

        diff_qty = db_qty - ap_qty
        diff_pct_entry = None
        if db_entry and ap_entry:
            diff_pct_entry = (db_entry - ap_entry) / ap_entry * 100.0

        extra = []
        if other_db:
            extra.append(f"ALSO_IN_{'RFTM' if is_crypto else 'MREV'}_DB")
        if db and db.get("close_reason") == "migrated_to_mrev":
            extra.append("LEGACY_MIGRATED")

        rows.append({
            "symbol": sym,
            "bot": bot,
            "db_qty": round(db_qty, 6),
            "alpaca_qty": round(ap_qty, 6),
            "db_entry": round(db_entry, 4) if db_entry else "",
            "alpaca_avg_entry": round(ap_entry, 4) if ap_entry else "",
            "diff_qty": round(diff_qty, 6),
            "diff_pct_entry": round(diff_pct_entry, 3) if diff_pct_entry is not None else "",
            "verdict": v,
            "notes": ";".join(extra) if extra else "",
        })

    out = OUT / "reconcile_db_vs_alpaca.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Print as markdown table
    print(f"# Reconciliation — {len(rows)} symbols")
    hdrs = list(rows[0].keys())
    print("| " + " | ".join(hdrs) + " |")
    print("| " + " | ".join("---" for _ in hdrs) + " |")
    for r in rows:
        print("| " + " | ".join(str(r[k]) for k in hdrs) + " |")

    # Summary by verdict
    from collections import Counter
    c = Counter(r["verdict"] for r in rows)
    print("\n## Verdict distribution")
    for k, v in c.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
