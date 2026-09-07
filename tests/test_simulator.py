from __future__ import annotations

import pytest

from scalping_bot.broker.simulator import LocalPaperBroker
from scalping_bot.models import BrokerError, OrderRequest, Side


def test_simulator_fills_and_pnl() -> None:
    broker = LocalPaperBroker(symbols=["AAPL", "BTC/USD"], cash=100_000, seed=1)
    px = broker.last_price("AAPL")
    assert px > 0
    order = broker.submit_order(OrderRequest(symbol="AAPL", qty=10, side=Side.BUY, reason="test"))
    assert order.status == "filled"
    pos = broker.get_position("AAPL")
    assert pos is not None and pos.qty == 10
    account = broker.get_account()
    assert account.cash < 100_000
    assert account.paper is True
    broker.advance()
    closed = broker.close_position("AAPL", reason="test_exit")
    assert closed is not None
    assert broker.get_position("AAPL") is None
    final = broker.get_account()
    assert pytest.approx(final.equity, rel=1e-9) == final.cash


def test_crypto_and_stock_asset_classes() -> None:
    broker = LocalPaperBroker(symbols=["AAPL", "BTC/USD"], cash=50_000, seed=2)
    assert broker.asset_class("AAPL").value == "stock"
    assert broker.asset_class("BTC/USD").value == "crypto"
    assert broker.is_tradable_now("BTC/USD") is True
    bars = broker.get_bars("BTC/USD", "1Min", 50)
    assert len(bars) == 50
    broker.submit_order(OrderRequest(symbol="BTC/USD", qty=0.01, side=Side.BUY))
    pos = broker.get_position("BTC/USD")
    assert pos is not None and pos.qty == pytest.approx(0.01)


def test_insufficient_cash_raises() -> None:
    broker = LocalPaperBroker(symbols=["AAPL"], cash=100, seed=3)
    with pytest.raises(BrokerError):
        broker.submit_order(OrderRequest(symbol="AAPL", qty=1000, side=Side.BUY))
