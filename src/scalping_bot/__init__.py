"""Paper-first scalping / day-trading bot for US stocks and crypto."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scalping-bot")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
