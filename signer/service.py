"""签到编排：遍历账号 → 签到 → 汇总 → 通知，并维护账号状态。"""

import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .api import SklandAuthError, SklandClient
from .kuro import KuroAuthError, KuroClient
from .models import (
    Account,
    PLATFORM_KURO,
    PLATFORM_NAMES,
    PLATFORM_NTE,
    PLATFORM_SKLAND,
    RoleResult,
)
from .nte import NteAuthError, NteClient
from .notifier import Notifier
from .storage import AccountStore

logger = logging.getLogger("Service")

AUTH_ERRORS = (SklandAuthError, KuroAuthError, NteAuthError)


class SignService:
    def __init__(self, store: AccountStore, notifier: Notifier, timezone: str = ""):
        self.store = store
        self.notifier = notifier
        self.timezone = timezone or ""
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        """与调度器同一时钟：优先配置时区（北京时间），无效回退本地。"""
        if self.timezone:
            try:
                return datetime.now(ZoneInfo(self.timezone))
            except Exception:
                logger.warning("时区 %s 无效，回退系统本地时间", self.timezone)
        return datetime.now()

    # ------------------------------------------------------------------ 单账号

    async def _sign_one(self, account: Account) -> list[RoleResult]:
        started = datetime.now()
        try:
            if account.platform == PLATFORM_KURO:
                results = await self._sign_kuro(account)
            elif account.platform == PLATFORM_NTE:
                results = await self._sign_nte(account)
            else:
                async with SklandClient(account.device_id or None) as client:
                    if not account.device_id:
                        account.device_id = client.device_id
                    results = await client.sign_account(account)
        except AUTH_ERRORS as e:
            logger.warning("账号 %s 认证失败: %s", account.uid, e)
            account.auth_failed_notified = True
            account.last_error = f"认证失败: {e}"
            self.store.replace_account(account)
            await self.notifier.send(
                "自动签到 - 凭证失效告警",
                f"账号 {account.uid}（{account.nickname}）的 token 已失效或换取凭据失败，"
                f"请在服务器上执行 `python main.py login` 重新登录。\n错误: {e}",
            )
            return [
                RoleResult(
                    account_uid=account.uid,
                    game="",
                    role_uid="",
                    nickname=account.nickname,
                    channel_name="",
                    success=False,
                    error=f"认证失败（token 可能已过期）: {e}",
                )
            ]
        except Exception as e:
            account.last_error = str(e)
            self.store.replace_account(account)
            logger.error("账号 %s 签到异常: %s", account.uid, e)
            return []

        elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)
        all_ok = bool(results) and all(r.ok for r in results)
        if all_ok:
            account.last_error = ""
            account.last_success_at = self._now().strftime("%Y-%m-%d %H:%M:%S")
            account.auth_failed_notified = False
        elif results:
            account.last_error = "; ".join(
                f"{r.game_name}/{r.nickname}: {r.error}" for r in results if not r.ok
            )
        self.store.replace_account(account)
        logger.info(
            "账号 %s 签到完成 roles=%d ok=%s elapsed=%dms",
            account.uid, len(results), all_ok, elapsed_ms,
        )
        return results

    # ------------------------------------------------------------- 平台渠道

    async def _sign_kuro(self, account: Account) -> list[RoleResult]:
        async with KuroClient(account.token, account_uid=account.uid) as client:
            return await client.sign_all()

    async def _sign_nte(self, account: Account) -> list[RoleResult]:
        async with NteClient(account.token, device_id=account.device_id or None) as client:
            try:
                results = await client.sign_all()
            except Exception:
                # refreshToken 轮换后即使后续步骤失败也必须落盘，否则凭证丢失
                if client.refresh_token and client.refresh_token != account.token:
                    account.token = client.refresh_token
                    self.store.replace_account(account)
                    logger.info("已保存轮换后的 nte refreshToken uid=%s", account.uid)
            else:
                if not account.device_id:
                    account.device_id = client.device_id
                if client.refresh_token and client.refresh_token != account.token:
                    account.token = client.refresh_token
                    self.store.replace_account(account)
                return results
            return []

    # ------------------------------------------------------------------ 汇总运行

    async def run(self, uid: str = "", force: bool = False, notify: bool = True) -> str:
        """执行签到并返回汇总文本。uid 为空时签全部启用的账号。"""
        async with self._lock:
            today = self._now().strftime("%Y-%m-%d")
            accounts = [a for a in self.store.load() if a.enabled]
            if uid:
                accounts = [a for a in accounts if a.uid == uid]
                if not accounts:
                    return "未找到该账号或账号已禁用"

            title = f"自动签到日报 {today}"
            entries: list[tuple[Account, list[RoleResult], str]] = []
            sections: list[str] = []
            ran_accounts = 0
            for account in accounts:
                if not force and account.last_run_date == today:
                    sections.append(
                        f"[{account.uid} {account.nickname}] 今日已完成，跳过"
                    )
                    continue
                results = await self._sign_one(account)
                ran_accounts += 1
                if results:
                    account.last_run_date = today
                    self.store.replace_account(account)
                sections.append(format_account_results(account, results))
                entries.append((account, results, ""))

            summary = "\n\n".join(sections) or "没有已启用的账号，请先 python main.py login"
            logger.info("本轮签到结束 accounts=%d", len(accounts))
            self.write_status_snapshot(today, entries)
            if notify and ran_accounts:
                # 邮件只发签到结果；游戏状态等 📊 信息条目仅进看板
                email_entries = [
                    (a, [r for r in rs if not r.info], note) for a, rs, note in entries
                ]
                await self.notifier.send(
                    title, summary, build_html_report(today, email_entries)
                )
            return f"{title}\n\n{summary}"

    # ------------------------------------------------------------- 状态快照/看板

    @property
    def status_file(self):
        return self.store.path.parent / "status.json"

    def write_status_snapshot(self, today: str, entries: list[tuple[Account, list[RoleResult], str]]) -> None:
        payload = {
            "updated_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            "date": today,
            "accounts": [self._serialize_account(a, rs, note) for a, rs, note in entries],
        }
        tmp = self.status_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.status_file)

    def load_status_snapshot(self) -> dict | None:
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _serialize_account(self, account: Account, results: list[RoleResult], note: str) -> dict:
        items = []
        for r in results or []:
            kind = "info" if r.info else ("ok" if r.success else ("already" if r.already_signed else "fail"))
            items.append(
                {
                    "kind": kind,
                    "line": _role_line(r),
                    "game": "" if r.info else r.game_name,
                    "nick": r.nickname,
                    "channel": r.channel_name,
                    "awards": r.awards,
                    "stats": r.stats,
                    "error": r.error,
                }
            )
        if note and not items:
            items.append({"kind": "info", "line": note, "game": "", "nick": "", "channel": "", "awards": [], "stats": None, "error": ""})
        return {
            "uid": account.uid,
            "nickname": account.nickname,
            "platform": account.platform,
            "platform_name": PLATFORM_NAMES.get(account.platform, account.platform),
            "enabled": account.enabled,
            "last_success_at": account.last_success_at,
            "last_run_date": account.last_run_date,
            "items": items,
        }

    async def refresh_status_only(self) -> dict:
        """不签到，仅拉取各账号实时状态（角色/游戏数据），更新并返回快照。"""
        today = self._now().strftime("%Y-%m-%d")
        entries: list[tuple[Account, list[RoleResult], str]] = []
        for account in [a for a in self.store.load() if a.enabled]:
            try:
                if account.platform == PLATFORM_KURO:
                    results = await self._status_kuro(account)
                elif account.platform == PLATFORM_NTE:
                    results = await self._status_nte(account)
                else:
                    results = await self._status_skland(account)
            except AUTH_ERRORS as e:
                results = [
                    RoleResult(
                        account_uid=account.uid, game="", role_uid="", nickname=account.nickname,
                        channel_name="", success=False, error=f"凭证失效: {e}",
                    )
                ]
            except Exception as e:
                logger.exception("拉取状态失败 uid=%s", account.uid)
                results = []
            entries.append((account, results, ""))
        self.write_status_snapshot(today, entries)
        return json.loads(self.status_file.read_text(encoding="utf-8"))

    async def _status_kuro(self, account: Account) -> list[RoleResult]:
        out: list[RoleResult] = []
        async with KuroClient(account.token, account_uid=account.uid) as client:
            roles = await client.get_roles()
            for role in roles:
                out.append(await client.get_widget_status(role))
        return out

    async def _status_nte(self, account: Account) -> list[RoleResult]:
        async with NteClient(account.token, device_id=account.device_id or None) as client:
            try:
                access = await client.access_token()
                roles = await client.get_roles(access)
            except Exception:
                if client.refresh_token and client.refresh_token != account.token:
                    account.token = client.refresh_token
                    self.store.replace_account(account)
                raise
            if client.refresh_token != account.token:
                account.token = client.refresh_token
                self.store.replace_account(account)
            out = []
            for role in roles:
                out.append(
                    RoleResult(
                        account_uid=account.uid,
                        game="neverness-to-everness",
                        role_uid=role["roleId"],
                        nickname=f"{role['roleName']}({role['level']}级)",
                        channel_name=f"区服 {role['serverId']}" if role.get("serverId") else "塔吉多",
                        success=True,
                        info=True,
                    )
                )
            return out

    async def _status_skland(self, account: Account) -> list[RoleResult]:
        out: list[RoleResult] = []
        async with SklandClient(account.device_id or None) as client:
            cred = await client.refresh_credential(account.token)
            bindings = await client.get_binding_list(cred)
            for b in bindings:
                names = [r.get("nickname") for r in b.roles if r.get("nickname")] or ([b.nickname] if b.nickname else [])
                for nick in names:
                    out.append(
                        RoleResult(
                            account_uid=account.uid,
                            game=b.app_code,
                            role_uid=b.uid if not b.roles else str(b.roles[0].get("roleId") or ""),
                            nickname=str(nick),
                            channel_name=b.channel_name,
                            success=True,
                            info=True,
                        )
                    )
        return out


