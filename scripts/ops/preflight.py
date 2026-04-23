#!/usr/bin/env python3
"""
preflight.py — chequeo pre-arranque.

Correr antes de un deploy / antes de re-habilitar cron / después de tocar
migraciones. Valida:

1. DBs locales existen y PRAGMA integrity_check=ok.
2. Schema de las tablas críticas matchea lo que el código espera.
3. .env.paper contiene los keys requeridos.
4. Alpaca API responde y buying_power > 0.
5. No hay posiciones huérfanas (Alpaca↔DB).
6. YAML de workflows parsea sin error sintáctico.

Exit 0 si todo OK. Exit 1 con reporte de qué falló.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from _db_health import (  # noqa: E402
    DBHealthError,
    MREV_REQUIRED_COLUMNS,
    RFTM_REQUIRED_COLUMNS,
    assert_db_health,
)


def _ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m  {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31mfail\033[0m  {msg}")


def check_dbs(failures: list) -> None:
    print("\n== DB health ==")
    mrev_db = REPO / "mrev_paper.db"
    rftm_db = REPO / "trading_paper.db"

    for path, cols, tbl, val in [
        (mrev_db, MREV_REQUIRED_COLUMNS, "mrev_runs", "RUNNING"),
        (rftm_db, RFTM_REQUIRED_COLUMNS, "runs", "running"),
    ]:
        if not path.exists():
            _ok(f"{path.name}: missing (ok — will init on first run)")
            continue
        try:
            report = assert_db_health(
                db_path=str(path),
                required_columns=cols,
                open_run_table=tbl,
                open_run_value=val,
            )
            _ok(f"{path.name}: integrity ok, {len(report['tables'])} tables validated")
        except DBHealthError as e:
            _fail(f"{path.name}: {e}")
            failures.append(f"DB {path.name}")


def check_env(failures: list) -> None:
    print("\n== .env.paper ==")
    env = REPO / ".env.paper"
    if not env.exists():
        _fail(".env.paper missing")
        failures.append("env")
        return
    content = env.read_text()
    required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]
    missing = [k for k in required if f"{k}=" not in content]
    if missing:
        _fail(f"missing keys: {', '.join(missing)}")
        failures.append("env-keys")
    else:
        _ok(f".env.paper contains {len(required)} required keys")


def check_alpaca(failures: list) -> None:
    print("\n== Alpaca API ==")
    # Parse .env.paper manualmente (no queremos depender de python-dotenv)
    env_path = REPO / ".env.paper"
    if not env_path.exists():
        _fail("no .env.paper — skipping Alpaca check")
        failures.append("alpaca")
        return
    env = {}
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    key = env.get("ALPACA_API_KEY", "")
    secret = env.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        _fail("ALPACA keys empty in .env.paper")
        failures.append("alpaca-keys")
        return

    req = urllib.request.Request(
        "https://paper-api.alpaca.markets/v2/account",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            acct = json.loads(r.read())
        bp = float(acct.get("buying_power", 0))
        status = acct.get("status", "")
        if status != "ACTIVE":
            _fail(f"account status={status} (expected ACTIVE)")
            failures.append("alpaca-status")
        elif bp <= 0:
            _fail(f"buying_power=${bp}")
            failures.append("alpaca-bp")
        else:
            _ok(f"account ACTIVE, buying_power=${bp:,.2f}")
    except urllib.error.HTTPError as e:
        _fail(f"Alpaca HTTP {e.code}")
        failures.append("alpaca-http")
    except Exception as e:
        _fail(f"Alpaca request failed: {e}")
        failures.append("alpaca-net")


def check_orphans(failures: list) -> None:
    print("\n== Orphan positions ==")
    # Esta check requiere Alpaca live. Lo hacemos light: listamos ambos lados
    # y reportamos diferencias sin forzar fix.
    env_path = REPO / ".env.paper"
    if not env_path.exists():
        _ok("skipped (no .env.paper)")
        return
    env = {}
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key = env.get("ALPACA_API_KEY", "")
    secret = env.get("ALPACA_SECRET_KEY", "")
    if not key:
        _ok("skipped (no Alpaca keys)")
        return

    req = urllib.request.Request(
        "https://paper-api.alpaca.markets/v2/positions",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            alpaca_positions = json.loads(r.read())
    except Exception as e:
        _fail(f"Alpaca positions fetch failed: {e}")
        failures.append("orphans-net")
        return

    alpaca_syms = {p["symbol"] for p in alpaca_positions}

    # MREV DB
    mrev_syms_db: set[str] = set()
    mrev_db = REPO / "mrev_paper.db"
    if mrev_db.exists():
        c = sqlite3.connect(str(mrev_db))
        rows = c.execute("SELECT symbol FROM mrev_positions WHERE status='OPEN'").fetchall()
        mrev_syms_db = {r[0].replace("/", "") for r in rows}
        c.close()

    # RFTM DB
    rftm_syms_db: set[str] = set()
    rftm_db = REPO / "trading_paper.db"
    if rftm_db.exists():
        c = sqlite3.connect(str(rftm_db))
        rows = c.execute("SELECT symbol FROM positions WHERE status='open'").fetchall()
        rftm_syms_db = {r[0] for r in rows}
        c.close()

    combined_db = mrev_syms_db | rftm_syms_db
    only_alpaca = alpaca_syms - combined_db
    only_db = combined_db - alpaca_syms
    if only_alpaca:
        _fail(f"in Alpaca but not in any DB: {sorted(only_alpaca)}")
        failures.append("orphan-alpaca")
    if only_db:
        _fail(f"in DB but not in Alpaca: {sorted(only_db)}")
        failures.append("orphan-db")
    if not only_alpaca and not only_db:
        _ok(f"DB↔Alpaca consistent ({len(alpaca_syms)} positions)")


def check_workflows(failures: list) -> None:
    print("\n== Workflows YAML ==")
    try:
        import yaml  # type: ignore
    except ImportError:
        _ok("PyYAML no instalado; skipping")
        return

    wf_dir = REPO / ".github" / "workflows"
    for yml in sorted(wf_dir.glob("*.yml")):
        try:
            yaml.safe_load(yml.read_text())
            _ok(f"{yml.name}: parses")
        except Exception as e:
            _fail(f"{yml.name}: {e}")
            failures.append(f"yml-{yml.name}")


def main() -> int:
    print("preflight — checks pre-arranque")
    failures: list[str] = []
    check_dbs(failures)
    check_env(failures)
    check_alpaca(failures)
    check_orphans(failures)
    check_workflows(failures)

    print()
    if failures:
        print(f"\033[31mFAILED\033[0m: {len(failures)} check(s) — {failures}")
        return 1
    print("\033[32mALL OK\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
