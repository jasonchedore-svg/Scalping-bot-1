from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith("ALPACA") or upper in {
            "PAPER",
            "ALLOW_LIVE_TRADING",
            "FORCE_SIMULATOR",
            "WATCHLIST",
            "DRY_RUN",
        }:
            monkeypatch.delenv(key, raising=False)
