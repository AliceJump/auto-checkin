"""多渠道通知：日志（始终）+ Telegram / Server酱 / PushPlus / 邮件。"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText

import httpx

logger = logging.getLogger("Notify")


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    auth_code: str = ""
    to_addrs: list[str] = field(default_factory=list)
    prefix: str = "[auto-checkin]"
    timeout: int = 30


@dataclass
class NotifyConfig:
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    serverchan_enabled: bool = False
    serverchan_send_key: str = ""
    pushplus_enabled: bool = False
    pushplus_token: str = ""
    email: EmailConfig = field(default_factory=EmailConfig)

    @classmethod
    def from_dict(cls, raw: dict) -> "NotifyConfig":
        raw = raw or {}
        tg = raw.get("telegram") or {}
        sc = raw.get("serverchan") or {}
        pp = raw.get("pushplus") or {}
        em = raw.get("email") or {}
        to_addrs = em.get("to_addrs") or []
        if isinstance(to_addrs, str):
            to_addrs = [x.strip() for x in to_addrs.split(",") if x.strip()]
        return cls(
            telegram_enabled=bool(tg.get("enabled")),
            telegram_bot_token=str(tg.get("bot_token") or ""),
            telegram_chat_id=str(tg.get("chat_id") or ""),
            serverchan_enabled=bool(sc.get("enabled")),
            serverchan_send_key=str(sc.get("send_key") or ""),
            pushplus_enabled=bool(pp.get("enabled")),
            pushplus_token=str(pp.get("token") or ""),
            email=EmailConfig(
                enabled=bool(em.get("enabled")),
                smtp_host=str(em.get("smtp_host") or ""),
                smtp_port=int(em.get("smtp_port") or 465),
                use_ssl=bool(em.get("use_ssl", True)),
                username=str(em.get("username") or ""),
                auth_code=str(em.get("auth_code") or ""),
                to_addrs=[str(x) for x in to_addrs],
                prefix=str(em.get("prefix") or "[auto-checkin]"),
                timeout=int(em.get("timeout") or 30),
            ),
        )


def _send_email_sync(cfg: EmailConfig, title: str, text: str, html: str | None) -> None:
    if html:
        msg = MIMEText(html, "html", "utf-8")
    else:
        msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(f"{cfg.prefix} {title}", "utf-8")
    msg["From"] = cfg.username
    msg["To"] = ", ".join(cfg.to_addrs)
    if cfg.use_ssl:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
    try:
        server.login(cfg.username, cfg.auth_code)
        server.sendmail(cfg.username, cfg.to_addrs, msg.as_string())
    finally:
        server.quit()


class Notifier:
    def __init__(self, config: NotifyConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, title: str, text: str, html: str | None = None) -> None:
        """推送到所有启用渠道；单渠道失败不影响其他渠道。html 仅邮件渠道使用。"""
        cfg = self.config
        if cfg.telegram_enabled:
            await self._try(f"telegram({cfg.telegram_chat_id})", self._send_telegram(title, text))
        if cfg.serverchan_enabled:
            await self._try("serverchan", self._send_serverchan(title, text))
        if cfg.pushplus_enabled:
            await self._try("pushplus", self._send_pushplus(title, text))
        if cfg.email.enabled and cfg.email.to_addrs:
            await self._try(
                f"email({', '.join(cfg.email.to_addrs)})",
                asyncio.to_thread(_send_email_sync, cfg.email, title, text, html),
            )

    async def _try(self, name: str, coro) -> None:
        try:
            await coro
        except Exception as e:
            logger.error("通知渠道 %s 发送失败: %s", name, e)

    async def _send_telegram(self, title: str, text: str) -> None:
        resp = await self._client.post(
            f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage",
            json={
                "chat_id": self.config.telegram_chat_id,
                "text": f"{title}\n{text}",
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()

    async def _send_serverchan(self, title: str, text: str) -> None:
        resp = await self._client.post(
            f"https://sctapi.ftqq.com/{self.config.serverchan_send_key}.send",
            data={"title": title, "desp": text},
        )
        resp.raise_for_status()

    async def _send_pushplus(self, title: str, text: str) -> None:
        resp = await self._client.post(
            "https://www.pushplus.plus/send",
            json={"token": self.config.pushplus_token, "title": title, "content": text},
        )
        resp.raise_for_status()