# ---------------------------------------------------------------------- 格式化


def _role_line(r: RoleResult) -> str:
    if r.info:
        stats = " · ".join(r.awards) if r.awards else "暂无数据"
        return f"📊 {r.nickname}: {stats}"
    name = f"{r.game_name} {r.nickname}({r.channel_name})".strip()
    if r.success:
        awards = "、".join(r.awards) if r.awards else "无奖励明细"
        return f"✅ {name}: 成功，{awards}"
    if r.already_signed:
        return f"ℹ️ {name}: 今日已签到"
    return f"❌ {name}: 失败，{r.error}"


def build_html_report(today: str, entries: list[tuple[Account, list[RoleResult], str]]) -> str:
    """生成邮件用 HTML 日报。entries: (账号, 角色结果列表, 跳过说明)。"""
    sign_results = [r for _, rs, _ in entries for r in rs if not r.info]
    total_roles = len(sign_results)
    ok = sum(1 for r in sign_results if r.ok)
    fail = total_roles - ok

    cards: list[str] = []
    for account, results, note in entries:
        plat = PLATFORM_NAMES.get(account.platform, account.platform).split("(")[0]
        if note or not results:
            body = f"<li style='color:#6b7280'>{note or '未找到可签到的角色'}</li>"
            if account.last_error and not results:
                body = f"<li style='color:#dc2626'>❌ 失败: {account.last_error}</li>"
        else:
            body = "".join(_result_html(r) for r in results) or "<li>无结果</li>"
        cards.append(
            "<div style='margin:10px 0;padding:10px 14px;background:#f9fafb;"
            "border:1px solid #eceef1;border-radius:8px'>"
            f"<div style='font-weight:600;margin-bottom:6px'>{plat} · {account.nickname}"
            f" <span style='color:#9ca3af;font-weight:400;font-size:12px'>{account.uid}</span></div>"
            f"<ul style='margin:4px 0 0;padding-left:18px;line-height:1.9;font-size:13px'>{body}</ul>"
            "</div>"
        )

    summary_bar = (
        f"<span>账号 <b>{len(entries)}</b></span>&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"<span style='color:#16a34a'>成功/已签 <b>{ok}</b></span>&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"<span style='color:{'#dc2626' if fail else '#6b7280'}'>失败 <b>{fail}</b></span>"
    )
    return (
        "<div style='font-family:-apple-system,\"Segoe UI\",Roboto,\"PingFang SC\","
        "\"Microsoft YaHei\",sans-serif;max-width:620px;margin:0 auto;color:#333'>"
        "<div style='background:#4f6ef7;color:#fff;padding:14px 18px;border-radius:10px 10px 0 0'>"
        "<span style='font-size:17px;font-weight:600'>📧 自动签到日报</span>"
        f"<span style='float:right;opacity:.9'>{today}</span></div>"
        "<div style='border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;"
        "padding:14px 18px'>"
        f"<p style='margin:0 0 6px;color:#555;font-size:13px'>{summary_bar}</p>"
        + "".join(cards)
        + "</div>"
        "<p style='color:#9ca3af;font-size:12px;text-align:center;margin-top:10px'>"
        "auto-checkin · 每日北京时间 05:00 自动执行</p></div>"
    )


