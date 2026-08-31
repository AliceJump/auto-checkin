"""应用配置加载。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .notifier import NotifyConfig
from .scheduler import ScheduleConfig

logger = logging.getLogger("Config")


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 18084


@dataclass
class AppConfig:
    base_dir: Path
    accounts_file: Path
    log_dir: Path
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    web: WebConfig = field(default_factory=WebConfig)


def load_app_config(config_path: str | Path | None = None) -> AppConfig:
    path = _find_config(config_path)
    raw: dict = {}
    if path and path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("读取配置失败（%s），使用默认配置: %s", path, e)
    else:
        logger.info("未找到 config.yaml，使用默认配置")

    base_dir = (path.parent if path else Path.cwd()).resolve()
    accounts_file = Path(str(raw.get("accounts_file") or "accounts.json"))
    log_dir = Path(str(raw.get("log_dir") or "logs"))
    return AppConfig(
        base_dir=base_dir,
        accounts_file=accounts_file if accounts_file.is_absolute() else base_dir / accounts_file,
        log_dir=log_dir if log_dir.is_absolute() else base_dir / log_dir,
        schedule=ScheduleConfig.from_dict(raw.get("schedule")),
        notify=NotifyConfig.from_dict(raw.get("notify")),
        web=_web_from(raw.get("web")),
    )


def _web_from(raw: dict | None) -> WebConfig:
    raw = raw or {}
    return WebConfig(
        enabled=bool(raw.get("enabled", True)),
        host=str(raw.get("host") or "127.0.0.1"),
        port=int(raw.get("port") or 18084),
    )


def _find_config(explicit: str | Path | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return p.resolve()
    for candidate in (Path.cwd() / "config.yaml", Path(__file__).resolve().parent.parent / "config.yaml"):
        if candidate.exists():
            return candidate
    return None
