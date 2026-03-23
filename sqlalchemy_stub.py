"""
sqlalchemy_stub.py — Minimal SQLAlchemy compatibility shim for testing.

Installs itself into sys.modules["sqlalchemy"] and sub-modules so that
ORM model files can be imported and unit-tested without a real SQLAlchemy
installation.

Supports:
  - All common column types (String, Integer, BigInteger, Numeric, DateTime, etc.)
  - ForeignKey, UniqueConstraint, PrimaryKeyConstraint, CheckConstraint, Index
  - mapped_column descriptor
  - DeclarativeBase with metaclass-based __init__
  - select / update / exists / and_ / cast / func
  - Session stub with add/commit/rollback/close/get/execute/scalars
  - event (no-op listens_for)
  - exc.OperationalError, exc.IntegrityError
"""
from __future__ import annotations

import sys
import types
from typing import Any, Optional


# ── Column type stubs ─────────────────────────────────────────────────────────

class _ColType:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass
    def __class_getitem__(cls, item: Any) -> Any:
        return cls

String = type("String", (_ColType,), {})
Integer = type("Integer", (_ColType,), {})
BigInteger = type("BigInteger", (_ColType,), {})
SmallInteger = type("SmallInteger", (_ColType,), {})
Numeric = type("Numeric", (_ColType,), {})
Float = type("Float", (_ColType,), {})
Boolean = type("Boolean", (_ColType,), {})
Text = type("Text", (_ColType,), {})
DateTime = type("DateTime", (_ColType,), {})
Date = type("Date", (_ColType,), {})
Time = type("Time", (_ColType,), {})
LargeBinary = type("LargeBinary", (_ColType,), {})
JSON = type("JSON", (_ColType,), {})
DECIMAL = type("DECIMAL", (_ColType,), {})
ARRAY = type("ARRAY", (_ColType,), {})
Enum = type("Enum", (_ColType,), {})
Interval = type("Interval", (_ColType,), {})
Unicode = type("Unicode", (_ColType,), {})
UnicodeText = type("UnicodeText", (_ColType,), {})


# ── Constraints / indexes ─────────────────────────────────────────────────────

class ForeignKey:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

class UniqueConstraint:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

class PrimaryKeyConstraint:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

class CheckConstraint:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

class Index:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass


# ── mapped_column ─────────────────────────────────────────────────────────────

class _MappedColumnDescriptor:
    """Acts as a descriptor that stores/retrieves values per-instance."""
    def __init__(self, col_type: Any = None, **kwargs: Any) -> None:
        self._default = kwargs.get("default", None)
        self._attr_name: Optional[str] = None

    def __set_name__(self, owner: Any, name: str) -> None:
        self._attr_name = f"_mc_{name}"

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        return getattr(obj, self._attr_name, self._default)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(obj, self._attr_name, value)


def mapped_column(*args: Any, **kwargs: Any) -> _MappedColumnDescriptor:
    col_type = args[0] if args else None
    return _MappedColumnDescriptor(col_type, **kwargs)


# ── Mapped type hint (no-op) ──────────────────────────────────────────────────

class Mapped:
    def __class_getitem__(cls, item: Any) -> Any:
        return cls


# ── relationship (no-op) ──────────────────────────────────────────────────────

def relationship(*a: Any, **kw: Any) -> None:
    return None


# ── DeclarativeBase ───────────────────────────────────────────────────────────

class _DeclarativeMeta(type):
    def __new__(mcs, name: str, bases: tuple, namespace: dict) -> type:
        cls = super().__new__(mcs, name, bases, namespace)
        # Collect all mapped_column descriptors for __init__
        cols = {}
        for klass in reversed(cls.__mro__):
            for attr_name, val in vars(klass).items():
                if isinstance(val, _MappedColumnDescriptor):
                    cols[attr_name] = val
        cls._mapped_cols = cols

        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)
            # Apply defaults for unmapped attributes
            for col_name, descriptor in self._mapped_cols.items():
                if descriptor._default is not None and not hasattr(self, descriptor._attr_name or ""):
                    pass  # defaults handled by descriptor.__get__

        cls.__init__ = __init__
        return cls


