"""
_kaizen_monthly_metrics — F6.5: snapshot mensual de métricas por regla.

Tabla `kaizen_monthly_metrics`:
- month TEXT (YYYY-MM)
- rule_id TEXT
- n_blocks INTEGER             — cuántas entradas bloqueó la regla ese mes
- n_shadows_closed INTEGER
- n_shadows_winners INTEGER
- n_shadows_losers INTEGER
- gross_saved_usd REAL
- gross_missed_usd REAL
- net_impact_usd REAL
- shadow_win_rate REAL
- shadow_expectancy REAL
- snapshot_dt TEXT             — cuándo se capturó este mes
- PRIMARY KEY (month, rule_id)

Cada run del workflow mensual:
1. Lee shadows CERRADOS durante el mes actual.
2. Agrega por rule_id.
3. UPSERT del row del mes.

F6.2 (email mensual) consume esta tabla para mostrar tendencias.
F6.3 (badge "REQUIERE DECISIÓN") usa la serie de los últimos 2 meses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS kaizen_monthly_metrics (
    month TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    n_blocks INTEGER DEFAULT 0,
    n_shadows_closed INTEGER DEFAULT 0,
    n_shadows_winners INTEGER DEFAULT 0,
    n_shadows_losers INTEGER DEFAULT 0,
    gross_saved_usd REAL DEFAULT 0,
    gross_missed_usd REAL DEFAULT 0,
    net_impact_usd REAL DEFAULT 0,
    shadow_win_rate REAL DEFAULT 0,
    shadow_expectancy REAL DEFAULT 0,
    snapshot_dt TEXT,
    PRIMARY KEY (month, rule_id)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SQL)
    conn.commit()


def current_month_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m")


def compute_month_aggregate(
    conn: sqlite3.Connection, month: Optional[str] = None,
) -> list[dict]:
    """Agrega shadows CERRADOS durante `month` por rule_id.

    `month` formato "YYYY-MM" (default: mes actual).
    """
    month = month or current_month_str()
    # Range: primer día del mes a primer día del siguiente
    rows = conn.execute(
        """SELECT rule_id, pnl_simulated
           FROM kaizen_shadow_trades
           WHERE status='closed'
             AND pnl_simulated IS NOT NULL
             AND substr(exit_dt_utc, 1, 7) = ?""",
        (month,),
    ).fetchall()
    by_rule: dict = {}
    for r in rows:
        rid = r["rule_id"]
        d = by_rule.setdefault(rid, {
            "rule_id": rid,
            "n_shadows_closed": 0,
            "n_shadows_winners": 0,
            "n_shadows_losers": 0,
            "gross_saved_usd": 0.0,
            "gross_missed_usd": 0.0,
        })
        pnl = float(r["pnl_simulated"])
        d["n_shadows_closed"] += 1
        if pnl > 0:
            d["n_shadows_winners"] += 1
            d["gross_missed_usd"] += pnl
        elif pnl < 0:
            d["n_shadows_losers"] += 1
            d["gross_saved_usd"] += -pnl

    out = []
    for d in by_rule.values():
        n = d["n_shadows_closed"]
        d["shadow_win_rate"] = (d["n_shadows_winners"] / n) if n else 0
        d["net_impact_usd"] = round(
            d["gross_saved_usd"] - d["gross_missed_usd"], 2
        )
        d["shadow_expectancy"] = round(d["net_impact_usd"] / n, 2) if n else 0
        d["gross_saved_usd"] = round(d["gross_saved_usd"], 2)
        d["gross_missed_usd"] = round(d["gross_missed_usd"], 2)
        out.append(d)
    return out


def snapshot_monthly_metrics(
    conn: sqlite3.Connection, month: Optional[str] = None,
    n_blocks_by_rule: Optional[dict[str, int]] = None,
) -> int:
    """Persiste el snapshot del mes. Devuelve cuántas filas escribió.

    `n_blocks_by_rule`: opcional dict con conteo de bloqueos ese mes
    (no se infiere de los shadows porque puede haber bloqueos cuyo
    shadow aún no cerró).
    """
    ensure_table(conn)
    month = month or current_month_str()
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    agg = compute_month_aggregate(conn, month)
    written = 0
    for a in agg:
        n_blocks = (n_blocks_by_rule or {}).get(a["rule_id"], 0)
        conn.execute(
            """INSERT OR REPLACE INTO kaizen_monthly_metrics
               (month, rule_id, n_blocks, n_shadows_closed,
                n_shadows_winners, n_shadows_losers,
                gross_saved_usd, gross_missed_usd, net_impact_usd,
                shadow_win_rate, shadow_expectancy, snapshot_dt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (month, a["rule_id"], n_blocks, a["n_shadows_closed"],
             a["n_shadows_winners"], a["n_shadows_losers"],
             a["gross_saved_usd"], a["gross_missed_usd"],
             a["net_impact_usd"], a["shadow_win_rate"],
             a["shadow_expectancy"], now),
        )
        written += 1
    conn.commit()
    return written


def get_rule_history(
    conn: sqlite3.Connection, rule_id: str, n_months: int = 3,
) -> list[dict]:
    """Últimos N meses de métricas para una regla."""
    ensure_table(conn)
    rows = conn.execute(
        """SELECT * FROM kaizen_monthly_metrics
           WHERE rule_id=? ORDER BY month DESC LIMIT ?""",
        (rule_id, n_months),
    ).fetchall()
    return [dict(r) for r in rows]


def rules_needing_decision(
    conn: sqlite3.Connection, n_blocks_min: int = 10,
) -> list[str]:
    """F6.3: reglas con net_impact_usd negativo durante 2+ meses
    consecutivos y al menos `n_blocks_min` bloqueos.

    Devuelve lista de rule_ids — la UI/email marca estos como
    "REQUIERE TU DECISIÓN".
    """
    ensure_table(conn)
    # Trae los 2 meses más recientes
    rows = conn.execute(
        """SELECT rule_id, month, net_impact_usd, n_blocks
           FROM kaizen_monthly_metrics
           ORDER BY month DESC"""
    ).fetchall()
    by_rule: dict = {}
    for r in rows:
        by_rule.setdefault(r["rule_id"], []).append(dict(r))
    out = []
    for rid, hist in by_rule.items():
        if len(hist) < 2:
            continue
        last2 = hist[:2]
        if all(h["net_impact_usd"] < 0 for h in last2) and \
           all(h["n_blocks"] >= n_blocks_min for h in last2):
            out.append(rid)
    return out
