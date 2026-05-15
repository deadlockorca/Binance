from __future__ import annotations

from dataclasses import dataclass

from aegis_engine.core.config import BotConfig


@dataclass
class OrderPlan:
    side: str
    entry_price: float
    quantity: float
    notional_usdt: float
    stop_loss: float
    take_profit: float
    expected_loss_usdt: float
    expected_profit_usdt: float


@dataclass
class AccountSnapshot:
    free_usdt: float
    total_usdt: float


class RiskEngine:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg

    def _calc_notional(self, account: AccountSnapshot, loss_pct: float) -> float:
        risk = self.cfg.risk
        if risk.sizing_mode == "margin":
            if risk.margin_per_trade_usdt > 0:
                margin_usdt = risk.margin_per_trade_usdt
            else:
                margin_usdt = account.free_usdt * (risk.margin_per_trade_pct / 100)
            return margin_usdt * risk.leverage

        risk_budget = account.total_usdt * (risk.risk_per_trade_pct / 100)
        if loss_pct <= 0:
            return 0
        return risk_budget / loss_pct

    def _default_brackets(self, side: str, entry_price: float) -> tuple[float, float]:
        risk = self.cfg.risk
        if side == "long":
            stop_loss = entry_price * (1 - risk.sl_pct / 100)
            take_profit = entry_price * (1 + risk.tp_pct / 100)
        else:
            stop_loss = entry_price * (1 + risk.sl_pct / 100)
            take_profit = entry_price * (1 - risk.tp_pct / 100)
        return stop_loss, take_profit

    def _resolve_brackets(
        self,
        side: str,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> tuple[float, float] | None:
        if stop_loss is None and take_profit is None:
            return self._default_brackets(side, entry_price)
        if stop_loss is None or take_profit is None:
            return None
        stop_loss = float(stop_loss)
        take_profit = float(take_profit)
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            return None
        if side == "long" and stop_loss < entry_price < take_profit:
            return stop_loss, take_profit
        if side == "short" and take_profit < entry_price < stop_loss:
            return stop_loss, take_profit
        return None

    def plan_order(
        self,
        side: str,
        entry_price: float,
        account: AccountSnapshot,
        size_multiplier: float = 1.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderPlan | None:
        risk = self.cfg.risk

        if account.free_usdt < risk.min_free_usdt_to_trade:
            return None

        brackets = self._resolve_brackets(side=side, entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit)
        if brackets is None:
            return None
        stop_loss_price, take_profit_price = brackets

        loss_pct = abs(entry_price - stop_loss_price) / entry_price
        profit_pct = abs(take_profit_price - entry_price) / entry_price

        notional = self._calc_notional(account, loss_pct=loss_pct)
        if notional <= 0:
            return None
        if size_multiplier <= 0:
            return None

        notional *= size_multiplier

        if risk.max_notional_usdt > 0:
            notional = min(notional, risk.max_notional_usdt)

        notional = max(notional, risk.min_notional_usdt)
        max_by_margin = account.free_usdt * risk.leverage * 0.98
        notional = min(notional, max_by_margin)

        if notional < risk.min_notional_usdt:
            return None

        quantity = notional / entry_price

        expected_loss = notional * loss_pct
        expected_profit = notional * profit_pct

        return OrderPlan(
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            notional_usdt=notional,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            expected_loss_usdt=expected_loss,
            expected_profit_usdt=expected_profit,
        )
