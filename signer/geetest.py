"""极验 GeeTest v4 滑块验证码自动破解。

移植自 mxyooR/Kuro_login 的 geetest_captcha 实现（GPL 项目）：
load → PoW → 缺口识别(ddddocr) → 轨迹构造 → 加密提交 → seccode。
仅依赖 pycryptodome 与 ddddocr。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import time
from typing import Any
from urllib.parse import urljoin

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)
HEADERS = {"User-Agent": UA}

# 极验前端加密用 RSA 公钥（公开常量）
_RSA_N = int(
    "00C1E3934D1614465B33053E7F48EE4EC87B14B95EF88947713D25EECBFF7E74C7977D02DC1D94"
    "51F79DD5D1C10C29ACB6A9B4D6FB7D0A0279B6719E1772565F09AF627715919221AEF91899CAE0"
    "8C0D686D748B20A3603BE2318CA6BC2B59706592A9219D0BF05C9F65023A21D2330807252AE006"
    "6D59CEEFA5F2748EA80BAB81",
    16,
)
_RSA_E = 0x10001


def _random_string(alphabet: str, length: int) -> str:
    return "".join(random.choice(alphabet) for _ in range(length))


def _guid() -> str:
    return _random_string(string.ascii_lowercase + string.digits, 16)


def _rsa_enc(plaintext: str) -> str:
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    pub = RSA.construct((_RSA_N, _RSA_E))
    cipher = PKCS1_v1_5.new(pub).encrypt(plaintext.encode("utf-8"))
    hex_str = cipher.hex()
    return ("0" + hex_str) if len(hex_str) % 2 else hex_str


def _aes_enc(plaintext: str, key: str) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    iv = b"0000000000000000"
    padded = pad(plaintext.encode("utf-8"), 16, "pkcs7")
    return AES.new(key=key.encode("utf-8"), mode=AES.MODE_CBC, iv=iv).encrypt(padded).hex()


def _encode_w(track: dict) -> str:
    key = _guid()
    return _aes_enc(json.dumps(track), key) + _rsa_enc(key)


def _convert_callback(sign: str, context: str) -> dict:
    return json.loads(context[len(sign) + 1 : len(context) - 1])


class GeeTestSolver:
    """GeeTest v4 滑块验证码求解器。"""

    def __init__(self, captcha_id: str, timeout: float = 20.0):
        self.captcha_id = captcha_id
        self.callback = f"geetest_{int(time.time() * 1000)}"
        self.info: dict[str, Any] = {}
        self._client = httpx.Client(timeout=timeout, headers=HEADERS)

    def close(self) -> None:
        self._client.close()

    def _load(self) -> None:
        resp = self._client.get(
            "https://gcaptcha4.geetest.com/load",
            params={
                "callback": self.callback,
                "captcha_id": self.captcha_id,
                "client_type": "web",
                "pt": "1",
                "lang": "zho",
            },
        )
        info = _convert_callback(self.callback, resp.text)
        if info.get("status") != "success":
            raise RuntimeError(f"极验 load 失败: {info}")
        self.info = info["data"]

    def _track(self) -> dict:
        captcha_type = self.info.get("captcha_type")
        if captcha_type != "slide":
            raise RuntimeError(f"不支持的极验类型: {captcha_type}（当前仅支持滑块）")
        return self._slide_track()

    def _slide_distance(self) -> int:
        import ddddocr

        det = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        slice_img = self._download(self.info["slice"])
        bg_img = self._download(self.info["bg"])
        try:
            result = det.slide_match(
                target_bytes=slice_img, background_bytes=bg_img, simple_target=True
            )
        except TypeError:  # ddddocr>=1.6 参数改名
            result = det.slide_match(
                target_img=slice_img, background_img=bg_img, simple_target=True
            )
        return int(result["target"][0])

    def _download(self, path: str) -> bytes:
        resp = self._client.get(urljoin("https://static.geetest.com/", path))
        resp.raise_for_status()
        return resp.content

    def _slide_track(self) -> dict:
        distance = self._slide_distance()
        pow_msg = "|".join(
            [
                str(self.info["pow_detail"]["version"]),
                str(self.info["pow_detail"]["bits"]),
                self.info["pow_detail"]["hashfunc"],
                self.info["pow_detail"]["datetime"],
                self.captcha_id,
                self.info["lot_number"],
                _guid(),
            ]
        )
        pow_sign = hashlib.new(self.info["pow_detail"]["hashfunc"])
        pow_sign.update(pow_msg.encode("utf-8"))
        return {
            "setLeft": distance,
            "passtime": random.randint(800, 2000),
            "userresponse": distance / 1.0059466666666665 + 2,
            "device_id": "",
            "lot_number": self.info["lot_number"],
            "pow_msg": pow_msg,
            "pow_sign": pow_sign.hexdigest(),
            "geetest": "captcha",
            "lang": "zh",
            "ep": "123",
            "biht": "1426265548",
            "dRjQ": "738u",
            "em": {"ph": 0, "cp": 0, "ek": "11", "wd": 1, "nt": 0, "si": 0, "sc": 0},
        }

    def _verify(self, w: str) -> dict:
        resp = self._client.get(
            "https://gcaptcha4.geetest.com/verify",
            params={
                "callback": self.callback,
                "captcha_id": self.captcha_id,
                "client_type": "web",
                "lot_number": self.info["lot_number"],
                "payload": self.info["payload"],
                "process_token": self.info["process_token"],
                "payload_protocol": "1",
                "pt": "1",
                "w": w,
            },
        )
        result = _convert_callback(self.callback, resp.text)
        if result.get("status") != "success":
            raise RuntimeError(f"极验校验未通过: {result}")
        return result

    def get_sec_code(self) -> str:
        """返回可直接放入 geeTestData 的 seccode 对象（dict）。"""
        self._load()
        track = self._track()
        w = _encode_w(track)
        result = self._verify(w)
        data = result.get("data") or {}
        if data.get("result") != "success" or "seccode" not in data:
            # 滑块位置不够准时会走到这里，交由外层重试
            raise RuntimeError(f"滑块校验未通过: {json.dumps(data, ensure_ascii=False)[:120]}")
        return data["seccode"]


def get_sec_code(captcha_id: str, max_attempts: int = 5) -> str:
    """带重试的求解入口，返回 json.dumps 后的 seccode 字符串。"""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        solver = GeeTestSolver(captcha_id)
        try:
            return json.dumps(solver.get_sec_code())
        except Exception as e:
            last_error = e
        finally:
            solver.close()
    raise RuntimeError(f"极验求解失败（已尝试 {max_attempts} 次）: {last_error}")
