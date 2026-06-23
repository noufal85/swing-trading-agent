"""Unit tests for agents.quant_engine._classify_strategy.

Covers the weak-tier softening: borderline setups are flagged ``weak`` and
kept (so they reach the LLM) instead of being silently excluded, while
genuinely-nothing names are still dropped. Strong tiers and MOM-over-MR
priority are preserved.
"""
import pytest

from agents.quant_engine import _classify_strategy


def ctx(mom_z=0.0, mr_z=0.0, vs_20ma=0.0):
    return {
        'momentum_zscore': mom_z,
        'mean_reversion_zscore': mr_z,
        'price_vs_20ma_pct': vs_20ma,
    }


@pytest.mark.parametrize("c, expected", [
    # Strong tiers
    (ctx(mom_z=0.9),                          ('MOMENTUM', False)),       # strong MOM
    (ctx(mr_z=-1.5, vs_20ma=-0.03),           ('MEAN_REVERSION', False)), # strong MR
    # Weak tiers (previously excluded → now flagged + kept)
    (ctx(mom_z=0.3),                          ('MOMENTUM', True)),        # weak MOM
    (ctx(mr_z=-0.7, vs_20ma=-0.02),           ('MEAN_REVERSION', True)),  # weak MR
    # Priority: strong MR beats weak MOM
    (ctx(mom_z=0.3, mr_z=-1.2, vs_20ma=-0.04), ('MEAN_REVERSION', False)),
    # Priority: strong MOM beats strong MR
    (ctx(mom_z=0.9, mr_z=-2.0, vs_20ma=-0.05), ('MOMENTUM', False)),
    # Priority: weak MOM beats weak MR
    (ctx(mom_z=0.2, mr_z=-0.8, vs_20ma=-0.03), ('MOMENTUM', True)),
    # Excluded: no momentum, not (sufficiently) oversold
    (ctx(mom_z=0.0, vs_20ma=0.01),            (None, False)),             # above 20MA, flat mom
    (ctx(mom_z=-0.5, mr_z=-0.3, vs_20ma=-0.03), (None, False)),           # oversold too shallow
])
def test_classify_strategy(c, expected):
    assert _classify_strategy(c) == expected


def test_boundary_mom_half_is_weak_not_strong():
    # mom_z exactly 0.5 is NOT > 0.5 (strong), but IS > 0.0 (weak).
    assert _classify_strategy(ctx(mom_z=0.5)) == ('MOMENTUM', True)


def test_boundary_mom_zero_excluded():
    # mom_z exactly 0.0 is NOT > 0.0 and not oversold → excluded.
    assert _classify_strategy(ctx(mom_z=0.0, vs_20ma=0.02)) == (None, False)


def test_oversold_above_20ma_not_mr():
    # mr_z deeply negative but price is ABOVE the 20MA → not a MR setup.
    assert _classify_strategy(ctx(mom_z=-0.2, mr_z=-2.0, vs_20ma=0.05)) == (None, False)


def test_none_price_vs_20ma_is_safe():
    # Defensive: price_vs_20ma_pct may be None; must not raise and not MR.
    assert _classify_strategy({'momentum_zscore': -0.3, 'mean_reversion_zscore': -1.5,
                               'price_vs_20ma_pct': None}) == (None, False)
