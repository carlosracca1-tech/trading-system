#!/usr/bin/env python3
"""
test_sheets.py — validar que el setup de Service Account está bien.

Pre-requisitos:
  pip install gspread google-auth

  export SHEETS_SPREADSHEET_ID='1abc...'        # ID del Sheet (de la URL)
  export SHEETS_SERVICE_ACCOUNT_JSON="$(cat ~/Downloads/service-account.json)"
  export SHEETS_DEBUG=1

Si todo está OK, manda 3 eventos ficticios a las pestañas RFTM y MREV.
Después borralas a mano.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def main() -> int:
    sid = os.environ.get("SHEETS_SPREADSHEET_ID", "").strip()
    sa = os.environ.get("SHEETS_SERVICE_ACCOUNT_JSON", "").strip()

    print(f"SHEETS_SPREADSHEET_ID: {sid[:8]}... ({len(sid)} chars)")
    print(f"SHEETS_SERVICE_ACCOUNT_JSON: {'set' if sa else 'MISSING'} ({len(sa)} chars)")

    if not sid:
        print("ERROR: exportá SHEETS_SPREADSHEET_ID con el ID del Sheet (de la URL)")
        return 1
    if not sa:
        print("ERROR: exportá SHEETS_SERVICE_ACCOUNT_JSON con el contenido del JSON")
        return 1

    try:
        import gspread  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
    except ImportError:
        print("ERROR: faltan paquetes. Correr:")
        print("  pip3 install gspread google-auth")
        return 1

    from _sheets_logger import log_trade_event, SHEETS_LOG_ENABLED, _get_client

    if not SHEETS_LOG_ENABLED:
        print("ERROR: SHEETS_LOG_ENABLED es False — chequear env vars")
        return 1

    # Verificar autenticación primero
    print()
    print("1) Autenticando con Service Account...")
    client = _get_client()
    if client is None:
        print("   ✗ FAIL — no se pudo autenticar")
        print("   Causas típicas: JSON malformado, service account deshabilitado")
        return 2
    print("   ✓ OK")

    # Verificar acceso al Sheet
    print()
    print("2) Abriendo Sheet...")
    try:
        sh = client.open_by_key(sid)
        print(f"   ✓ OK — Sheet: {sh.title}")
    except Exception as e:
        print(f"   ✗ FAIL — {type(e).__name__}: {e}")
        info = __import__("json").loads(sa)
        print()
        print(f"   ¿Compartiste el Sheet con esta cuenta?")
        print(f"     {info.get('client_email', '<no email>')}")
        print(f"   Debe estar como Editor.")
        return 2

    # Mandar 3 eventos de prueba
    print()
    print("3) Enviando 3 eventos de prueba...")
    test_id = uuid.uuid4().hex[:8]
    rftm_trade = f"RFTM-TEST{test_id}"
    mrev_trade = f"MREV-TEST{test_id}"

    events = [
        dict(bot="RFTM", symbol="TEST.SPY", side="BUY", qty=10, price=600.0,
             trade_id=rftm_trade, event_id=f"{rftm_trade}-BUY",
             stage=0, running_qty=10, initial_qty=10, entry_price=600.0,
             reason="test_buy", broker_order_id=f"test-{test_id}-buy"),
        dict(bot="RFTM", symbol="TEST.SPY", side="SELL_TP1", qty=5, price=630.0,
             trade_id=rftm_trade, event_id=f"{rftm_trade}-SELL_TP1",
             stage=1, running_qty=5, initial_qty=10, entry_price=600.0,
             realized_pnl_event=(630.0 - 600.0) * 5,
             reason="test_partial_tp1", broker_order_id=f"test-{test_id}-tp1"),
        dict(bot="MREV", symbol="TEST.BTC/USD", side="BUY", qty=0.05, price=80000.0,
             trade_id=mrev_trade, event_id=f"{mrev_trade}-BUY",
             stage=0, running_qty=0.05, initial_qty=0.05, entry_price=80000.0,
             reason="test_buy_crypto", broker_order_id=f"test-{test_id}-mrev"),
    ]

    ok = 0
    for i, e in enumerate(events, 1):
        result = log_trade_event(**e)
        status = "OK" if result else "FAIL"
        print(f"   [{i}/3] {status:4s} {e['bot']:4s} {e['symbol']:14s} "
              f"{e['side']:10s} qty={e['qty']}")
        if result:
            ok += 1

    print()
    if ok == 3:
        print(f"✓ TODO OK — 3/3 eventos escritos en el Sheet.")
        print(f"  Andá a tu Sheet y deberías ver:")
        print(f"  - Pestaña RFTM: 2 filas con trade_id = {rftm_trade}")
        print(f"  - Pestaña MREV: 1 fila con trade_id = {mrev_trade}")
        print(f"  Después de verificar, borralas a mano.")
        return 0
    else:
        print(f"✗ {ok}/3 OK — revisar errores arriba.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
