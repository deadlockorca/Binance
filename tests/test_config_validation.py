from __future__ import annotations

from pathlib import Path

import yaml

from aegis_engine.core.config import load_config


def test_invalid_execution_modes_are_normalized(tmp_path, monkeypatch):
    source = Path("configs/config.yaml")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))

    data["execution"]["entry_order"] = "bad"
    data["execution"]["exit_order"] = "bad"
    data["execution"]["ref_price"] = "bad"
    data["execution"]["entry_order_by_setup"] = {"retest": "bad", "pullback": "maker", "breakout": "bad"}

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "x")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "y")
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", "x")
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", "y")

    cfg = load_config(cfg_path)

    assert cfg.execution.entry_order == "market"
    assert cfg.execution.exit_order == "taker"
    assert cfg.execution.ref_price == "book"
    assert cfg.execution.entry_order_by_setup["retest"] == "market"
    assert cfg.execution.entry_order_by_setup["pullback"] == "maker"
    assert cfg.execution.entry_order_by_setup["breakout"] == "market"
