from __future__ import annotations

from scalping_bot.broker.alpaca import AlpacaBroker
from scalping_bot.broker.base import Broker
from scalping_bot.broker.simulator import LocalPaperBroker
from scalping_bot.config import Settings
from scalping_bot.logging_setup import get_logger
from scalping_bot.models import LiveTradingDisabledError

log = get_logger("broker.factory")


def make_broker(settings: Settings) -> Broker:
    settings.assert_trading_mode()
    if settings.use_simulator():
        if not settings.paper:
            raise LiveTradingDisabledError("Live mode cannot use the local simulator.")
        log.info("using_local_paper_simulator", reason="no_alpaca_keys_or_force_simulator")
        return LocalPaperBroker(
            symbols=list(settings.watchlist),
            cash=settings.initial_cash,
            seed=settings.simulator_seed,
        )
    log.info("using_alpaca_broker", paper=settings.paper, url=settings.effective_base_url())
    return AlpacaBroker(settings)
