"""森空岛签到 API 客户端。

签名算法与接口流程参考 MAA1999/37Bot 的 plugins/skland 实现，
并参考了 YueHen14/skyland-auto-sign 与 UKMeng/nonebot-plugin-skland-arksign。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from urllib.parse import urlparse

import httpx

from .models import (
    GAME_ARKNIGHTS,
    GAME_ENDFIELD,
    Account,
    Binding,
    Credential,
    RoleResult,
)

APP_CODE = "4ca99fa6b56cc2ba"
AS_BASE = "https://as.hypergryph.com"
SKLAND_BASE = "https://zonai.skland.com"
USER_AGENT = (
    "Skland/1.0.1 (com.hypergryph.skland; build:100001014; Android 31; ) "
    "Okhttp/4.11.0"
)
# 登录相关接口（AS 域名）使用 WebView 风格 UA + 注册过的设备 ID，降低风控概率
LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 "
    "Safari/537.36; SKLand/1.52.1"
)
TIMEOUT = httpx.Timeout(30.0)

ALREADY_SIGNED_KEYS = ("已签到", "重复", "请勿", "already")

logger = logging.getLogger("Skland")


class SklandAuthError(RuntimeError):
    """登录态异常：token 失效或换取 cred 失败。"""


class SklandError(RuntimeError):
    """一般业务错误。"""


def _is_auth_message(message: str) -> bool:
    low = message.lower()
    return any(k in message for k in ("登录", "登陆")) or any(
        k in low for k in ("token", "cred", "unauthorized")
    )


class SklandClient:
    def __init__(self, device_id: str | None = None):
        # 显式传入的 device_id 视为已注册的真实设备（来自 accounts.json），直接复用
        self._login_did = device_id
        self.device_id = device_id or f"B{uuid.uuid4().hex}"
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SklandClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------- 登录请求头

    def _login_headers(self) -> dict[str, str]:
        return {
            "User-Agent": LOGIN_USER_AGENT,
            "Accept-Encoding": "gzip",
            "Connection": "close",
            "dId": self._login_did or self.device_id,
            "X-Requested-With": "com.hypergryph.skland",
        }

    async def ensure_login_device(self) -> str:
        """为登录接口注册真实设备指纹；已有则复用，失败回退随机 ID 并告警。"""
        if self._login_did:
            return self._login_did
        from .deviceid import get_registered_device_id

        try:
            self._login_did = get_registered_device_id()
            self.device_id = self._login_did
            logger.info("设备指纹注册成功 dId=%s...", self._login_did[:12])
        except Exception as e:
            logger.warning("设备指纹注册失败，回退随机设备 ID: %s", e)
        return self._login_did or self.device_id

    # ------------------------------------------------------------------ 签名

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
            "Connection": "close",
            "dId": self.device_id,
        }

    def _signed_headers(
        self, cred: Credential, method: str, url: str, body_or_query: str
    ) -> dict[str, str]:
        parsed = urlparse(url)
        source = parsed.query if method.upper() == "GET" else body_or_query
        timestamp = str(int(time.time()) - 2)
        sign_header = {
            "platform": "",
            "timestamp": timestamp,
            "dId": "",
            "vName": "",
        }
        sign_header_json = json.dumps(sign_header, separators=(",", ":"))
        payload = f"{parsed.path}{source}{timestamp}{sign_header_json}"
        sha = hmac.new(
            cred.token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        sign = hashlib.md5(sha.encode("utf-8")).hexdigest()

        headers = self._base_headers()
        headers.update(
            {
                "cred": cred.cred,
                "sign": sign,
                "platform": sign_header["platform"],
                "timestamp": timestamp,
                "dId": sign_header["dId"],
                "vName": sign_header["vName"],
            }
        )
        return headers

    # ------------------------------------------------------------- 登录/凭据

    async def get_grant_code(self, token: str) -> str:
        await self.ensure_login_device()
        resp = await self._client.post(
            f"{AS_BASE}/user/oauth2/v2/grant",
            headers=self._login_headers(),
            json={"appCode": APP_CODE, "token": token, "type": 0},
        )
        data = _json_of(resp)
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandAuthError(data.get("msg") or data.get("message") or str(data))
        return str(data["data"]["code"])

    async def send_phone_code(self, phone: str) -> None:
        await self.ensure_login_device()
        resp = await self._client.post(
            f"{AS_BASE}/general/v1/send_phone_code",
            headers=self._login_headers(),
            json={"phone": phone, "type": 2},
        )
        data = _json_of(resp)
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandError(data.get("msg") or data.get("message") or str(data))

    async def get_token_by_phone_code(self, phone: str, code: str) -> str:
        await self.ensure_login_device()
        resp = await self._client.post(
            f"{AS_BASE}/user/auth/v2/token_by_phone_code",
            headers=self._login_headers(),
            json={"phone": phone, "code": code},
        )
        data = _json_of(resp)
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandAuthError(data.get("msg") or data.get("message") or str(data))
        return str(data["data"]["token"])

    async def get_token_by_password(self, phone: str, password: str) -> str:
        """手机号 + 密码登录（不依赖短信，风控概率更低）。"""
        await self.ensure_login_device()
        resp = await self._client.post(
            f"{AS_BASE}/user/auth/v1/token_by_phone_password",
            headers=self._login_headers(),
            json={"phone": phone, "password": password},
        )
        data = _json_of(resp)
        if resp.status_code != 200 or data.get("status") != 0:
            raise SklandAuthError(data.get("msg") or data.get("message") or str(data))
        return str(data["data"]["token"])

    async def get_credential(self, grant_code: str) -> Credential:
        await self.ensure_login_device()
        resp = await self._client.post(
            f"{SKLAND_BASE}/web/v1/user/auth/generate_cred_by_code",
            headers=self._login_headers(),
            json={"code": grant_code, "kind": 1},
        )
        data = _json_of(resp)
        if data.get("code") != 0:
            raise SklandAuthError(data.get("message") or str(data))
        return Credential(token=data["data"]["token"], cred=data["data"]["cred"])

    async def refresh_credential(self, token: str) -> Credential:
        """用长期 token 换取新的 cred。"""
        grant = await self.get_grant_code(token)
        return await self.get_credential(grant)

    # ------------------------------------------------------------ 绑定/角色

    async def get_binding_list(self, cred: Credential) -> list[Binding]:
        url = f"{SKLAND_BASE}/api/v1/game/player/binding"
        resp = await self._client.get(url, headers=self._signed_headers(cred, "GET", url, ""))
        data = _json_of(resp)
        if data.get("code") != 0:
            message = data.get("message") or str(data)
            if _is_auth_message(message):
                raise SklandAuthError(message)
            raise SklandError(message)

        bindings: list[Binding] = []
        for item in data.get("data", {}).get("list", []):
            app_code = item.get("appCode")
            if app_code not in (GAME_ARKNIGHTS, GAME_ENDFIELD):
                continue
            for binding in item.get("bindingList", []):
                bindings.append(
                    Binding(
                        app_code=app_code,
                        game_id=int(binding.get("gameId") or 1),
                        uid=str(binding.get("uid") or ""),
                        nickname=str(binding.get("nickName") or "Unknown"),
                        channel_name=str(binding.get("channelName") or "Unknown"),
                        game_name=str(binding.get("gameName") or ""),
                        roles=list(binding.get("roles") or []),
                    )
                )
        return bindings

    # ---------------------------------------------------------------- 签到

    async def sign_arknights(self, cred: Credential, binding: Binding, account_uid: str) -> RoleResult:
        url = f"{SKLAND_BASE}/api/v1/game/attendance"
        body = {"gameId": binding.game_id, "uid": binding.uid}
        body_text = json.dumps(body, separators=(",", ":"))
        headers = self._signed_headers(cred, "POST", url, body_text)
        headers["Content-Type"] = "application/json"
        resp = await self._client.post(url, headers=headers, content=body_text)
        data = _json_of(resp)
        result = RoleResult(
            account_uid=account_uid,
            game=GAME_ARKNIGHTS,
            role_uid=binding.uid,
            nickname=binding.nickname,
            channel_name=binding.channel_name,
            success=False,
        )
        if data.get("code") != 0:
            result.error = data.get("message") or str(data)
            result.already_signed = _looks_already_signed(result.error)
            return result
        for award in data.get("data", {}).get("awards", []):
            resource = award.get("resource") or {}
            name = resource.get("name") or "Unknown"
            count = award.get("count") or 1
            result.awards.append(f"{name}x{count}")
        result.success = True
        return result

    async def sign_endfield_roles(
        self, cred: Credential, binding: Binding, account_uid: str
    ) -> list[RoleResult]:
        url = f"{SKLAND_BASE}/web/v1/game/endfield/attendance"
        results: list[RoleResult] = []
        roles = binding.roles or []
        if not roles:
            return [
                RoleResult(
                    account_uid=account_uid,
                    game=GAME_ENDFIELD,
                    role_uid=binding.uid,
                    nickname=binding.nickname,
                    channel_name=binding.channel_name,
                    success=False,
                    error="没有终末地角色信息",
                )
            ]
        for role in roles:
            role_id = str(role.get("roleId") or "")
            server_id = str(role.get("serverId") or "")
            nickname = str(role.get("nickname") or binding.nickname)
            result = RoleResult(
                account_uid=account_uid,
                game=GAME_ENDFIELD,
                role_uid=role_id,
                nickname=nickname,
                channel_name=binding.channel_name,
                success=False,
            )
            headers = self._signed_headers(cred, "POST", url, "")
            headers["Content-Type"] = "application/json"
            headers["origin"] = "https://game.skland.com"
            headers["referer"] = "https://game.skland.com/"
            headers["sk-game-role"] = f"3_{role_id}_{server_id}"
            resp = await self._client.post(url, headers=headers, content="")
            data = _json_of(resp)
            if data.get("code") != 0:
                result.error = data.get("message") or str(data)
                result.already_signed = _looks_already_signed(result.error)
                results.append(result)
                continue
            resource_map = data.get("data", {}).get("resourceInfoMap", {})
            for award in data.get("data", {}).get("awardIds", []):
                award_id = str(award.get("id") or "")
                resource = resource_map.get(award_id) or {}
                name = resource.get("name") or "Unknown"
                count = resource.get("count") or award.get("count") or 1
                result.awards.append(f"{name}x{count}")
            result.success = True
            results.append(result)
        return results

    async def sign_account(self, account: Account) -> list[RoleResult]:
        """完整流程：token 换 cred → 拉绑定 → 按账号配置的游戏逐角色签到。"""
        cred = await self.refresh_credential(account.token)
        bindings = await self.get_binding_list(cred)
        games = set(account.games)
        results: list[RoleResult] = []
        for binding in bindings:
            if binding.app_code not in games:
                continue
            if binding.app_code == GAME_ENDFIELD:
                results.extend(await self.sign_endfield_roles(cred, binding, account.uid))
            else:
                results.append(await self.sign_arknights(cred, binding, account.uid))
        return results


# --------------------------------------------------------------------- 工具


def _json_of(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception as e:
        raise SklandError(f"响应解析失败 HTTP {resp.status_code}: {e}") from e


def _looks_already_signed(message: str) -> bool:
    low = message.lower()
    return any(k in message for k in ALREADY_SIGNED_KEYS) or "already" in low
