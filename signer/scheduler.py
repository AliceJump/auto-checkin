"""每日定时调度：每 30 秒检查一次；支持错过时间点补签与失败限次重试。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .service import SignService
from .storage import AccountStore

logger = logging.getLogger("Scheduler")

CHECK_INTERVAL_SECONDS = 30
DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass
class ScheduleConfig:
    enabled: bool = True
    hour: int = 5               # 默认北京时间 06:00 前
    minute: int = 0
    timezone: str = DEFAULT_TIMEZONE
    catch_up: bool = True       # 启动时若已错过今日时间点则立即补签
    max_attempts: int = 3       # 每日最多尝试轮数（含首轮），防止账号持续失败时反复打接口

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ScheduleConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            hour=int(raw.get("hour", 5)),
            minute=int(raw.get("minute", 0)),
            timezone=str(raw.get("timezone") or DEFAULT_TIMEZONE),
            catch_up=bool(raw.get("catch_up", True)),
            max_attempts=max(1, int(raw.get("max_attempts", 3))),
        )


class Scheduler:
    def __init__(self, store: AccountStore, service: SignService, config: ScheduleConfig):
        self.store = store
        self.service = service
        self.config = config
        self._started_at = self._now()

    def _now(self) -> datetime:
        """按配置时区取当前时间（默认北京时间），无效时回退系统本地时间。"""
        tz_name = self.config.timezone
        if tz_name:
            try:
                return datetime.now(ZoneInfo(tz_name))
            except Exception:
                logger.warning("时区 %s 无效，回退系统本地时间", tz_name)
        return datetime.now()

    async def run_forever(self) -> None:
        logger.info(
            "定时任务启动：每日 %02d:%02d（%s）补签=%s",
            self.config.hour, self.config.minute, self.config.timezone, self.config.catch_up,
        )
        first_tick = True
        while True:
            try:
                await self._tick(catch_up=first_tick and self.config.catch_up)
            except Exception:
                logger.exception("定时任务异常")
            first_tick = False
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _tick(self, catch_up: bool) -> None:
        if not self.config.enabled or not self.store.load():
            return
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        target = now.replace(hour=self.config.hour, minute=self.config.minute, second=0, microsecond=0)
        if now < target:
            return

        meta = self.store.get_daily_meta()
        if meta["date"] == today and meta["attempts"] >= self.config.max_attempts:
            return
        # 进程在今日时间点之后才启动、未开启补签且今天还没跑过：跳过今天
        if not catch_up and meta["date"] != today and self._started_at >= target:
            self.store.set_daily_meta(today, self.config.max_attempts)
            logger.info("已错过今日 %02d:%02d 且未开启补签，跳过", self.config.hour, self.config.minute)
            return
        # 当天首轮未到点不执行；已到点后：
        # - 首轮（date != today）：立即执行
        # - 后续轮次：仅在还有账号未完成时按重试间隔执行
        if meta["date"] == today and meta["attempts"] > 0:
            pending = [a for a in self.store.load() if a.enabled and a.last_run_date != today]
            if not pending:
                return
            next_retry = target + timedelta(minutes=15 * meta["attempts"])
            if now < next_retry:
                return

        attempts = (meta["attempts"] + 1) if meta["date"] == today else 1
        reason = "补签" if (catch_up and meta["date"] != today) else ("重试" if attempts > 1 else "定时")
        logger.info("触发每日签到（%s）date=%s attempts=%d", reason, today, attempts)
        await self.service.run(notify=True)
        self.store.set_daily_meta(today, attempts)
