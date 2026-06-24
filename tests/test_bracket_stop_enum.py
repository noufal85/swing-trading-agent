"""Regression test for the bracket stop-loss leg lookup.

alpaca-py order enums subclass ``(str, Enum)`` so ``str(OrderType.STOP)`` is
``'OrderType.STOP'`` (not ``'stop'``). The old code matched legs via
``str(leg.type).lower() in ('stop', ...)`` which never matched a real enum, so
``modify_bracket_stop`` always reported "No stop-loss leg found" and broker-side
stops were never updated. These tests pin the normalized-value comparison.
"""
import enum
import pytest

from tools.execution.alpaca_orders import _enum_val, _ALPACA_AVAILABLE


class FakeOrderType(str, enum.Enum):
    STOP = 'stop'
    STOP_LIMIT = 'stop_limit'
    LIMIT = 'limit'


class FakeStatus(str, enum.Enum):
    HELD = 'held'
    FILLED = 'filled'


def test_enum_stringifies_to_classname():
    # Guard: this is the exact alpaca-py behavior the bug hinged on.
    assert str(FakeOrderType.STOP) != 'stop'
    assert FakeOrderType.STOP.value == 'stop'


@pytest.mark.parametrize("x, expected", [
    (FakeOrderType.STOP, 'stop'),
    (FakeOrderType.STOP_LIMIT, 'stop_limit'),
    (FakeOrderType.LIMIT, 'limit'),
    (FakeStatus.FILLED, 'filled'),
    ('stop', 'stop'),          # plain string (mock broker / tests)
    ('STOP', 'stop'),
    (None, ''),
])
def test_enum_val_normalizes(x, expected):
    assert _enum_val(x) == expected


def test_stop_leg_matches_via_enum_value():
    # The matching predicate the fix relies on.
    assert _enum_val(FakeOrderType.STOP) in ('stop', 'stop_limit')
    assert _enum_val(FakeOrderType.LIMIT) not in ('stop', 'stop_limit')


@pytest.mark.skipif(not _ALPACA_AVAILABLE, reason="alpaca-py not installed")
def test_modify_bracket_stop_finds_enum_stop_leg(monkeypatch):
    from tools.execution import alpaca_orders as ao

    class Leg:
        def __init__(self, type_, status, id_):
            self.type, self.status, self.id = type_, status, id_

    class Parent:
        legs = [Leg(FakeOrderType.LIMIT, FakeStatus.HELD, 'tp1'),
                Leg(FakeOrderType.STOP, FakeStatus.HELD, 'sl1')]

    class Client:
        def get_order_by_id(self, oid):
            return Parent()
        def replace_order_by_id(self, oid, req):
            self.replaced = (oid, req)

    client = Client()
    monkeypatch.setattr(ao, '_get_trading_client', lambda: client)
    monkeypatch.setattr(ao, 'ReplaceOrderRequest', lambda **kw: kw)

    res = ao.modify_bracket_stop('parent1', 123.45)
    assert res['modified'] is True
    assert res['stop_order_id'] == 'sl1'
    assert client.replaced[0] == 'sl1'
