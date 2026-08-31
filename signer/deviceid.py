"""注册真实的森空岛设备 ID（dId）。

移植自 NoelZong/skland-auto-sign 的 SecuritySm 实现：
向鹰角的设备指纹服务（fp-it.portal101.cn，数美 SDK）注册设备档案，
换取合法 deviceId，避免短信/密码登录时触发"设备信息无效"风控。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import time
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.algorithms import AES

try:  # cryptography 48+ 将 TripleDES 移入 decrepit
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers.base import Cipher
from cryptography.hazmat.primitives.ciphers.modes import CBC, ECB

logger = logging.getLogger("DeviceId")

DEVICES_INFO_URL = "https://fp-it.portal101.cn/deviceprofile/v4"

SM_CONFIG = {
    "organization": "UWXspnCCJN4sfYlNfqps",
    "appId": "default",
    "publicKey": (
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCmxMNr7n8ZeT0tE1R9j/mPixoinPkeM+k4VGIn/s0k7N5rJAfnZ0eMER"
        "+QhwFvshzo0LNmeUkpR8uIlU/GEVr8mN28sKmwd2gpygqj0ePnBmOW4v0ZVwbSYK+izkhVFk2V/doLoMbWy6b+UnA8mkjv"
        "g0iYWRByfRsK2gdl7llqCwIDAQAB"
    ),
}

_PK = serialization.load_der_public_key(base64.b64decode(SM_CONFIG["publicKey"]))

_DES_RULE = {
    "appId": {"key": "uy7mzc4h", "name": "xx"},
    "canvas": {"key": "snrn887t", "name": "yk"},
    "clientSize": {"key": "cpmjjgsu", "name": "zx"},
    "organization": {"key": "78moqjfc", "name": "dp"},
    "os": {"key": "je6vk6t4", "name": "pj"},
    "platform": {"key": "pakxhcd2", "name": "gm"},
    "plugins": {"key": "v51m3pzl", "name": "kq"},
    "pmf": {"key": "2mdeslu3", "name": "vw"},
    "referer": {"key": "y7bmrjlc", "name": "ab"},
    "res": {"key": "whxqm2a7", "name": "hf"},
    "rtype": {"key": "x8o2h2bl", "name": "lo"},
    "sdkver": {"key": "9q3dcxp2", "name": "sc"},
    "status": {"key": "2jbrxxw4", "name": "an"},
    "subVersion": {"key": "eo3i2puh", "name": "ns"},
    "svm": {"key": "fzj3kaeh", "name": "qr"},
    "time": {"key": "q2t3odsk", "name": "nb"},
    "timezone": {"key": "1uv05lj5", "name": "as"},
    "tn": {"key": "x9nzj1bp", "name": "py"},
    "trees": {"key": "acfs0xo4", "name": "pi"},
    "ua": {"key": "k92crp1t", "name": "bj"},
    "url": {"key": "y95hjkoo", "name": "cf"},
    "vpw": {"key": "r9924ab5", "name": "ca"},
}

# 不加密但需要改名的字段（protocol/version 保持原名，无需处理）
_DES_RENAME = {"box": "jf"}

_BROWSER_ENV = {
    "plugins": (
        "MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,"
        "MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1"
    ),
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
    ),
    "canvas": "259ffe69",
    "timezone": -480,
    "platform": "Win32",
    "url": "https://www.skland.com/",
    "referer": "",
    "res": "1920_1080_24_1.25",
    "clientSize": "0_0_1080_1920_1920_1080_1920_1080",
    "status": "0011",
}


def _des(o: dict) -> dict:
    result: dict = {}
    for key, value in o.items():
        rule = _DES_RULE.get(key)
        if rule:
            cipher = Cipher(TripleDES(rule["key"].encode("utf-8")), ECB())
            data = str(value).encode("utf-8") + b"\x00" * 8
            result[rule["name"]] = base64.b64encode(
                cipher.encryptor().update(data)
            ).decode("utf-8")
        elif key in _DES_RENAME:
            result[_DES_RENAME[key]] = value
        else:
            result[key] = value
    return result


def _aes(v: bytes, k: bytes) -> str:
    cipher = Cipher(AES(k), CBC(b"0102030405060708"))
    v += b"\x00"
    while len(v) % 16 != 0:
        v += b"\x00"
    return cipher.encryptor().update(v).hex()


def _gzip_b64(o: dict) -> bytes:
    stream = gzip.compress(json.dumps(o, ensure_ascii=False).encode("utf-8"), 2, mtime=0)
    return base64.b64encode(stream)


def _get_tn(o: dict) -> str:
    parts: list[str] = []
    for key in sorted(o.keys()):
        value = o[key]
        if isinstance(value, (int, float)):
            value = str(value * 10000)
        elif isinstance(value, dict):
            value = _get_tn(value)
        parts.append(value)
    return "".join(parts)


def _get_smid() -> str:
    t = time.localtime()
    stamp = "{:04d}{:02d}{:02d}{:02d}{:02d}{:02d}".format(
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec
    )
    v = stamp + hashlib.md5(str(uuid.uuid4()).encode()).hexdigest() + "00"
    tail = hashlib.md5(("smsk_web_" + v).encode()).hexdigest()[:14]
    return v + tail + "0"


def get_registered_device_id(timeout: float = 15.0) -> str:
    """向数美设备指纹服务注册设备，返回形如 B<deviceId> 的 dId。失败抛异常。"""
    uid = str(uuid.uuid4()).encode("utf-8")
    pri_id = hashlib.md5(uid).hexdigest()[:16].encode("utf-8")
    ep = base64.b64encode(_PK.encrypt(uid, padding.PKCS1v15())).decode("utf-8")

    now_ms = int(time.time() * 1000)
    profile = {
        **_BROWSER_ENV,
        "vpw": str(uuid.uuid4()),
        "svm": now_ms,
        "trees": str(uuid.uuid4()),
        "pmf": now_ms,
        "protocol": 102,
        "organization": SM_CONFIG["organization"],
        "appId": SM_CONFIG["appId"],
        "os": "web",
        "version": "3.0.0",
        "sdkver": "3.0.0",
        "box": "",
        "rtype": "all",
        "smid": _get_smid(),
        "subVersion": "1.0.0",
        "time": 0,
    }
    profile["tn"] = hashlib.md5(_get_tn(profile).encode()).hexdigest()

    payload = {
        "appId": "default",
        "compress": 2,
        "data": _aes(_gzip_b64(_des(profile)), pri_id),
        "encode": 5,
        "ep": ep,
        "organization": SM_CONFIG["organization"],
        "os": "web",
    }
    resp = httpx.post(DEVICES_INFO_URL, json=payload, timeout=timeout)
    data = resp.json()
    if data.get("code") != 1100:
        raise RuntimeError(f"dId 注册失败: {data}")
    return "B" + data["detail"]["deviceId"]