class DeclarativeBase(metaclass=_DeclarativeMeta):
    __tablename__: str = ""
    __table_args__: Any = {}
    metadata: Any = None

    def __repr__(self) -> str:
        cols = {k: getattr(self, k, None) for k in self._mapped_cols}
        pairs = ", ".join(f"{k}={v!r}" for k, v in cols.items())
        return f"<{type(self).__name__}({pairs})>"


# ── Mixin stubs ───────────────────────────────────────────────────────────────

class _TimestampMixin:
    pass

class _UUIDMixin:
    pass


# ── func, text, and_ / or_ / cast ─────────────────────────────────────────────

class _Func:
    def __getattr__(self, name: str) -> Any:
        def _fn(*a: Any, **kw: Any) -> None:
            return None
        return _fn

func = _Func()


def text(sql: str) -> str:
    return sql


def and_(*clauses: Any) -> Any:
    return None


def or_(*clauses: Any) -> Any:
    return None


def cast(col: Any, type_: Any) -> Any:
    return col


def not_(clause: Any) -> Any:
    return clause


# ── select / update / exists ──────────────────────────────────────────────────

class _Query:
    def __init__(self, *args: Any) -> None:
        self._args = args

    def where(self, *a: Any, **kw: Any) -> "_Query":
        return self

    def filter(self, *a: Any, **kw: Any) -> "_Query":
        return self

    def limit(self, n: int) -> "_Query":
        return self

    def offset(self, n: int) -> "_Query":
        return self

    def order_by(self, *a: Any) -> "_Query":
        return self

    def values(self, **kw: Any) -> "_Query":
        return self

    def returning(self, *a: Any) -> "_Query":
        return self

    def scalar_subquery(self) -> "_Query":
        return self

    def exists(self) -> "_Query":
        return self


def select(*args: Any) -> _Query:
    return _Query(*args)


def update(table: Any) -> _Query:
    return _Query(table)


def exists(*args: Any) -> _Query:
    return _Query(*args)


def insert(table: Any) -> _Query:
    return _Query(table)


def delete(table: Any) -> _Query:
    return _Query(table)


# ── Session stub ──────────────────────────────────────────────────────────────

class _ScalarsResult:
    def __init__(self, items: list) -> None:
        self._items = items

    def first(self) -> Any:
        return self._items[0] if self._items else None

    def all(self) -> list:
        return list(self._items)

    def __iter__(self):
        return iter(self._items)


class _ExecuteResult:
    def __init__(self) -> None:
        self._rows: list = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def scalars(self) -> "_ScalarsResult":
        return _ScalarsResult(self._rows)


class Session:
    def __init__(self, *a: Any, **kw: Any) -> None:
        self._store: dict[str, Any] = {}
        self._added: list = []

    def add(self, obj: Any) -> None:
        self._added.append(obj)

    def add_all(self, objs: list) -> None:
        self._added.extend(objs)

    def get(self, model: Any, pk: Any) -> Any:
        return self._store.get(str(pk))

    def execute(self, stmt: Any, *a: Any, **kw: Any) -> _ExecuteResult:
        return _ExecuteResult()

    def scalars(self, stmt: Any, *a: Any, **kw: Any) -> _ScalarsResult:
        return _ScalarsResult([])

    def scalar(self, stmt: Any, *a: Any, **kw: Any) -> Any:
        return None

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def flush(self, *a: Any) -> None:
        pass

    def refresh(self, obj: Any) -> None:
        pass

    def delete(self, obj: Any) -> None:
        pass

    def merge(self, obj: Any) -> Any:
        return obj

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()


def sessionmaker(*a: Any, **kw: Any) -> type:
    return Session


def create_engine(*a: Any, **kw: Any) -> Any:
    class _Engine:
        def connect(self): return self
        def execute(self, *a, **kw): return _ExecuteResult()
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def dispose(self): pass
    return _Engine()


# ── exc module ────────────────────────────────────────────────────────────────

class _SAExcModule(types.ModuleType):
    class OperationalError(Exception):
        pass
    class IntegrityError(Exception):
        pass
    class ProgrammingError(Exception):
        pass
    class DataError(Exception):
        pass
    class NoResultFound(Exception):
        pass
    class MultipleResultsFound(Exception):
        pass
    class InvalidRequestError(Exception):
        pass
    class DBAPIError(Exception):
        pass