def _stats_table_html(stats: list[dict]) -> str:
    rows = "".join(
        "<tr>"
        f"<td style='padding:3px 10px 3px 0'>{s['name']}</td>"
        f"<td style='padding:3px 12px;font-variant-numeric:tabular-nums'>{s['cur']}"
        + (f" / {s['total']}" if s.get("total") else "")
        + "</td>"
        "</tr>"
        for s in stats
    )
    return (
        "<table style='border-collapse:collapse;font-size:12.5px;color:#4b5563;"
        "margin:4px 0 2px;background:#fff;border:1px solid #e5e7eb;border-radius:6px'>"
        + rows
        + "</table>"
    )


def _result_html(r: RoleResult) -> str:
    icon_color = "#16a34a" if r.success else ("#2563eb" if r.already_signed else "#dc2626")
    line = _role_line(r)
    head = f"<span style='color:{icon_color};font-weight:600'>{line[:2]}</span>{line[2:]}"
    if r.info and r.stats:
        return f"<li>{head}{_stats_table_html(r.stats)}</li>"
    return f"<li>{head}</li>"


def format_account_results(account: Account, results: list[RoleResult]) -> str:
    lines = [f"[{account.uid} {account.nickname}]"]
    if not results:
        if account.last_error:
            lines.append(f"❌ 失败: {account.last_error}")
        else:
            lines.append("未找到可签到的角色（检查账号的游戏选择配置）")
        return "\n".join(lines)
    for r in results:
        lines.append(_role_line(r))
    return "\n".join(lines)
