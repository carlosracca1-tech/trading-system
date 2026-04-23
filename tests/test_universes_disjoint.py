"""
Invariante de arquitectura (CLAUDE.md): MREV solo cripto, RFTM solo ETFs.
Si alguien pusiera un ETF en CRYPTO_SYMBOLS o viceversa, los bots se
pisarían posiciones (bug histórico pre-2026-04-22).
"""
from __future__ import annotations


def test_etf_and_crypto_do_not_overlap():
    from standalone_paper_trader import ETF_UNIVERSE
    from standalone_mrev_trader import CRYPTO_SYMBOLS, ALL_SYMBOLS, ETF_SYMBOLS

    assert set(ETF_UNIVERSE) & set(CRYPTO_SYMBOLS) == set()
    assert ETF_SYMBOLS == []
    assert set(ALL_SYMBOLS) == set(CRYPTO_SYMBOLS)


def test_crypto_symbols_have_slash():
    """Convención: cripto es 'BTC/USD' no 'BTCUSD' en nuestro código."""
    from standalone_mrev_trader import CRYPTO_SYMBOLS
    for sym in CRYPTO_SYMBOLS:
        assert "/" in sym, f"{sym} no tiene slash"


def test_etf_universe_nonempty():
    from standalone_paper_trader import ETF_UNIVERSE
    assert len(ETF_UNIVERSE) >= 10
