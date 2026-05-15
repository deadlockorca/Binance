from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import re
import threading
import time
from typing import Any

import ccxt
import pandas as pd

from aegis_engine.core.config import BotConfig
from aegis_engine.risk.risk_engine import AccountSnapshot

LOGGER = logging.getLogger(__name__)


def to_ccxt_symbol(symbol: str) -> str:
    if "/" in symbol:
        return symbol
    symbol = symbol.strip().upper()
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    return symbol


def to_binance_symbol_id(symbol: str) -> str:
    text = symbol.strip().upper()
    if not text:
        return text
    text = text.split(":")[0]
    text = text.replace("/", "")
    return text


@dataclass
class MarketContextSnapshot:
    oi_change_pct: float | None = None
    funding_rate: float | None = None
    basis_rate: float | None = None
    taker_buy_sell_ratio: float | None = None
    top_trader_long_short_ratio: float | None = None
    global_long_short_ratio: float | None = None
    depth_imbalance_ratio: float | None = None
    depth_total_notional: float | None = None
    adl_risk: str | None = None
    liquidation_notional_window_usdt: float | None = None
    liquidation_count_window: int | None = None


@dataclass
class PaperPosition:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    tp_price: float
    sl_price: float
    entry_order_id: str
    tp_order_id: str
    sl_order_id: str
    opened_ts_ms: int


@dataclass
class CloseResult:
    order: dict[str, Any] | None
    order_id: str | None
    filled_amount: float
    mode: str


