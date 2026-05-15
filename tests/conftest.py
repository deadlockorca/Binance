from __future__ import annotations

from pathlib import Path

from aegis_engine.core.config import AnalyticsConfig
from aegis_engine.core.config import AppConfig
from aegis_engine.core.config import BotConfig
from aegis_engine.core.config import CandleFilterConfig
from aegis_engine.core.config import EntryConfig
from aegis_engine.core.config import ExchangeConfig
from aegis_engine.core.config import ExecutionConfig
from aegis_engine.core.config import FiltersConfig
from aegis_engine.core.config import HoldManagementConfig
from aegis_engine.core.config import LoggingConfig
from aegis_engine.core.config import MarketContextConfig
from aegis_engine.core.config import RangeTradingConfig
from aegis_engine.core.config import RetestConfig
from aegis_engine.core.config import RiskConfig
from aegis_engine.core.config import SafetyConfig
from aegis_engine.core.config import SchedulerConfig
from aegis_engine.core.config import SideFilter
from aegis_engine.core.config import StrategyConfig
from aegis_engine.core.config import TimeframesConfig


def make_test_config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        app=AppConfig(name="test", mode="paper", timezone="UTC", symbols=["BTCUSDT"]),
        timeframes=TimeframesConfig(confirm="1h", entry="15m", warmup_bars=100, min_confirm_bars=20),
        strategy=StrategyConfig(
            ema_fast=20,
            ema_slow=50,
            rsi_period=14,
            adx_period=14,
            atr_period=14,
            volume_ma_period=20,
            breakout_lookback=20,
            breakout_buffer_bps=5.0,
        ),
        range_trading=RangeTradingConfig(
            enabled=True,
            lookback=64,
            min_touches=2,
            edge_buffer_bps=18,
            breakout_buffer_bps=8,
            max_adx=18,
            max_ema_spread_bps=35,
            min_width_bps=70,
            max_width_bps=260,
            min_width_atr=2.2,
            rsi_long_max=43,
            rsi_short_min=57,
            min_reward_r=1.1,
            sl_atr_buffer=0.35,
            tp_mid_buffer_bps=4,
        ),
        filters=FiltersConfig(
            min_confluence_score=4,
            score_gap_min=2,
            opponent_score_max=2,
            long=SideFilter(rsi_min=55, adx_min=20, volume_multiplier=1.2),
            short=SideFilter(rsi_max=45, adx_min=20, volume_multiplier=1.2),
        ),
        entry=EntryConfig(
            allow_retest=True,
            allow_pullback=True,
            allow_breakout=True,
            rsi_long_th=55,
            rsi_short_th=45,
            setup_priority=["retest", "pullback", "breakout"],
            size_multipliers={"retest": 1.0, "pullback": 0.7, "breakout": 0.4, "range_revert": 0.6},
            retest=RetestConfig(max_bars_since_breakout=6, touch_buffer_bps=12, reclaim_buffer_bps=3, require_ema_slope=True),
            candle_filter=CandleFilterConfig(
                enabled=True,
                min_body_bps=4.0,
                min_body_atr=0.12,
                max_total_wick_ratio=0.85,
                max_opposite_wick_ratio=0.55,
                min_rejection_wick_ratio=0.12,
                volume_min_mult=0.85,
                breakout_require_directional_body=True,
                pullback_require_rejection_wick=True,
                retest_require_rejection_wick=True,
            ),
            max_dist_ema_bps=35,
        ),
        market_context=MarketContextConfig(
            enabled=False,
            require_data=False,
            market_data_source="execution",
            period="15m",
            oi_lookback=3,
            min_oi_change_pct=0.02,
            funding_abs_max=0.0005,
            basis_contract_type="PERPETUAL",
            basis_abs_max=0.0015,
            taker_ratio_long_min=1.01,
            taker_ratio_short_max=0.99,
            global_long_ratio_max=1.8,
            global_short_ratio_min=0.55,
            top_trader_long_ratio_max=1.6,
            top_trader_short_ratio_min=0.65,
            depth_enabled=False,
            depth_levels=20,
            depth_long_min_ratio=0.95,
            depth_short_max_ratio=1.05,
            depth_max_age_sec=5,
            depth_rest_fallback=True,
            liquidation_enabled=False,
            liquidation_window_sec=60,
            liquidation_max_usdt=5_000_000,
            adl_enabled=False,
            adl_block_levels=["high"],
            ws_enabled=False,
            ws_depth_enabled=False,
            ws_liquidation_enabled=False,
            ws_depth_interval="100ms",
        ),
        hold_management=HoldManagementConfig(
            enabled=True,
            soft_timeout_bars={"retest": 10, "pullback": 8, "breakout": 6, "range_revert": 5},
            hard_timeout_bars={"retest": 16, "pullback": 12, "breakout": 9, "range_revert": 8},
            progress_min_r=0.2,
            move_sl_to_be_on_extend=True,
            be_buffer_bps=3.0,
            require_trend_for_extend=True,
            use_taker_for_extend=True,
            close_on_soft_timeout_if_weak=True,
        ),
        risk=RiskConfig(
            leverage=5,
            margin_mode="isolated",
            sizing_mode="risk",
            risk_per_trade_pct=0.25,
            margin_per_trade_usdt=0,
            margin_per_trade_pct=0,
            max_notional_usdt=500,
            max_concurrent_positions=1,
            daily_dd_stop_pct=2.0,
            consecutive_loss_stop=4,
            min_free_usdt_to_trade=20,
            min_notional_usdt=5,
            sl_pct=0.6,
            tp_pct=1.0,
        ),
        execution=ExecutionConfig(
            entry_order="market",
            entry_order_by_setup={"retest": "maker", "pullback": "maker", "breakout": "market", "range_revert": "maker"},
            exit_order="taker",
            ref_price="book",
            max_spread_bps=8,
            max_slippage_bps=5,
            maker_timeout_sec=10,
            maker_poll_interval_sec=1.0,
            maker_min_fill_ratio=0.7,
            maker_partial_fill_action="close",
            maker_max_reprice=1,
            maker_post_only=True,
            maker_price_offset_bps=0.0,
            use_exchange_tp_sl=True,
            tp_trigger_type="TAKE_PROFIT_MARKET",
            sl_trigger_type="STOP_MARKET",
        ),
        safety=SafetyConfig(
            require_candle_close=True,
            min_liquidity_usd=3_000_000,
            adx_no_trade_below=20,
            min_atr_usd=3,
            min_atr_pct=0.1,
            max_atr_pct=1.2,
            auto_repair_brackets=True,
            repair_brackets_cooldown_sec=30,
        ),
        scheduler=SchedulerConfig(align_to_candle_close=True, poll_seconds=2),
        logging=LoggingConfig(level="INFO", file_live=str(tmp_path / "live.log"), rich_console=False, runtime_heartbeat_sec=30),
        analytics=AnalyticsConfig(
            trades_csv=str(tmp_path / "trades.csv"),
            kpi_window_trades=50,
            summary_every_closed=1,
            fee_maker=0.0002,
            fee_taker=0.0005,
            funding_apply=True,
            active_state_json=str(tmp_path / "active_state.json"),
        ),
        exchange=ExchangeConfig(
            name="binanceusdm",
            market_type="swap",
            settle="USDT",
            demo_trading=True,
            api_key="x",
            api_secret="y",
            enable_rate_limit=True,
            timeout=20_000,
            options={"defaultType": "future"},
        ),
    )
