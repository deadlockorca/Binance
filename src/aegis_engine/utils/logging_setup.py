from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: str, file_path: str, rich_console: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if rich_console:
        try:
            from rich.logging import RichHandler

            stream = RichHandler(rich_tracebacks=True, show_path=False)
            stream.setFormatter(logging.Formatter("%(message)s"))
        except Exception:
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
    else:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
    root.addHandler(stream)

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