_sa_exc = _SAExcModule("sqlalchemy.exc")
for _name in list(vars(_SAExcModule)):
    if not _name.startswith("_"):
        _obj = getattr(_SAExcModule, _name)
        _sa_exc.__dict__[_name] = _obj


# ── event module ──────────────────────────────────────────────────────────────

class _Event:
    @staticmethod
    def listens_for(target: Any, identifier: str, *a: Any, **kw: Any) -> Any:
        def decorator(fn: Any) -> Any:
            return fn
        return decorator

    @staticmethod
    def listen(target: Any, identifier: str, fn: Any, *a: Any, **kw: Any) -> None:
        pass

    @staticmethod
    def remove(target: Any, identifier: str, fn: Any) -> None:
        pass

_event_mod = _Event()


# ── orm module ────────────────────────────────────────────────────────────────

class _ORM(types.ModuleType):
    DeclarativeBase = DeclarativeBase
    Mapped = Mapped
    mapped_column = staticmethod(mapped_column)
    Session = Session
    sessionmaker = staticmethod(sessionmaker)
    relationship = staticmethod(relationship)

    class MappedColumn:
        pass

    @staticmethod
    def declared_attr(fn: Any) -> Any:
        return fn

_orm_mod = _ORM("sqlalchemy.orm")


# ── dialects module (stub) ────────────────────────────────────────────────────

_dialects = types.ModuleType("sqlalchemy.dialects")
_dialects_pg = types.ModuleType("sqlalchemy.dialects.postgresql")
_dialects_pg.UUID = String
_dialects_pg.JSONB = JSON
_dialects_pg.ARRAY = ARRAY
_dialects_pg.insert = insert


# ── Assemble the top-level sqlalchemy module ──────────────────────────────────

_sa = types.ModuleType("sqlalchemy")

_sa.String = String
_sa.Integer = Integer
_sa.BigInteger = BigInteger
_sa.SmallInteger = SmallInteger
_sa.Numeric = Numeric
_sa.Float = Float
_sa.Boolean = Boolean
_sa.Text = Text
_sa.DateTime = DateTime
_sa.Date = Date
_sa.Time = Time
_sa.LargeBinary = LargeBinary
_sa.JSON = JSON
_sa.DECIMAL = DECIMAL
_sa.ARRAY = ARRAY
_sa.Enum = Enum
_sa.Interval = Interval
_sa.Unicode = Unicode
_sa.UnicodeText = UnicodeText

_sa.ForeignKey = ForeignKey
_sa.UniqueConstraint = UniqueConstraint
_sa.PrimaryKeyConstraint = PrimaryKeyConstraint
_sa.CheckConstraint = CheckConstraint
_sa.Index = Index

_sa.mapped_column = mapped_column
_sa.Mapped = Mapped
_sa.relationship = relationship
_sa.DeclarativeBase = DeclarativeBase

_sa.func = func
_sa.text = text
_sa.and_ = and_
_sa.or_ = or_
_sa.cast = cast
_sa.not_ = not_
_sa.select = select
_sa.update = update
_sa.exists = exists
_sa.insert = insert
_sa.delete = delete

_sa.Session = Session
_sa.sessionmaker = sessionmaker
_sa.create_engine = create_engine

_sa.exc = _sa_exc
_sa.event = _event_mod
_sa.orm = _orm_mod

# Install into sys.modules
sys.modules["sqlalchemy"] = _sa
sys.modules["sqlalchemy.orm"] = _orm_mod
sys.modules["sqlalchemy.exc"] = _sa_exc
sys.modules["sqlalchemy.dialects"] = _dialects
sys.modules["sqlalchemy.dialects.postgresql"] = _dialects_pg

# ── Also expose DeclarativeBase via sqlalchemy.orm ────────────────────────────
_orm_mod.DeclarativeBase = DeclarativeBase
_orm_mod.Mapped = Mapped
_orm_mod.mapped_column = mapped_column
_orm_mod.Session = Session
_orm_mod.sessionmaker = sessionmaker
_orm_mod.relationship = relationship
