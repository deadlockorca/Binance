from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from aegis_engine.core.config import BotConfig
from aegis_engine.execution.executor import ExecutionResult

LOGGER = logging.getLogger(__name__)


@dataclass
class ActiveTrade:
    symbol: str
    side: str
    setup: str
    entry_at: datetime
    entry_price: float
    quantity: float
    notional_usdt: float
    expected_loss_usdt: float
    expected_profit_usdt: float
    entry_order_id: str | None
    tp_order_id: str | None
    sl_order_id: str | None
    entry_fee_rate: float
    entry_fee_usdt: float
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class PendingExit:
    reason: str
    order_id: str | None = None
    exit_fee_rate: float | None = None


class KpiTracker:
    COLUMNS = [
        "entry_at",
        "closed_at",
        "hold_minutes",
        "symbol",
        "side",
        "setup",
        "entry_price",
        "exit_price",
        "quantity",
        "notional_usdt",
        "gross_pnl_usdt",
        "fees_usdt",
        "funding_usdt",
        "net_pnl_usdt",
        "pnl_usdt",
        "pnl_pct_notional",
        "net_pnl_pct_notional",
        "expected_loss_usdt",
        "expected_profit_usdt",
        "r_multiple",
        "outcome",
        "exit_reason",
        "entry_order_id",
        "tp_order_id",
        "sl_order_id",
        "exit_order_id",
        "entry_fee_usdt",
        "exit_fee_usdt",
    ]

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.active_by_symbol: dict[str, ActiveTrade] = {}
        self.pending_exit_by_symbol: dict[str, PendingExit] = {}
        self.trades_path = Path(cfg.analytics.trades_csv)
        self.state_path = Path(cfg.analytics.active_state_json)
        self.window = cfg.analytics.kpi_window_trades
        self.summary_every_closed = cfg.analytics.summary_every_closed
        self.fee_maker = cfg.analytics.fee_maker
        self.fee_taker = cfg.analytics.fee_taker
        self.funding_apply = cfg.analytics.funding_apply
        self.closed_counter = 0
        self.current_loss_streak_count = 0
        self._ensure_file()
        self._load_state()
        self.current_loss_streak_count = self._load_loss_streak_from_file()

    def _ensure_file(self) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        if self.trades_path.exists():
            try:
                with self.trades_path.open("r", encoding="utf-8") as f:
                    first = f.readline().strip()
                headers = first.split(",") if first else []
            except Exception:
                headers = []
            if headers == self.COLUMNS:
                return
            if headers:
                backup = self.trades_path.with_suffix(f".bak-{int(datetime.now(tz=timezone.utc).timestamp())}.csv")
                self.trades_path.rename(backup)
                LOGGER.warning("Trades CSV header changed. Backed up old file to %s", backup)

        with self.trades_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
            writer.writeheader()

    def _load_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to load active state file %s: %s", self.state_path, exc)
            return

        if not isinstance(payload, list):
            return
        restored: dict[str, ActiveTrade] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            entry_raw = item.get("entry_at")
            try:
                entry_at = datetime.fromisoformat(str(entry_raw))
            except Exception:
                entry_at = datetime.now(timezone.utc)
            if entry_at.tzinfo is None:
                entry_at = entry_at.replace(tzinfo=timezone.utc)
            active = ActiveTrade(
                symbol=symbol,
                side=str(item.get("side") or "").lower().strip() or "long",
                setup=str(item.get("setup") or "unknown").lower().strip(),
                entry_at=entry_at,
                entry_price=float(item.get("entry_price") or 0.0),
                quantity=float(item.get("quantity") or 0.0),
                notional_usdt=float(item.get("notional_usdt") or 0.0),
                expected_loss_usdt=float(item.get("expected_loss_usdt") or 0.0),
                expected_profit_usdt=float(item.get("expected_profit_usdt") or 0.0),
                entry_order_id=self._to_optional_str(item.get("entry_order_id")),
                tp_order_id=self._to_optional_str(item.get("tp_order_id")),
                sl_order_id=self._to_optional_str(item.get("sl_order_id")),
                entry_fee_rate=float(item.get("entry_fee_rate") or 0.0),
                entry_fee_usdt=float(item.get("entry_fee_usdt") or 0.0),
                stop_loss=float(item.get("stop_loss") or 0.0),
                take_profit=float(item.get("take_profit") or 0.0),
            )
            if active.quantity <= 0:
                continue
            if active.notional_usdt <= 0:
                active.notional_usdt = active.quantity * max(active.entry_price, 0.0)
            restored[symbol] = active
        self.active_by_symbol = restored
        if restored:
            LOGGER.warning("Restored %d active trade(s) from state file", len(restored))

    def _save_state(self) -> None:
        rows: list[dict[str, Any]] = []
        for trade in self.active_by_symbol.values():
            row = asdict(trade)
            row["entry_at"] = trade.entry_at.isoformat()
            rows.append(row)
        self.state_path.write_text(json.dumps(rows, ensure_ascii=True, indent=2), encoding="utf-8")

    def _to_optional_str(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def register_entry(self, result: ExecutionResult) -> None:
        setup = (result.setup or "unknown").lower().strip()
        active = ActiveTrade(
            symbol=result.symbol,
            side=result.side,
            setup=setup,
            entry_at=datetime.now(timezone.utc),
            entry_price=float(result.entry_price),
            quantity=float(result.quantity),
            notional_usdt=float(result.notional_usdt),
            expected_loss_usdt=float(result.expected_loss_usdt),
            expected_profit_usdt=float(result.expected_profit_usdt),
            entry_order_id=result.entry_order_id,
            tp_order_id=result.tp_order_id,
            sl_order_id=result.sl_order_id,
            entry_fee_rate=float(result.entry_fee_rate),
            entry_fee_usdt=float(result.entry_fee_usdt),
            stop_loss=float(result.stop_loss),
            take_profit=float(result.take_profit),
        )
        prev = self.active_by_symbol.get(result.symbol)
        if prev is not None:
            LOGGER.warning("Replacing active trade state for %s", result.symbol)
        self.active_by_symbol[result.symbol] = active
        self.pending_exit_by_symbol.pop(result.symbol, None)
        self._save_state()

    def register_recovered_position(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        setup: str = "recovered",
    ) -> None:
        if quantity <= 0 or entry_price <= 0:
            return
        notional = quantity * entry_price
        sl_pct = max(0.0, float(self.cfg.risk.sl_pct)) / 100
        tp_pct = max(0.0, float(self.cfg.risk.tp_pct)) / 100
        active = ActiveTrade(
            symbol=symbol,
            side=side,
            setup=str(setup).lower().strip() or "recovered",
            entry_at=datetime.now(timezone.utc),
            entry_price=entry_price,
            quantity=quantity,
            notional_usdt=notional,
            expected_loss_usdt=notional * sl_pct,
            expected_profit_usdt=notional * tp_pct,
            entry_order_id=None,
            tp_order_id=None,
            sl_order_id=None,
            entry_fee_rate=self.fee_taker,
            entry_fee_usdt=notional * self.fee_taker,
            stop_loss=entry_price * (1 - sl_pct) if side == "long" else entry_price * (1 + sl_pct),
            take_profit=entry_price * (1 + tp_pct) if side == "long" else entry_price * (1 - tp_pct),
        )
        self.active_by_symbol[symbol] = active
        self.pending_exit_by_symbol.pop(symbol, None)
        self._save_state()
        LOGGER.warning("Recovered position into KPI state: %s side=%s qty=%.8f", symbol, side, quantity)

    def get_active_trade(self, symbol: str) -> ActiveTrade | None:
        return self.active_by_symbol.get(symbol)

    def active_symbols(self) -> list[str]:
        return list(self.active_by_symbol.keys())

    def set_pending_exit(
        self,
        symbol: str,
        reason: str,
        order_id: str | None = None,
        exit_fee_rate: float | None = None,
    ) -> None:
        if not reason:
            return
        self.pending_exit_by_symbol[symbol] = PendingExit(reason=reason, order_id=order_id, exit_fee_rate=exit_fee_rate)

    def set_pending_exit_reason(self, symbol: str, reason: str) -> None:
        self.set_pending_exit(symbol=symbol, reason=reason)

    def update_sl_order_id(self, symbol: str, sl_order_id: str | None, stop_loss: float | None = None) -> None:
        active = self.active_by_symbol.get(symbol)
        if active is None:
            return
        active.sl_order_id = sl_order_id
        if stop_loss is not None and stop_loss > 0:
            active.stop_loss = float(stop_loss)
        self._save_state()

    def sync_symbol(self, symbol: str, exchange: Any) -> None:
        active = self.active_by_symbol.get(symbol)
        if active is None:
            return

        if exchange.has_open_position(symbol):
            return

        pending = self.pending_exit_by_symbol.pop(symbol, None)
        close_info = self._resolve_close(symbol, active, exchange, pending=pending)
        self._record_closed_trade(active, close_info, exchange=exchange)
        self.active_by_symbol.pop(symbol, None)
        self._save_state()

        self.closed_counter += 1
        if self.closed_counter % self.summary_every_closed == 0:
            self.log_kpi_summary()

    def _resolve_close(
        self,
        symbol: str,
        active: ActiveTrade,
        exchange: Any,
        pending: PendingExit | None = None,
    ) -> dict[str, Any]:
        tp_order = exchange.fetch_order_safe(symbol, active.tp_order_id)
        sl_order = exchange.fetch_order_safe(symbol, active.sl_order_id)

        tp_filled = self._is_filled(tp_order)
        sl_filled = self._is_filled(sl_order)

        if tp_filled and not sl_filled:
            return self._close_from_order(tp_order, "tp", exit_fee_rate=self._default_exit_fee_rate("tp", pending))
        if sl_filled and not tp_filled:
            return self._close_from_order(sl_order, "sl", exit_fee_rate=self._default_exit_fee_rate("sl", pending))
        if tp_filled and sl_filled:
            tp_ts = self._order_timestamp(tp_order)
            sl_ts = self._order_timestamp(sl_order)
            if sl_ts and tp_ts and sl_ts > tp_ts:
                return self._close_from_order(sl_order, "sl", exit_fee_rate=self._default_exit_fee_rate("sl", pending))
            return self._close_from_order(tp_order, "tp", exit_fee_rate=self._default_exit_fee_rate("tp", pending))

        if pending and pending.order_id:
            pending_order = exchange.fetch_order_safe(symbol, pending.order_id)
            if self._is_filled(pending_order):
                return self._close_from_order(
                    pending_order,
                    pending.reason,
                    exit_fee_rate=self._default_exit_fee_rate(pending.reason, pending),
                )

        # Fallback: position has been closed but trigger status is unclear.
        mark = exchange.mark_price(symbol)
        fallback_reason = pending.reason if pending else "unknown"
        return {
            "exit_reason": fallback_reason or "unknown",
            "exit_price": float(mark),
            "closed_at": datetime.now(timezone.utc),
            "exit_order_id": pending.order_id if pending else None,
            "exit_fee_rate": self._default_exit_fee_rate(fallback_reason, pending),
        }

    def _default_exit_fee_rate(self, reason: str, pending: PendingExit | None = None) -> float:
        if pending and pending.exit_fee_rate is not None:
            return max(0.0, float(pending.exit_fee_rate))
        reason_l = str(reason or "").lower().strip()
        if reason_l in {"tp", "sl"}:
            tp_market = "MARKET" in str(self.cfg.execution.tp_trigger_type).upper()
            sl_market = "MARKET" in str(self.cfg.execution.sl_trigger_type).upper()
            if reason_l == "tp":
                return self.fee_taker if tp_market else self.fee_maker
            return self.fee_taker if sl_market else self.fee_maker
        if str(self.cfg.execution.exit_order).lower().strip() == "maker":
            return self.fee_maker
        return self.fee_taker

    def _close_from_order(
        self,
        order: dict[str, Any] | None,
        reason: str,
        exit_fee_rate: float,
    ) -> dict[str, Any]:
        if order is None:
            return {
                "exit_reason": reason,
                "exit_price": 0.0,
                "closed_at": datetime.now(timezone.utc),
                "exit_order_id": None,
                "exit_fee_rate": exit_fee_rate,
            }
        return {
            "exit_reason": reason,
            "exit_price": float(self._order_price(order) or 0.0),
            "closed_at": self._order_timestamp(order) or datetime.now(timezone.utc),
            "exit_order_id": str(order.get("id") or order.get("info", {}).get("orderId") or "") or None,
            "exit_fee_rate": exit_fee_rate,
        }

    def _record_closed_trade(self, active: ActiveTrade, close_info: dict[str, Any], exchange: Any) -> None:
        exit_price = float(close_info.get("exit_price") or 0.0)
        closed_at = close_info.get("closed_at")
        if not isinstance(closed_at, datetime):
            closed_at = datetime.now(timezone.utc)

        if active.side == "long":
            gross_pnl_usdt = (exit_price - active.entry_price) * active.quantity
        else:
            gross_pnl_usdt = (active.entry_price - exit_price) * active.quantity

        exit_order_id = self._to_optional_str(close_info.get("exit_order_id"))
        exit_fee_rate = max(0.0, float(close_info.get("exit_fee_rate") or 0.0))
        entry_fee_usdt = max(0.0, float(active.entry_fee_usdt))

        exit_fee_usdt: float
        exit_fee_from_exchange = None
        fee_method = getattr(exchange, "fee_for_order", None)
        if callable(fee_method):
            try:
                exit_fee_from_exchange = fee_method(active.symbol, exit_order_id)
            except Exception:  # noqa: BLE001
                exit_fee_from_exchange = None
        if exit_fee_from_exchange is None:
            exit_notional = abs(exit_price * active.quantity)
            exit_fee_usdt = exit_notional * exit_fee_rate
        else:
            exit_fee_usdt = max(0.0, float(exit_fee_from_exchange))

        fees_usdt = entry_fee_usdt + exit_fee_usdt

        funding_usdt = 0.0
        if self.funding_apply:
            funding_method = getattr(exchange, "funding_fee_between", None)
            if callable(funding_method):
                try:
                    funding_usdt = float(funding_method(active.symbol, active.entry_at, closed_at))
                except Exception:  # noqa: BLE001
                    funding_usdt = 0.0

        net_pnl_usdt = gross_pnl_usdt - fees_usdt + funding_usdt
        pnl_pct_notional = (net_pnl_usdt / active.notional_usdt) * 100 if active.notional_usdt > 0 else 0.0
        net_pnl_pct_notional = pnl_pct_notional
        r_multiple = net_pnl_usdt / active.expected_loss_usdt if active.expected_loss_usdt > 0 else 0.0
        hold_minutes = max(0.0, (closed_at - active.entry_at).total_seconds() / 60.0)

        if net_pnl_usdt > 0:
            outcome = "win"
        elif net_pnl_usdt < 0:
            outcome = "loss"
        else:
            outcome = "flat"

        row = {
            "entry_at": active.entry_at.isoformat(),
            "closed_at": closed_at.isoformat(),
            "hold_minutes": round(hold_minutes, 3),
            "symbol": active.symbol,
            "side": active.side,
            "setup": active.setup,
            "entry_price": round(active.entry_price, 8),
            "exit_price": round(exit_price, 8),
            "quantity": round(active.quantity, 8),
            "notional_usdt": round(active.notional_usdt, 8),
            "gross_pnl_usdt": round(gross_pnl_usdt, 8),
            "fees_usdt": round(fees_usdt, 8),
            "funding_usdt": round(funding_usdt, 8),
            "net_pnl_usdt": round(net_pnl_usdt, 8),
            "pnl_usdt": round(net_pnl_usdt, 8),
            "pnl_pct_notional": round(pnl_pct_notional, 6),
            "net_pnl_pct_notional": round(net_pnl_pct_notional, 6),
            "expected_loss_usdt": round(active.expected_loss_usdt, 8),
            "expected_profit_usdt": round(active.expected_profit_usdt, 8),
            "r_multiple": round(r_multiple, 6),
            "outcome": outcome,
            "exit_reason": str(close_info.get("exit_reason") or "unknown"),
            "entry_order_id": active.entry_order_id or "",
            "tp_order_id": active.tp_order_id or "",
            "sl_order_id": active.sl_order_id or "",
            "exit_order_id": exit_order_id or "",
            "entry_fee_usdt": round(entry_fee_usdt, 8),
            "exit_fee_usdt": round(exit_fee_usdt, 8),
        }

        with self.trades_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.COLUMNS)
            writer.writerow(row)

        if outcome == "loss":
            self.current_loss_streak_count += 1
        elif outcome == "win":
            self.current_loss_streak_count = 0

        LOGGER.info(
            "CLOSE %s setup=%s reason=%s gross=%.4f net=%.4f fees=%.4f funding=%.4f r=%.3f",
            active.symbol,
            active.setup,
            row["exit_reason"],
            row["gross_pnl_usdt"],
            row["net_pnl_usdt"],
            row["fees_usdt"],
            row["funding_usdt"],
            row["r_multiple"],
        )

    def _is_filled(self, order: dict[str, Any] | None) -> bool:
        if not order:
            return False
        status = str(order.get("status") or "").lower()
        if status == "closed":
            return True
        info = order.get("info", {})
        raw_status = str(info.get("status") or "").upper()
        if raw_status == "FILLED":
            return True
        filled = float(order.get("filled") or info.get("executedQty") or 0)
        remaining = order.get("remaining")
        if remaining is not None:
            try:
                if filled > 0 and float(remaining) <= 0:
                    return True
            except (TypeError, ValueError):
                return False
        return False

    def _order_price(self, order: dict[str, Any]) -> float | None:
        info = order.get("info", {})
        values = [
            order.get("average"),
            info.get("avgPrice"),
            order.get("price"),
            info.get("price"),
            info.get("stopPrice"),
        ]
        for value in values:
            if value is None:
                continue
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                continue
            if value_f > 0:
                return value_f
        return None

    def _order_timestamp(self, order: dict[str, Any] | None) -> datetime | None:
        if not order:
            return None
        raw = order.get("lastTradeTimestamp") or order.get("timestamp")
        if raw is None:
            raw = order.get("info", {}).get("updateTime")
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    def _load_loss_streak_from_file(self) -> int:
        if not self.trades_path.exists():
            return 0
        try:
            df = pd.read_csv(self.trades_path)
        except Exception:
            return 0
        if df.empty or "outcome" not in df.columns:
            return 0
        streak = 0
        for outcome in reversed(df["outcome"].astype(str).tolist()):
            out = outcome.strip().lower()
            if out == "loss":
                streak += 1
                continue
            if out == "win":
                break
        return streak

    def current_loss_streak(self) -> int:
        return max(0, int(self.current_loss_streak_count))

    def log_kpi_summary(self) -> None:
        if not self.trades_path.exists():
            return
        df = pd.read_csv(self.trades_path)
        if df.empty:
            return

        window_df = df.tail(self.window)
        overall = self._metrics(window_df)
        LOGGER.info(
            "KPI overall last_%d trades=%d winrate=%.2f%% expectancyR=%.3f net_pnl=%.3f",
            self.window,
            int(overall["trades"]),
            overall["winrate_pct"],
            overall["expectancy_r"],
            overall["pnl_total"],
        )

        for setup, grp in window_df.groupby("setup"):
            m = self._metrics(grp)
            LOGGER.info(
                "KPI setup=%s trades=%d winrate=%.2f%% expectancyR=%.3f net_pnl=%.3f",
                setup,
                int(m["trades"]),
                m["winrate_pct"],
                m["expectancy_r"],
                m["pnl_total"],
            )

    def _metrics(self, df: pd.DataFrame) -> dict[str, float]:
        if df.empty:
            return {
                "trades": 0,
                "winrate_pct": 0.0,
                "expectancy_r": 0.0,
                "pnl_total": 0.0,
            }

        pnl_col = "pnl_usdt" if "pnl_usdt" in df.columns else "net_pnl_usdt"
        pnl = pd.to_numeric(df[pnl_col], errors="coerce").fillna(0.0)
        rvals = pd.to_numeric(df["r_multiple"], errors="coerce").fillna(0.0)
        wins = int((pnl > 0).sum())
        trades = int(len(df))
        winrate_pct = (wins / trades * 100) if trades > 0 else 0.0
        return {
            "trades": float(trades),
            "winrate_pct": float(winrate_pct),
            "expectancy_r": float(rvals.mean()),
            "pnl_total": float(pnl.sum()),
        }
