from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal

import pandas as pd

from aegis_engine.core.config import BotConfig
from aegis_engine.utils.indicators import adx, atr, ema, macd_hist, rsi


SignalSide = Literal["long", "short", "flat"]
VALID_SETUPS = {"retest", "pullback", "breakout", "range_revert"}


@dataclass
class Signal:
    side: SignalSide
    reason: str
    long_score: int
    short_score: int
    entry_type: str | None
    stop_loss: float | None = None
    take_profit: float | None = None


def _with_indicators(df: pd.DataFrame, cfg: BotConfig) -> pd.DataFrame:
    out = df.copy()
    st = cfg.strategy
    out["ema_fast"] = ema(out["close"], st.ema_fast)
    out["ema_slow"] = ema(out["close"], st.ema_slow)
    out["rsi"] = rsi(out["close"], st.rsi_period)
    out["adx"] = adx(out, st.adx_period)
    out["atr"] = atr(out, st.atr_period)
    out["vol_ma"] = out["volume"].rolling(st.volume_ma_period).mean()
    out["macd_hist"] = macd_hist(out["close"])
    return out


def _score_bias(last: pd.Series, cfg: BotConfig) -> tuple[int, int]:
    f = cfg.filters
    long_score = 0
    short_score = 0

    long_checks = [
        last["close"] > last["ema_fast"],
        last["ema_fast"] > last["ema_slow"],
        last["rsi"] >= f.long.rsi_min,
        last["adx"] >= f.long.adx_min,
        last["volume"] >= (last["vol_ma"] * f.long.volume_multiplier),
        last["macd_hist"] > 0,
    ]
    short_checks = [
        last["close"] < last["ema_fast"],
        last["ema_fast"] < last["ema_slow"],
        last["rsi"] <= f.short.rsi_max,
        last["adx"] >= f.short.adx_min,
        last["volume"] >= (last["vol_ma"] * f.short.volume_multiplier),
        last["macd_hist"] < 0,
    ]

    long_score = sum(1 for x in long_checks if x)
    short_score = sum(1 for x in short_checks if x)
    return long_score, short_score


def _within_ema_distance(last: pd.Series, cfg: BotConfig) -> bool:
    max_dist = cfg.entry.max_dist_ema_bps
    ema_fast = float(last["ema_fast"] or 0)
    close = float(last["close"] or 0)
    if ema_fast <= 0 or close <= 0:
        return False
    dist_bps = abs(close - ema_fast) / ema_fast * 10000
    return dist_bps <= max_dist


def _entry_candle_filter(last: pd.Series, cfg: BotConfig, side: str, setup: str) -> tuple[bool, str]:
    cf = cfg.entry.candle_filter
    if not cf.enabled:
        return True, "candle_filter_disabled"

    open_price = float(last.get("open", 0) or 0)
    high = float(last.get("high", 0) or 0)
    low = float(last.get("low", 0) or 0)
    close = float(last.get("close", 0) or 0)
    volume = float(last.get("volume", 0) or 0)
    vol_ma = float(last.get("vol_ma", 0) or 0)
    atr_value = float(last.get("atr", 0) or 0)

    if open_price <= 0 or high <= 0 or low <= 0 or close <= 0:
        return False, "candle_invalid_ohlc"

    candle_range = high - low
    if candle_range <= 0:
        return False, "candle_zero_range"

    body = abs(close - open_price)
    ref_price = open_price if open_price > 0 else close
    body_bps = body / ref_price * 10000

    if body_bps < cf.min_body_bps:
        return False, "candle_body_bps_too_small"

    if atr_value > 0 and cf.min_body_atr > 0:
        if body / atr_value < cf.min_body_atr:
            return False, "candle_body_atr_too_small"

    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    total_wick_ratio = (upper_wick + lower_wick) / candle_range
    if total_wick_ratio > cf.max_total_wick_ratio:
        return False, "candle_total_wick_too_large"

    opposite_wick_ratio = (upper_wick / candle_range) if side == "long" else (lower_wick / candle_range)
    if opposite_wick_ratio > cf.max_opposite_wick_ratio:
        return False, "candle_opposite_wick_too_large"

    if cf.volume_min_mult > 0 and vol_ma > 0:
        if volume < vol_ma * cf.volume_min_mult:
            return False, "candle_volume_too_low"

    is_bull = close > open_price
    is_bear = close < open_price
    if setup == "breakout" and cf.breakout_require_directional_body:
        if side == "long" and not is_bull:
            return False, "candle_breakout_not_bullish"
        if side == "short" and not is_bear:
            return False, "candle_breakout_not_bearish"

    need_rejection = False
    if setup == "pullback":
        need_rejection = cf.pullback_require_rejection_wick
    elif setup == "retest":
        need_rejection = cf.retest_require_rejection_wick
    elif setup == "range_revert":
        need_rejection = True

    if need_rejection:
        rejection_ratio = (lower_wick / candle_range) if side == "long" else (upper_wick / candle_range)
        if rejection_ratio < cf.min_rejection_wick_ratio:
            return False, "candle_rejection_wick_too_small"

    return True, "candle_ok"


