"""Tests para _bracket_orders — F3.1.

Verifica:
- bracket_orders_enabled() respeta la env var con varios valores truthy.
- calc_safety_stop_price() delega a recalc_stop_for_stage correctamente.
- submit_safety_stop pasa el body correcto al submit_fn.
- cancel_safety_stop maneja None/empty/404 sin romper.
- replace_safety_stop hace cancel + submit en ese orden.

Stdlib only.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _bracket_orders import (  # noqa: E402
    SafetyStopRequest,
    bracket_orders_enabled,
    calc_safety_stop_price,
    cancel_safety_stop,
    replace_safety_stop,
    submit_safety_stop,
)


class FeatureFlagTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("RFTM_BRACKET_ORDERS_ENABLED", None)

    def test_default_off(self):
        self.assertFalse(bracket_orders_enabled())

    def test_truthy_values(self):
        for val in ["1", "true", "True", "TRUE", "yes", "YES"]:
            os.environ["RFTM_BRACKET_ORDERS_ENABLED"] = val
            self.assertTrue(bracket_orders_enabled(), f"failed for {val!r}")

    def test_falsy_values(self):
        for val in ["0", "false", "no", "", "anything"]:
            os.environ["RFTM_BRACKET_ORDERS_ENABLED"] = val
            self.assertFalse(bracket_orders_enabled(), f"failed for {val!r}")


class CalcSafetyStopTests(unittest.TestCase):
    def test_stage0_fixed_pct(self):
        # Esquema fixed-pct (2026-05-21): entry=100 → stop = 100 × 0.95 = 95
        stop = calc_safety_stop_price(
            entry_price=100, stage=0, atr=2.0, current_stop=None,
        )
        self.assertEqual(stop, 95.0)

    def test_stage1_breakeven(self):
        stop = calc_safety_stop_price(
            entry_price=100, stage=1, atr=2.0, current_stop=95.0,
        )
        self.assertEqual(stop, 100.0)

    def test_invariant_only_up(self):
        # current_stop más alto que calculado — el current gana
        stop = calc_safety_stop_price(
            entry_price=100, stage=0, atr=2.0, current_stop=99.0,
        )
        self.assertEqual(stop, 99.0)


class SubmitSafetyStopTests(unittest.TestCase):
    def test_submit_sends_correct_body(self):
        captured = []

        def fake_submit(method, path, body):
            captured.append((method, path, body))
            return {"id": "ord-abc123", "status": "new"}

        req = SafetyStopRequest(symbol="SPY", qty=10, stop_price=425.50)
        result = submit_safety_stop(req, submit_fn=fake_submit)

        self.assertTrue(result.ok)
        self.assertEqual(result.order_id, "ord-abc123")
        self.assertEqual(len(captured), 1)
        method, path, body = captured[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/orders")
        self.assertEqual(body["symbol"], "SPY")
        self.assertEqual(body["qty"], "10")
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["type"], "stop")
        self.assertEqual(body["stop_price"], "425.5")
        self.assertEqual(body["time_in_force"], "gtc")

    def test_submit_rounds_stop_price_to_2_decimals(self):
        captured = []
        def fake_submit(method, path, body):
            captured.append(body)
            return {"id": "x"}

        req = SafetyStopRequest(symbol="SPY", qty=1, stop_price=425.4999)
        submit_safety_stop(req, submit_fn=fake_submit)
        self.assertEqual(captured[0]["stop_price"], "425.5")

    def test_submit_failure_returns_error(self):
        def fake_submit(method, path, body):
            return None  # ej. HTTP 400 o timeout

        req = SafetyStopRequest(symbol="SPY", qty=1, stop_price=400)
        result = submit_safety_stop(req, submit_fn=fake_submit)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_submit_no_id_in_response(self):
        def fake_submit(method, path, body):
            return {"status": "accepted"}  # falta "id"

        req = SafetyStopRequest(symbol="SPY", qty=1, stop_price=400)
        result = submit_safety_stop(req, submit_fn=fake_submit)
        self.assertFalse(result.ok)


class CancelSafetyStopTests(unittest.TestCase):
    def test_cancel_none_is_noop_success(self):
        call_count = [0]
        def fake_submit(*args, **kwargs):
            call_count[0] += 1
            return None
        result = cancel_safety_stop(None, submit_fn=fake_submit)
        self.assertTrue(result.ok)
        self.assertEqual(call_count[0], 0, "no debió llamar a Alpaca")

    def test_cancel_empty_str_is_noop(self):
        result = cancel_safety_stop("", submit_fn=lambda *a, **kw: None)
        self.assertTrue(result.ok)

    def test_cancel_sends_delete(self):
        captured = []
        def fake_submit(method, path, body):
            captured.append((method, path, body))
            return {"id": "x", "status": "canceled"}
        result = cancel_safety_stop("ord-xyz", submit_fn=fake_submit)
        self.assertTrue(result.ok)
        self.assertEqual(captured[0][0], "DELETE")
        self.assertEqual(captured[0][1], "/orders/ord-xyz")

    def test_cancel_404_treated_as_success(self):
        def fake_submit(*args, **kwargs):
            return None  # ej. la orden ya no existe
        result = cancel_safety_stop("ord-already-gone", submit_fn=fake_submit)
        # OK: el resultado deseado ya se cumplió
        self.assertTrue(result.ok)
        self.assertEqual(result.error, "already_gone")


class ReplaceSafetyStopTests(unittest.TestCase):
    def test_replace_cancels_then_submits(self):
        events = []
        def fake_submit(method, path, body):
            events.append((method, path))
            if method == "DELETE":
                return {"id": "x", "status": "canceled"}
            return {"id": "new-ord-id", "status": "new"}

        new_req = SafetyStopRequest(symbol="SPY", qty=5, stop_price=420)
        result = replace_safety_stop("old-id", new_req, submit_fn=fake_submit)
        self.assertTrue(result.ok)
        self.assertEqual(result.order_id, "new-ord-id")
        # Ordenó: DELETE old, POST new
        self.assertEqual(events[0], ("DELETE", "/orders/old-id"))
        self.assertEqual(events[1], ("POST", "/orders"))

    def test_replace_with_no_old_id(self):
        # Sin orden vieja a cancelar — solo submitea la nueva
        events = []
        def fake_submit(method, path, body):
            events.append(method)
            return {"id": "new-ord"}
        new_req = SafetyStopRequest(symbol="SPY", qty=5, stop_price=420)
        result = replace_safety_stop(None, new_req, submit_fn=fake_submit)
        self.assertTrue(result.ok)
        self.assertEqual(events, ["POST"])  # NO hay DELETE


if __name__ == "__main__":
    unittest.main()
