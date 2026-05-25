#!/usr/bin/env python3
"""
restore_highest_since_entry.py — one-shot recovery del campo
`highest_since_entry` que quedó reseteado a `entry_price` en posiciones
abiertas con `partial_tp_taken >= 1`.

Contexto del bug:
- Cuando `seed_missing_positions.py`, `mark_partial_tp_done.py`, o la
  sección 3 de `sync_with_alpaca` re-insertan una posición que ya había
  disparado TP1/TP2, dejan `highest_since_entry = entry_price`.
- Esto rompe el trailing stop del watchdog (`check_exit` calcula
  `profit_atr = (high - entry) / atr`, da 0, el trailing nunca se activa).
- La posición termina vendiendo en `E3_stop_loss` cuando el precio
  toca el `stop_loss` (= entry post-TP1 = breakeven), generando
  "pérdidas diminutas" por slippage en vez de defender el runner.

Lo que sabemos *con certeza* sobre el `highest_since_entry` real:
- Si `partial_tp_taken >= 1` → el high tuvo que llegar al menos a
  `entry × (1 + PARTIAL_TP1_PCT)` (default +5%) para que TP1 dispare.
- Si `partial_tp_taken >= 2` → el high tuvo que llegar al menos a
  `entry × (1 + PARTIAL_TP2_PCT)` (default +7.5%) para que TP2 dispare.

Este script bumpea el `highest_since_entry` al floor que sabemos que
ocurrió. Es CONSERVADOR — usa el threshold del TP exacto, sin margen.
Eso reactiva el trailing stop sin "inventar" un high que no podemos
probar.

Uso:
    python3 scripts/ops/restore_highest_since_entry.py            # dry-run
    python3 scripts/ops/restore_highest_since_entry.py --apply    # ejecuta

Idempotente. Se puede correr las veces que quieras. No baja el high
nunca — solo lo sube al floor stage-aware.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Mismos defaults que CLAUDE.md y los bots
PARTIAL_TP1_PCT = float(os.environ.get("PARTIAL_TP1_PCT", "0.05"))
PARTIAL_TP2_PCT = float(os.environ.get("PARTIAL_TP2_PCT", "0.075"))

RFTM_DB = Path(os.environ.get(
    "RFTM_DB_PATH", str(REPO_ROOT / "trading_paper.db")))
MREV_DB = Path(os.environ.get(
    "MREV_DB_PATH", str(REPO_ROOT / "mrev_paper.db")))


def floor_for_stage(entry: float, stage: int) -> float:
    """Devuelve el high mínimo certero según el stage.

    stage 0 → entry (no se garantiza nada por encima)
    stage 1 → entry × (1 + TP1_PCT)
    stage 2 → entry × (1 + TP2_PCT)
    """
    if stage >= 2:
        return entry * (1.0 + PARTIAL_TP2_PCT)
    if stage == 1:
        return entry * (1.0 + PARTIAL_TP1_PCT)
    return entry


def process_table(db_path: Path, table: str, dry: bool) -> dict:
    """Procesa una tabla de positions y bumpea highest_since_entry.

    Devuelve dict con stats: total, bumped, lista de tuplas (sym, before, after).
    """
    stats = {"total": 0, "bumped": 0, "changes": []}
    if not db_path.exists():
        print(f"  [{db_path.name}] no existe — skip")
        return stats

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Detectar si la tabla existe — el script es seguro en DBs nuevas
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        print(f"  [{db_path.name}] tabla {table} no existe — skip")
        conn.close()
        return stats

    required = {"symbol", "entry_price", "highest_since_entry",
                "partial_tp_taken", "status", "id"}
    missing = required - set(cols)
    if missing:
        print(f"  [{db_path.name}.{table}] columnas faltantes {missing} — skip")
        conn.close()
        return stats

    status_open = "open" if table == "positions" else "OPEN"
    rows = conn.execute(
        f"""SELECT id, symbol, entry_price, highest_since_entry,
                   partial_tp_taken
            FROM {table}
            WHERE status = ?""",
        (status_open,),
    ).fetchall()

    for r in rows:
        stats["total"] += 1
        sym = r["symbol"]
        entry = float(r["entry_price"] or 0)
        try:
            stage = int(r["partial_tp_taken"] or 0)
        except (TypeError, ValueError):
            stage = 0
        cur_high = float(r["highest_since_entry"] or 0)

        if stage <= 0 or entry <= 0:
            continue

        floor = floor_for_stage(entry, stage)

        # Invariante: solo subimos el high.
        if cur_high >= floor:
            continue

        new_high = floor
        stats["bumped"] += 1
        stats["changes"].append((sym, stage, cur_high, new_high))
        print(f"  [{db_path.name}] {sym:<10} stage={stage} "
              f"high {cur_high:.4f} → {new_high:.4f} "
              f"(entry={entry:.4f}, +{(floor/entry-1)*100:.2f}% floor)")
        if not dry:
            conn.execute(
                f"UPDATE {table} SET highest_since_entry=? WHERE id=?",
                (new_high, r["id"]),
            )

    if not dry:
        conn.commit()
    conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Aplicar cambios. Sin esto, solo dry-run.")
    args = ap.parse_args()

    dry = not args.apply

    print(f"restore_highest_since_entry.py — {'DRY-RUN' if dry else 'APPLY'}")
    print(f"  RFTM_DB={RFTM_DB}")
    print(f"  MREV_DB={MREV_DB}")
    print(f"  TP1_PCT={PARTIAL_TP1_PCT*100:.2f}%  TP2_PCT={PARTIAL_TP2_PCT*100:.2f}%")
    print()

    print("== RFTM (positions) ==")
    rftm = process_table(RFTM_DB, "positions", dry)
    print()
    print("== MREV (mrev_positions) ==")
    mrev = process_table(MREV_DB, "mrev_positions", dry)
    print()

    total = rftm["total"] + mrev["total"]
    bumped = rftm["bumped"] + mrev["bumped"]
    print(f"Resumen: {bumped}/{total} posiciones con high reconstruido"
          f"{' (dry-run, no escribió DB)' if dry else ''}")
    if dry and bumped > 0:
        print()
        print("→ Para aplicar: python3 scripts/ops/restore_highest_since_entry.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
