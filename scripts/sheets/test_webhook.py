#!/usr/bin/env python3
"""
test_webhook.py — manda 3 trades ficticios al webhook para verificar
que la Sheet está bien configurada.

Después de correr esto, abrí tu Google Sheet y deberías ver:
- En la pestaña RFTM: 2 filas nuevas (1 BUY + 1 SELL_TP1)
- En la pestaña MREV: 1 fila nueva (1 BUY)

Si el script termina con "OK 3/3" pero no ves filas, la URL apunta a
otra hoja o el Apps Script tiene un error.

Uso:
    export SHEETS_WEBHOOK_URL='https://script.google.com/macros/s/..../exec'
    python3 scripts/sheets/test_webhook.py

Limpieza: cuando confirmes que anduvo, borrá las 3 filas a mano de la Sheet.
Tienen marcador "TEST-" en el trade_id para identificarlas.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from _sheets_logger import log_trade_event  # noqa: E402


def main() -> int:
    webhook = os.environ.get("SHEETS_WEBHOOK_URL", "").strip()
    if not webhook:
        print("ERROR: exportá SHEETS_WEBHOOK_URL primero")
        print("  export SHEETS_WEBHOOK_URL='https://script.google.com/macros/s/..../exec'")
        return 1

    print(f"Webhook: {webhook[:60]}...")
    print()

    test_id = uuid.uuid4().hex[:8]
    rftm_trade = f"RFTM-TEST{test_id}"
    mrev_trade = f"MREV-TEST{test_id}"

    events = [
        {
            "bot": "RFTM",
            "symbol": "TEST.SPY",
            "side": "BUY",
            "qty": 10,
            "price": 600.0,
            "trade_id": rftm_trade,
            "event_id": f"{rftm_trade}-BUY",
            "stage": 0,
            "running_qty": 10,
            "initial_qty": 10,
            "entry_price": 600.0,
            "reason": "test_buy",
            "broker_order_id": f"test-order-{test_id}-buy",
        },
        {
            "bot": "RFTM",
            "symbol": "TEST.SPY",
            "side": "SELL_TP1",
            "qty": 5,
            "price": 630.0,
            "trade_id": rftm_trade,
            "event_id": f"{rftm_trade}-SELL_TP1",
            "stage": 1,
            "running_qty": 5,
            "initial_qty": 10,
            "entry_price": 600.0,
            "realized_pnl_event": (630.0 - 600.0) * 5,
            "reason": "test_partial_tp1",
            "broker_order_id": f"test-order-{test_id}-tp1",
        },
        {
            "bot": "MREV",
            "symbol": "TEST.BTC/USD",
            "side": "BUY",
            "qty": 0.05,
            "price": 80000.0,
            "trade_id": mrev_trade,
            "event_id": f"{mrev_trade}-BUY",
            "stage": 0,
            "running_qty": 0.05,
            "initial_qty": 0.05,
            "entry_price": 80000.0,
            "reason": "test_buy_crypto",
            "broker_order_id": f"test-order-{test_id}-mrev",
        },
    ]

    ok_count = 0
    for i, e in enumerate(events, 1):
        result = log_trade_event(**e)
        status = "OK" if result else "FAIL"
        print(f"  [{i}/{len(events)}] {status:4s}  {e['bot']:4s} {e['symbol']:14s} "
              f"{e['side']:10s} qty={e['qty']:8.4f}")
        if result:
            ok_count += 1

    print()
    print(f"Total: {ok_count}/{len(events)} eventos enviados con éxito")
    print()
    if ok_count == len(events):
        print("✓ Andá a tu Google Sheet y deberías ver:")
        print(f"  - Pestaña RFTM: 2 filas nuevas con trade_id = {rftm_trade}")
        print(f"  - Pestaña MREV: 1 fila nueva con trade_id = {mrev_trade}")
        print()
        print("Si las ves: TODO BIEN. Borralas a mano cuando quieras.")
        print("Si no las ves: la URL apunta a otra hoja o el Apps Script tiene un bug.")
        return 0
    else:
        print("✗ Algún POST falló. Posibles causas:")
        print("  - La URL es incorrecta (¿copiaste bien?)")
        print("  - El deployment del Apps Script no está como 'Anyone'")
        print("  - El Apps Script tiene un error de sintaxis (chequeá el editor)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
