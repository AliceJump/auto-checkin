"""森空岛自动签到服务 - CLI 入口。

用法:
  python main.py login [--token XXX] [--games arknights,endfield]
                                        登录并保存账号（token 或短信验证码）
  python main.py sign [--uid UID] [--force]   立即签到
  python main.py list                          查看账号与状态
  python main.py remove <uid>                  删除账号
  python main.py enable|disable <uid>          启用/禁用账号
  python main.py test-notify                   发送测试通知
  python main.py run                           启动守护进程（每日定时签到）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from signer.api import SklandClient, SklandError
from signer.config import load_app_config
from signer.logsetup import setup_logging
from signer.models import (
    ALL_GAMES,
    ALL_PLATFORMS,
    GAME_NAMES,
    PLATFORM_NAMES,
    PLATFORM_KURO,
    PLATFORM_NTE,
    Account,
)
from signer.notifier import Notifier, NotifyConfig
from signer.scheduler import Scheduler
from signer.service import SignService
from signer.storage import AccountStore

logger = logging.getLogger("main")


# --------------------------------------------------------------------- 构建

def build(config_path: str | None = None):
    cfg = load_app_config(config_path)
    setup_logging(cfg.log_dir)
    store = AccountStore(cfg.accounts_file)
    notifier = Notifier(cfg.notify)
    service = SignService(store, notifier, cfg.schedule.timezone)
    scheduler = Scheduler(store, service, cfg.schedule)
    return cfg, store, notifier, service, scheduler


# --------------------------------------------------------------------- 登录

async def _login_flow(client: SklandClient) -> tuple[str, str]:
    """返回长期鹰角 token 与使用的登录方式描述。"""
    print("选择登录方式:")
    print("  1. 粘贴鹰角通行证 token")
    print("  2. 手机号 + 短信验证码")
    print("  3. 手机号 + 密码")
    choice = input("请输入 [1/2/3]（回车默认 2）: ").strip() or "2"

    if choice == "1":
        token = input("请粘贴 token: ").strip()
        if not token:
            raise SystemExit("token 不能为空")
        # 校验有效性
        cred = await client.refresh_credential(token)
        await client.get_binding_list(cred)
        return token, "token"

    phone = input("请输入手机号: ").strip()
    if not phone:
        raise SystemExit("手机号不能为空")

    if choice == "3":
        import getpass

        password = getpass.getpass("请输入密码（输入不回显）: ").strip()
        if not password:
            raise SystemExit("密码不能为空")
        token = await client.get_token_by_password(phone, password)
        print("密码登录成功，已获取长期 token")
        return token, "密码"

    await client.send_phone_code(phone)
    code = input("验证码已发送，请输入短信验证码: ").strip()
    if not code:
        raise SystemExit("验证码不能为空")
    token = await client.get_token_by_phone_code(phone, code)
    print("短信登录成功，已获取长期 token")
    return token, "短信"


def _choose_games() -> list[str]:
    print("选择要签到的游戏（多选用逗号分隔，回车=全部）:")
    for i, game in enumerate(ALL_GAMES, 1):
        print(f"  {i}. {GAME_NAMES[game]} ({game})")
    raw = input("请输入 [1/2/1,2]: ").strip()
    if not raw:
        return list(ALL_GAMES)
    picked = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip().lower()
        if part in ALL_GAMES:
            picked.append(part)
        elif part.isdigit() and 1 <= int(part) <= len(ALL_GAMES):
            picked.append(ALL_GAMES[int(part) - 1])
    return list(dict.fromkeys(picked)) or list(ALL_GAMES)


def _choose_platform() -> str:
    print("选择账号平台:")
    for i, p in enumerate(ALL_PLATFORMS, 1):
        print(f"  {i}. {PLATFORM_NAMES[p]}")
    raw = input("请输入 [1/2/3]（回车默认 1）: ").strip() or "1"
    if raw.isdigit() and 1 <= int(raw) <= len(ALL_PLATFORMS):
        return ALL_PLATFORMS[int(raw) - 1]
    if raw in ALL_PLATFORMS:
        return raw
    raise SystemExit("无效的平台选择")


async def _login_skland(args) -> Account:
    async with SklandClient() as client:
        try:
            if args.token:
                token, method = args.token.strip(), "token"
                cred = await client.refresh_credential(token)
                await client.get_binding_list(cred)
            else:
                token, method = await _login_flow(client)

            bindings = await client.get_binding_list(await client.refresh_credential(token))
        except SklandError as e:
            raise SystemExit(f"登录失败: {e}")

        if not bindings:
            raise SystemExit("该账号没有绑定明日方舟或终末地角色")

        print("\n绑定角色:")
        for b in bindings:
            roles_note = f"，{len(b.roles)} 个终末地角色" if b.roles else ""
            print(f"  - {GAME_NAMES.get(b.app_code, b.app_code)} {b.nickname}({b.channel_name}) uid={b.uid}{roles_note}")

        games = (
            [g for g in (args.games or "").split(",") if g.strip() in ALL_GAMES]
            if args.games
            else []
        )
        if not games:
            games = _choose_games()

        # 账号命名优先用所选游戏的角色名；终末地绑定的昵称在 roles 里
        chosen = [b for b in bindings if b.app_code in games] or bindings
        primary = (
            next((b for b in chosen if b.app_code == "arknights"), None)
            or next((b for b in chosen if b.app_code == "endfield"), None)
            or chosen[0]
        )
        nickname = primary.nickname or "Unknown"
        if nickname == "Unknown" or (not primary.nickname and primary.roles):
            role_nick = (primary.roles[0].get("nickname") if primary.roles else "") or (
                (primary.default_role or {}).get("nickname")
                if hasattr(primary, "default_role")
                else ""
            )
            if role_nick:
                nickname = role_nick

        return Account(
            uid=primary.uid,
            nickname=nickname,
            token=token,
            device_id=client.device_id,
            games=games,
        ), method


async def _login_kuro(args) -> tuple[Account, str]:
    from signer.kuro import KuroAuthError, KuroClient

    print("库街区登录方式:")
    print("  1. 粘贴 token")
    print("  2. 手机号 + 短信验证码")
    choice = input("请输入 [1/2]（回车默认 2）: ").strip() or "2"

    async with KuroClient() as client:
        if choice == "1":
            token = args.token or input("请粘贴库街区 token（抓包 api.kurobbs.com 的 token 字段）: ").strip()
            if not token:
                raise SystemExit("token 不能为空")
            client.token = token
            method = "token"
        else:
            phone = input("请输入手机号: ").strip()
            if not phone:
                raise SystemExit("手机号不能为空")
            await client.send_sms_code(phone)
            code = input("验证码已发送，请输入短信验证码: ").strip()
            if not code:
                raise SystemExit("验证码不能为空")
            info = await client.sms_login(phone, code)
            client.token = info["token"]
            method = "短信"

        try:
            roles = await client.get_roles()
        except KuroAuthError as e:
            raise SystemExit(f"token 校验失败: {e}")
        if not roles:
            raise SystemExit("token 有效但没有鸣潮角色，请先在库街区绑定角色")
        for r in roles:
            print(f"  - 鸣潮 {r['nickName']} roleId={r['roleId']}")
        first = roles[0]

    account = Account(
        uid=f"kuro_{first['roleId']}",
        nickname=first["nickName"],
        token=client.token,
        platform=PLATFORM_KURO,
        games=["wuthering-waves"],
    )
    return account, method


async def _login_nte(args) -> tuple[Account, str]:
    from signer.nte import NteAuthError, NteClient

    print("塔吉多登录方式:")
    print("  1. 粘贴 refreshToken")
    print("  2. 手机号 + 短信验证码（完美世界账号）")
    choice = input("请输入 [1/2]（回车默认 2）: ").strip() or "2"

    async with NteClient() as client:
        try:
            if choice == "1":
                rt = input("请粘贴 refreshToken: ").strip()
                if not rt:
                    raise SystemExit("refreshToken 不能为空")
                client.refresh_token = rt
                access = await client.access_token()
                method = "refreshToken"
            else:
                phone = input("请输入手机号: ").strip()
                if not phone:
                    raise SystemExit("手机号不能为空")
                await client.send_captcha(phone)
                code = input("验证码已发送，请输入短信验证码: ").strip()
                if not code:
                    raise SystemExit("验证码不能为空")
                access, uid = await client.sms_login(phone, code)
                method = "短信"
            role_ids = await client.get_roles(access)
        except NteAuthError as e:
            raise SystemExit(f"登录失败: {e}")

        if not role_ids:
            raise SystemExit("登录成功但未找到异环游戏角色")
        for r in role_ids:
            print(f"  - 异环 {r['roleName']}({r['level']}级) roleId={r['roleId']}")
        first = role_ids[0]

    return Account(
        uid=f"nte_{client.uid}",
        nickname=first["roleName"],
        token=client.refresh_token,
        device_id=client.device_id,
        platform=PLATFORM_NTE,
        games=["neverness-to-everness"],
    ), method


async def cmd_login(args) -> int:
    _, store, _, service, _ = build(args.config)
    platform = args.platform if args.platform in ALL_PLATFORMS else _choose_platform()
    if platform == PLATFORM_KURO:
        account, method = await _login_kuro(args)
    elif platform == PLATFORM_NTE:
        account, method = await _login_nte(args)
    else:
        result = await _login_skland(args)
        account, method = result[0], result[1]

    store.upsert(account)
    plat_name = PLATFORM_NAMES.get(account.platform, account.platform)
    print(f"\n账号已保存: [{plat_name}] {account.uid} {account.nickname}（登录方式: {method}）")
    print("正在立即执行一次签到...")
    result_text = await service.run(uid=account.uid, force=True, notify=False)
    print(result_text)
    return 0


# --------------------------------------------------------------------- 其他命令

async def cmd_sign(args) -> int:
    _, store, _, service, _ = build(args.config)
    if args.uid and not store.get(args.uid):
        logger.error("未找到账号 %s", args.uid)
        return 1
    print(await service.run(uid=args.uid or "", force=args.force))
    return 0


async def cmd_list(args) -> int:
    _, store, *_ = build(args.config)
    accounts = store.load()
    if not accounts:
        print("暂无账号，先执行: python main.py login")
        return 0
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    for a in accounts:
        status = "启用" if a.enabled else "禁用"
        last = a.last_success_at or "从未成功"
        err = f"\n       上次错误: {a.last_error}" if a.last_error else ""
        games = "/".join(GAME_NAMES.get(g, g) for g in a.games)
        print(f"[{status}] {a.uid} {a.nickname}  游戏: {games}")
        print(f"       上次成功: {last}  今日已签: {'是' if a.last_run_date == today else '否'}{err}")
    return 0


async def cmd_remove(args) -> int:
    _, store, *_ = build(args.config)
    ok = store.remove(args.uid)
    print("已删除" if ok else f"未找到账号 {args.uid}")
    return 0 if ok else 1


async def cmd_toggle(args) -> int:
    _, store, *_ = build(args.config)
    account = store.get(args.uid)
    if not account:
        print(f"未找到账号 {args.uid}")
        return 1
    account.enabled = args.command == "enable"
    store.replace_account(account)
    print(f"账号 {account.uid} 已{'启用' if account.enabled else '禁用'}")
    return 0


async def cmd_test_notify(args) -> int:
    cfg, _, notifier, *_ = build(args.config)
    enabled = [
        name
        for name, on in {
            "telegram": cfg.notify.telegram_enabled,
            "serverchan": cfg.notify.serverchan_enabled,
            "pushplus": cfg.notify.pushplus_enabled,
            f"email({cfg.notify.email.username})": cfg.notify.email.enabled,
        }.items()
        if on
    ]
    print(f"启用渠道: {', '.join(enabled) or '无（仅日志）'}")
    await notifier.send("自动签到测试", "这是一条测试通知，收到即配置成功 ✅")
    print("已发送，请到各渠道确认")
    return 0


async def cmd_run(args) -> int:
    cfg, _, _, service, scheduler = build(args.config)
    if cfg.web.enabled:
        from signer.webui import start_webui

        start_webui(cfg.web.host, cfg.web.port, service)
        logger.info("状态看板已启动: http://%s:%d", cfg.web.host, cfg.web.port)
    logger.info(
        "守护进程启动，每日 %02d:%02d（%s）签到，Ctrl+C 退出",
        scheduler.config.hour,
        scheduler.config.minute,
        scheduler.config.timezone,
    )
    await scheduler.run_forever()


COMMANDS = {
    "login": cmd_login,
    "sign": cmd_sign,
    "list": cmd_list,
    "remove": cmd_remove,
    "enable": cmd_toggle,
    "disable": cmd_toggle,
    "test-notify": cmd_test_notify,
    "run": cmd_run,
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="森空岛自动签到服务")
    parser.add_argument("--config", help="config.yaml 路径（默认自动查找）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="登录并保存账号")
    p.add_argument("--platform", help="平台: skland / kuro / nte")
    p.add_argument("--token", help="非交互：直接使用 token（森空岛=鹰角token，库街区=社区token）")
    p.add_argument("--games", help="森空岛：逗号分隔，如 arknights,endfield")

    p = sub.add_parser("sign", help="立即签到")
    p.add_argument("--uid", default="", help="仅签指定账号")
    p.add_argument("--force", action="store_true", help="忽略今日已签标记强制重签")

    sub.add_parser("list", help="查看账号")

    p = sub.add_parser("remove", help="删除账号")
    p.add_argument("uid")

    p = sub.add_parser("enable", help="启用账号")
    p.add_argument("uid")
    p = sub.add_parser("disable", help="禁用账号")
    p.add_argument("uid")

    sub.add_parser("test-notify", help="发送测试通知")
    sub.add_parser("run", help="启动定时守护进程")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    handler = COMMANDS[args.command]
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\n已退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
