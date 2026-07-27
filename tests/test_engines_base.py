"""Base-contract property module for the av-engines-foundation spec
(``worker/engines/base.py``).

Epic 3 extends this file with the real base-contract properties — design
Properties P1 (``Engine_Result`` serialisation), P2 (invocation never mutates
the caller's options or context), P16 (options parsing is total and ignores
unknown keys), P17 (options serialisation round-trips), P18 (resolution is
idempotent and order-insensitive), P19 (``Options_Digest`` determinism), and
P20 (planning is pure, seeded, and reproducible) — plus the abstract-surface
and contract-pin unit tests.

Until then the file holds exactly ONE test, and it is deliberately NOT one of
the numbered design properties: it is a tooling smoke check that exists only to
prove the ``hypothesis`` toolchain is declared in ``requirements-dev.txt``,
installed, and collectable by ``pytest`` (task 1.3) before any real property is
written. It imports nothing from ``worker.engines`` — that package does not
exist until epic 2 — so it passes on the current checkout.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st


# --- Tooling smoke check (task 1.3 — NOT a numbered design property) --------
# Feature: av-engines-foundation, Tooling smoke check: hypothesis collects and runs
@settings(max_examples=100)
@given(st.integers())
def test_hypothesis_toolchain_available(value):
    """Validates: Requirements 22.7

    A trivial invariant (``value + 0 == value`` for every integer) exercised
    through ``@given``/``@settings`` so a missing or unimportable ``hypothesis``
    fails collection loudly. Not part of P1-P20 coverage.
    """
    assert value + 0 == value
