from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

from dotenv import load_dotenv
import yaml


@dataclass
class AppConfig:
    name: str
    mode: str
    timezone: str
    symbols: list[str]


@dataclass
class TimeframesConfig:
    confirm: str
    entry: str
    warmup_bars: int
    min_confirm_bars: int


@dataclass
class StrategyConfig:
    ema_fast: int
    ema_slow: int
    rsi_period: int
    adx_period: int
    atr_period: int
    volume_ma_period: int
    breakout_lookback: int
    breakout_buffer_bps: float


@dataclass
class RangeTradingConfig:
    enabled: bool
    lookback: int
    min_touches: int
    edge_buffer_bps: float
    breakout_buffer_bps: float
    max_adx: float
    max_ema_spread_bps: float
    min_width_bps: float
    max_width_bps: float
    min_width_atr: float
    rsi_long_max: float
    rsi_short_min: float
    min_reward_r: float
    sl_atr_buffer: float
    tp_mid_buffer_bps: float


@dataclass
class SideFilter:
    rsi_min: float | None = None
    rsi_max: float | None = None
    adx_min: float = 0
    volume_multiplier: float = 1.0


@dataclass
class FiltersConfig:
    min_confluence_score: int
    score_gap_min: int
    opponent_score_max: int
    long: SideFilter
    short: SideFilter


@dataclass
class RetestConfig:
    max_bars_since_breakout: int
    touch_buffer_bps: float
    reclaim_buffer_bps: float
    require_ema_slope: bool


@dataclass
class CandleFilterConfig:
    enabled: bool
    min_body_bps: float
    min_body_atr: float
    max_total_wick_ratio: float
    max_opposite_wick_ratio: float
    min_rejection_wick_ratio: float
    volume_min_mult: float
    breakout_require_directional_body: bool
    pullback_require_rejection_wick: bool
    retest_require_rejection_wick: bool


@dataclass
class EntryConfig:
    allow_retest: bool
    allow_pullback: bool
    allow_breakout: bool
    rsi_long_th: float
    rsi_short_th: float
    setup_priority: list[str]
    size_multipliers: dict[str, float]
    retest: RetestConfig
    candle_filter: CandleFilterConfig
    max_dist_ema_bps: float


@dataclass
class MarketContextConfig:
    enabled: bool
    require_data: bool
    market_data_source: str
    period: str
    oi_lookback: int
    min_oi_change_pct: float
    funding_abs_max: float
    basis_contract_type: str
    basis_abs_max: float
    taker_ratio_long_min: float
    taker_ratio_short_max: float
    global_long_ratio_max: float
    global_short_ratio_min: float
    top_trader_long_ratio_max: float
    top_trader_short_ratio_min: float
    depth_enabled: bool
    depth_levels: int
    depth_long_min_ratio: float
    depth_short_max_ratio: float
    depth_max_age_sec: int
    depth_rest_fallback: bool
    liquidation_enabled: bool
    liquidation_window_sec: int
    liquidation_max_usdt: float
    adl_enabled: bool
    adl_block_levels: list[str]
    ws_enabled: bool
    ws_depth_enabled: bool
    ws_liquidation_enabled: bool
    ws_depth_interval: str


@dataclass
class HoldManagementConfig:
    enabled: bool
    soft_timeout_bars: dict[str, int]
    hard_timeout_bars: dict[str, int]
    progress_min_r: float
    move_sl_to_be_on_extend: bool
    be_buffer_bps: float
    require_trend_for_extend: bool
    use_taker_for_extend: bool
    close_on_soft_timeout_if_weak: bool


@dataclass
class RiskConfig:
    leverage: int
    margin_mode: str
    sizing_mode: str
    risk_per_trade_pct: float
    margin_per_trade_usdt: float
    margin_per_trade_pct: float
    max_notional_usdt: float
    max_concurrent_positions: int
    daily_dd_stop_pct: float
    consecutive_loss_stop: int
    min_free_usdt_to_trade: float
    min_notional_usdt: float
    sl_pct: float
    tp_pct: float


