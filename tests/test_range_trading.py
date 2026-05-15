from __future__ import annotations

from pathlib import Path
import math

import pandas as pd

from aegis_engine.risk.risk_engine import AccountSnapshot
from aegis_engine.risk.risk_engine import RiskEngine
from aegis_engine.strategy.trend_signal import build_signal
from tests.conftest import make_test_config


def _confirm_range_df(rows: int = 160) -> pd.DataFrame:
    data = []
    for i in range(rows):
        amp = 0.05
        close = 100 + (amp if i % 2 == 0 else -amp)
        open_price = 100 + (-amp if i % 2 == 0 else amp)
        high = max(open_price, close) + amp
        low = min(open_price, close) - amp
        data.append(
            {
                "timestamp": pd.Timestamp("2024-01-01") + pd.Timedelta(hours=i),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
            }
        )
    return pd.DataFrame(data)


def _entry_range_df(rows: int = 160) -> pd.DataFrame:
    data = []
    for i in range(rows):
        close = 100 + math.sin(i / 3) * 0.45
        open_price = 100 + math.sin((i - 1) / 3) * 0.45 if i else close
        high = max(open_price, close) + 0.08
        low = min(open_price, close) - 0.08
        data.append(
            {
                "timestamp": pd.Timestamp("2024-01-01") + pd.Timedelta(minutes=15 * i),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
            }
        )
    return pd.DataFrame(data)


def _cfg(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.market_context.enabled = False
    cfg.safety.min_atr_usd = 0
    cfg.safety.min_atr_pct = 0
    cfg.safety.min_liquidity_usd = 0
    return cfg


def test_range_reversion_signal_uses_dynamic_brackets(tmp_path):
    cfg = _cfg(tmp_path)
    confirm_df = _confirm_range_df()
    entry_df = _entry_range_df()
    entry_df.loc[159, ["open", "high", "low", "close", "volume"]] = [99.35, 99.65, 99.23, 99.55, 1100]

    signal = build_signal(confirm_df=confirm_df, entry_df=entry_df, cfg=cfg, market_ctx=None)

    assert signal.side == "long"
    assert signal.reason == "range_long_confirmed"
    assert signal.entry_type == "range_revert"
    assert signal.stop_loss is not None
    assert signal.take_profit is not None
    assert signal.stop_loss < 99.55 < signal.take_profit


def test_range_reversion_short_signal_uses_dynamic_brackets(tmp_path):
    cfg = _cfg(tmp_path)
    confirm_df = _confirm_range_df()
    entry_df = _entry_range_df()
    entry_df.loc[159, ["open", "high", "low", "close", "volume"]] = [100.65, 100.77, 100.35, 100.45, 1100]

    signal = build_signal(confirm_df=confirm_df, entry_df=entry_df, cfg=cfg, market_ctx=None)

    assert signal.side == "short"
    assert signal.reason == "range_short_confirmed"
    assert signal.entry_type == "range_revert"
    assert signal.stop_loss is not None
    assert signal.take_profit is not None
    assert signal.take_profit < 100.45 < signal.stop_loss


def test_risk_engine_sizes_custom_stop_distance(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.risk.risk_per_trade_pct = 1.0
    cfg.risk.max_notional_usdt = 0
    engine = RiskEngine(cfg)

    plan = engine.plan_order(
        side="long",
        entry_price=100.0,
        account=AccountSnapshot(free_usdt=1000.0, total_usdt=1000.0),
        stop_loss=99.0,
        take_profit=102.0,
    )

    assert plan is not None
    assert round(plan.notional_usdt, 6) == 1000.0
    assert round(plan.expected_loss_usdt, 6) == 10.0
    assert round(plan.expected_profit_usdt, 6) == 20.0
