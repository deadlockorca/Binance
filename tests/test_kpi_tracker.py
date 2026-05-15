from __future__ import annotations

from datetime import datetime

import pandas as pd

from aegis_engine.analytics.kpi_tracker import KpiTracker
from aegis_engine.execution.executor import ExecutionResult
from tests.conftest import make_test_config


class DummyExchange:
    def __init__(self) -> None:
        self.position_open = False

    def has_open_position(self, symbol: str) -> bool:
        return self.position_open

    def fetch_order_safe(self, symbol: str, order_id: str | None):
        if order_id == "sl-order":
            return {
                "id": "sl-order",
                "status": "closed",
                "filled": 1.0,
                "remaining": 0.0,
                "average": 99.0,
                "timestamp": 1_700_000_000_000,
                "info": {"status": "FILLED", "avgPrice": "99.0", "updateTime": 1_700_000_000_000},
            }
        if order_id == "tp-order":
            return {
                "id": "tp-order",
                "status": "canceled",
                "filled": 0.0,
                "remaining": 1.0,
                "timestamp": 1_700_000_000_000,
                "info": {"status": "CANCELED", "updateTime": 1_700_000_000_000},
            }
        return None

    def mark_price(self, symbol: str) -> float:
        return 100.0

    def fee_for_order(self, symbol: str, order_id: str | None):
        return None

    def funding_fee_between(self, symbol: str, start_at: datetime, end_at: datetime) -> float:
        return -0.02


def test_kpi_tracker_records_net_pnl_and_loss_streak(tmp_path):
    cfg = make_test_config(tmp_path)
    tracker = KpiTracker(cfg)

    result = ExecutionResult(
        symbol="BTCUSDT",
        side="long",
        setup="retest",
        entry_price=100.0,
        quantity=1.0,
        notional_usdt=100.0,
        expected_loss_usdt=1.0,
        expected_profit_usdt=1.5,
        entry_order_id="entry-order",
        tp_order_id="tp-order",
        sl_order_id="sl-order",
        entry_fee_rate=0.0002,
        entry_fee_usdt=0.02,
    )
    tracker.register_entry(result)

    exchange = DummyExchange()
    tracker.sync_symbol("BTCUSDT", exchange)

    df = pd.read_csv(cfg.analytics.trades_csv)
    assert len(df) == 1

    row = df.iloc[0]
    assert row["exit_reason"] == "sl"
    assert round(float(row["gross_pnl_usdt"]), 4) == -1.0
    assert round(float(row["fees_usdt"]), 4) == round(0.02 + (99.0 * cfg.analytics.fee_taker), 4)
    assert round(float(row["funding_usdt"]), 4) == -0.02

    expected_net = -1.0 - (0.02 + 99.0 * cfg.analytics.fee_taker) - 0.02
    assert round(float(row["net_pnl_usdt"]), 4) == round(expected_net, 4)
    assert tracker.current_loss_streak() == 1


def test_kpi_tracker_restores_active_state(tmp_path):
    cfg = make_test_config(tmp_path)
    tracker = KpiTracker(cfg)

    result = ExecutionResult(
        symbol="BTCUSDT",
        side="short",
        setup="pullback",
        entry_price=200.0,
        quantity=0.5,
        notional_usdt=100.0,
        expected_loss_usdt=1.0,
        expected_profit_usdt=1.5,
        entry_order_id="entry-2",
        tp_order_id="tp-2",
        sl_order_id="sl-2",
        entry_fee_rate=0.0005,
        entry_fee_usdt=0.05,
    )
    tracker.register_entry(result)

    restored = KpiTracker(cfg)
    active = restored.get_active_trade("BTCUSDT")
    assert active is not None
    assert active.side == "short"
    assert active.quantity == 0.5
    assert active.setup == "pullback"
