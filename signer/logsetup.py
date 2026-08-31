"""日志配置：控制台 + 滚动文件。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        log_dir / "signer.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FMT))
    root.addHandler(file_handler)