class BinanceUsdmClient:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        ex_cfg = cfg.exchange

        self.exchange_cls = getattr(ccxt, ex_cfg.name)
        self.exchange_kwargs = {
            "apiKey": ex_cfg.api_key,
            "secret": ex_cfg.api_secret,
            "enableRateLimit": ex_cfg.enable_rate_limit,
            "timeout": ex_cfg.timeout,
            "options": ex_cfg.options,
        }
        self.exchange = self.exchange_cls(
            {
                **self.exchange_kwargs,
            }
        )
        self._public_exchange: Any | None = None
        self._market_exchange: Any | None = None
        self._streaming_supported = False
        self._stream_ws_module: Any | None = None
        self._ws_started = False
        self._ws_stop = threading.Event()
        self._ws_lock = threading.Lock()
        self._ws_apps: list[Any] = []
        self._ws_threads: list[threading.Thread] = []
        self._depth_cache: dict[str, tuple[float, float, int]] = {}
        self._liquidation_events: dict[str, list[tuple[int, float]]] = {}
        self._raw_endpoint_cooldown_until_ms: dict[str, int] = {}
        self._raw_endpoint_next_skip_log_ms: dict[str, int] = {}
        self._raw_endpoint_demo_unavailable_logged: set[str] = set()
        self._paper_mode = cfg.app.mode == "paper"
        self._paper_positions: dict[str, PaperPosition] = {}
        self._paper_orders: dict[str, dict[str, Any]] = {}

        if ex_cfg.demo_trading:
            if hasattr(self.exchange, "enable_demo_trading"):
                self.exchange.enable_demo_trading(True)
            elif hasattr(self.exchange, "set_sandbox_mode"):
                # Backward compatibility for older ccxt versions without enable_demo_trading.
                self.exchange.set_sandbox_mode(True)

        self.exchange.load_markets()
        self._init_streaming_capability()

    def _is_demo_trading(self) -> bool:
        return bool(self.cfg.exchange.demo_trading)

    def _market_data_source(self) -> str:
        source = str(self.cfg.market_context.market_data_source).strip().lower()
        if source in {"execution", "live", "demo"}:
            return source
        return "execution"

    def _is_market_data_demo(self) -> bool:
        source = self._market_data_source()
        if source == "execution":
            return self._is_demo_trading()
        return source == "demo"

    def _futures_ws_base_url(self) -> str:
        if self._is_market_data_demo():
            return "wss://demo-fstream.binance.com"
        return "wss://fstream.binance.com"

    def _init_streaming_capability(self) -> None:
        try:
            import websocket  # type: ignore[import-not-found]
        except Exception:
            self._streaming_supported = False
            self._stream_ws_module = None
            return
        self._streaming_supported = True
        self._stream_ws_module = websocket

    def start_market_streams(self, symbols: list[str]) -> None:
        ctx_cfg = self.cfg.market_context
        if not ctx_cfg.ws_enabled:
            return
        if self._ws_started:
            return
        if not self._streaming_supported or self._stream_ws_module is None:
            LOGGER.warning("market streams disabled: websocket-client not installed")
            return

        self._ws_started = True
        ws_symbols = [to_binance_symbol_id(s).lower() for s in symbols if str(s).strip()]
        LOGGER.info(
            "market streams start trade_mode=%s data_source=%s base=%s symbols=%s",
            "demo" if self._is_demo_trading() else "live",
            self._market_data_source(),
            self._futures_ws_base_url(),
            ",".join(ws_symbols),
        )

        if ctx_cfg.ws_liquidation_enabled:
            thread = threading.Thread(target=self._run_liquidation_stream, name="binance-force-order-ws", daemon=True)
            thread.start()
            self._ws_threads.append(thread)

        if ctx_cfg.ws_depth_enabled and ws_symbols:
            thread = threading.Thread(
                target=self._run_depth_stream,
                args=(ws_symbols,),
                name="binance-depth-ws",
                daemon=True,
            )
            thread.start()
            self._ws_threads.append(thread)

    def close_market_streams(self) -> None:
        self._ws_stop.set()
        with self._ws_lock:
            apps = list(self._ws_apps)
        for app in apps:
            try:
                app.close()
            except Exception:
                pass

    def _run_stream_loop(self, name: str, url: str, message_handler: Any) -> None:
        ws_mod = self._stream_ws_module
        if ws_mod is None:
            return

        while not self._ws_stop.is_set():
            def on_message(_ws: Any, message: str) -> None:
                try:
                    payload = json.loads(message)
                except Exception:
                    return
                message_handler(payload)

            def on_error(_ws: Any, err: Any) -> None:
                LOGGER.warning("%s error: %s", name, err)

            def on_close(_ws: Any, status_code: Any, close_msg: Any) -> None:
                if self._ws_stop.is_set():
                    LOGGER.info("%s closed", name)
                    return
                LOGGER.warning("%s closed code=%s msg=%s", name, status_code, close_msg)

            ws_app = ws_mod.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
            with self._ws_lock:
                self._ws_apps.append(ws_app)
            try:
                ws_app.run_forever(ping_interval=150, ping_timeout=20)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("%s run_forever failed: %s", name, exc)
            finally:
                with self._ws_lock:
                    try:
                        self._ws_apps.remove(ws_app)
                    except ValueError:
                        pass
            if not self._ws_stop.is_set():
                time.sleep(3)

    def _run_liquidation_stream(self) -> None:
        url = f"{self._futures_ws_base_url()}/market/ws/!forceOrder@arr"

        def handler(payload: dict[str, Any]) -> None:
            data = payload.get("data", payload)
            if not isinstance(data, dict):
                return
            if str(data.get("e", "")).lower() != "forceorder":
                return
            order = data.get("o", {})
            if not isinstance(order, dict):
                return

            symbol = str(order.get("s") or "").upper()
            if not symbol:
                return
            price = self._safe_float(order.get("ap")) or self._safe_float(order.get("p")) or 0.0
            qty = self._safe_float(order.get("z")) or self._safe_float(order.get("q")) or 0.0
            ts = int(self._safe_float(order.get("T")) or self._safe_float(data.get("E")) or int(time.time() * 1000))
            notional = abs(price * qty)
            if notional <= 0:
                return

            with self._ws_lock:
                events = self._liquidation_events.setdefault(symbol, [])
                events.append((ts, notional))
                cutoff = ts - max(300, int(self.cfg.market_context.liquidation_window_sec) * 2) * 1000
                self._liquidation_events[symbol] = [(t, n) for (t, n) in events if t >= cutoff]

        self._run_stream_loop(name="force-order-stream", url=url, message_handler=handler)

    def _run_depth_stream(self, ws_symbols: list[str]) -> None:
        levels = self.cfg.market_context.depth_levels
        interval = self.cfg.market_context.ws_depth_interval
        stream_name = f"@depth{levels}" if interval == "250ms" else f"@depth{levels}@{interval}"
        streams = "/".join(f"{sym}{stream_name}" for sym in ws_symbols)
        url = f"{self._futures_ws_base_url()}/public/stream?streams={streams}"

        def handler(payload: dict[str, Any]) -> None:
            stream = str(payload.get("stream") or "")
            data = payload.get("data", payload)
            if not isinstance(data, dict):
                return
            symbol = str(data.get("s") or "").upper()
            if not symbol and stream:
                symbol = stream.split("@", 1)[0].upper()
            if not symbol:
                return

            bids = data.get("b") or []
            asks = data.get("a") or []
            bid_notional = 0.0
            ask_notional = 0.0
            for row in bids:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                px = self._safe_float(row[0]) or 0.0
                qty = self._safe_float(row[1]) or 0.0
                bid_notional += px * qty
            for row in asks:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                px = self._safe_float(row[0]) or 0.0
                qty = self._safe_float(row[1]) or 0.0
                ask_notional += px * qty

            if bid_notional <= 0 or ask_notional <= 0:
                return
            ratio = bid_notional / ask_notional
            total_notional = bid_notional + ask_notional
            ts = int(self._safe_float(data.get("E")) or int(time.time() * 1000))
            with self._ws_lock:
                self._depth_cache[symbol] = (ratio, total_notional, ts)

        self._run_stream_loop(name="depth-stream", url=url, message_handler=handler)

    def _depth_from_ws(self, symbol: str, max_age_sec: int) -> tuple[float, float] | None:
        sid = to_binance_symbol_id(symbol)
        now_ms = int(time.time() * 1000)
        with self._ws_lock:
            record = self._depth_cache.get(sid)
        if not record:
            return None
        ratio, total_notional, ts = record
        if now_ms - ts > max(1, max_age_sec) * 1000:
            return None
        return ratio, total_notional

    def _depth_from_rest(self, symbol: str, levels: int) -> tuple[float, float] | None:
        market_symbol = to_ccxt_symbol(symbol)
        market_exchange = self._get_market_exchange()
        try:
            ob = market_exchange.fetch_order_book(market_symbol, limit=levels)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("fetch_order_book failed symbol=%s err=%s", symbol, exc)
            return None
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        bid_notional = 0.0
        ask_notional = 0.0
        for px, qty in bids:
            bid_notional += float(px) * float(qty)
        for px, qty in asks:
            ask_notional += float(px) * float(qty)
        if bid_notional <= 0 or ask_notional <= 0:
            return None
        return bid_notional / ask_notional, bid_notional + ask_notional

    def _liquidation_window_stats(self, symbol: str, window_sec: int) -> tuple[float, int] | None:
        sid = to_binance_symbol_id(symbol)
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - max(1, int(window_sec)) * 1000
        with self._ws_lock:
            events = list(self._liquidation_events.get(sid, []))
        if not events:
            return 0.0, 0
        events = [(ts, n) for (ts, n) in events if ts >= cutoff]
        if not events:
            return 0.0, 0
        total = sum(n for _, n in events)
        return total, len(events)

    def configure_symbol(self, symbol: str) -> None:
        market_symbol = to_ccxt_symbol(symbol)
        try:
            self.exchange.set_margin_mode(self.cfg.risk.margin_mode, market_symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("set_margin_mode failed for %s: %s", market_symbol, exc)
        try:
            self.exchange.set_leverage(self.cfg.risk.leverage, market_symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("set_leverage failed for %s: %s", market_symbol, exc)

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        market_symbol = to_ccxt_symbol(symbol)
        market_exchange = self._get_market_exchange()
        rows = market_exchange.fetch_ohlcv(market_symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        market_exchange = self._get_market_exchange()
        return market_exchange.fetch_ticker(to_ccxt_symbol(symbol))

    def _safe_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _raw_rate_limit_until_ms(self, error_text: str) -> int | None:
        lower = error_text.lower()
        if "-1003" not in error_text and "too many requests" not in lower and "too much request weight" not in lower:
            return None

        match = re.search(r"banned until\s*([0-9][0-9 ]{8,20})", error_text, flags=re.IGNORECASE)
        if match:
            digits = re.sub(r"\s+", "", match.group(1))
            try:
                return int(digits)
            except ValueError:
                pass
        return self._now_ms() + 60_000

    def _activate_raw_cooldown(self, method_name: str, error_text: str) -> bool:
        ban_until = self._raw_rate_limit_until_ms(error_text)
        if ban_until is None:
            return False

        now_ms = self._now_ms()
        if ban_until <= now_ms:
            ban_until = now_ms + 60_000

        prev = int(self._raw_endpoint_cooldown_until_ms.get(method_name, 0))
        if ban_until > prev:
            self._raw_endpoint_cooldown_until_ms[method_name] = ban_until
            wait_sec = max(1, int((ban_until - now_ms + 999) // 1000))
            LOGGER.warning("raw endpoint rate-limited method=%s cooldown=%ss", method_name, wait_sec)
        return True

    def _new_paper_order_id(self, prefix: str) -> str:
        return f"paper-{prefix}-{pd.Timestamp.utcnow().value}"

    def _paper_create_order(
        self,
        *,
        symbol: str,
        order_id: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None = None,
        stop_price: float | None = None,
        reduce_only: bool = False,
        status: str = "open",
        filled: float = 0.0,
        ts_ms: int | None = None,
    ) -> dict[str, Any]:
        ts_ms = ts_ms or self._now_ms()
        info: dict[str, Any] = {
            "orderId": order_id,
            "status": status.upper(),
            "reduceOnly": "true" if reduce_only else "false",
            "updateTime": ts_ms,
        }
        if stop_price is not None:
            info["stopPrice"] = stop_price
        out = {
            "id": order_id,
            "symbol": to_ccxt_symbol(symbol),
            "side": side.lower(),
            "type": order_type,
            "status": status,
            "amount": amount,
            "filled": filled,
            "remaining": max(0.0, amount - filled),
            "price": price,
            "average": price if filled > 0 and price is not None else None,
            "timestamp": ts_ms,
            "lastTradeTimestamp": ts_ms if filled > 0 else None,
            "reduceOnly": reduce_only,
            "info": info,
        }
        self._paper_orders[order_id] = out
        return out

    def _paper_get_order(self, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        order = self._paper_orders.get(str(order_id))
        if order is None:
            return None
        return dict(order)

    def _paper_set_order_status(
        self,
        order_id: str,
        *,
        status: str,
        filled: float | None = None,
        average: float | None = None,
        ts_ms: int | None = None,
    ) -> None:
        order = self._paper_orders.get(order_id)
        if order is None:
            return
        ts_ms = ts_ms or self._now_ms()
        order["status"] = status
        if filled is not None:
            amount = float(order.get("amount") or 0.0)
            order["filled"] = float(filled)
            order["remaining"] = max(0.0, amount - float(filled))
        if average is not None:
            order["average"] = float(average)
        if status == "closed":
            order["lastTradeTimestamp"] = ts_ms
        info = order.setdefault("info", {})
        info["status"] = str(status).upper()
        info["updateTime"] = ts_ms

    def paper_open_position(
        self,
        *,
        symbol: str,
        side: str,
        amount: float,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not self._paper_mode:
            raise RuntimeError("paper_open_position is only available in paper mode")

        now_ms = self._now_ms()
        entry_order_id = self._new_paper_order_id("entry")
        tp_order_id = self._new_paper_order_id("tp")
        sl_order_id = self._new_paper_order_id("sl")
        close_side = "sell" if side == "long" else "buy"

        entry_order = self._paper_create_order(
            symbol=symbol,
            order_id=entry_order_id,
            side="buy" if side == "long" else "sell",
            order_type="market",
            amount=amount,
            price=entry_price,
            reduce_only=False,
            status="closed",
            filled=amount,
            ts_ms=now_ms,
        )
        tp_order = self._paper_create_order(
            symbol=symbol,
            order_id=tp_order_id,
            side=close_side,
            order_type="TAKE_PROFIT_MARKET",
            amount=amount,
            price=None,
            stop_price=take_profit,
            reduce_only=True,
            status="open",
            filled=0.0,
            ts_ms=now_ms,
        )
        sl_order = self._paper_create_order(
            symbol=symbol,
            order_id=sl_order_id,
            side=close_side,
            order_type="STOP_MARKET",
            amount=amount,
            price=None,
            stop_price=stop_loss,
            reduce_only=True,
            status="open",
            filled=0.0,
            ts_ms=now_ms,
        )
        self._paper_positions[symbol] = PaperPosition(
            symbol=symbol,
            side=side,
            quantity=amount,
            entry_price=entry_price,
            tp_price=take_profit,
            sl_price=stop_loss,
            entry_order_id=entry_order_id,
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            opened_ts_ms=now_ms,
        )
        return entry_order, tp_order, sl_order

    def paper_sync_candle(self, symbol: str, candle: pd.Series | dict[str, Any]) -> None:
        if not self._paper_mode:
            return
        position = self._paper_positions.get(symbol)
        if position is None:
            return

        high = float(candle.get("high", 0) or 0)  # type: ignore[arg-type]
        low = float(candle.get("low", 0) or 0)  # type: ignore[arg-type]
        ts_raw = candle.get("timestamp")  # type: ignore[arg-type]
        if isinstance(ts_raw, pd.Timestamp):
            ts_ms = int(ts_raw.value // 1_000_000)
        else:
            ts_ms = self._now_ms()
        if high <= 0 or low <= 0:
            return

        side = position.side
        tp_hit = high >= position.tp_price if side == "long" else low <= position.tp_price
        sl_hit = low <= position.sl_price if side == "long" else high >= position.sl_price
        if not tp_hit and not sl_hit:
            return

        # If both SL/TP hit in same candle we pick SL to stay conservative.
        trigger = "sl" if sl_hit else "tp"
        if trigger == "tp":
            trigger_id = position.tp_order_id
            other_id = position.sl_order_id
            exit_price = position.tp_price
        else:
            trigger_id = position.sl_order_id
            other_id = position.tp_order_id
            exit_price = position.sl_price

        self._paper_set_order_status(trigger_id, status="closed", filled=position.quantity, average=exit_price, ts_ms=ts_ms)
        self._paper_set_order_status(other_id, status="canceled", filled=0.0, ts_ms=ts_ms)
        self._paper_positions.pop(symbol, None)
        LOGGER.info(
            "PAPER_TRIGGER %s reason=%s side=%s qty=%.8f exit=%.4f",
            symbol,
            trigger,
            side,
            position.quantity,
            exit_price,
        )

    def _safe_call_raw(self, method_name: str, params: dict[str, Any]) -> Any | None:
        market_exchange = self._get_market_exchange()
        method = getattr(market_exchange, method_name, None)
        if method is None:
            return None

        now_ms = self._now_ms()
        cooldown_until_ms = int(self._raw_endpoint_cooldown_until_ms.get(method_name, 0))
        if cooldown_until_ms > now_ms:
            next_log_ms = int(self._raw_endpoint_next_skip_log_ms.get(method_name, 0))
            if now_ms >= next_log_ms:
                wait_sec = max(1, int((cooldown_until_ms - now_ms + 999) // 1000))
                LOGGER.info("raw endpoint cooldown active method=%s wait=%ss", method_name, wait_sec)
                self._raw_endpoint_next_skip_log_ms[method_name] = now_ms + 60_000
            return None

        def try_public_fallback() -> Any | None:
            public_exchange = self._get_public_exchange()
            public_method = getattr(public_exchange, method_name, None)
            if public_method is None:
                return None
            try:
                return public_method(params)
            except Exception as public_exc:  # noqa: BLE001
                public_err = str(public_exc)
                if self._is_market_data_demo() and "does not have a testnet/sandbox URL for fapiData endpoints" in public_err:
                    if method_name not in self._raw_endpoint_demo_unavailable_logged:
                        self._raw_endpoint_demo_unavailable_logged.add(method_name)
                        LOGGER.info("raw endpoint unavailable in demo mode: method=%s", method_name)
                    return None
                if self._is_market_data_demo() and method_name == "fapiPublicGetSymbolAdlRisk" and "\"code\":-1121" in public_err:
                    if method_name not in self._raw_endpoint_demo_unavailable_logged:
                        self._raw_endpoint_demo_unavailable_logged.add(method_name)
                        LOGGER.info("raw endpoint unavailable in demo mode: method=%s", method_name)
                    return None
                if self._activate_raw_cooldown(method_name, str(public_exc)):
                    return None
                LOGGER.warning(
                    "raw public endpoint failed method=%s params=%s err=%s",
                    method_name,
                    params,
                    public_exc,
                )
                return None

        try:
            return method(params)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if self._activate_raw_cooldown(method_name, err):
                return None
            if "does not have a testnet/sandbox URL for fapiData endpoints" in err:
                if self._is_market_data_demo():
                    if method_name not in self._raw_endpoint_demo_unavailable_logged:
                        self._raw_endpoint_demo_unavailable_logged.add(method_name)
                        LOGGER.info("raw endpoint unavailable in demo mode: method=%s", method_name)
                    return None
                return try_public_fallback()
            if self._is_market_data_demo() and method_name == "fapiPublicGetSymbolAdlRisk" and "\"code\":-1121" in err:
                if method_name not in self._raw_endpoint_demo_unavailable_logged:
                    self._raw_endpoint_demo_unavailable_logged.add(method_name)
                    LOGGER.info("raw endpoint unavailable in demo mode: method=%s", method_name)
                return None
            if self._is_market_data_demo() and method_name.startswith(("fapiPublicGet", "fapiDataGet")):
                result = try_public_fallback()
                if result is not None:
                    return result
            LOGGER.warning("raw endpoint failed method=%s params=%s err=%s", method_name, params, exc)
            return None

    def _get_market_exchange(self) -> Any:
        if self._market_data_source() == "execution":
            return self.exchange
        if self._market_exchange is None:
            self._market_exchange = self._get_public_exchange()
        return self._market_exchange

    def _get_public_exchange(self) -> Any:
        if self._public_exchange is not None:
            return self._public_exchange

        ex_cfg = self.cfg.exchange
        self._public_exchange = self.exchange_cls(
            {
                "enableRateLimit": ex_cfg.enable_rate_limit,
                "timeout": ex_cfg.timeout,
                "options": ex_cfg.options,
            }
        )
        if self._is_market_data_demo():
            if hasattr(self._public_exchange, "enable_demo_trading"):
                self._public_exchange.enable_demo_trading(True)
            elif hasattr(self._public_exchange, "set_sandbox_mode"):
                self._public_exchange.set_sandbox_mode(True)
        self._public_exchange.load_markets()
        return self._public_exchange

    def fetch_market_context(self, symbol: str, period: str, oi_lookback: int) -> MarketContextSnapshot:
        sid = to_binance_symbol_id(symbol)
        out = MarketContextSnapshot()
        ctx_cfg = self.cfg.market_context

        oi_hist = self._safe_call_raw(
            "fapiDataGetOpenInterestHist",
            {"symbol": sid, "period": period, "limit": max(2, int(oi_lookback))},
        )
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            first = oi_hist[0] or {}
            last = oi_hist[-1] or {}
            first_oi = self._safe_float(first.get("sumOpenInterestValue")) or self._safe_float(first.get("sumOpenInterest"))
            last_oi = self._safe_float(last.get("sumOpenInterestValue")) or self._safe_float(last.get("sumOpenInterest"))
            if first_oi and first_oi > 0 and last_oi is not None:
                out.oi_change_pct = ((last_oi - first_oi) / first_oi) * 100

        funding_hist = self._safe_call_raw("fapiPublicGetFundingRate", {"symbol": sid, "limit": 1})
        if isinstance(funding_hist, list) and funding_hist:
            latest = funding_hist[-1] or {}
            out.funding_rate = self._safe_float(latest.get("fundingRate"))

        basis_hist = self._safe_call_raw(
            "fapiDataGetBasis",
            {
                "pair": sid,
                "contractType": ctx_cfg.basis_contract_type,
                "period": period,
                "limit": 1,
            },
        )
        if isinstance(basis_hist, list) and basis_hist:
            latest = basis_hist[-1] or {}
            out.basis_rate = self._safe_float(latest.get("basisRate"))

        taker_hist = self._safe_call_raw(
            "fapiDataGetTakerlongshortRatio",
            {"symbol": sid, "period": period, "limit": 1},
        )
        if isinstance(taker_hist, list) and taker_hist:
            latest = taker_hist[-1] or {}
            out.taker_buy_sell_ratio = self._safe_float(latest.get("buySellRatio"))

        top_hist = self._safe_call_raw(
            "fapiDataGetTopLongShortPositionRatio",
            {"symbol": sid, "period": period, "limit": 1},
        )
        if isinstance(top_hist, list) and top_hist:
            latest = top_hist[-1] or {}
            out.top_trader_long_short_ratio = self._safe_float(latest.get("longShortRatio"))

        global_hist = self._safe_call_raw(
            "fapiDataGetGlobalLongShortAccountRatio",
            {"symbol": sid, "period": period, "limit": 1},
        )
        if isinstance(global_hist, list) and global_hist:
            latest = global_hist[-1] or {}
            out.global_long_short_ratio = self._safe_float(latest.get("longShortRatio"))

        if ctx_cfg.depth_enabled:
            depth = self._depth_from_ws(symbol=sid, max_age_sec=ctx_cfg.depth_max_age_sec)
            if depth is None and ctx_cfg.depth_rest_fallback:
                depth = self._depth_from_rest(symbol=sid, levels=ctx_cfg.depth_levels)
            if depth is not None:
                out.depth_imbalance_ratio = depth[0]
                out.depth_total_notional = depth[1]

        if ctx_cfg.adl_enabled:
            adl_raw = self._safe_call_raw("fapiPublicGetSymbolAdlRisk", {"symbol": sid})
            if isinstance(adl_raw, dict):
                out.adl_risk = str(adl_raw.get("adlRisk") or "").lower().strip() or None

        if ctx_cfg.liquidation_enabled:
            liq_stats = self._liquidation_window_stats(symbol=sid, window_sec=ctx_cfg.liquidation_window_sec)
            if liq_stats is not None:
                out.liquidation_notional_window_usdt = liq_stats[0]
                out.liquidation_count_window = liq_stats[1]

        return out

    def spread_bps(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        bid = float(ticker.get("bid") or 0)
        ask = float(ticker.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            return 9999
        mid = (bid + ask) / 2
        return ((ask - bid) / mid) * 10000

    def best_bid_ask(self, symbol: str) -> tuple[float, float]:
        ticker = self.fetch_ticker(symbol)
        bid = float(ticker.get("bid") or 0)
        ask = float(ticker.get("ask") or 0)
        return bid, ask

    def mark_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        mark = ticker.get("info", {}).get("markPrice")
        if mark:
            return float(mark)
        last = ticker.get("last")
        if last:
            return float(last)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            return (float(bid) + float(ask)) / 2
        raise RuntimeError(f"Cannot derive mark price for {symbol}")

    def fetch_account_snapshot(self) -> AccountSnapshot:
        balance = self.exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        free_usdt = float(usdt.get("free") or 0)
        total_usdt = float(usdt.get("total") or 0)
        return AccountSnapshot(free_usdt=free_usdt, total_usdt=total_usdt)

    def _positions_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        market_symbol = to_ccxt_symbol(symbol)
        try:
            positions = self.exchange.fetch_positions([market_symbol])
        except Exception:
            positions = self.exchange.fetch_positions()
        out: list[dict[str, Any]] = []
        for pos in positions:
            if pos.get("symbol") == market_symbol:
                out.append(pos)
        return out

    def _extract_position(self, pos: dict[str, Any]) -> dict[str, Any] | None:
        info = pos.get("info", {})
        raw_amt = info.get("positionAmt")
        side = str(pos.get("side") or "").lower().strip()
        qty = 0.0

        if raw_amt is not None:
            try:
                amt = float(raw_amt)
            except (TypeError, ValueError):
                amt = 0.0
            if amt > 0:
                side = "long"
            elif amt < 0:
                side = "short"
            qty = abs(amt)
        else:
            try:
                qty = abs(float(pos.get("contracts") or pos.get("positionAmt") or 0))
            except (TypeError, ValueError):
                qty = 0.0

        if qty <= 0:
            return None

        if side not in {"long", "short"}:
            raw_side = str(info.get("positionSide") or "").upper()
            if raw_side == "LONG":
                side = "long"
            elif raw_side == "SHORT":
                side = "short"
            else:
                side = "long"

        entry_price = 0.0
        for value in [pos.get("entryPrice"), info.get("entryPrice"), pos.get("average"), pos.get("markPrice")]:
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v > 0:
                entry_price = v
                break

        return {"side": side, "quantity": qty, "entry_price": entry_price}

    def fetch_open_position(self, symbol: str) -> dict[str, Any] | None:
        if self._paper_mode:
            pos = self._paper_positions.get(symbol)
            if pos is None:
                return None
            return {"side": pos.side, "quantity": pos.quantity, "entry_price": pos.entry_price}
        positions = self._positions_for_symbol(symbol)
        for pos in positions:
            parsed = self._extract_position(pos)
            if parsed:
                return parsed
        return None

    def count_open_positions(self) -> int:
        if self._paper_mode:
            return len(self._paper_positions)
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return 0
        count = 0
        for pos in positions:
            parsed = self._extract_position(pos)
            if parsed:
                count += 1
        return count

    def has_open_position(self, symbol: str) -> bool:
        return self.fetch_open_position(symbol) is not None

    def fetch_open_orders_safe(self, symbol: str) -> list[dict[str, Any]]:
        if self._paper_mode:
            market_symbol = to_ccxt_symbol(symbol)
            out: list[dict[str, Any]] = []
            for order in self._paper_orders.values():
                if order.get("symbol") != market_symbol:
                    continue
                if str(order.get("status") or "").lower() == "open":
                    out.append(dict(order))
            return out
        market_symbol = to_ccxt_symbol(symbol)
        try:
            orders = self.exchange.fetch_open_orders(market_symbol)
            if isinstance(orders, list):
                return orders
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("fetch_open_orders failed symbol=%s err=%s", symbol, exc)
        return []

    def _is_reduce_only_order(self, order: dict[str, Any]) -> bool:
        info = order.get("info", {})
        for value in [order.get("reduceOnly"), info.get("reduceOnly"), info.get("closePosition")]:
            if isinstance(value, bool):
                if value:
                    return True
            elif value is not None and str(value).lower() == "true":
                return True
        return False

    def _order_kind(self, order: dict[str, Any]) -> str:
        info = order.get("info", {})
        for value in [order.get("type"), info.get("type"), info.get("origType")]:
            if not value:
                continue
            text = str(value).upper()
            if "TAKE_PROFIT" in text:
                return "tp"
            if "STOP" in text:
                return "sl"
        return ""

    def protection_status(self, symbol: str, position_side: str) -> tuple[bool, bool]:
        orders = self.fetch_open_orders_safe(symbol)
        close_side = "sell" if position_side == "long" else "buy"
        has_tp = False
        has_sl = False
        for order in orders:
            side = str(order.get("side") or "").lower()
            if side and side != close_side:
                continue
            if not self._is_reduce_only_order(order):
                continue
            kind = self._order_kind(order)
            if kind == "tp":
                has_tp = True
            elif kind == "sl":
                has_sl = True
            if has_tp and has_sl:
                break
        return has_tp, has_sl

    def cancel_protection_orders(self, symbol: str, position_side: str | None = None) -> int:
        if self._paper_mode:
            canceled = 0
            for order_id, order in list(self._paper_orders.items()):
                if order.get("symbol") != to_ccxt_symbol(symbol):
                    continue
                if str(order.get("status") or "").lower() != "open":
                    continue
                if not self._is_reduce_only_order(order):
                    continue
                side = str(order.get("side") or "").lower().strip()
                if position_side in {"long", "short"}:
                    close_side = "sell" if position_side == "long" else "buy"
                    if side and side != close_side:
                        continue
                self._paper_set_order_status(order_id, status="canceled", filled=0.0)
                canceled += 1
            return canceled

        orders = self.fetch_open_orders_safe(symbol)
        close_side = ""
        if position_side in {"long", "short"}:
            close_side = "sell" if position_side == "long" else "buy"

        canceled = 0
        for order in orders:
            if not self._is_reduce_only_order(order):
                continue
            side = str(order.get("side") or "").lower().strip()
            if close_side and side and side != close_side:
                continue
            order_id = str(order.get("id") or order.get("info", {}).get("orderId") or "")
            if not order_id:
                continue
            if self.cancel_order_safe(symbol, order_id) is not None:
                canceled += 1
        return canceled

    def replace_stop_loss(self, symbol: str, side: str, amount: float, stop_loss: float) -> dict[str, Any] | None:
        if self._paper_mode:
            pos = self._paper_positions.get(symbol)
            if pos is None:
                return None
            close_side = "sell" if side == "long" else "buy"
            for oid, order in list(self._paper_orders.items()):
                if order.get("symbol") != to_ccxt_symbol(symbol):
                    continue
                if str(order.get("status") or "").lower() != "open":
                    continue
                if str(order.get("side") or "").lower().strip() != close_side:
                    continue
                if self._order_kind(order) != "sl":
                    continue
                self._paper_set_order_status(oid, status="canceled", filled=0.0)
            pos.sl_price = stop_loss
            sl_order = self._paper_create_order(
                symbol=symbol,
                order_id=self._new_paper_order_id("sl"),
                side="sell" if side == "long" else "buy",
                order_type="STOP_MARKET",
                amount=amount,
                price=None,
                stop_price=stop_loss,
                reduce_only=True,
                status="open",
                filled=0.0,
            )
            pos.sl_order_id = str(sl_order.get("id") or "")
            return sl_order

        orders = self.fetch_open_orders_safe(symbol)
        close_side = "sell" if side == "long" else "buy"
        for order in orders:
            order_side = str(order.get("side") or "").lower().strip()
            if order_side and order_side != close_side:
                continue
            if not self._is_reduce_only_order(order):
                continue
            if self._order_kind(order) != "sl":
                continue
            order_id = str(order.get("id") or order.get("info", {}).get("orderId") or "")
            if order_id:
                self.cancel_order_safe(symbol, order_id)
        try:
            return self.create_sl_trigger(symbol=symbol, side=side, amount=amount, stop_loss=stop_loss)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("replace_stop_loss failed symbol=%s side=%s err=%s", symbol, side, exc)
            return None

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        market_symbol = to_ccxt_symbol(symbol)
        return float(self.exchange.amount_to_precision(market_symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        market_symbol = to_ccxt_symbol(symbol)
        return float(self.exchange.price_to_precision(market_symbol, price))

    def create_entry_market(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        market_symbol = to_ccxt_symbol(symbol)
        ccxt_side = "buy" if side == "long" else "sell"
        return self.exchange.create_order(
            symbol=market_symbol,
            type="market",
            side=ccxt_side,
            amount=amount,
            params={"newClientOrderId": f"tb-entry-{pd.Timestamp.utcnow().value}"},
        )

    def create_entry_limit(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        post_only: bool = True,
    ) -> dict[str, Any]:
        market_symbol = to_ccxt_symbol(symbol)
        ccxt_side = "buy" if side == "long" else "sell"
        params: dict[str, Any] = {
            "newClientOrderId": f"tb-maker-{pd.Timestamp.utcnow().value}",
        }
        if post_only:
            params["postOnly"] = True
            params["timeInForce"] = "GTX"
        return self.exchange.create_order(
            symbol=market_symbol,
            type="limit",
            side=ccxt_side,
            amount=amount,
            price=price,
            params=params,
        )

    def cancel_order_safe(self, symbol: str, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        if self._paper_mode:
            order = self._paper_orders.get(str(order_id))
            if order is None:
                return None
            self._paper_set_order_status(str(order_id), status="canceled", filled=float(order.get("filled") or 0.0))
            return self._paper_get_order(str(order_id))
        market_symbol = to_ccxt_symbol(symbol)
        try:
            return self.exchange.cancel_order(str(order_id), market_symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("cancel_order failed symbol=%s order_id=%s err=%s", symbol, order_id, exc)
            return None

    def close_position_market(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        if self._paper_mode:
            pos = self._paper_positions.get(symbol)
            if pos is None:
                raise RuntimeError(f"No paper position to close for {symbol}")
            qty = min(float(amount), float(pos.quantity))
            if qty <= 0:
                raise RuntimeError(f"Invalid close amount for {symbol}: {amount}")
            mark = self.mark_price(symbol)
            close_order = self._paper_create_order(
                symbol=symbol,
                order_id=self._new_paper_order_id("close"),
                side="sell" if side == "long" else "buy",
                order_type="market",
                amount=qty,
                price=mark,
                reduce_only=True,
                status="closed",
                filled=qty,
            )
            if qty >= pos.quantity * 0.999999:
                self._paper_set_order_status(pos.tp_order_id, status="canceled", filled=0.0)
                self._paper_set_order_status(pos.sl_order_id, status="canceled", filled=0.0)
                self._paper_positions.pop(symbol, None)
            else:
                pos.quantity = max(0.0, pos.quantity - qty)
            return close_order

        market_symbol = to_ccxt_symbol(symbol)
        close_side = "sell" if side == "long" else "buy"
        return self.exchange.create_order(
            symbol=market_symbol,
            type="market",
            side=close_side,
            amount=amount,
            params={
                "reduceOnly": True,
                "newClientOrderId": f"tb-close-{pd.Timestamp.utcnow().value}",
            },
        )

    def create_tp_trigger(
        self,
        symbol: str,
        side: str,
        amount: float,
        take_profit: float,
    ) -> dict[str, Any]:
        if self._paper_mode:
            pos = self._paper_positions.get(symbol)
            if pos is not None:
                pos.tp_price = take_profit
            tp_order = self._paper_create_order(
                symbol=symbol,
                order_id=self._new_paper_order_id("tp"),
                side="sell" if side == "long" else "buy",
                order_type="TAKE_PROFIT_MARKET",
                amount=amount,
                price=None,
                stop_price=take_profit,
                reduce_only=True,
                status="open",
                filled=0.0,
            )
            if pos is not None:
                pos.tp_order_id = str(tp_order.get("id") or "")
            return tp_order

        market_symbol = to_ccxt_symbol(symbol)
        close_side = "sell" if side == "long" else "buy"
        tp_type = self.cfg.execution.tp_trigger_type
        tp_params = {
            "reduceOnly": True,
            "stopPrice": self.price_to_precision(symbol, take_profit),
            "workingType": "MARK_PRICE",
            "newClientOrderId": f"tb-tp-{pd.Timestamp.utcnow().value}",
        }
        return self.exchange.create_order(
            symbol=market_symbol,
            type=tp_type,
            side=close_side,
            amount=amount,
            price=None,
            params=tp_params,
        )

    def create_sl_trigger(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
    ) -> dict[str, Any]:
        if self._paper_mode:
            pos = self._paper_positions.get(symbol)
            if pos is not None:
                pos.sl_price = stop_loss
            sl_order = self._paper_create_order(
                symbol=symbol,
                order_id=self._new_paper_order_id("sl"),
                side="sell" if side == "long" else "buy",
                order_type="STOP_MARKET",
                amount=amount,
                price=None,
                stop_price=stop_loss,
                reduce_only=True,
                status="open",
                filled=0.0,
            )
            if pos is not None:
                pos.sl_order_id = str(sl_order.get("id") or "")
            return sl_order

        market_symbol = to_ccxt_symbol(symbol)
        close_side = "sell" if side == "long" else "buy"
        sl_type = self.cfg.execution.sl_trigger_type
        sl_params = {
            "reduceOnly": True,
            "stopPrice": self.price_to_precision(symbol, stop_loss),
            "workingType": "MARK_PRICE",
            "newClientOrderId": f"tb-sl-{pd.Timestamp.utcnow().value}",
        }
        return self.exchange.create_order(
            symbol=market_symbol,
            type=sl_type,
            side=close_side,
            amount=amount,
            price=None,
            params=sl_params,
        )

    def create_exit_triggers(
        self,
        symbol: str,
        side: str,
        amount: float,
        take_profit: float,
        stop_loss: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tp_order = self.create_tp_trigger(symbol=symbol, side=side, amount=amount, take_profit=take_profit)
        sl_order = self.create_sl_trigger(symbol=symbol, side=side, amount=amount, stop_loss=stop_loss)
        return tp_order, sl_order

    def fetch_order_safe(self, symbol: str, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None
        if self._paper_mode:
            return self._paper_get_order(order_id)
        market_symbol = to_ccxt_symbol(symbol)
        try:
            return self.exchange.fetch_order(str(order_id), market_symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("fetch_order failed symbol=%s order_id=%s err=%s", symbol, order_id, exc)
            return None

    def order_filled_qty(self, order: dict[str, Any] | None) -> float:
        if not order:
            return 0.0
        info = order.get("info", {})
        return float(order.get("filled") or info.get("executedQty") or 0.0)

    def reference_price(self, symbol: str, side: str) -> float:
        mode = str(self.cfg.execution.ref_price).lower().strip()
        bid, ask = self.best_bid_ask(symbol)
        if mode == "book":
            if side == "long":
                return ask if ask > 0 else self.mark_price(symbol)
            return bid if bid > 0 else self.mark_price(symbol)
        if mode == "best":
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return self.mark_price(symbol)
        return self.mark_price(symbol)

    def maker_close_price(self, symbol: str, side: str) -> float:
        bid, ask = self.best_bid_ask(symbol)
        if bid <= 0 or ask <= 0:
            return 0.0
        offset = max(0.0, float(self.cfg.execution.maker_price_offset_bps)) / 10000
        if side == "long":
            price = ask * (1 - offset)
            floor = bid * 1.00001
            price = max(price, floor)
        else:
            price = bid * (1 + offset)
            cap = ask * 0.99999
            price = min(price, cap)
        return self.price_to_precision(symbol, price)

    def create_close_limit(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        post_only: bool = True,
    ) -> dict[str, Any]:
        market_symbol = to_ccxt_symbol(symbol)
        close_side = "sell" if side == "long" else "buy"
        params: dict[str, Any] = {
            "reduceOnly": True,
            "newClientOrderId": f"tb-close-maker-{pd.Timestamp.utcnow().value}",
        }
        if post_only:
            params["postOnly"] = True
            params["timeInForce"] = "GTX"
        if self._paper_mode:
            return self._paper_create_order(
                symbol=symbol,
                order_id=self._new_paper_order_id("close-maker"),
                side=close_side,
                order_type="limit",
                amount=amount,
                price=price,
                reduce_only=True,
                status="open",
                filled=0.0,
            )
        return self.exchange.create_order(
            symbol=market_symbol,
            type="limit",
            side=close_side,
            amount=amount,
            price=price,
            params=params,
        )

    def wait_order(
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
            latest = self.fetch_order_safe(symbol, order_id)
            if latest is None:
                time.sleep(poll_sec)
                continue
            status = str(latest.get("status") or "").lower()
            if status in {"closed", "canceled"}:
                return latest
            filled = self.order_filled_qty(latest)
            if filled >= target_amount:
                return latest
            time.sleep(poll_sec)
        return self.fetch_order_safe(symbol, order_id) or latest

    def close_position(
        self,
        *,
        symbol: str,
        side: str,
        amount: float,
        mode: str,
        maker_timeout_sec: int,
        maker_poll_sec: float,
        maker_post_only: bool = True,
    ) -> CloseResult:
        mode = str(mode).lower().strip()
        if mode != "maker":
            order = self.close_position_market(symbol=symbol, side=side, amount=amount)
            oid = str(order.get("id") or "") or None
            return CloseResult(order=order, order_id=oid, filled_amount=amount, mode="taker")

        price = self.maker_close_price(symbol=symbol, side=side)
        if price <= 0:
            order = self.close_position_market(symbol=symbol, side=side, amount=amount)
            oid = str(order.get("id") or "") or None
            return CloseResult(order=order, order_id=oid, filled_amount=amount, mode="taker_fallback")

        order = self.create_close_limit(symbol=symbol, side=side, amount=amount, price=price, post_only=maker_post_only)
        order_id = str(order.get("id") or "")
        latest = self.wait_order(
            symbol=symbol,
            order_id=order_id,
            timeout_sec=max(1, int(maker_timeout_sec)),
            poll_sec=max(0.2, float(maker_poll_sec)),
            target_amount=amount,
        )
        if latest is None:
            latest = order
        filled = self.order_filled_qty(latest)
        status = str(latest.get("status") or "").lower()
        if status == "open" or filled < amount:
            self.cancel_order_safe(symbol, order_id)
            refreshed = self.fetch_order_safe(symbol, order_id)
            if refreshed is not None:
                latest = refreshed
                filled = self.order_filled_qty(latest)

        if filled >= amount:
            return CloseResult(order=latest, order_id=order_id or None, filled_amount=filled, mode="maker")

        remaining = max(0.0, amount - filled)
        if remaining > 0:
            market_order = self.close_position_market(symbol=symbol, side=side, amount=remaining)
            mkt_id = str(market_order.get("id") or "") or None
            return CloseResult(order=market_order, order_id=mkt_id, filled_amount=amount, mode="maker_to_taker")
        return CloseResult(order=latest, order_id=order_id or None, filled_amount=filled, mode="maker_partial")

    def fee_for_order(self, symbol: str, order_id: str | None) -> float | None:
        if not order_id:
            return None
        if self._paper_mode:
            return None
        market_symbol = to_ccxt_symbol(symbol)
        params: dict[str, Any] = {}
        try:
            params["orderId"] = int(order_id)
        except (TypeError, ValueError):
            params["orderId"] = order_id
        try:
            trades = self.exchange.fetch_my_trades(market_symbol, limit=50, params=params)
        except Exception:
            return None
        if not isinstance(trades, list):
            return None
        total = 0.0
        found = False
        for trade in trades:
            fee = trade.get("fee") or {}
            cost = fee.get("cost")
            if cost is None:
                info = trade.get("info", {})
                cost = info.get("commission")
            try:
                fee_cost = abs(float(cost))
            except (TypeError, ValueError):
                fee_cost = 0.0
            if fee_cost > 0:
                total += fee_cost
                found = True
        return total if found else None

    def funding_fee_between(self, symbol: str, start_at: datetime, end_at: datetime) -> float:
        if self._paper_mode:
            return 0.0
        method = getattr(self.exchange, "fapiPrivateGetIncome", None)
        if method is None:
            return 0.0
        sid = to_binance_symbol_id(symbol)
        try:
            start_ms = int(start_at.timestamp() * 1000)
            end_ms = int(end_at.timestamp() * 1000)
        except Exception:
            return 0.0
        params = {
            "symbol": sid,
            "incomeType": "FUNDING_FEE",
            "startTime": max(0, start_ms - 1000),
            "endTime": max(start_ms, end_ms + 1000),
            "limit": 100,
        }
        try:
            rows = method(params)
        except Exception:
            return 0.0
        if not isinstance(rows, list):
            return 0.0
        total = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            income = row.get("income")
            try:
                total += float(income)
            except (TypeError, ValueError):
                continue
        return total