@dataclass
class ExecutionConfig:
    entry_order: str
    entry_order_by_setup: dict[str, str]
    exit_order: str
    ref_price: str
    max_spread_bps: float
    max_slippage_bps: float
    maker_timeout_sec: int
    maker_poll_interval_sec: float
    maker_min_fill_ratio: float
    maker_partial_fill_action: str
    maker_max_reprice: int
    maker_post_only: bool
    maker_price_offset_bps: float
    use_exchange_tp_sl: bool
    tp_trigger_type: str
    sl_trigger_type: str


@dataclass
class SafetyConfig:
    require_candle_close: bool
    min_liquidity_usd: float
    adx_no_trade_below: float
    min_atr_usd: float
    min_atr_pct: float
    max_atr_pct: float
    auto_repair_brackets: bool
    repair_brackets_cooldown_sec: int


@dataclass
class SchedulerConfig:
    align_to_candle_close: bool
    poll_seconds: int


@dataclass
class LoggingConfig:
    level: str
    file_live: str
    rich_console: bool
    runtime_heartbeat_sec: int


@dataclass
class AnalyticsConfig:
    trades_csv: str
    kpi_window_trades: int
    summary_every_closed: int
    fee_maker: float
    fee_taker: float
    funding_apply: bool
    active_state_json: str


@dataclass
class ExchangeConfig:
    name: str
    market_type: str
    settle: str
    demo_trading: bool
    api_key: str
    api_secret: str
    enable_rate_limit: bool
    timeout: int
    options: dict[str, Any]


@dataclass
class BotConfig:
    app: AppConfig
    timeframes: TimeframesConfig
    strategy: StrategyConfig
    range_trading: RangeTradingConfig
    filters: FiltersConfig
    entry: EntryConfig
    market_context: MarketContextConfig
    hold_management: HoldManagementConfig
    risk: RiskConfig
    execution: ExecutionConfig
    safety: SafetyConfig
    scheduler: SchedulerConfig
    logging: LoggingConfig
    analytics: AnalyticsConfig
    exchange: ExchangeConfig


class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")
    return data


