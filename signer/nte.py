"""塔吉多（异环官方社区）签到渠道。

认证链路（参考 Candy-QAQ/NTE-Auto-Sign）：
  完美世界用户中心(user.laohu.com) 短信登录 → 塔吉多用户中心换 accessToken/refreshToken
每日运行用 refreshToken 换新 accessToken（会轮换，需回写持久化）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
import uuid
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .models import RoleResult

logger = logging.getLogger("NTE")

# ---- 完美世界用户中心（laohu SDK）----
LAOHU_SECRET = "89155cc4e8634ec5b1b6364013b23e3e"
LAOHU_APP_ID = "10550"
LAOHU_BID = "com.pwrd.htassistant"
DEVICETYPE = DEVICENAME = DEVICEMODEL = "LGE-AN10"
TYPE = "16"
VERSIONCODE = "1"
AREACODEID = "1"
DEVICESYS = "12"
SDKVERSION = "4.129.0"
CHANNELID = "1"

URL_SEND_CAPTCHA = "https://user.laohu.com/m/newApi/sendPhoneCaptchaWithOutLogin"
URL_CHECK_CAPTCHA = "https://user.laohu.com/m/newApi/checkPhoneCaptchaWithOutLogin"
URL_SMS_LOGIN = "https://user.laohu.com/openApi/sms/new/login"

# ---- 塔吉多社区 ----
TGD_BASE = "https://bbs-api.tajiduo.com"
USER_CENTER_APP_ID = "10551"
APPVERSION = "1.2.5"
OKHTTP_UA = "okhttp/4.12.0"
DEFAULT_GAME_ID = "1289"
SIGN_GAME_IDS = ("1289", "1257")
COMMUNITY_ID = "1"
USER_CENTER_DS_SECRET = "pUds3dfMkl"

URL_USER_CENTER_LOGIN = f"{TGD_BASE}/usercenter/api/login"
URL_REFRESH_TOKEN = f"{TGD_BASE}/usercenter/api/refreshToken"
URL_GAME_ROLES = f"{TGD_BASE}/usercenter/api/v2/getGameRoles"
URL_APP_SIGNIN = f"{TGD_BASE}/apihub/api/signin"
URL_GAME_SIGNIN = f"{TGD_BASE}/apihub/awapi/sign"
URL_GAME_SIGN_STATE = f"{TGD_BASE}/apihub/awapi/signin/state"
URL_GAME_SIGN_REWARDS = f"{TGD_BASE}/apihub/awapi/sign/rewards"


class NteAuthError(RuntimeError):
    """refreshToken 失效或登录态异常。"""


def _sig(params: dict, secret: str = LAOHU_SECRET) -> str:
    values = "".join(str(params[k]) for k in sorted(params.keys()))
    return hashlib.md5((values + secret).encode("utf-8")).hexdigest()


def _user_center_ds() -> str:
    import secrets
    import string

    timestamp = str(int(time.time()))
    alphabet = string.ascii_letters + string.digits
    nonce = "".join(secrets.choice(alphabet) for _ in range(8))
    digest = hashlib.md5(
        f"{timestamp}{nonce}{APPVERSION}{USER_CENTER_DS_SECRET}".encode("utf-8")
    ).hexdigest()
    return f"{timestamp},{nonce},{digest}"


def _aes_b64(text: str, secret: str = LAOHU_SECRET) -> str:
    key = secret[-16:].encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode("utf-8")


def _already_signed(msg: str) -> bool:
    return any(k in msg for k in ("已签", "签到过", "重复"))


def _json_of(resp: httpx.Response, action: str) -> dict:
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"{action} 响应解析失败 HTTP {resp.status_code}: {e}") from e


class NteClient:
    def __init__(self, refresh_token: str = "", uid: str = "10000000", device_id: str | None = None):
        self.refresh_token = refresh_token.strip()
        self.uid = uid or "10000000"
        self.device_id = device_id or uuid.uuid4().hex
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NteClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------ 登录

    def _laohu_common(self) -> dict:
        return {
            "deviceType": DEVICETYPE,
            "deviceId": self.device_id,
            "deviceName": DEVICENAME,
            "versionCode": VERSIONCODE,
            "t": str(int(time.time())),
            "areaCodeId": AREACODEID,
            "appId": LAOHU_APP_ID,
            "deviceSys": DEVICESYS,
            "cellphone": "",
            "deviceModel": DEVICEMODEL,
            "sdkVersion": SDKVERSION,
            "bid": LAOHU_BID,
            "channelId": CHANNELID,
        }

    async def send_captcha(self, phone: str) -> None:
        data = self._laohu_common()
        data["cellphone"] = phone
        data["type"] = TYPE
        data["sign"] = _sig(data)
        resp = await self._client.post(
            URL_SEND_CAPTCHA,
            content=urlencode(data),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r = _json_of(resp, "发送验证码")
        if r.get("code") != 0:
            raise NteAuthError(f"发送验证码失败: {r.get('message') or r.get('msg') or r}")

    async def sms_login(self, phone: str, code: str) -> tuple[str, str]:
        """完美世界短信登录 → 换取塔吉多 accessToken/refreshToken。"""
        base = self._laohu_common()
        base.update(
            {
                "idfa": "",
                "sign": "",
                "adm": "",
                "type": TYPE,
                "version": VERSIONCODE,
                "mac": "",
                "t": str(int(time.time() * 1000)),
                "captcha": _aes_b64(code),
                "cellphone": _aes_b64(phone),
            }
        )
        base["sign"] = _sig(base)
        resp = await self._client.post(
            URL_SMS_LOGIN,
            content=urlencode(base),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r = _json_of(resp, "登录")
        if r.get("code") != 0:
            raise NteAuthError(f"完美世界登录失败: {r.get('message') or r.get('msg') or r}")
        result = r.get("result") or {}
        laohu_token, user_id = result.get("token"), result.get("userId")
        if not laohu_token or user_id is None:
            raise NteAuthError(f"登录结果缺少 token/userId: {r}")

        headers = {
            "platform": "android",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "deviceid": self.device_id,
            "Authorization": "",
            "appVersion": APPVERSION,
            "uid": "0",
            "debug-uid": "3",
            "ds": _user_center_ds(),
            "User-Agent": OKHTTP_UA,
        }
        payload = {"token": laohu_token, "userIdentity": str(user_id), "appId": USER_CENTER_APP_ID}
        resp = await self._client.post(URL_USER_CENTER_LOGIN, headers=headers, content=urlencode(payload))
        r = _json_of(resp, "塔吉多登录")
        if r.get("code") != 0:
            raise NteAuthError(f"塔吉多登录失败: {r.get('msg') or r}")
        data = r.get("data") or {}
        if not data.get("accessToken") or not data.get("refreshToken"):
            raise NteAuthError(f"塔吉多返回缺少 accessToken/refreshToken: {r}")
        if data.get("uid"):
            self.uid = str(data["uid"])
        self.refresh_token = data["refreshToken"]
        return data["accessToken"], self.uid

    # ------------------------------------------------------------ 凭证与数据

    async def access_token(self) -> str:
        if not self.refresh_token:
            raise NteAuthError("缺少 refreshToken")
        headers = {
            "platform": "android",
            "Accept": "application/json, text/plain, */*",
            "deviceid": self.device_id,
            "Authorization": self.refresh_token,
            "appVersion": APPVERSION,
            "uid": str(self.uid or "0"),
            "debug-uid": "3",
            "ds": _user_center_ds(),
            "User-Agent": OKHTTP_UA,
        }
        resp = await self._client.post(URL_REFRESH_TOKEN, headers=headers)
        if resp.status_code == 402:
            raise NteAuthError("refreshToken 已失效，请重新登录")
        r = _json_of(resp, "刷新token")
        if r.get("code") != 0:
            msg = str(r.get("msg") or r)
            raise NteAuthError(("refreshToken 已失效，请重新登录" if "402" in msg or "失效" in msg else f"刷新token失败: {msg}"))
        data = r.get("data") or {}
        if not data.get("accessToken") or not data.get("refreshToken"):
            raise NteAuthError(f"刷新token返回缺少字段: {r}")
        self.refresh_token = data["refreshToken"]
        if data.get("uid"):
            self.uid = str(data["uid"])
        return data["accessToken"]

    def _tgd_headers(self, access_token: str) -> dict:
        return {
            "platform": "android",
            "Accept": "application/json",
            "authorization": access_token,
            "uid": self.uid,
            "deviceid": self.device_id,
            "appversion": APPVERSION,
            "ds": _user_center_ds(),
            "User-Agent": OKHTTP_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def get_roles(self, access_token: str, game_id: str = DEFAULT_GAME_ID) -> list[dict]:
        resp = await self._client.get(
            URL_GAME_ROLES,
            headers=self._tgd_headers(access_token),
            params={"gameId": game_id},
        )
        r = _json_of(resp, "获取角色列表")
        if r.get("code") != 0:
            raise RuntimeError(f"获取角色列表失败: {r.get('msg') or r}")
        roles = []
        for role in (r.get("data") or {}).get("roles", []):
            role_id = str(role.get("roleId", "")).strip()
            if role_id:
                roles.append(
                    {
                        "roleId": role_id,
                        "roleName": str(role.get("roleName") or role_id),
                        "level": role.get("lev"),
                        "serverId": str(role.get("serverId") or ""),
                    }
                )
        return roles

    async def app_signin(self, access_token: str) -> RoleResult:
        result = RoleResult(
            account_uid="", game="nte-app", role_uid="", nickname="社区", channel_name="塔吉多", success=False
        )
        resp = await self._client.post(
            URL_APP_SIGNIN,
            headers=self._tgd_headers(access_token),
            content=urlencode({"communityId": COMMUNITY_ID}),
        )
        r = _json_of(resp, "社区签到")
        if r.get("code") == 0:
            data = r.get("data") or {}
            result.success = True
            result.awards = [f"经验+{data.get('exp', 0)}", f"金币+{data.get('goldCoin', 0)}"]
        elif _already_signed(str(r.get("msg") or "")):
            result.already_signed = True
        else:
            result.error = str(r.get("msg") or r)
        return result

    async def game_signin(self, access_token: str, role: dict) -> RoleResult:
        role_id = role["roleId"]
        result = RoleResult(
            account_uid="",
            game="neverness-to-everness",
            role_uid=role_id,
            nickname=f"{role['roleName']}({role['level']}级)" if role.get("level") else role["roleName"],
            channel_name="塔吉多",
            success=False,
        )
        headers = {
            "platform": "android",
            "authorization": access_token,
            "appversion": APPVERSION,
            "User-Agent": OKHTTP_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        errors: list[str] = []
        for game_id in SIGN_GAME_IDS:
            resp = await self._client.post(
                URL_GAME_SIGNIN,
                headers=headers,
                content=urlencode({"roleId": role_id, "gameId": game_id}),
            )
            r = _json_of(resp, f"游戏签到(gameId={game_id})")
            if r.get("code") == 0:
                result.success = True
                reward = await self._today_reward(access_token, role_id, game_id)
                if reward:
                    result.awards.append(reward)
                return result
            msg = str(r.get("msg") or r)
            if _already_signed(msg):
                state = await self._sign_state(access_token, game_id)
                if state and state.get("todaySign"):
                    result.already_signed = True
                    return result
            errors.append(f"gameId={game_id}: {msg}")
        result.error = "；".join(errors) or "游戏签到失败"
        return result

    async def _sign_state(self, access_token: str, game_id: str) -> dict | None:
        try:
            resp = await self._client.get(
                URL_GAME_SIGN_STATE,
                headers={"Authorization": access_token},
                params={"gameId": game_id},
            )
            r = _json_of(resp, "查询签到状态")
            return r.get("data") if r.get("code") == 0 else None
        except Exception:
            return None

    async def _today_reward(self, access_token: str, role_id: str, game_id: str) -> str:
        try:
            state = await self._sign_state(access_token, game_id)
            days = int((state or {}).get("days") or 0)
            if days <= 0:
                return ""
            resp = await self._client.get(
                URL_GAME_SIGN_REWARDS,
                headers={"Authorization": access_token},
                params={"gameId": game_id, "roleId": role_id},
            )
            r = _json_of(resp, "查询奖励")
            items = r.get("data")
            if isinstance(items, list) and len(items) >= days:
                item = items[days - 1] or {}
                name = item.get("name") or item.get("itemName") or ""
                num = item.get("num") or item.get("count") or ""
                return f"{name}x{num}" if name else ""
        except Exception as e:
            logger.debug("查询今日奖励失败: %s", e)
        return ""

    async def sign_all(self) -> list[RoleResult]:
        access = await self.access_token()
        results = [await self.app_signin(access)]
        roles = await self.get_roles(access)
        for role in roles:
            results.append(await self.game_signin(access, role))
        if len(results) == 1:
            results.append(
                RoleResult(
                    account_uid="",
                    game="neverness-to-everness",
                    role_uid="",
                    nickname="无角色",
                    channel_name="塔吉多",
                    success=False,
                    error="未找到游戏角色（确认已创建角色并绑定社区）",
                )
            )
        return results
