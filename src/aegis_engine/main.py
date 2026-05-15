from __future__ import annotations

import argparse
import logging
import sys

from ccxt.base.errors import AuthenticationError
from ccxt.base.errors import NotSupported

from aegis_engine.core.bot import TradingEngine
from aegis_engine.core.config import ConfigError, load_config
from aegis_engine.utils.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aegis Engine for Binance USDT-M")
    parser.add_argument("--config", default="configs/config.yaml", help="Path to config yaml")
    parser.add_argument("--mode", choices=["paper", "demo", "live"], help="Override app.mode")
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbols override. Example: BTCUSDT,ETHUSDT",
    )
    parser.add_argument(
        "--market-data-source",
        choices=["execution", "live", "demo"],
        help="Override market data source for signals/context while keeping execution mode unchanged",
    )
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle")
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Required flag to run in live mode",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols_override: list[str] | None = None
    if args.symbols:
        symbols_override = [s.upper().strip() for s in str(args.symbols).split(",") if s.strip()]
        if not symbols_override:
            print("Config error: --symbols is empty", file=sys.stderr)
            return 2
    try:
        cfg = load_config(
            args.config,
            mode_override=args.mode,
            symbols_override=symbols_override,
            market_data_source_override=args.market_data_source,
        )
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.logging.level, cfg.logging.file_live, rich_console=cfg.logging.rich_console)
    logger = logging.getLogger(__name__)

    if cfg.app.mode == "live" and not args.allow_live:
        logger.error("Refuse to run live mode without --allow-live")
        return 3

    logger.info("Starting engine mode=%s symbols=%s", cfg.app.mode, ",".join(cfg.app.symbols))

    bot = TradingEngine(cfg)
    def _log_run_hint(exc: Exception) -> None:
        message = str(exc)
        if isinstance(exc, AuthenticationError) or "\"code\":-2015" in message:
            logger.error(
                "Authentication failed (-2015). Check demo API key/secret, futures permission, IP whitelist, and that key belongs to Binance Demo Futures."
            )
        elif isinstance(exc, NotSupported):
            logger.error("Exchange mode not supported by current endpoints/config: %s", message)

    try:
        if args.once:
            try:
                bot.run_cycle()
                return 0
            except Exception as exc:  # noqa: BLE001
                logger.exception("run_cycle error (once): %s", exc)
                _log_run_hint(exc)
                return 4
        bot.run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_forever error: %s", exc)
        _log_run_hint(exc)
        return 4
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
