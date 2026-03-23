#!/usr/bin/env python3
"""
run_tests.py — Custom test runner (no pytest required).

Discovers and runs all test_*.py files without any external dependencies.
Provides a minimal pytest-compatible shim so existing test files work as-is.

Usage:
    python3 run_tests.py              # run all tests
    python3 run_tests.py indicators   # filter by name
    python3 run_tests.py -v           # verbose output
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# ── Make project root importable ──────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ── Install SQLAlchemy stub if sqlalchemy is missing ─────────────────────────
try:
    import sqlalchemy  # noqa: F401
except ImportError:
    stub_path = ROOT / "sqlalchemy_stub.py"
    if stub_path.exists():
        spec = importlib.util.spec_from_file_location("_sa_stub_loader", stub_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        print("WARNING: sqlalchemy not found and sqlalchemy_stub.py missing.")
        print("         Some tests may fail.")


# ── Minimal pytest shim ───────────────────────────────────────────────────────

class _ApproxScalar:
    def __init__(self, value: float, rel: float = 1e-6, abs: float = 1e-12) -> None:
        self._value = value
        self._rel = rel
        self._abs = abs

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (int, float)):
            return NotImplemented
        tolerance = max(self._rel * max(abs(self._value), abs(float(other))), self._abs)
        return abs(float(other) - self._value) <= tolerance

    def __repr__(self) -> str:
        return f"approx({self._value!r})"


class _RaisesContext:
    def __init__(self, expected_exc: type) -> None:
        self._expected = expected_exc
        self.value: Optional[Exception] = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is None:
            raise AssertionError(f"Expected {self._expected.__name__} but no exception was raised")
        if not issubclass(exc_type, self._expected):
            return False  # re-raise unexpected exception
        self.value = exc_val
        return True  # suppress the expected exception


class _Mark:
    @staticmethod
    def parametrize(argnames: str, argvalues: list) -> Callable:
        def decorator(fn: Callable) -> Callable:
            fn._parametrize = (argnames, argvalues)
            return fn
        return decorator

    @staticmethod
    def skip(reason: str = "") -> Callable:
        def decorator(fn: Callable) -> Callable:
            fn._skip = reason or "skipped"
            return fn
        return decorator

    @staticmethod
    def skipif(condition: bool, reason: str = "") -> Callable:
        def decorator(fn: Callable) -> Callable:
            if condition:
                fn._skip = reason or "condition"
            return fn
        return decorator

    @staticmethod
    def xfail(reason: str = "") -> Callable:
        def decorator(fn: Callable) -> Callable:
            fn._xfail = reason
            return fn
        return decorator


class _PytestShim:
    """Minimal shim exposing the pytest symbols tests actually use."""

    @staticmethod
    def approx(value: float, rel: float = 1e-6, abs: float = 1e-12) -> _ApproxScalar:
        return _ApproxScalar(value, rel=rel, abs=abs)

    @staticmethod
    def raises(exc_type: type, *args: Any, **kwargs: Any) -> _RaisesContext:
        return _RaisesContext(exc_type)

    @staticmethod
    def fixture(fn: Optional[Callable] = None, **kwargs: Any) -> Any:
        if fn is not None:
            fn._is_fixture = True
            return fn
        def decorator(f: Callable) -> Callable:
            f._is_fixture = True
            return f
        return decorator

    mark = _Mark()

    class MonkeyPatch:
        def setattr(self, obj: Any, name: str, value: Any) -> None:
            setattr(obj, name, value)
        def setenv(self, name: str, value: str) -> None:
            os.environ[name] = value
        def delenv(self, name: str, raising: bool = True) -> None:
            if raising:
                del os.environ[name]
            else:
                os.environ.pop(name, None)


# Install shim into sys.modules so "import pytest" works in test files
_shim = _PytestShim()
_shim.__name__ = "pytest"
_shim.__spec__ = None
sys.modules.setdefault("pytest", _shim)


# ── Test discovery & execution ────────────────────────────────────────────────

class _TestResult:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors: list[tuple[str, str]] = []


def _collect_fixtures(cls_or_module: Any) -> dict[str, Callable]:
    """Collect all fixture functions from a class or module."""
    fixtures: dict[str, Callable] = {}
    for name in dir(cls_or_module):
        obj = getattr(cls_or_module, name, None)
        if callable(obj) and getattr(obj, "_is_fixture", False):
            fixtures[name] = obj
    return fixtures


def _resolve_fixtures(fn: Callable, fixtures: dict[str, Callable]) -> dict[str, Any]:
    """Resolve fixture arguments for a test function."""
    sig = inspect.signature(fn)
    kwargs: dict[str, Any] = {}
    for param_name in sig.parameters:
        if param_name == "self":
            continue
        if param_name in fixtures:
            fixture_fn = fixtures[param_name]
            # Call the fixture (generators not supported — call and return)
            val = fixture_fn()
            if inspect.isgenerator(val):
                val = next(val)
            kwargs[param_name] = val
        elif param_name == "monkeypatch":
            kwargs[param_name] = _PytestShim.MonkeyPatch()
    return kwargs


def _run_test_fn(
    fn: Callable,
    fixtures: dict[str, Callable],
    instance: Any = None,
    verbose: bool = False,
    label: str = "",
) -> tuple[str, Optional[str]]:
    """
    Run a single test function.
    Returns ("pass", None) | ("skip", reason) | ("fail", traceback_str)
    """
    if getattr(fn, "_skip", None) is not None:
        return ("skip", fn._skip)

    try:
        kwargs = _resolve_fixtures(fn, fixtures)
        if instance is not None:
            fn(instance, **kwargs)
        else:
            fn(**kwargs)
        return ("pass", None)
    except AssertionError as exc:
        tb = traceback.format_exc()
        return ("fail", tb)
    except Exception as exc:
        tb = traceback.format_exc()
        return ("fail", tb)


def _run_parametrized(
    fn: Callable,
    fixtures: dict[str, Callable],
    instance: Any = None,
    verbose: bool = False,
    base_label: str = "",
) -> list[tuple[str, str, Optional[str]]]:
    """Run a parametrized test, returning list of (status, label, detail)."""
    argnames_str, argvalues = fn._parametrize
    argnames = [a.strip() for a in argnames_str.split(",")]
    results = []
    for vals in argvalues:
        if not isinstance(vals, (list, tuple)):
            vals = (vals,)
        label = f"{base_label}[{','.join(str(v) for v in vals)}]"
        # Inject parametrized args as fixture overrides
        param_fixtures = dict(fixtures)
        for name, val in zip(argnames, vals):
            captured_val = val
            param_fixtures[name] = lambda _v=captured_val: _v
        status, detail = _run_test_fn(fn, param_fixtures, instance, verbose, label)
        results.append((status, label, detail))
    return results


def _run_module(
    module_path: Path,
    name_filter: str,
    result: _TestResult,
    verbose: bool,
) -> None:
    rel_path = module_path.relative_to(ROOT)
    module_name = str(rel_path).replace(os.sep, ".").removesuffix(".py")

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        tb = traceback.format_exc()
        print(f"\nERROR loading {rel_path}:\n{tb}")
        result.failed += 1
        result.errors.append((str(rel_path), tb))
        return

    module_fixtures = _collect_fixtures(module)

    # Find test classes and standalone test functions
    test_items: list[tuple[str, Callable, Any, dict]] = []

    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and name.startswith("Test"):
            instance = obj()
            cls_fixtures = {**module_fixtures, **_collect_fixtures(obj)}
            # Setup
            if hasattr(instance, "setup_method"):
                pass  # called per-test below
            for mname in dir(obj):
                if not mname.startswith("test"):
                    continue
                mfn = getattr(instance, mname)
                if callable(mfn):
                    test_items.append((f"{name}::{mname}", mfn, instance, cls_fixtures))
        elif callable(obj) and name.startswith("test_"):
            test_items.append((name, obj, None, module_fixtures))

    if not test_items:
        return

    printed_header = False

    for label, fn, instance, fixtures in test_items:
        if name_filter and name_filter.lower() not in label.lower() and name_filter.lower() not in str(rel_path).lower():
            continue

        if not printed_header:
            print(f"\n{rel_path}")
            printed_header = True

        # Setup
        if instance is not None and hasattr(instance, "setup_method"):
            try:
                instance.setup_method(fn)
            except Exception:
                pass

        # Parametrized?
        raw_fn = fn.__func__ if hasattr(fn, "__func__") else fn
        if hasattr(raw_fn, "_parametrize"):
            items = _run_parametrized(raw_fn, fixtures, instance, verbose, label)
            for status, plabel, detail in items:
                _print_result(status, plabel, detail, result, verbose)
        else:
            status, detail = _run_test_fn(raw_fn, fixtures, instance, verbose, label)
            _print_result(status, label, detail, result, verbose)

        # Teardown
        if instance is not None and hasattr(instance, "teardown_method"):
            try:
                instance.teardown_method(fn)
            except Exception:
                pass


def _print_result(
    status: str,
    label: str,
    detail: Optional[str],
    result: _TestResult,
    verbose: bool,
) -> None:
    if status == "pass":
        result.passed += 1
        if verbose:
            print(f"  PASS  {label}")
        else:
            print(".", end="", flush=True)
    elif status == "skip":
        result.skipped += 1
        print(f"  SKIP  {label}  ({detail})")
    else:
        result.failed += 1
        result.errors.append((label, detail or ""))
        print(f"\n  FAIL  {label}")
        if detail:
            # Print last few lines of traceback
            lines = detail.strip().splitlines()
            for line in lines[-8:]:
                print(f"        {line}")


def discover_test_files(root: Path) -> list[Path]:
    files = []
    for p in sorted(root.rglob("test_*.py")):
        # Skip __pycache__ and .venv
        parts = p.parts
        if "__pycache__" in parts or ".venv" in parts or "node_modules" in parts:
            continue
        files.append(p)
    return files


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run tests without pytest")
    parser.add_argument("filter", nargs="?", default="", help="Filter tests by name")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    test_files = discover_test_files(ROOT)
    if not test_files:
        print("No test files found.")
        return 0

    result = _TestResult()
    print(f"Discovered {len(test_files)} test file(s)")

    for path in test_files:
        _run_module(path, args.filter, result, args.verbose)

    total = result.passed + result.failed + result.skipped
    print(f"\n\n{'='*60}")
    print(f"  {total} tests — {result.passed} passed, {result.failed} failed, {result.skipped} skipped")
    print(f"{'='*60}")

    if result.errors:
        print(f"\n  {len(result.errors)} failure(s):")
        for label, _ in result.errors:
            print(f"    ✗  {label}")

    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