def _pick_exchange(raw: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode in {"demo", "paper"}:
        section = raw.get("exchange_demo")
    else:
        section = raw.get("exchange")
    if not isinstance(section, dict):
        raise ConfigError("Missing exchange section for current app.mode")
    return section


def _must_env(var_name: str) -> str:
    value = os.getenv(var_name, "").strip()
    if not value:
        raise ConfigError(f"Missing environment variable: {var_name}")
    return value


def _build_exchange(section: dict[str, Any]) -> ExchangeConfig:
    ccxt_cfg = section.get("ccxt", {})
    if not isinstance(ccxt_cfg, dict):
        raise ConfigError("exchange.ccxt must be a mapping")

    api_key_env = str(ccxt_cfg.get("api_key_env", "")).strip()
    api_secret_env = str(ccxt_cfg.get("api_secret_env", "")).strip()
    if not api_key_env or not api_secret_env:
        raise ConfigError("exchange.ccxt api_key_env/api_secret_env are required")

    return ExchangeConfig(
        name=str(section.get("name", "binanceusdm")),
        market_type=str(section.get("market_type", "swap")),
        settle=str(section.get("settle", "USDT")),
        demo_trading=bool(section.get("demo_trading", False)),
        api_key=_must_env(api_key_env),
        api_secret=_must_env(api_secret_env),
        enable_rate_limit=bool(ccxt_cfg.get("enableRateLimit", True)),
        timeout=int(ccxt_cfg.get("timeout", 20000)),
        options=dict(ccxt_cfg.get("options", {})),
    )


def load_config(
    path: str | Path = "configs/config.yaml",
    mode_override: str | None = None,
    symbols_override: list[str] | None = None,
    market_data_source_override: str | None = None,
) -> BotConfig:
    load_dotenv()
    raw = _read_yaml(Path(path))

    app_raw = raw.get("app", {})
    app = AppConfig(
        name=str(app_raw.get("name", "aegis-engine")),
        mode=str(app_raw.get("mode", "demo")).lower(),
        timezone=str(app_raw.get("timezone", "UTC")),
        symbols=[str(x) for x in app_raw.get("symbols", ["BTCUSDT"])],
    )
    if mode_override:
        app.mode = mode_override.lower().strip()
    if symbols_override:
        parsed_symbols = [str(x).upper().strip() for x in symbols_override if str(x).strip()]
        if not parsed_symbols:
            raise ConfigError("symbols_override is provided but empty")
        app.symbols = parsed_symbols

    if app.mode not in {"paper", "demo", "live"}:
        raise ConfigError("app.mode must be one of: paper, demo, live")

    tf_raw = raw.get("timeframes", {})
    timeframes = TimeframesConfig(
        confirm=str(tf_raw.get("confirm", "1h")),
        entry=str(tf_raw.get("entry", "15m")),
        warmup_bars=int(tf_raw.get("warmup_bars", 250)),
        min_confirm_bars=int(tf_raw.get("min_confirm_bars", 30)),
    )

    st_raw = raw.get("strategy", {})
    strategy = StrategyConfig(
        ema_fast=int(st_raw.get("ema_fast", 20)),
        ema_slow=int(st_raw.get("ema_slow", 50)),
        rsi_period=int(st_raw.get("rsi_period", 14)),
        adx_period=int(st_raw.get("adx_period", 14)),
        atr_period=int(st_raw.get("atr_period", 14)),
        volume_ma_period=int(st_raw.get("volume_ma_period", 20)),
        breakout_lookback=int(st_raw.get("breakout_lookback", 20)),
        breakout_buffer_bps=float(st_raw.get("breakout_buffer_bps", 5)),
    )

    range_raw = raw.get("range_trading", {})
    if not isinstance(range_raw, dict):
        range_raw = {}
    range_trading = RangeTradingConfig(
        enabled=bool(range_raw.get("enabled", True)),
        lookback=max(20, int(range_raw.get("lookback", 64))),
        min_touches=max(1, int(range_raw.get("min_touches", 2))),
        edge_buffer_bps=max(0.0, float(range_raw.get("edge_buffer_bps", 18))),
        breakout_buffer_bps=max(0.0, float(range_raw.get("breakout_buffer_bps", 8))),
        max_adx=max(0.0, float(range_raw.get("max_adx", 18))),
        max_ema_spread_bps=max(0.0, float(range_raw.get("max_ema_spread_bps", 35))),
        min_width_bps=max(0.0, float(range_raw.get("min_width_bps", 70))),
        max_width_bps=max(0.0, float(range_raw.get("max_width_bps", 260))),
        min_width_atr=max(0.0, float(range_raw.get("min_width_atr", 2.2))),
        rsi_long_max=float(range_raw.get("rsi_long_max", 43)),
        rsi_short_min=float(range_raw.get("rsi_short_min", 57)),
        min_reward_r=max(0.0, float(range_raw.get("min_reward_r", 1.1))),
        sl_atr_buffer=max(0.0, float(range_raw.get("sl_atr_buffer", 0.35))),
        tp_mid_buffer_bps=max(0.0, float(range_raw.get("tp_mid_buffer_bps", 4))),
    )

    flt_raw = raw.get("filters", {})
    long_raw = flt_raw.get("long", {})
    short_raw = flt_raw.get("short", {})
    filters = FiltersConfig(
        min_confluence_score=int(flt_raw.get("min_confluence_score", 3)),
        score_gap_min=max(0, int(flt_raw.get("score_gap_min", 2))),
        opponent_score_max=max(0, int(flt_raw.get("opponent_score_max", 2))),
        long=SideFilter(
            rsi_min=float(long_raw.get("rsi_min", 55)),
            adx_min=float(long_raw.get("adx_min", 20)),
            volume_multiplier=float(long_raw.get("volume_multiplier", 1.2)),
        ),
        short=SideFilter(
            rsi_max=float(short_raw.get("rsi_max", 45)),
            adx_min=float(short_raw.get("adx_min", 20)),
            volume_multiplier=float(short_raw.get("volume_multiplier", 1.2)),
        ),
    )

    ent_raw = raw.get("entry", {})
    setup_priority_raw = ent_raw.get("setup_priority", ["retest", "pullback", "breakout"])
    if not isinstance(setup_priority_raw, list):
        setup_priority_raw = ["retest", "pullback", "breakout"]
    setup_priority = [str(x).lower().strip() for x in setup_priority_raw if str(x).strip()]
    if not setup_priority:
        setup_priority = ["retest", "pullback", "breakout"]

    size_mult_raw = ent_raw.get("size_multipliers", {})
    size_multipliers = {"retest": 1.0, "pullback": 0.7, "breakout": 0.4, "range_revert": 0.6}
    if isinstance(size_mult_raw, dict):
        for key, value in size_mult_raw.items():
            try:
                size_multipliers[str(key).lower().strip()] = float(value)
            except (TypeError, ValueError):
                continue

    retest_raw = ent_raw.get("retest", {})
    if not isinstance(retest_raw, dict):
        retest_raw = {}
    candle_raw = ent_raw.get("candle_filter", {})
    if not isinstance(candle_raw, dict):
        candle_raw = {}

    entry = EntryConfig(
        allow_retest=bool(ent_raw.get("allow_retest", True)),
        allow_pullback=bool(ent_raw.get("allow_pullback", True)),
        allow_breakout=bool(ent_raw.get("allow_breakout", True)),
        rsi_long_th=float(ent_raw.get("rsi_long_th", 55)),
        rsi_short_th=float(ent_raw.get("rsi_short_th", 45)),
        setup_priority=setup_priority,
        size_multipliers=size_multipliers,
        retest=RetestConfig(
            max_bars_since_breakout=int(retest_raw.get("max_bars_since_breakout", 6)),
            touch_buffer_bps=float(retest_raw.get("touch_buffer_bps", 12)),
            reclaim_buffer_bps=float(retest_raw.get("reclaim_buffer_bps", 3)),
            require_ema_slope=bool(retest_raw.get("require_ema_slope", True)),
        ),
        candle_filter=CandleFilterConfig(
            enabled=bool(candle_raw.get("enabled", True)),
            min_body_bps=max(0.0, float(candle_raw.get("min_body_bps", 4.0))),
            min_body_atr=max(0.0, float(candle_raw.get("min_body_atr", 0.12))),
            max_total_wick_ratio=min(1.0, max(0.0, float(candle_raw.get("max_total_wick_ratio", 0.85)))),
            max_opposite_wick_ratio=min(1.0, max(0.0, float(candle_raw.get("max_opposite_wick_ratio", 0.55)))),
            min_rejection_wick_ratio=min(1.0, max(0.0, float(candle_raw.get("min_rejection_wick_ratio", 0.12)))),
            volume_min_mult=max(0.0, float(candle_raw.get("volume_min_mult", 0.85))),
            breakout_require_directional_body=bool(candle_raw.get("breakout_require_directional_body", True)),
            pullback_require_rejection_wick=bool(candle_raw.get("pullback_require_rejection_wick", True)),
            retest_require_rejection_wick=bool(candle_raw.get("retest_require_rejection_wick", True)),
        ),
        max_dist_ema_bps=max(0.0, float(ent_raw.get("max_dist_ema_bps", 35))),
    )

    ctx_raw = raw.get("market_context", {})
    market_data_source = str(ctx_raw.get("market_data_source", "execution")).strip().lower()
    if market_data_source_override is not None:
        market_data_source = str(market_data_source_override).strip().lower()
    if market_data_source not in {"execution", "live", "demo"}:
        raise ConfigError("market_context.market_data_source must be one of: execution, live, demo")
    depth_levels = max(5, int(ctx_raw.get("depth_levels", 20)))
    if depth_levels not in {5, 10, 20}:
        depth_levels = 20
    ws_depth_interval = str(ctx_raw.get("ws_depth_interval", "100ms")).strip().lower() or "100ms"
    if ws_depth_interval not in {"100ms", "250ms", "500ms"}:
        ws_depth_interval = "100ms"
    adl_block_raw = ctx_raw.get("adl_block_levels", ["high"])
    if not isinstance(adl_block_raw, list):
        adl_block_raw = [adl_block_raw]
    market_context = MarketContextConfig(
        enabled=bool(ctx_raw.get("enabled", True)),
        require_data=bool(ctx_raw.get("require_data", True)),
        market_data_source=market_data_source,
        period=str(ctx_raw.get("period", "15m")).strip() or "15m",
        oi_lookback=max(2, int(ctx_raw.get("oi_lookback", 3))),
        min_oi_change_pct=max(0.0, float(ctx_raw.get("min_oi_change_pct", 0.05))),
        funding_abs_max=max(0.0, float(ctx_raw.get("funding_abs_max", 0.0005))),
        basis_contract_type=str(ctx_raw.get("basis_contract_type", "PERPETUAL")).strip().upper() or "PERPETUAL",
        basis_abs_max=max(0.0, float(ctx_raw.get("basis_abs_max", 0.0015))),
        taker_ratio_long_min=max(0.0, float(ctx_raw.get("taker_ratio_long_min", 1.03))),
        taker_ratio_short_max=max(0.0, float(ctx_raw.get("taker_ratio_short_max", 0.97))),
        global_long_ratio_max=max(0.0, float(ctx_raw.get("global_long_ratio_max", 1.8))),
        global_short_ratio_min=max(0.0, float(ctx_raw.get("global_short_ratio_min", 0.55))),
        top_trader_long_ratio_max=max(0.0, float(ctx_raw.get("top_trader_long_ratio_max", 1.6))),
        top_trader_short_ratio_min=max(0.0, float(ctx_raw.get("top_trader_short_ratio_min", 0.65))),
        depth_enabled=bool(ctx_raw.get("depth_enabled", True)),
        depth_levels=depth_levels,
        depth_long_min_ratio=max(0.0, float(ctx_raw.get("depth_long_min_ratio", 1.03))),
        depth_short_max_ratio=max(0.0, float(ctx_raw.get("depth_short_max_ratio", 0.97))),
        depth_max_age_sec=max(1, int(ctx_raw.get("depth_max_age_sec", 5))),
        depth_rest_fallback=bool(ctx_raw.get("depth_rest_fallback", True)),
        liquidation_enabled=bool(ctx_raw.get("liquidation_enabled", True)),
        liquidation_window_sec=max(5, int(ctx_raw.get("liquidation_window_sec", 60))),
        liquidation_max_usdt=max(0.0, float(ctx_raw.get("liquidation_max_usdt", 1500000))),
        adl_enabled=bool(ctx_raw.get("adl_enabled", True)),
        adl_block_levels=[str(x).strip().lower() for x in adl_block_raw if str(x).strip()],
        ws_enabled=bool(ctx_raw.get("ws_enabled", True)),
        ws_depth_enabled=bool(ctx_raw.get("ws_depth_enabled", True)),
        ws_liquidation_enabled=bool(ctx_raw.get("ws_liquidation_enabled", True)),
        ws_depth_interval=ws_depth_interval,
    )

    hold_raw = raw.get("hold_management", {})
    soft_raw = hold_raw.get("soft_timeout_bars", {})
    hard_raw = hold_raw.get("hard_timeout_bars", {})
    if not isinstance(soft_raw, dict):
        soft_raw = {}
    if not isinstance(hard_raw, dict):
        hard_raw = {}
    default_soft = {"retest": 10, "pullback": 8, "breakout": 6, "range_revert": 5}
    default_hard = {"retest": 16, "pullback": 12, "breakout": 9, "range_revert": 8}
    soft_timeout_bars: dict[str, int] = {}
    hard_timeout_bars: dict[str, int] = {}
    for setup in {"retest", "pullback", "breakout", "range_revert"}:
        soft_value = max(1, int(soft_raw.get(setup, default_soft[setup])))
        hard_value = max(soft_value + 1, int(hard_raw.get(setup, default_hard[setup])))
        soft_timeout_bars[setup] = soft_value
        hard_timeout_bars[setup] = hard_value

    hold_management = HoldManagementConfig(
        enabled=bool(hold_raw.get("enabled", True)),
        soft_timeout_bars=soft_timeout_bars,
        hard_timeout_bars=hard_timeout_bars,
        progress_min_r=max(0.0, float(hold_raw.get("progress_min_r", 0.2))),
        move_sl_to_be_on_extend=bool(hold_raw.get("move_sl_to_be_on_extend", True)),
        be_buffer_bps=max(0.0, float(hold_raw.get("be_buffer_bps", 3.0))),
        require_trend_for_extend=bool(hold_raw.get("require_trend_for_extend", True)),
        use_taker_for_extend=bool(hold_raw.get("use_taker_for_extend", True)),
        close_on_soft_timeout_if_weak=bool(hold_raw.get("close_on_soft_timeout_if_weak", True)),
    )

    risk_raw = raw.get("risk", {})
    risk = RiskConfig(
        leverage=int(risk_raw.get("leverage", 5)),
        margin_mode=str(risk_raw.get("margin_mode", "isolated")),
        sizing_mode=str(risk_raw.get("sizing_mode", "risk")),
        risk_per_trade_pct=float(risk_raw.get("risk_per_trade_pct", 0.25)),
        margin_per_trade_usdt=float(risk_raw.get("margin_per_trade_usdt", 0)),
        margin_per_trade_pct=float(risk_raw.get("margin_per_trade_pct", 0)),
        max_notional_usdt=float(risk_raw.get("max_notional_usdt", 500)),
        max_concurrent_positions=int(risk_raw.get("max_concurrent_positions", 1)),
        daily_dd_stop_pct=float(risk_raw.get("daily_dd_stop_pct", 2.0)),
        consecutive_loss_stop=int(risk_raw.get("consecutive_loss_stop", 4)),
        min_free_usdt_to_trade=float(risk_raw.get("min_free_usdt_to_trade", 20)),
        min_notional_usdt=float(risk_raw.get("min_notional_usdt", 5)),
        sl_pct=float(risk_raw.get("sl_pct", 0.6)),
        tp_pct=float(risk_raw.get("tp_pct", 1.0)),
    )

    exe_raw = raw.get("execution", {})
    entry_by_setup_raw = exe_raw.get("entry_order_by_setup", {})
    entry_by_setup: dict[str, str] = {}
    if isinstance(entry_by_setup_raw, dict):
        for key, value in entry_by_setup_raw.items():
            setup = str(key).lower().strip()
            mode = str(value).lower().strip()
            if setup:
                entry_by_setup[setup] = mode
    if not entry_by_setup:
        entry_by_setup = {
            "retest": "maker",
            "pullback": "maker",
            "breakout": "market",
            "range_revert": "maker",
        }
    entry_by_setup.setdefault("range_revert", "maker")
    for key, value in list(entry_by_setup.items()):
        mode = str(value).lower().strip()
        if mode not in {"maker", "market"}:
            mode = "market"
        entry_by_setup[key] = mode

    partial_action = str(exe_raw.get("maker_partial_fill_action", "close")).lower()
    if partial_action not in {"close", "keep"}:
        partial_action = "close"

    entry_order = str(exe_raw.get("entry_order", "market")).lower().strip()
    if entry_order not in {"maker", "market"}:
        entry_order = "market"

    exit_order = str(exe_raw.get("exit_order", "taker")).lower().strip()
    if exit_order not in {"maker", "taker"}:
        exit_order = "taker"

    ref_price = str(exe_raw.get("ref_price", "book")).lower().strip()
    if ref_price not in {"book", "best", "mark"}:
        ref_price = "book"

    execution = ExecutionConfig(
        entry_order=entry_order,
        entry_order_by_setup=entry_by_setup,
        exit_order=exit_order,
        ref_price=ref_price,
        max_spread_bps=float(exe_raw.get("max_spread_bps", 8)),
        max_slippage_bps=float(exe_raw.get("max_slippage_bps", 5)),
        maker_timeout_sec=max(1, int(exe_raw.get("maker_timeout_sec", 10))),
        maker_poll_interval_sec=max(0.2, float(exe_raw.get("maker_poll_interval_sec", 1.0))),
        maker_min_fill_ratio=min(1.0, max(0.0, float(exe_raw.get("maker_min_fill_ratio", 0.7)))),
        maker_partial_fill_action=partial_action,
        maker_max_reprice=max(0, int(exe_raw.get("maker_max_reprice", 1))),
        maker_post_only=bool(exe_raw.get("maker_post_only", True)),
        maker_price_offset_bps=float(exe_raw.get("maker_price_offset_bps", 0.0)),
        use_exchange_tp_sl=bool(exe_raw.get("use_exchange_tp_sl", True)),
        tp_trigger_type=str(exe_raw.get("tp_trigger_type", "TAKE_PROFIT_MARKET")),
        sl_trigger_type=str(exe_raw.get("sl_trigger_type", "STOP_MARKET")),
    )

    safety_raw = raw.get("safety", {})
    safety = SafetyConfig(
        require_candle_close=bool(safety_raw.get("require_candle_close", True)),
        min_liquidity_usd=float(safety_raw.get("min_liquidity_usd", 0)),
        adx_no_trade_below=float(safety_raw.get("adx_no_trade_below", 20)),
        min_atr_usd=float(safety_raw.get("min_atr_usd", 0)),
        min_atr_pct=max(0.0, float(safety_raw.get("min_atr_pct", 0.10))),
        max_atr_pct=max(0.0, float(safety_raw.get("max_atr_pct", 1.20))),
        auto_repair_brackets=bool(safety_raw.get("auto_repair_brackets", True)),
        repair_brackets_cooldown_sec=max(5, int(safety_raw.get("repair_brackets_cooldown_sec", 30))),
    )

    sch_raw = raw.get("scheduler", {})
    scheduler = SchedulerConfig(
        align_to_candle_close=bool(sch_raw.get("align_to_candle_close", True)),
        poll_seconds=int(sch_raw.get("poll_seconds", 2)),
    )

    log_raw = raw.get("logging", {})
    logging_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")),
        file_live=str(log_raw.get("file_live", "logs/live.log")),
        rich_console=bool(log_raw.get("rich_console", True)),
        runtime_heartbeat_sec=max(10, int(log_raw.get("runtime_heartbeat_sec", 60))),
    )

    analytics_raw = raw.get("analytics", {})
    analytics = AnalyticsConfig(
        trades_csv=str(analytics_raw.get("trades_csv", "logs/trades.csv")),
        kpi_window_trades=max(10, int(analytics_raw.get("kpi_window_trades", 50))),
        summary_every_closed=max(1, int(analytics_raw.get("summary_every_closed", 1))),
        fee_maker=max(0.0, float(analytics_raw.get("fee_maker", 0.0002))),
        fee_taker=max(0.0, float(analytics_raw.get("fee_taker", 0.0005))),
        funding_apply=bool(analytics_raw.get("funding_apply", True)),
        active_state_json=str(analytics_raw.get("active_state_json", "logs/active_trades.json")),
    )

    exchange = _build_exchange(_pick_exchange(raw, app.mode))

    return BotConfig(
        app=app,
        timeframes=timeframes,
        strategy=strategy,
        range_trading=range_trading,
        filters=filters,
        entry=entry,
        market_context=market_context,
        hold_management=hold_management,
        risk=risk,
        execution=execution,
        safety=safety,
        scheduler=scheduler,
        logging=logging_cfg,
        analytics=analytics,
        exchange=exchange,
    )
