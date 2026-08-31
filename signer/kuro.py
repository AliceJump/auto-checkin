"""库街区（库洛游戏）签到渠道：鸣潮。

token 获取方式：抓包库街区 APP/Web（api.kurobbs.com 任一请求的 token 字段），
长期有效，失效后重新抓取即可。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from .models import RoleResult

logger = logging.getLogger("Kuro")

GAME_ID_WUWA = "3"
SERVER_ID_WUWA = "76402e5b20be2c39f095a152090afddc"
GAME_NAME = "鸣潮"

# 库街区极验滑块的 captcha_id（公开常量，来自库街区 APP 前端）
ANDROID_CAPTCHA_ID = "3f7e2d848ce0cb7e7d019d621e556ce2"


def _solve_geetest() -> str:
    from .geetest import get_sec_code

    return get_sec_code(ANDROID_CAPTCHA_ID)

BASE = "https://api.kurobbs.com"
URL_ROLE_LIST = f"{BASE}/user/role/findRoleList"
URL_SIGN_IN = f"{BASE}/encourage/signIn/v2"
URL_SIGN_RECORD = f"{BASE}/encourage/signIn/queryRecordV2"
URL_SMS_CODE = f"{BASE}/user/getSmsCode"
URL_SDK_LOGIN = f"{BASE}/user/sdkLogin"

CODE_SUCCESS = 200
CODE_ALREADY_SIGNED = 1511

# 库街区 APP 风格请求头（devCode 不做实际校验，取自公开文档示例）
_SMS_HEADERS = {
    "osVersion": "Android",
    "devCode": "073A9EFAC18FC50616DD15808DAE719DBCB904B7",
    "distinct_id": "96b1567b-b5e6-422f-a1dd-7cb1e58c5db7",
    "countryCode": "CN",
    "ip": "192.168.102.138",
    "model": "23127PN0CC",
    "source": "android",
    "lang": "zh-Hans",
    "version": "2.2.0",
    "versionCode": "2200",
    "channelId": "2",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Encoding": "gzip",
    "User-Agent": "okhttp/3.11.0",
}
_LOGIN_HEADERS = {
    "osversion": "Android",
    "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
    "distinct_id": "765485e7-30ce-4496-9a9c-a2ac1c03c02c",
    "countrycode": "CN",
    "ip": "10.0.2.233",
    "model": "2211133C",
    "source": "android",
    "lang": "zh-Hans",
    "version": "1.0.9",
    "versioncode": "1090",
    "content-type": "application/x-www-form-urlencoded",
    "accept-encoding": "gzip",
    "user-agent": "okhttp/3.10.0",
}


class KuroAuthError(RuntimeError):
    """token 失效。"""


def _clean_token(token: str) -> str:
    """容忍粘贴整段 Cookie / JSON 片段的情况，提取纯 JWT。"""
    token = token.strip().strip('"').strip("'")
    if "user_token=" in token:
        token = token.split("user_token=", 1)[1]
    for sep in ('"', "'", ",", ";", "}", " "):
        token = token.split(sep)[0]
    return token.strip()


def _bbs_headers(token: str) -> dict[str, str]:
    """findRoleList 要求的安卓 APP 风格请求头（对齐官方文档）。"""
    return {
        "osversion": "Android",
        "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
        "countrycode": "CN",
        "ip": "10.0.2.233",
        "model": "2211133C",
        "source": "android",
        "lang": "zh-Hans",
        "version": "1.0.9",
        "versioncode": "1090",
        "token": token,
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/3.10.0",
    }


def _game_headers(token: str) -> dict[str, str]:
    """signIn v2 要求的库街区 WebView 风格请求头（对齐官方文档）。"""
    return {
        "pragma": "no-cache",
        "cache-control": "no-cache",
        "accept": "application/json, text/plain, */*",
        "source": "android",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 13; 2211133C Build/TKQ1.220905.001; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/114.0.5735.131 "
            "Mobile Safari/537.36 Kuro/1.0.9 KuroGameBox/1.0.9"
        ),
        "token": token,
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://web-static.kurobbs.com",
        "x-requested-with": "com.kurogame.kjq",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }


class KuroClient:
    def __init__(self, token: str = "", account_uid: str = ""):
        self.token = _clean_token(token)
        self.account_uid = account_uid
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KuroClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------ 登录

    async def send_sms_code(self, phone: str, auto_geetest: bool = True) -> None:
        """发送短信验证码。库街区强制极验滑块校验，默认自动破解。"""
        sec_code = ""
        if auto_geetest:
            from .geetest import get_sec_code

            sec_code = await asyncio.to_thread(_solve_geetest)
        data = _json_of(
            await self._client.post(
                URL_SMS_CODE,
                headers=_SMS_HEADERS,
                data={"mobile": phone, "geeTestData": sec_code},
            )
        )
        if data.get("code") == 242:
            raise KuroAuthError("发送验证码频繁，请稍后再试")
        if data.get("code") != 200:
            raise KuroAuthError(f"发送验证码失败: {data.get('msg') or data}")
        if (data.get("data") or {}).get("geeTest"):
            raise KuroAuthError("极验未通过（可重试）")

    async def sms_login(self, phone: str, code: str) -> dict:
        """验证码登录，返回账号信息 {token, userName, userId}。

        注意：库街区 APP 端单点登录，新 token 会使 APP 上的旧登录失效。
        """
        resp = await self._client.post(
            URL_SDK_LOGIN,
            headers=_LOGIN_HEADERS,
            data={"code": code, "devCode": "2fba3859fe9bfe9099f2696b8648c2c6", "gameList": "", "mobile": phone},
        )
        data = _json_of(resp)
        if data.get("code") != 200:
            msg = str(data.get("msg") or data)
            raise KuroAuthError("验证码错误或已过期" if data.get("code") == -130 else f"登录失败: {msg}")
        result = data.get("data") or {}
        if not result.get("token"):
            raise KuroAuthError(f"登录结果缺少 token: {data}")
        return {
            "token": str(result["token"]),
            "userName": str(result.get("userName") or "Unknown"),
            "userId": str(result.get("userId") or ""),
        }

    # ------------------------------------------------------------ 绑定/角色

    @staticmethod
    def _check(data: dict, action: str) -> dict:
        code = data.get("code")
        if code not in (CODE_SUCCESS, CODE_ALREADY_SIGNED):
            msg = str(data.get("msg") or data)
            if code == 220 or "令牌" in msg or "登录" in msg:
                raise KuroAuthError(
                    f"{action}失败: {msg}。token 无效或已过期——请确认复制的是完整 JWT"
                    "（eyJ 开头的长字符串），且账号仍保持登录状态"
                )
            raise RuntimeError(f"{action}失败: {msg}")
        return data

    async def get_roles(self) -> list[dict]:
        """返回鸣潮角色列表 [{roleId, userId, nickName, serverId}]。"""
        resp = await self._client.post(
            URL_ROLE_LIST,
            headers=_bbs_headers(self.token),
            data={"gameId": GAME_ID_WUWA},
        )
        data = _json_of(resp)
        self._check(data, "获取角色列表")
        raw = data.get("data")
        items = raw if isinstance(raw, list) else (raw or {}).get("list", [])
        roles = []
        for item in items:
            if str(item.get("gameId", GAME_ID_WUWA)) != GAME_ID_WUWA:
                continue
            roles.append(
                {
                    "roleId": str(item.get("roleId") or ""),
                    "userId": str(item.get("userId") or ""),
                    "nickName": str(item.get("roleName") or item.get("nickName") or "Unknown"),
                    "serverId": str(item.get("serverId") or SERVER_ID_WUWA),
                }
            )
        return [r for r in roles if r["roleId"]]

    async def sign_role(self, role: dict) -> RoleResult:
        result = RoleResult(
            account_uid=self.account_uid,
            game="wuthering-waves",
            role_uid=role["roleId"],
            nickname=role["nickName"],
            channel_name="库街区",
            success=False,
        )
        # 库街区要求两位数字月份（如 "08"），而非完整年月
        month = datetime.now().strftime("%m")
        payload = {
            "gameId": GAME_ID_WUWA,
            "serverId": role["serverId"],
            "roleId": role["roleId"],
            "userId": role["userId"],
            "reqMonth": month,
        }
        # 签到接口实测使用与角色列表相同的安卓风格头（WebView 头会报服务器外部错误）
        resp = await self._client.post(URL_SIGN_IN, headers=_bbs_headers(self.token), data=payload)
        data = _json_of(resp)
        code = data.get("code")
        if code == CODE_SUCCESS:
            result.success = True
            result.awards = [await self._today_reward(role)]
            return result
        if code == CODE_ALREADY_SIGNED or "已签" in str(data.get("msg", "")):
            result.already_signed = True
            return result
        result.error = str(data.get("msg") or data)
        return result

    async def _today_reward(self, role: dict) -> str:
        try:
            resp = await self._client.post(
                URL_SIGN_RECORD,
                headers=_game_headers(self.token),
                data={
                    "gameId": GAME_ID_WUWA,
                    "serverId": role["serverId"],
                    "roleId": role["roleId"],
                    "userId": role["userId"],
                },
            )
            data = _json_of(resp)
            records = data.get("data")
            if isinstance(records, list) and records:
                return str(records[0].get("goodsName") or "")
        except Exception as e:
            logger.debug("查询签到奖励失败: %s", e)
        return ""

    async def get_widget_status(self, role: dict) -> RoleResult:
        """拉取库街区小组件的鸣潮游戏状态（体力/活跃度/深渊/周本等）。"""
        result = RoleResult(
            account_uid=self.account_uid,
            game="wuthering-waves",
            role_uid=role["roleId"],
            nickname=f"{role['nickName']} 游戏状态",
            channel_name="库街区",
            success=True,
            info=True,
        )
        try:
            # getData 返回缓存值；refresh 强制拉实时数据（官方文档注明更准确）
            resp = None
            for url in (
                f"{BASE}/gamer/widget/game3/refresh",
                f"{BASE}/gamer/widget/game3/getData",
            ):
                resp = await self._client.post(
                    url,
                    headers=_game_headers(self.token),
                    data={
                        "gameId": GAME_ID_WUWA,
                        "roleId": role["roleId"],
                        "serverId": role["serverId"],
                        "sizeType": 1,
                        "type": 2,
                    },
                )
                body = _json_of(resp)
                if body.get("code") == 200:
                    break
            data = (resp.json() if resp else {}).get("data") or {}
        except Exception as e:
            result.success = False
            result.error = str(e)
            return result

        stats: list[dict] = []
        lines: list[str] = []
        for key in (
            "energyData",
            "livenessData",
            "storeEnergyData",
            "towerData",
            "slashTowerData",
            "weeklyData",
            "weeklyRougeData",
        ):
            item = data.get(key)
            if not isinstance(item, dict):
                continue
            name, cur, total = str(item.get("name") or key), item.get("cur"), item.get("total")
            if total in (None, 0) and cur in (None, 0):
                continue
            stats.append({"name": name, "cur": cur or 0, "total": total or 0})
            lines.append(f"{name} {cur}/{total}")
        if data.get("roleName"):
            server = data.get("serverName", "")
            head = f"{data['roleName']} · {server}".strip()
            stats.insert(0, {"name": head, "cur": None, "total": None})
            lines.insert(0, head)
        result.stats = stats
        result.awards = lines
        return result

    async def sign_all(self) -> list[RoleResult]:
        roles = await self.get_roles()
        if not roles:
            raise KuroAuthError("token 有效但没有鸣潮角色，请确认已在库街区绑定角色")
        results = [await self.sign_role(r) for r in roles]
        for r in roles:
            results.append(await self.get_widget_status(r))
        return results


def _json_of(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"响应解析失败 HTTP {resp.status_code}: {e}") from e