def _retest_long(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    st = cfg.strategy
    en = cfg.entry
    rt = en.retest
    last_idx = len(df) - 1
    last = df.iloc[last_idx]
    prev = df.iloc[last_idx - 1]

    levels = df["high"].shift(1).rolling(st.breakout_lookback).max()
    breakout_flags = df["close"] > (levels * (1 + st.breakout_buffer_bps / 10000))

    level = None
    start = max(0, last_idx - rt.max_bars_since_breakout)
    for i in range(last_idx - 1, start - 1, -1):
        level_i = levels.iloc[i]
        if pd.isna(level_i):
            continue
        if bool(breakout_flags.iloc[i]):
            level = float(level_i)
            break

    if level is None:
        return False, "no_recent_breakout"

    upper = level * (1 + rt.touch_buffer_bps / 10000)
    lower = level * (1 - rt.touch_buffer_bps / 10000)
    touched = (last["low"] <= upper) and (last["low"] >= lower)
    reclaimed = last["close"] >= level * (1 + rt.reclaim_buffer_bps / 10000)
    ema_slope_ok = (last["ema_fast"] > prev["ema_fast"]) if rt.require_ema_slope else True
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="long", setup="retest")

    ok = (
        touched
        and reclaimed
        and ema_slope_ok
        and (last["close"] > last["ema_fast"])
        and (last["rsi"] >= en.rsi_long_th)
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if ok:
        return True, "retest"
    if not candle_ok:
        return False, candle_reason
    return False, "retest_not_confirmed"


def _pullback_long(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    en = cfg.entry

    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema_slope_up = last["ema_fast"] > prev["ema_fast"]
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="long", setup="pullback")

    pullback_ok = (
        (last["low"] <= last["ema_fast"])
        and (last["close"] > last["ema_fast"])
        and (last["rsi"] >= en.rsi_long_th)
        and ema_slope_up
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if pullback_ok:
        return True, "pullback"
    if not candle_ok:
        return False, candle_reason
    return False, "no_long_entry"


def _breakout_long(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    st = cfg.strategy
    en = cfg.entry
    last = df.iloc[-1]
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="long", setup="breakout")
    rolling_high = df["high"].shift(1).rolling(st.breakout_lookback).max().iloc[-1]
    breakout_price = rolling_high * (1 + st.breakout_buffer_bps / 10000)
    breakout_ok = (
        (last["close"] > breakout_price)
        and (last["rsi"] >= en.rsi_long_th)
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if breakout_ok:
        return True, "breakout"
    if not candle_ok:
        return False, candle_reason
    return False, "no_long_entry"


def _retest_short(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    st = cfg.strategy
    en = cfg.entry
    rt = en.retest
    last_idx = len(df) - 1
    last = df.iloc[last_idx]
    prev = df.iloc[last_idx - 1]

    levels = df["low"].shift(1).rolling(st.breakout_lookback).min()
    breakdown_flags = df["close"] < (levels * (1 - st.breakout_buffer_bps / 10000))

    level = None
    start = max(0, last_idx - rt.max_bars_since_breakout)
    for i in range(last_idx - 1, start - 1, -1):
        level_i = levels.iloc[i]
        if pd.isna(level_i):
            continue
        if bool(breakdown_flags.iloc[i]):
            level = float(level_i)
            break

    if level is None:
        return False, "no_recent_breakdown"

    upper = level * (1 + rt.touch_buffer_bps / 10000)
    lower = level * (1 - rt.touch_buffer_bps / 10000)
    touched = (last["high"] >= lower) and (last["high"] <= upper)
    reclaimed = last["close"] <= level * (1 - rt.reclaim_buffer_bps / 10000)
    ema_slope_ok = (last["ema_fast"] < prev["ema_fast"]) if rt.require_ema_slope else True
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="short", setup="retest")

    ok = (
        touched
        and reclaimed
        and ema_slope_ok
        and (last["close"] < last["ema_fast"])
        and (last["rsi"] <= en.rsi_short_th)
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if ok:
        return True, "retest"
    if not candle_ok:
        return False, candle_reason
    return False, "retest_not_confirmed"


def _pullback_short(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    en = cfg.entry

    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema_slope_down = last["ema_fast"] < prev["ema_fast"]
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="short", setup="pullback")

    pullback_ok = (
        (last["high"] >= last["ema_fast"])
        and (last["close"] < last["ema_fast"])
        and (last["rsi"] <= en.rsi_short_th)
        and ema_slope_down
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if pullback_ok:
        return True, "pullback"
    if not candle_ok:
        return False, candle_reason
    return False, "no_short_entry"


def _breakout_short(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    st = cfg.strategy
    en = cfg.entry
    last = df.iloc[-1]
    candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="short", setup="breakout")
    rolling_low = df["low"].shift(1).rolling(st.breakout_lookback).min().iloc[-1]
    breakdown_price = rolling_low * (1 - st.breakout_buffer_bps / 10000)
    breakout_ok = (
        (last["close"] < breakdown_price)
        and (last["rsi"] <= en.rsi_short_th)
        and _within_ema_distance(last, cfg)
        and candle_ok
    )
    if breakout_ok:
        return True, "breakout"
    if not candle_ok:
        return False, candle_reason
    return False, "no_short_entry"


def _pick_long_entry(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    en = cfg.entry
    checks = {
        "retest": _retest_long,
        "pullback": _pullback_long,
        "breakout": _breakout_long,
    }
    enabled = {
        "retest": en.allow_retest,
        "pullback": en.allow_pullback,
        "breakout": en.allow_breakout,
    }
    for setup in en.setup_priority:
        if setup not in VALID_SETUPS:
            continue
        if not enabled.get(setup, False):
            continue
        ok, setup_name = checks[setup](df, cfg)
        if ok:
            return True, setup_name
    return False, "no_long_entry"


def _pick_short_entry(df: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str]:
    en = cfg.entry
    checks = {
        "retest": _retest_short,
        "pullback": _pullback_short,
        "breakout": _breakout_short,
    }
    enabled = {
        "retest": en.allow_retest,
        "pullback": en.allow_pullback,
        "breakout": en.allow_breakout,
    }
    for setup in en.setup_priority:
        if setup not in VALID_SETUPS:
            continue
        if not enabled.get(setup, False):
            continue
        ok, setup_name = checks[setup](df, cfg)
        if ok:
            return True, setup_name
    return False, "no_short_entry"


def _range_width_stats(entry: pd.DataFrame, cfg: BotConfig) -> tuple[bool, str, dict[str, float]]:
    rg = cfg.range_trading
    if len(entry) < rg.lookback + 2:
        return False, "range_not_enough_bars", {}

    window = entry.iloc[-(rg.lookback + 1) : -1]
    last = entry.iloc[-1]
    range_high = float(window["high"].max())
    range_low = float(window["low"].min())
    close = float(last["close"] or 0)
    atr_value = float(last["atr"] or 0)
    if range_high <= 0 or range_low <= 0 or close <= 0 or range_high <= range_low:
        return False, "range_invalid_levels", {}

    mid = (range_high + range_low) / 2
    width = range_high - range_low
    width_bps = width / mid * 10000
    if width_bps < rg.min_width_bps:
        return False, "range_width_too_narrow", {}
    if rg.max_width_bps > 0 and width_bps > rg.max_width_bps:
        return False, "range_width_too_wide", {}
    if atr_value <= 0:
        return False, "range_atr_invalid", {}
    width_atr = width / atr_value
    if width_atr < rg.min_width_atr:
        return False, "range_width_atr_too_small", {}

    edge = rg.edge_buffer_bps / 10000
    support_touches = int((window["low"] <= range_low * (1 + edge)).sum())
    resistance_touches = int((window["high"] >= range_high * (1 - edge)).sum())
    if support_touches < rg.min_touches or resistance_touches < rg.min_touches:
        return False, "range_not_enough_touches", {}

    break_buffer = rg.breakout_buffer_bps / 10000
    if close < range_low * (1 - break_buffer) or close > range_high * (1 + break_buffer):
        return False, "range_already_breaking", {}

    return True, "range_ok", {
        "range_high": range_high,
        "range_low": range_low,
        "mid": mid,
        "width_bps": width_bps,
        "width_atr": width_atr,
        "atr": atr_value,
    }


def _range_regime_ok(confirm_last: pd.Series, cfg: BotConfig) -> tuple[bool, str]:
    rg = cfg.range_trading
    adx_value = float(confirm_last["adx"] or 0)
    if adx_value > rg.max_adx:
        return False, "range_adx_too_high"

    close = float(confirm_last["close"] or 0)
    ema_fast = float(confirm_last["ema_fast"] or 0)
    ema_slow = float(confirm_last["ema_slow"] or 0)
    if close <= 0 or ema_fast <= 0 or ema_slow <= 0:
        return False, "range_invalid_ema"
    ema_spread_bps = abs(ema_fast - ema_slow) / close * 10000
    if rg.max_ema_spread_bps > 0 and ema_spread_bps > rg.max_ema_spread_bps:
        return False, "range_ema_spread_too_wide"

    return True, "range_regime_ok"


def _range_brackets(
    side: str,
    entry_price: float,
    levels: dict[str, float],
    cfg: BotConfig,
) -> tuple[bool, str, float | None, float | None]:
    rg = cfg.range_trading
    range_high = levels["range_high"]
    range_low = levels["range_low"]
    mid = levels["mid"]
    atr_value = levels["atr"]
    if entry_price <= 0 or atr_value <= 0:
        return False, "range_invalid_bracket_price", None, None

    stop_buffer = max(atr_value * rg.sl_atr_buffer, entry_price * (rg.breakout_buffer_bps / 10000))
    tp_buffer = rg.tp_mid_buffer_bps / 10000
    if side == "long":
        stop_loss = range_low - stop_buffer
        take_profit = mid * (1 - tp_buffer)
        if stop_loss <= 0 or not (stop_loss < entry_price < take_profit):
            return False, "range_long_bracket_invalid", None, None
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
    else:
        stop_loss = range_high + stop_buffer
        take_profit = mid * (1 + tp_buffer)
        if not (take_profit < entry_price < stop_loss):
            return False, "range_short_bracket_invalid", None, None
        risk = stop_loss - entry_price
        reward = entry_price - take_profit

    if risk <= 0 or reward <= 0:
        return False, "range_reward_invalid", None, None
    reward_r = reward / risk
    if reward_r < rg.min_reward_r:
        return False, "range_reward_r_too_low", None, None
    return True, "range_brackets_ok", stop_loss, take_profit


def _build_range_signal(
    confirm: pd.DataFrame,
    entry: pd.DataFrame,
    cfg: BotConfig,
    market_ctx: dict[str, Any] | None,
    long_score: int,
    short_score: int,
) -> Signal | None:
    rg = cfg.range_trading
    if not rg.enabled:
        return None

    regime_ok, _ = _range_regime_ok(confirm.iloc[-1], cfg)
    if not regime_ok:
        return None

    range_ok, _, levels = _range_width_stats(entry, cfg)
    if not range_ok:
        return None

    last = entry.iloc[-1]
    close = float(last["close"] or 0)
    high = float(last["high"] or 0)
    low = float(last["low"] or 0)
    rsi_value = float(last["rsi"] or 0)
    if close <= 0 or high <= 0 or low <= 0:
        return None

    range_high = levels["range_high"]
    range_low = levels["range_low"]
    mid = levels["mid"]
    edge = rg.edge_buffer_bps / 10000
    support_zone = range_low * (1 + edge)
    resistance_zone = range_high * (1 - edge)

    long_near_support = low <= support_zone and range_low < close <= support_zone and close < mid
    short_near_resistance = high >= resistance_zone and resistance_zone <= close < range_high and close > mid

    if long_near_support and rsi_value <= rg.rsi_long_max:
        candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="long", setup="range_revert")
        if not candle_ok:
            return Signal(side="flat", reason=f"range_long_{candle_reason}", long_score=long_score, short_score=short_score, entry_type=None)
        market_ok, market_reason = _passes_market_context("long", cfg, market_ctx)
        if not market_ok:
            return Signal(side="flat", reason=market_reason, long_score=long_score, short_score=short_score, entry_type=None)
        bracket_ok, bracket_reason, stop_loss, take_profit = _range_brackets("long", close, levels, cfg)
        if not bracket_ok:
            return Signal(side="flat", reason=bracket_reason, long_score=long_score, short_score=short_score, entry_type=None)
        return Signal(
            side="long",
            reason="range_long_confirmed",
            long_score=long_score,
            short_score=short_score,
            entry_type="range_revert",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    if short_near_resistance and rsi_value >= rg.rsi_short_min:
        candle_ok, candle_reason = _entry_candle_filter(last, cfg, side="short", setup="range_revert")
        if not candle_ok:
            return Signal(side="flat", reason=f"range_short_{candle_reason}", long_score=long_score, short_score=short_score, entry_type=None)
        market_ok, market_reason = _passes_market_context("short", cfg, market_ctx)
        if not market_ok:
            return Signal(side="flat", reason=market_reason, long_score=long_score, short_score=short_score, entry_type=None)
        bracket_ok, bracket_reason, stop_loss, take_profit = _range_brackets("short", close, levels, cfg)
        if not bracket_ok:
            return Signal(side="flat", reason=bracket_reason, long_score=long_score, short_score=short_score, entry_type=None)
        return Signal(
            side="short",
            reason="range_short_confirmed",
            long_score=long_score,
            short_score=short_score,
            entry_type="range_revert",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    return None


def _market_ctx_value(market_ctx: dict[str, Any] | None, key: str) -> float | None:
    if not market_ctx:
        return None
    try:
        value = market_ctx.get(key)
    except AttributeError:
        return None
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _passes_market_context(side: str, cfg: BotConfig, market_ctx: dict[str, Any] | None) -> tuple[bool, str]:
    ctx_cfg = cfg.market_context
    if not ctx_cfg.enabled:
        return True, "market_context_disabled"
    if not market_ctx:
        return (not ctx_cfg.require_data), "market_context_unavailable"

    oi_change_pct = _market_ctx_value(market_ctx, "oi_change_pct")
    funding_rate = _market_ctx_value(market_ctx, "funding_rate")
    taker_ratio = _market_ctx_value(market_ctx, "taker_buy_sell_ratio")
    top_ratio = _market_ctx_value(market_ctx, "top_trader_long_short_ratio")
    global_ratio = _market_ctx_value(market_ctx, "global_long_short_ratio")
    basis_rate = _market_ctx_value(market_ctx, "basis_rate")
    depth_ratio = _market_ctx_value(market_ctx, "depth_imbalance_ratio")
    liq_notional = _market_ctx_value(market_ctx, "liquidation_notional_window_usdt")
    adl_risk = str((market_ctx or {}).get("adl_risk") or "").lower().strip() or None

    if ctx_cfg.require_data:
        required = [oi_change_pct, funding_rate, taker_ratio, top_ratio, global_ratio]
        if ctx_cfg.depth_enabled:
            required.append(depth_ratio)
        if ctx_cfg.liquidation_enabled:
            required.append(liq_notional)
        if ctx_cfg.adl_enabled:
            required.append(1.0 if adl_risk else None)
        if any(x is None for x in required):
            return False, "market_context_missing_fields"

    if oi_change_pct is not None and oi_change_pct < ctx_cfg.min_oi_change_pct:
        return False, "market_ctx_oi_change_too_low"

    if funding_rate is not None and abs(funding_rate) > ctx_cfg.funding_abs_max:
        return False, "market_ctx_funding_too_high"

    if basis_rate is not None and abs(basis_rate) > ctx_cfg.basis_abs_max:
        return False, "market_ctx_basis_too_high"

    if ctx_cfg.depth_enabled and depth_ratio is not None:
        if side == "long" and depth_ratio < ctx_cfg.depth_long_min_ratio:
            return False, "market_ctx_depth_not_support_long"
        if side == "short" and depth_ratio > ctx_cfg.depth_short_max_ratio:
            return False, "market_ctx_depth_not_support_short"

    if ctx_cfg.liquidation_enabled and liq_notional is not None and liq_notional > ctx_cfg.liquidation_max_usdt:
        return False, "market_ctx_liquidation_shock"

    if ctx_cfg.adl_enabled and adl_risk and adl_risk in set(ctx_cfg.adl_block_levels):
        return False, "market_ctx_adl_block"

    if side == "long":
        if taker_ratio is not None and taker_ratio < ctx_cfg.taker_ratio_long_min:
            return False, "market_ctx_taker_not_support_long"
        if global_ratio is not None and global_ratio > ctx_cfg.global_long_ratio_max:
            return False, "market_ctx_long_crowded"
        if top_ratio is not None and top_ratio > ctx_cfg.top_trader_long_ratio_max:
            return False, "market_ctx_top_long_crowded"
    else:
        if taker_ratio is not None and taker_ratio > ctx_cfg.taker_ratio_short_max:
            return False, "market_ctx_taker_not_support_short"
        if global_ratio is not None and global_ratio < ctx_cfg.global_short_ratio_min:
            return False, "market_ctx_short_crowded"
        if top_ratio is not None and top_ratio < ctx_cfg.top_trader_short_ratio_min:
            return False, "market_ctx_top_short_crowded"

    return True, "market_context_ok"


def build_signal(
    confirm_df: pd.DataFrame,
    entry_df: pd.DataFrame,
    cfg: BotConfig,
    market_ctx: dict[str, Any] | None = None,
) -> Signal:
    if len(confirm_df) < cfg.timeframes.min_confirm_bars:
        return Signal(side="flat", reason="not_enough_confirm_bars", long_score=0, short_score=0, entry_type=None)

    confirm = _with_indicators(confirm_df, cfg).dropna()
    entry = _with_indicators(entry_df, cfg).dropna()

    if len(confirm) < 5 or len(entry) < 5:
        return Signal(side="flat", reason="not_enough_indicator_data", long_score=0, short_score=0, entry_type=None)

    c_last = confirm.iloc[-1]
    if c_last["atr"] < cfg.safety.min_atr_usd:
        return Signal(side="flat", reason="atr_below_min", long_score=0, short_score=0, entry_type=None)

    close_price = float(c_last["close"] or 0)
    atr_value = float(c_last["atr"] or 0)
    if close_price > 0 and atr_value > 0:
        atr_pct = atr_value / close_price * 100
        if cfg.safety.min_atr_pct > 0 and atr_pct < cfg.safety.min_atr_pct:
            return Signal(side="flat", reason="atr_pct_below_min", long_score=0, short_score=0, entry_type=None)
        if cfg.safety.max_atr_pct > 0 and atr_pct > cfg.safety.max_atr_pct:
            return Signal(side="flat", reason="atr_pct_above_max", long_score=0, short_score=0, entry_type=None)

    est_liquidity = float(c_last["close"] * c_last["volume"])
    if est_liquidity < cfg.safety.min_liquidity_usd:
        return Signal(side="flat", reason="liquidity_below_min", long_score=0, short_score=0, entry_type=None)

    long_score, short_score = _score_bias(c_last, cfg)
    min_score = cfg.filters.min_confluence_score

    range_signal = _build_range_signal(
        confirm=confirm,
        entry=entry,
        cfg=cfg,
        market_ctx=market_ctx,
        long_score=long_score,
        short_score=short_score,
    )
    if range_signal is not None:
        return range_signal

    if c_last["adx"] < cfg.safety.adx_no_trade_below:
        return Signal(side="flat", reason="adx_no_trade", long_score=long_score, short_score=short_score, entry_type=None)

    if long_score < min_score and short_score < min_score:
        return Signal(side="flat", reason="confluence_below_min", long_score=long_score, short_score=short_score, entry_type=None)

    if long_score > short_score:
        if long_score - short_score < cfg.filters.score_gap_min:
            return Signal(
                side="flat",
                reason="score_gap_too_small",
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        if short_score > cfg.filters.opponent_score_max:
            return Signal(
                side="flat",
                reason="opponent_score_too_high",
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        market_ok, market_reason = _passes_market_context("long", cfg, market_ctx)
        if not market_ok:
            return Signal(
                side="flat",
                reason=market_reason,
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        ok, entry_type = _pick_long_entry(entry, cfg)
        if ok:
            return Signal(side="long", reason="long_confirmed", long_score=long_score, short_score=short_score, entry_type=entry_type)
        return Signal(side="flat", reason=f"long_bias_{entry_type}", long_score=long_score, short_score=short_score, entry_type=None)

    if short_score > long_score:
        if short_score - long_score < cfg.filters.score_gap_min:
            return Signal(
                side="flat",
                reason="score_gap_too_small",
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        if long_score > cfg.filters.opponent_score_max:
            return Signal(
                side="flat",
                reason="opponent_score_too_high",
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        market_ok, market_reason = _passes_market_context("short", cfg, market_ctx)
        if not market_ok:
            return Signal(
                side="flat",
                reason=market_reason,
                long_score=long_score,
                short_score=short_score,
                entry_type=None,
            )
        ok, entry_type = _pick_short_entry(entry, cfg)
        if ok:
            return Signal(side="short", reason="short_confirmed", long_score=long_score, short_score=short_score, entry_type=entry_type)
        return Signal(side="flat", reason=f"short_bias_{entry_type}", long_score=long_score, short_score=short_score, entry_type=None)

    return Signal(side="flat", reason="balanced_scores", long_score=long_score, short_score=short_score, entry_type=None)
