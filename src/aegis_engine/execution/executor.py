from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from aegis_engine.core.config import BotConfig
from aegis_engine.exchange.binance_client import BinanceUsdmClient
from aegis_engine.risk.risk_engine import OrderPlan

LOGGER = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    symbol: str
    side: str
    setup: str
    entry_price: float
    quantity: float
    notional_usdt: float
    expected_loss_usdt: float
    expected_profit_usdt: float
    entry_order_id: str | None = None
    tp_order_id: str | None = None
    sl_order_id: str | None = None
    entry_order: dict[str, Any] | None = None
    tp_order: dict[str, Any] | None = None
    sl_order: dict[str, Any] | None = None
    entry_fee_rate: float = 0.0
    entry_fee_usdt: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0


class TradeExecutor:
    def __init__(self, cfg: BotConfig, exchange: BinanceUsdmClient) -> None:
        self.cfg = cfg
        self.exchange = exchange

    def _slippage_bps(self, ref_price: float, fill_price: float) -> float:
        return abs(fill_price - ref_price) / ref_price * 10000

    def _entry_mode_for_setup(self, setup: str) -> str:
        mode = self.cfg.execution.entry_order_by_setup.get(setup, self.cfg.execution.entry_order)
        mode = str(mode).lower().strip()
        return mode if mode in {"maker", "market"} else "market"

    def _entry_fee_rate(self, setup: str) -> float:
        mode = self._entry_mode_for_setup(setup)
        if mode == "maker":
            return self.cfg.analytics.fee_maker
        return self.cfg.analytics.fee_taker

    def _filled_qty(self, order: dict[str, Any] | None) -> float:
        if not order:
            return 0.0
        info = order.get("info", {})
        return float(order.get("filled") or info.get("executedQty") or 0.0)

    def _avg_price(self, order: dict[str, Any] | None, fallback: float) -> float:
        if not order:
            return fallback
        info = order.get("info", {})
        for value in [order.get("average"), info.get("avgPrice"), order.get("price"), info.get("price")]:
            try:
                val = float(value)
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
        return fallback

    def _wait_for_order(
        self,
        symbol: str,
        order_id: str,
        timeout_sec: int,
        poll_sec: float,
        target_amount: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_sec
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = self.exchange.fetch_order_safe(symbol, order_id)
            if latest is None:
                time.sleep(poll_sec)
                continue
            status = str(latest.get("status") or "").lower()
            if status in {"closed", "canceled"}:
                return latest
            filled = self._filled_qty(latest)
            if filled >= target_amount:
                return latest
            time.sleep(poll_sec)
        return self.exchange.fetch_order_safe(symbol, order_id) or latest

    def _build_result(
        self,
        symbol: str,
        side: str,
        setup: str,
        quantity: float,
        fill_price: float,
        plan: OrderPlan,
        entry_order: dict[str, Any] | None,
        tp_order: dict[str, Any] | None,
        sl_order: dict[str, Any] | None,
        entry_fee_rate: float,
    ) -> ExecutionResult:
        requested_qty = max(plan.quantity, 1e-12)
        fill_ratio = max(0.0, min(1.0, quantity / requested_qty))
        scaled_loss = plan.expected_loss_usdt * fill_ratio
        scaled_profit = plan.expected_profit_usdt * fill_ratio
        notional = quantity * fill_price
        entry_fee_usdt = abs(notional) * max(0.0, entry_fee_rate)
        return ExecutionResult(
            symbol=symbol,
            side=side,
            setup=setup,
            entry_price=fill_price,
            quantity=quantity,
            notional_usdt=notional,
            expected_loss_usdt=scaled_loss,
            expected_profit_usdt=scaled_profit,
            entry_order_id=str(entry_order.get("id")) if entry_order and entry_order.get("id") else None,
            tp_order_id=str(tp_order.get("id")) if tp_order and tp_order.get("id") else None,
            sl_order_id=str(sl_order.get("id")) if sl_order and sl_order.get("id") else None,
            entry_order=entry_order,
            tp_order=tp_order,
            sl_order=sl_order,
            entry_fee_rate=entry_fee_rate,
            entry_fee_usdt=entry_fee_usdt,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
        )

    def _place_bracket(
        self,
        symbol: str,
        side: str,
        amount: float,
        plan: OrderPlan,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not self.cfg.execution.use_exchange_tp_sl:
            return None, None
        tp_order, sl_order = self.exchange.create_exit_triggers(
            symbol=symbol,
            side=side,
            amount=amount,
            take_profit=plan.take_profit,
            stop_loss=plan.stop_loss,
        )
        LOGGER.info("Bracket set for %s: tp=%.4f sl=%.4f", symbol, plan.take_profit, plan.stop_loss)
        return tp_order, sl_order

    def _execute_market_entry(self, symbol: str, plan: OrderPlan, setup: str, amount: float) -> ExecutionResult | None:
        ref_price = self.exchange.reference_price(symbol=symbol, side=plan.side)
        entry_order = self.exchange.create_entry_market(symbol=symbol, side=plan.side, amount=amount)
        fill_price = self._avg_price(entry_order, ref_price)
        slippage = self._slippage_bps(ref_price, fill_price)
        LOGGER.info(
            "ENTRY market %s %s amount=%s fill=%.4f ref=%.4f slippage=%.2f bps",
            symbol,
            plan.side,
            amount,
            fill_price,
            ref_price,
            slippage,
        )
        if slippage > self.cfg.execution.max_slippage_bps:
            LOGGER.warning(
                "High slippage on %s: %.2f bps > %.2f bps",
                symbol,
                slippage,
                self.cfg.execution.max_slippage_bps,
            )
        tp_order, sl_order = self._place_bracket(symbol=symbol, side=plan.side, amount=amount, plan=plan)
        return self._build_result(
            symbol=symbol,
            side=plan.side,
            setup=setup,
            quantity=float(amount),
            fill_price=fill_price,
            plan=plan,
            entry_order=entry_order,
            tp_order=tp_order,
            sl_order=sl_order,
            entry_fee_rate=self._entry_fee_rate(setup),
        )

    def _maker_limit_price(self, symbol: str, side: str) -> float:
        bid, ask = self.exchange.best_bid_ask(symbol)
        if bid <= 0 or ask <= 0:
            return 0.0
        ref_mode = str(self.cfg.execution.ref_price).lower().strip()
        if ref_mode == "best":
            anchor = (bid + ask) / 2
        elif ref_mode == "mark":
            anchor = self.exchange.mark_price(symbol)
        elif side == "long":
            anchor = bid
        else:
            anchor = ask

        offset = self.cfg.execution.maker_price_offset_bps / 10000
        if side == "long":
            price = anchor * (1 + offset)
            cap = ask * 0.99999
            price = min(price, cap)
        else:
            price = anchor * (1 - offset)
            floor = bid * 1.00001
            price = max(price, floor)
        return self.exchange.price_to_precision(symbol, price)

    def _execute_maker_entry(self, symbol: str, plan: OrderPlan, setup: str, amount: float) -> ExecutionResult | None:
        timeout_sec = self.cfg.execution.maker_timeout_sec
        poll_sec = self.cfg.execution.maker_poll_interval_sec
        min_ratio = self.cfg.execution.maker_min_fill_ratio
        partial_action = self.cfg.execution.maker_partial_fill_action
        max_reprice = self.cfg.execution.maker_max_reprice

        for attempt in range(max_reprice + 1):
            limit_price = self._maker_limit_price(symbol, plan.side)
            if limit_price <= 0:
                LOGGER.warning("Skip %s: cannot derive maker price", symbol)
                return None

            entry_order = self.exchange.create_entry_limit(
                symbol=symbol,
                side=plan.side,
                amount=amount,
                price=limit_price,
                post_only=self.cfg.execution.maker_post_only,
            )
            order_id = str(entry_order.get("id") or "")
            LOGGER.info(
                "ENTRY maker %s %s amount=%.8f price=%.4f attempt=%d/%d timeout=%ss",
                symbol,
                plan.side,
                amount,
                limit_price,
                attempt + 1,
                max_reprice + 1,
                timeout_sec,
            )

            latest = self._wait_for_order(symbol, order_id, timeout_sec, poll_sec, amount)
            if latest is None:
                latest = entry_order
            status = str(latest.get("status") or "").lower()
            filled = self._filled_qty(latest)

            if status in {"open"} or filled < amount:
                self.exchange.cancel_order_safe(symbol, order_id)
                refreshed = self.exchange.fetch_order_safe(symbol, order_id)
                if refreshed is not None:
                    latest = refreshed
                    filled = self._filled_qty(latest)

            if filled <= 0:
                LOGGER.info("Maker not filled for %s on attempt %d", symbol, attempt + 1)
                continue

            fill_ratio = filled / amount if amount > 0 else 0.0
            avg_fill = self._avg_price(latest, fallback=limit_price)
            filled_amount = self.exchange.amount_to_precision(symbol, filled)

            if filled_amount <= 0:
                LOGGER.info("Maker filled below precision for %s", symbol)
                continue

            if fill_ratio < min_ratio and partial_action == "close":
                LOGGER.info(
                    "Partial fill below min ratio on %s: filled=%.4f%% < %.4f%%, closing partial",
                    symbol,
                    fill_ratio * 100,
                    min_ratio * 100,
                )
                self.exchange.close_position(
                    symbol=symbol,
                    side=plan.side,
                    amount=filled_amount,
                    mode=self.cfg.execution.exit_order,
                    maker_timeout_sec=self.cfg.execution.maker_timeout_sec,
                    maker_poll_sec=self.cfg.execution.maker_poll_interval_sec,
                    maker_post_only=self.cfg.execution.maker_post_only,
                )
                return None

            if fill_ratio < min_ratio:
                LOGGER.info(
                    "Partial fill kept on %s: filled=%.4f%% < %.4f%%",
                    symbol,
                    fill_ratio * 100,
                    min_ratio * 100,
                )
            else:
                LOGGER.info("Maker fill accepted on %s: filled=%.4f%%", symbol, fill_ratio * 100)

            tp_order, sl_order = self._place_bracket(symbol=symbol, side=plan.side, amount=filled_amount, plan=plan)
            return self._build_result(
                symbol=symbol,
                side=plan.side,
                setup=setup,
                quantity=filled_amount,
                fill_price=avg_fill,
                plan=plan,
                entry_order=latest,
                tp_order=tp_order,
                sl_order=sl_order,
                entry_fee_rate=self._entry_fee_rate(setup),
            )

        LOGGER.info("Skip %s: maker entry not filled after %d attempt(s)", symbol, max_reprice + 1)
        return None

    def execute(self, symbol: str, plan: OrderPlan, setup: str) -> ExecutionResult | None:
        spread = self.exchange.spread_bps(symbol)
        if spread > self.cfg.execution.max_spread_bps:
            LOGGER.info("Skip %s: spread %.2f bps > %.2f", symbol, spread, self.cfg.execution.max_spread_bps)
            return None

        amount = self.exchange.amount_to_precision(symbol, plan.quantity)
        if amount <= 0:
            LOGGER.warning("Skip %s: amount after precision is zero", symbol)
            return None

        if self.cfg.app.mode == "paper":
            entry_order, tp_order, sl_order = self.exchange.paper_open_position(
                symbol=symbol,
                side=plan.side,
                amount=amount,
                entry_price=plan.entry_price,
                take_profit=plan.take_profit,
                stop_loss=plan.stop_loss,
            )
            LOGGER.info(
                "PAPER %s %s qty=%.6f entry=%.2f sl=%.2f tp=%.2f notional=%.2f setup=%s",
                symbol,
                plan.side,
                amount,
                plan.entry_price,
                plan.stop_loss,
                plan.take_profit,
                amount * plan.entry_price,
                setup,
            )
            return self._build_result(
                symbol=symbol,
                side=plan.side,
                setup=setup,
                quantity=amount,
                fill_price=plan.entry_price,
                plan=plan,
                entry_order=entry_order,
                tp_order=tp_order,
                sl_order=sl_order,
                entry_fee_rate=self._entry_fee_rate(setup),
            )

        mode = self._entry_mode_for_setup(setup)
        if mode == "maker":
            return self._execute_maker_entry(symbol=symbol, plan=plan, setup=setup, amount=amount)
        return self._execute_market_entry(symbol=symbol, plan=plan, setup=setup, amount=amount)
