from __future__ import annotations

from uuid import uuid4

from scalping_bot.models import (
    Account,
    AssetClass,
    BrokerError,
    Order,
    OrderRequest,
    Position,
    Side,
    infer_asset_class,
    normalize_symbol,
    round_price,
)


class SimulatedLedger:
    """Cash, positions, and market fills with slippage + crypto fees.

    Shared by the local random-walk simulator and the historical replay broker
    so backtests use the same execution assumptions as `scalping-bot demo`.
    """

    def __init__(
        self,
        cash: float,
        *,
        slippage_bps: dict[str, float] | None = None,
        crypto_fee_bps: float = 15.0,
    ) -> None:
        self.cash = cash
        self.slippage_bps = slippage_bps or {"stock": 2.0, "crypto": 4.0}
        self.crypto_fee_bps = crypto_fee_bps
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.fees_paid = 0.0
        self.slippage_paid = 0.0

    def get_account(self) -> Account:
        invested = sum(p.market_value for p in self.positions.values())
        equity = self.cash + invested
        return Account(equity=equity, cash=self.cash, buying_power=self.cash, paper=True)

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(normalize_symbol(symbol))

    def mark_to_market(self, last_price) -> None:
        for symbol, pos in list(self.positions.items()):
            px = last_price(symbol)
            self.positions[symbol] = Position(
                symbol=symbol,
                qty=pos.qty,
                avg_entry_price=pos.avg_entry_price,
                market_value=pos.qty * px,
                unrealized_pl=(px - pos.avg_entry_price) * pos.qty,
                asset_class=pos.asset_class,
                side=pos.side,
            )

    def fill(self, request: OrderRequest, raw_price: float) -> Order:
        symbol = normalize_symbol(request.symbol)
        asset = infer_asset_class(symbol)
        if request.qty <= 0:
            raise BrokerError("qty must be positive")
        if raw_price <= 0:
            raise BrokerError("invalid last price")
        kind = "crypto" if asset is AssetClass.CRYPTO else "stock"
        slip = self.slippage_bps[kind] / 10_000.0
        if request.side is Side.BUY:
            px = raw_price * (1.0 + slip)
        else:
            px = raw_price * (1.0 - slip)
        px = round_price(px, asset)
        notional = px * request.qty
        fee = notional * (self.crypto_fee_bps / 10_000.0) if asset is AssetClass.CRYPTO else 0.0
        self.fees_paid += fee
        self.slippage_paid += abs(px - raw_price) * request.qty

        if request.side is Side.BUY:
            if notional + fee > self.cash + 1e-6:
                raise BrokerError("insufficient cash in simulator")
            self.cash -= notional + fee
            existing = self.positions.get(symbol)
            if existing:
                new_qty = existing.qty + request.qty
                avg = (existing.avg_entry_price * existing.qty + px * request.qty) / new_qty
            else:
                new_qty = request.qty
                avg = px
            self.positions[symbol] = Position(
                symbol=symbol,
                qty=new_qty,
                avg_entry_price=avg,
                market_value=new_qty * px,
                unrealized_pl=0.0,
                asset_class=asset,
                side=Side.BUY,
            )
        else:
            existing = self.positions.get(symbol)
            if existing is None or existing.qty <= 0:
                raise BrokerError(f"no long position to sell for {symbol}")
            sell_qty = min(request.qty, existing.qty)
            self.cash += px * sell_qty - fee
            remaining = existing.qty - sell_qty
            if remaining <= 1e-12:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = Position(
                    symbol=symbol,
                    qty=remaining,
                    avg_entry_price=existing.avg_entry_price,
                    market_value=remaining * px,
                    unrealized_pl=(px - existing.avg_entry_price) * remaining,
                    asset_class=asset,
                    side=Side.BUY,
                )

        order = Order(
            id=str(uuid4()),
            symbol=symbol,
            qty=request.qty,
            side=request.side,
            status="filled",
            filled_qty=request.qty,
            filled_avg_price=px,
            client_order_id=request.client_order_id,
        )
        self.orders.append(order)
        return order
