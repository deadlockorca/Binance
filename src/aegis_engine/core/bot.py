from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from zoneinfo import ZoneInfo
import logging
import time

import pandas as pd

from aegis_engine.analytics.kpi_tracker import KpiTracker
from aegis_engine.core.config import BotConfig
from aegis_engine.exchange.binance_client import BinanceUsdmClient
from aegis_engine.execution.executor import TradeExecutor
from aegis_engine.risk.risk_engine import RiskEngine
from aegis_engine.strategy.trend_signal import build_signal
from aegis_engine.utils.indicators import adx
from aegis_engine.utils.indicators import ema

LOGGER = logging.getLogger(__name__)


@dataclass
class DailyRiskState:
    day_key: str
    day_start_equity: float
    halted: bool = False
    loss_halted: bool = False


@dataclass
class HoldState:
    extended: bool = False
    be_moved: bool = False


class TradingEngine:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.exchange = BinanceUsdmClient(cfg)
        self.risk_engine = RiskEngine(cfg)
        self.executor = TradeExecutor(cfg, self.exchange)
        self.kpi_tracker = KpiTracker(cfg)
        self.last_candle_by_symbol: dict[str, int] = {}
        self.last_bracket_repair_ts: dict[str, float] = {}
        self.hold_state_by_symbol: dict[str, HoldState] = {}
        self.warmup_ready_by_symbol: dict[str, bool] = {}
        self.warmup_progress_by_symbol: dict[str, tuple[int, int]] = {}
        self.daily_state: DailyRiskState | None = None
        self._last_loss_halt_streak = -1
        self._last_runtime_log_ts = 0.0
        self._last_account_total_usdt = 0.0
        self._last_account_free_usdt = 0.0
        self._last_daily_dd_pct = 0.0
        self.tz = ZoneInfo(cfg.app.timezone)

        for symbol in cfg.app.symbols:
            self.exchange.configure_symbol(symbol)
        self.exchange.start_market_streams(cfg.app.symbols)
        self._reconcile_startup_state()
        self._log_boot_summary()

    def _log_boot_summary(self) -> None:
        LOGGER.info(
            "BOOT mode=%s symbols=%s tf_confirm=%s tf_entry=%s setup_priority=%s entry_by_setup=%s leverage=%sx risk_per_trade=%.3f%% sl=%.3f%% tp=%.3f%%",
            self.cfg.app.mode,
            ",".join(self.cfg.app.symbols),
            self.cfg.timeframes.confirm,
            self.cfg.timeframes.entry,
            ",".join(self.cfg.entry.setup_priority),
            self.cfg.execution.entry_order_by_setup,
            self.cfg.risk.leverage,
            self.cfg.risk.risk_per_trade_pct,
            self.cfg.risk.sl_pct,
            self.cfg.risk.tp_pct,
        )

    def _drop_open_candle(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        if not self.cfg.safety.require_candle_close:
            return df
        if len(df) <= 1:
            return df
        tf_seconds = self._timeframe_seconds(timeframe)
        last_ts = pd.Timestamp(df.iloc[-1]["timestamp"])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")

        now_utc = pd.Timestamp.now(tz="UTC")
        candle_end = last_ts + pd.Timedelta(seconds=tf_seconds)

        # Drop only when the last candle has not ended yet.
        if now_utc < candle_end:
            return df.iloc[:-1].copy()
        return df

    def _update_daily_risk(self) -> bool:
        account = self.exchange.fetch_account_snapshot()
        self._last_account_free_usdt = account.free_usdt
        self._last_account_total_usdt = account.total_usdt
        now = datetime.now(tz=self.tz)
        day_key = now.strftime("%Y-%m-%d")

        if self.daily_state is None or self.daily_state.day_key != day_key:
            self.daily_state = DailyRiskState(day_key=day_key, day_start_equity=account.total_usdt, halted=False, loss_halted=False)
            self._last_daily_dd_pct = 0.0
            LOGGER.info("Daily risk reset: day=%s start_equity=%.2f", day_key, account.total_usdt)
            return False

        if self.daily_state.day_start_equity <= 0:
            self._last_daily_dd_pct = 0.0
            return False

        dd_pct = ((self.daily_state.day_start_equity - account.total_usdt) / self.daily_state.day_start_equity) * 100
        self._last_daily_dd_pct = dd_pct
        if dd_pct >= self.cfg.risk.daily_dd_stop_pct:
            self.daily_state.halted = True

        if self.daily_state.halted:
            LOGGER.warning("Trading halted by daily DD guard: dd=%.2f%%", dd_pct)
            return True

        return False

    def _update_consecutive_loss_guard(self) -> bool:
        limit = int(self.cfg.risk.consecutive_loss_stop)
        if limit <= 0:
            return False

        streak = self.kpi_tracker.current_loss_streak()
        halted = streak >= limit

        if self.daily_state is not None:
            self.daily_state.loss_halted = halted

        if halted and self._last_loss_halt_streak != streak:
            LOGGER.warning(
                "Trading halted by consecutive-loss guard: streak=%d limit=%d",
                streak,
                limit,
            )
            self._last_loss_halt_streak = streak
        elif not halted:
            self._last_loss_halt_streak = -1

        return halted

    def _reconcile_startup_state(self) -> None:
        for symbol in self.cfg.app.symbols:
            try:
                position = self.exchange.fetch_open_position(symbol)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Startup reconcile fetch_open_position failed for %s: %s", symbol, exc)
                continue

            active = self.kpi_tracker.get_active_trade(symbol)

            if position is None and active is not None:
                try:
                    self.kpi_tracker.sync_symbol(symbol, self.exchange)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Startup reconcile sync failed for %s: %s", symbol, exc)
                continue

            if position is None:
                continue

            side = str(position.get("side") or "").lower().strip()
            quantity = float(position.get("quantity") or 0.0)
            entry_price = float(position.get("entry_price") or 0.0)
            if side not in {"long", "short"} or quantity <= 0:
                continue
            if entry_price <= 0:
                try:
                    entry_price = self.exchange.mark_price(symbol)
                except Exception:
                    entry_price = 0.0
            if entry_price <= 0:
                continue

            if active is None:
                self.kpi_tracker.register_recovered_position(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    setup="recovered",
                )
                self.hold_state_by_symbol[symbol] = HoldState()
                continue

            if abs(active.quantity - quantity) > 1e-9:
                LOGGER.warning(
                    "Startup reconcile quantity mismatch %s state=%.8f exchange=%.8f. Replacing state.",
                    symbol,
                    active.quantity,
                    quantity,
                )
                self.kpi_tracker.register_recovered_position(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    setup=active.setup or "recovered",
                )

    def _in_entry_close_window(self) -> bool:
        tf_seconds = self._timeframe_seconds(self.cfg.timeframes.entry)
        now = time.time()
        phase = now % max(60, tf_seconds)
        window = max(2, min(15, self.cfg.scheduler.poll_seconds * 2))
        return phase <= window

    def _log_runtime_heartbeat(self, halted_daily: bool | None = None, halted_losses: bool | None = None, force: bool = False) -> None:
        now_ts = time.time()
        interval = max(10, int(self.cfg.logging.runtime_heartbeat_sec))
        if not force and (now_ts - self._last_runtime_log_ts) < interval:
            return
        self._last_runtime_log_ts = now_ts

        day_key = self.daily_state.day_key if self.daily_state is not None else "n/a"
        if halted_daily is None:
            halted_daily = bool(self.daily_state.halted) if self.daily_state is not None else False
        if halted_losses is None:
            halted_losses = bool(self.daily_state.loss_halted) if self.daily_state is not None else False

        open_positions = 0
        try:
            open_positions = self.exchange.count_open_positions()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("count_open_positions failed in heartbeat: %s", exc)

        LOGGER.info(
            "RUNTIME day=%s equity_total=%.2f equity_free=%.2f dd=%.2f%% dd_limit=%.2f%% loss_streak=%d/%d halted_daily=%s halted_loss=%s open_positions=%d tracked_trades=%d",
            day_key,
            self._last_account_total_usdt,
            self._last_account_free_usdt,
            self._last_daily_dd_pct,
            self.cfg.risk.daily_dd_stop_pct,
            self.kpi_tracker.current_loss_streak(),
            self.cfg.risk.consecutive_loss_stop,
            halted_daily,
            halted_losses,
            open_positions,
            len(self.kpi_tracker.active_symbols()),
        )

    def _current_entry_candle_id(self, symbol: str) -> tuple[int, pd.DataFrame]:
        limit = self.cfg.timeframes.warmup_bars + 10
        entry_df = self.exchange.fetch_ohlcv_df(symbol, self.cfg.timeframes.entry, limit)
        entry_df = self._drop_open_candle(entry_df, self.cfg.timeframes.entry)
        if entry_df.empty:
            return -1, entry_df
        ts = entry_df.iloc[-1]["timestamp"]
        return int(pd.Timestamp(ts).value), entry_df

    def _log_warmup_status(self, symbol: str, entry_df: pd.DataFrame, confirm_df: pd.DataFrame, *, is_new_candle: bool) -> None:
        target = max(1, int(self.cfg.timeframes.warmup_bars))
        entry_bars = int(len(entry_df))
        confirm_bars = int(len(confirm_df))
        ready = entry_bars >= target and confirm_bars >= target
        was_ready = bool(self.warmup_ready_by_symbol.get(symbol, False))

        self.warmup_ready_by_symbol[symbol] = ready
        if ready:
            if not was_ready:
                LOGGER.info(
                    "WARMUP_DONE %s target=%d entry_bars=%d confirm_bars=%d",
                    symbol,
                    target,
                    entry_bars,
                    confirm_bars,
                )
            return

        if not is_new_candle:
            return

        progress = (entry_bars, confirm_bars)
        prev_progress = self.warmup_progress_by_symbol.get(symbol)
        if prev_progress == progress:
            return
        self.warmup_progress_by_symbol[symbol] = progress

        LOGGER.info(
            "WARMUP %s target=%d entry_bars=%d confirm_bars=%d missing_entry=%d missing_confirm=%d",
            symbol,
            target,
            entry_bars,
            confirm_bars,
            max(0, target - entry_bars),
            max(0, target - confirm_bars),
        )

    def _repair_position_brackets_if_needed(self, symbol: str) -> None:
        if not self.cfg.execution.use_exchange_tp_sl:
            return
        if not self.cfg.safety.auto_repair_brackets:
            return

        position = self.exchange.fetch_open_position(symbol)
        if not position:
            return

        side = str(position.get("side") or "").lower().strip()
        quantity = float(position.get("quantity") or 0)
        if side not in {"long", "short"} or quantity <= 0:
            return

        has_tp, has_sl = self.exchange.protection_status(symbol, side)
        if has_tp and has_sl:
            return

        if self.cfg.app.mode == "demo":
            # Demo futures may not reliably expose conditional TP/SL via openOrders.
            # Auto-repair based only on openOrders can spam duplicate brackets.
            skip_key = f"{symbol}#demo_repair_skip"
            now_ts = time.time()
            last_log = self.last_bracket_repair_ts.get(skip_key, 0.0)
            if now_ts - last_log >= 300:
                self.last_bracket_repair_ts[skip_key] = now_ts
                LOGGER.warning(
                    "Skip bracket auto-repair for %s in demo mode: missing TP/SL cannot be verified reliably from openOrders",
                    symbol,
                )
            return

        now_ts = time.time()
        cooldown = self.cfg.safety.repair_brackets_cooldown_sec
        last_ts = self.last_bracket_repair_ts.get(symbol, 0.0)
        if now_ts - last_ts < cooldown:
            return
        self.last_bracket_repair_ts[symbol] = now_ts

        entry_price = float(position.get("entry_price") or 0)
        if entry_price <= 0:
            entry_price = self.exchange.mark_price(symbol)
        if entry_price <= 0:
            LOGGER.warning("Cannot repair brackets for %s: invalid entry/mark price", symbol)
            return

        amount = self.exchange.amount_to_precision(symbol, quantity)
        if amount <= 0:
            LOGGER.warning("Cannot repair brackets for %s: quantity below precision", symbol)
            return

        active = self.kpi_tracker.get_active_trade(symbol)
        if active is not None and active.side == side and active.take_profit > 0 and active.stop_loss > 0:
            tp_price = active.take_profit
            sl_price = active.stop_loss
        elif side == "long":
            tp_price = entry_price * (1 + self.cfg.risk.tp_pct / 100)
            sl_price = entry_price * (1 - self.cfg.risk.sl_pct / 100)
        else:
            tp_price = entry_price * (1 - self.cfg.risk.tp_pct / 100)
            sl_price = entry_price * (1 + self.cfg.risk.sl_pct / 100)

        if not has_tp:
            self.exchange.create_tp_trigger(symbol=symbol, side=side, amount=amount, take_profit=tp_price)
        if not has_sl:
            self.exchange.create_sl_trigger(symbol=symbol, side=side, amount=amount, stop_loss=sl_price)

        LOGGER.warning(
            "Repaired brackets for %s: side=%s qty=%s tp_missing=%s sl_missing=%s",
            symbol,
            side,
            amount,
            not has_tp,
            not has_sl,
        )

    def _fetch_market_context(self, symbol: str) -> dict[str, float | None] | None:
        if not self.cfg.market_context.enabled:
            return None
        try:
            snapshot = self.exchange.fetch_market_context(
                symbol=symbol,
                period=self.cfg.market_context.period,
                oi_lookback=self.cfg.market_context.oi_lookback,
            )
            return asdict(snapshot)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Market context fetch failed for %s: %s", symbol, exc)
            return None

    def _timeframe_seconds(self, timeframe: str) -> int:
        text = str(timeframe).strip().lower()
        if not text:
            return 60
        unit = text[-1]
        try:
            value = int(text[:-1])
        except (TypeError, ValueError):
            return 60
        if value <= 0:
            return 60
        if unit == "m":
            return value * 60
        if unit == "h":
            return value * 3600
        if unit == "d":
            return value * 86400
        return 60

    def _close_position_with_reason(self, symbol: str, side: str, quantity: float, reason: str) -> bool:
        amount = self.exchange.amount_to_precision(symbol, quantity)
        if amount <= 0:
            LOGGER.warning("Cannot close %s by %s: quantity below precision", symbol, reason)
            return False

        try:
            close_result = self.exchange.close_position(
                symbol=symbol,
                side=side,
                amount=amount,
                mode=self.cfg.execution.exit_order,
                maker_timeout_sec=self.cfg.execution.maker_timeout_sec,
                maker_poll_sec=self.cfg.execution.maker_poll_interval_sec,
                maker_post_only=self.cfg.execution.maker_post_only,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("close_position failed for %s reason=%s err=%s", symbol, reason, exc)
            return False

        exit_fee_rate = self._exit_fee_rate_from_close_mode(close_result.mode)
        self.kpi_tracker.set_pending_exit(
            symbol=symbol,
            reason=reason,
            order_id=close_result.order_id,
            exit_fee_rate=exit_fee_rate,
        )

        try:
            canceled = self.exchange.cancel_protection_orders(symbol=symbol, position_side=side)
            if canceled > 0:
                LOGGER.info("Canceled %d protection order(s) after closing %s", canceled, symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("cancel_protection_orders failed for %s: %s", symbol, exc)

        LOGGER.warning(
            "Force close %s by %s qty=%s mode=%s order_id=%s",
            symbol,
            reason,
            amount,
            close_result.mode,
            close_result.order_id or "",
        )
        return True

    def _exit_fee_rate_from_close_mode(self, mode: str) -> float:
        if str(mode).lower().strip().startswith("maker"):
            return self.cfg.analytics.fee_maker
        return self.cfg.analytics.fee_taker

    def _move_sl_to_break_even(self, symbol: str, side: str, quantity: float, entry_price: float) -> tuple[str | None, float | None]:
        amount = self.exchange.amount_to_precision(symbol, quantity)
        if amount <= 0:
            return None, None

        be_bps = self.cfg.hold_management.be_buffer_bps / 10000
        if side == "long":
            be_price = entry_price * (1 + be_bps)
        else:
            be_price = entry_price * (1 - be_bps)

        sl_order = self.exchange.replace_stop_loss(
            symbol=symbol,
            side=side,
            amount=amount,
            stop_loss=be_price,
        )
        if not sl_order:
            return None, be_price
        order_id = str(sl_order.get("id") or "")
        return order_id or None, be_price

    def _is_trend_strong_for_extend(
        self,
        side: str,
        entry_df: pd.DataFrame,
        mark_price: float,
        market_ctx: dict[str, float | None] | None,
    ) -> bool:
        if len(entry_df) < max(self.cfg.strategy.ema_fast, self.cfg.strategy.adx_period) + 5:
            return False

        df = entry_df.copy()
        df["ema_fast"] = ema(df["close"], self.cfg.strategy.ema_fast)
        df["adx"] = adx(df, self.cfg.strategy.adx_period)
        df = df.dropna()
        if len(df) < 3:
            return False

        last = df.iloc[-1]
        prev = df.iloc[-2]
        ema_fast = float(last["ema_fast"])
        adx_now = float(last["adx"])
        adx_prev = float(prev["adx"])
        adx_ok = adx_now >= adx_prev and adx_now >= self.cfg.safety.adx_no_trade_below

        if side == "long":
            price_ok = mark_price > ema_fast
        else:
            price_ok = mark_price < ema_fast

        taker_ok = True
        if self.cfg.hold_management.use_taker_for_extend:
            taker_ratio = None
            if market_ctx:
                value = market_ctx.get("taker_buy_sell_ratio")
                try:
                    taker_ratio = float(value) if value is not None else None
                except (TypeError, ValueError):
                    taker_ratio = None

            if taker_ratio is None:
                taker_ok = not self.cfg.market_context.require_data
            elif side == "long":
                taker_ok = taker_ratio >= 1.0
            else:
                taker_ok = taker_ratio <= 1.0

        return price_ok and adx_ok and taker_ok

    def _handle_hold_management(
        self,
        symbol: str,
        entry_df: pd.DataFrame,
        market_ctx: dict[str, float | None] | None,
    ) -> bool:
        hold_cfg = self.cfg.hold_management
        if not hold_cfg.enabled:
            return False

        active = self.kpi_tracker.get_active_trade(symbol)
        if active is None:
            self.hold_state_by_symbol.pop(symbol, None)
            return False

        position = self.exchange.fetch_open_position(symbol)
        if not position:
            self.hold_state_by_symbol.pop(symbol, None)
            return False

        side = str(position.get("side") or "").lower().strip()
        quantity = float(position.get("quantity") or 0)
        if side not in {"long", "short"} or quantity <= 0:
            return False

        setup = str(active.setup or "").lower().strip()
        soft_bars = hold_cfg.soft_timeout_bars.get(setup, 8)
        hard_bars = hold_cfg.hard_timeout_bars.get(setup, max(soft_bars + 1, 12))

        tf_seconds = self._timeframe_seconds(self.cfg.timeframes.entry)
        held_seconds = (datetime.now(timezone.utc) - active.entry_at).total_seconds()
        bars_held = int(max(0, held_seconds) // tf_seconds)

        if bars_held < soft_bars:
            return False

        if bars_held >= hard_bars:
            closed = self._close_position_with_reason(
                symbol=symbol,
                side=side,
                quantity=quantity,
                reason="timeout_hard",
            )
            if closed:
                self.hold_state_by_symbol.pop(symbol, None)
            return closed

        hold_state = self.hold_state_by_symbol.setdefault(symbol, HoldState())
        if hold_state.extended:
            return False

        mark_price = self.exchange.mark_price(symbol)
        if mark_price <= 0:
            return False
        if side == "long":
            pnl_usdt = (mark_price - active.entry_price) * quantity
        else:
            pnl_usdt = (active.entry_price - mark_price) * quantity
        r_now = pnl_usdt / active.expected_loss_usdt if active.expected_loss_usdt > 0 else 0.0

        trend_strong = self._is_trend_strong_for_extend(side=side, entry_df=entry_df, mark_price=mark_price, market_ctx=market_ctx)
        allow_extend = r_now >= hold_cfg.progress_min_r
        if hold_cfg.require_trend_for_extend:
            allow_extend = allow_extend and trend_strong
        else:
            allow_extend = allow_extend or trend_strong

        if allow_extend:
            hold_state.extended = True
            if hold_cfg.move_sl_to_be_on_extend and not hold_state.be_moved:
                sl_order_id, be_price = self._move_sl_to_break_even(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    entry_price=active.entry_price,
                )
                if sl_order_id:
                    hold_state.be_moved = True
                    self.kpi_tracker.update_sl_order_id(symbol, sl_order_id, stop_loss=be_price)
                    LOGGER.info(
                        "Hold extend %s setup=%s bars=%d r=%.3f trend=%s sl->BE order=%s",
                        symbol,
                        setup,
                        bars_held,
                        r_now,
                        trend_strong,
                        sl_order_id,
                    )
                else:
                    LOGGER.warning(
                        "Hold extend %s setup=%s bars=%d r=%.3f trend=%s but failed to move SL to BE",
                        symbol,
                        setup,
                        bars_held,
                        r_now,
                        trend_strong,
                    )
            else:
                LOGGER.info(
                    "Hold extend %s setup=%s bars=%d r=%.3f trend=%s",
                    symbol,
                    setup,
                    bars_held,
                    r_now,
                    trend_strong,
                )
            return False

        if not hold_cfg.close_on_soft_timeout_if_weak:
            return False

        closed = self._close_position_with_reason(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason="timeout_soft_weak",
        )
        if closed:
            self.hold_state_by_symbol.pop(symbol, None)
        return closed

    def _maintenance_symbol(self, symbol: str) -> None:
        try:
            self._repair_position_brackets_if_needed(symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Bracket repair failed for %s: %s", symbol, exc)
        try:
            self.kpi_tracker.sync_symbol(symbol, self.exchange)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("KPI sync failed for %s: %s", symbol, exc)

    def _maintenance_cycle(self) -> None:
        for symbol in self.cfg.app.symbols:
            self._maintenance_symbol(symbol)

    def run_cycle(self) -> None:
        halted_daily = self._update_daily_risk()
        halted_losses = self._update_consecutive_loss_guard()
        halted = halted_daily or halted_losses
        self._log_runtime_heartbeat(halted_daily=halted_daily, halted_losses=halted_losses)

        for symbol in self.cfg.app.symbols:
            candle_id, entry_df = self._current_entry_candle_id(symbol)
            if candle_id < 0:
                self._maintenance_symbol(symbol)
                continue

            is_new_candle = self.last_candle_by_symbol.get(symbol) != candle_id
            if self.cfg.app.mode == "paper" and is_new_candle:
                try:
                    self.exchange.paper_sync_candle(symbol, entry_df.iloc[-1])
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("paper_sync_candle failed for %s: %s", symbol, exc)

            self._maintenance_symbol(symbol)

            if not is_new_candle:
                continue
            self.last_candle_by_symbol[symbol] = candle_id
            last_entry = entry_df.iloc[-1]
            LOGGER.info(
                "CANDLE %s ts=%s close=%.4f volume=%.4f",
                symbol,
                pd.Timestamp(last_entry["timestamp"]).isoformat(),
                float(last_entry["close"]),
                float(last_entry["volume"]),
            )

            confirm_limit = self.cfg.timeframes.warmup_bars + 10
            confirm_df = self.exchange.fetch_ohlcv_df(symbol, self.cfg.timeframes.confirm, confirm_limit)
            confirm_df = self._drop_open_candle(confirm_df, self.cfg.timeframes.confirm)
            self._log_warmup_status(symbol=symbol, entry_df=entry_df, confirm_df=confirm_df, is_new_candle=is_new_candle)
            market_ctx = self._fetch_market_context(symbol)

            if self._handle_hold_management(symbol=symbol, entry_df=entry_df, market_ctx=market_ctx):
                continue

            signal = build_signal(confirm_df=confirm_df, entry_df=entry_df, cfg=self.cfg, market_ctx=market_ctx)
            LOGGER.info(
                "%s signal=%s reason=%s long_score=%d short_score=%d market_ctx=%s",
                symbol,
                signal.side,
                signal.reason,
                signal.long_score,
                signal.short_score,
                market_ctx,
            )

            if signal.side == "flat":
                continue

            if halted:
                LOGGER.info("Skip %s due to risk halt (daily=%s consecutive_loss=%s)", symbol, halted_daily, halted_losses)
                continue

            if self.exchange.has_open_position(symbol):
                LOGGER.info("Skip %s because position already open", symbol)
                continue

            if self.exchange.count_open_positions() >= self.cfg.risk.max_concurrent_positions:
                LOGGER.info("Skip %s due max_concurrent_positions", symbol)
                continue

            account = self.exchange.fetch_account_snapshot()
            entry_price = self.exchange.mark_price(symbol)
            setup = signal.entry_type or ""
            size_multiplier = float(self.cfg.entry.size_multipliers.get(setup, 1.0))
            plan = self.risk_engine.plan_order(
                side=signal.side,
                entry_price=entry_price,
                account=account,
                size_multiplier=size_multiplier,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
            if not plan:
                LOGGER.info("Skip %s due risk plan not valid", symbol)
                continue

            LOGGER.info(
                "%s setup=%s size_multiplier=%.2f planned_notional=%.2f",
                symbol,
                setup,
                size_multiplier,
                plan.notional_usdt,
            )
            result = self.executor.execute(symbol=symbol, plan=plan, setup=setup)
            if result is not None:
                self.kpi_tracker.register_entry(result)
                self.hold_state_by_symbol[result.symbol] = HoldState()

    def run_forever(self) -> None:
        poll = max(1, self.cfg.scheduler.poll_seconds)
        while True:
            try:
                if self.cfg.scheduler.align_to_candle_close and not self._in_entry_close_window():
                    halted_daily: bool | None = None
                    halted_losses: bool | None = None
                    if self.daily_state is None:
                        try:
                            halted_daily = self._update_daily_risk()
                            halted_losses = self._update_consecutive_loss_guard()
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.warning("pre-window risk snapshot failed: %s", exc)
                    self._maintenance_cycle()
                    self._log_runtime_heartbeat(halted_daily=halted_daily, halted_losses=halted_losses)
                else:
                    self.run_cycle()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("run_cycle error: %s", exc)
            time.sleep(poll)

    def close(self) -> None:
        try:
            self.exchange.close_market_streams()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("close_market_streams failed: %s", exc)
