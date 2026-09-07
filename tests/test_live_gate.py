from __future__ import annotations

import pytest

from scalping_bot.config import LIVE_BASE_URL, PAPER_BASE_URL, Settings
from scalping_bot.models import LiveTradingDisabledError


def test_paper_is_default() -> None:
    s = Settings()
    assert s.paper is True
    assert s.allow_live_trading is False
    assert s.live_confirmed is False
    s.assert_trading_mode()
    assert s.effective_base_url() == PAPER_BASE_URL
    assert s.use_simulator() is True


def test_live_refused_without_gates() -> None:
    payload = Settings().model_dump()
    payload["paper"] = False
    payload["allow_live_trading"] = False
    payload["live_confirmed"] = False
    payload["alpaca_api_key"] = "key"
    payload["alpaca_secret_key"] = "secret"
    payload["alpaca_base_url"] = LIVE_BASE_URL
    s = Settings.model_validate(payload)
    with pytest.raises(LiveTradingDisabledError, match="hard-gated"):
        s.assert_trading_mode()


def test_live_refused_if_paper_url() -> None:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": False,
            "allow_live_trading": True,
            "live_confirmed": True,
            "alpaca_api_key": "key",
            "alpaca_secret_key": "secret",
            "alpaca_base_url": PAPER_BASE_URL,
        }
    )
    s = Settings.model_validate(payload)
    with pytest.raises(LiveTradingDisabledError, match="paper-api"):
        s.assert_trading_mode()


def test_live_allowed_only_with_all_gates() -> None:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": False,
            "allow_live_trading": True,
            "live_confirmed": True,
            "alpaca_api_key": "key",
            "alpaca_secret_key": "secret",
            "alpaca_base_url": LIVE_BASE_URL,
        }
    )
    s = Settings.model_validate(payload)
    s.assert_trading_mode()
    assert s.effective_base_url() == LIVE_BASE_URL


def test_swing_does_not_weaken_live_gates() -> None:
    payload = Settings().model_dump()
    payload.update(
        {
            "paper": False,
            "mode": "swing",
            "allow_live_trading": False,
            "live_confirmed": False,
            "alpaca_api_key": "key",
            "alpaca_secret_key": "secret",
            "alpaca_base_url": LIVE_BASE_URL,
        }
    )
    s = Settings.model_validate(payload)
    assert s.is_swing()
    with pytest.raises(LiveTradingDisabledError, match="hard-gated"):
        s.assert_trading_mode()


def test_swing_profile_widens_stops() -> None:
    s = Settings.model_validate({**Settings().model_dump(), "mode": "swing"})
    assert s.strategy == "trend_swing"
    assert s.bar_timeframe == "1Day"
    assert s.hold_overnight is True
    assert s.session_flatten is False
    assert s.max_stop_pct >= 0.10
    assert s.max_trades_per_day <= 2
    assert s.paper is True
    assert s.allow_live_trading is False
