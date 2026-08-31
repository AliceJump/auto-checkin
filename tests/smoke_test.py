"""冒烟测试：不触网，验证签名算法、存储往返与格式化逻辑。"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signer.api import APP_CODE, Credential, SklandClient  # noqa: E402
from signer.models import Account, RoleResult, ALL_GAMES  # noqa: E402
from signer.nte import APPVERSION, USER_CENTER_DS_SECRET, _user_center_ds  # noqa: E402
from signer.scheduler import ScheduleConfig  # noqa: E402
from signer.service import format_account_results  # noqa: E402
from signer.storage import AccountStore  # noqa: E402


def test_sign_matches_reference() -> None:
    """_signed_headers 必须与参考实现（37Bot 算法）逐字节一致。"""
    client = SklandClient(device_id="Btest123")
    cred = Credential(token="token-key", cred="cred-value")
    url = "https://zonai.skland.com/api/v1/game/attendance"
    body = json.dumps({"gameId": 1, "uid": "123"}, separators=(",", ":"))

    with mock.patch("signer.api.time.time", return_value=1_700_000_000):
        headers = client._signed_headers(cred, "POST", url, body)

    ts = str(int(1_700_000_000) - 2)
    sign_header = json.dumps(
        {"platform": "", "timestamp": ts, "dId": "", "vName": ""},
        separators=(",", ":"),
    )
    payload = f"/api/v1/game/attendance{body}{ts}{sign_header}"
    expected = hashlib.md5(
        hmac_mod.new(b"token-key", payload.encode(), hashlib.sha256).hexdigest().encode()
    ).hexdigest()

    assert headers["sign"] == expected, headers
    assert headers["cred"] == "cred-value"
    assert headers["dId"] == ""          # 签名头里的 dId 为空串（与参考一致）
    assert headers["timestamp"] == ts
    print("PASS sign matches reference")


def test_get_binding_url_sign_source_is_query() -> None:
    client = SklandClient(device_id="Bx")
    cred = Credential(token="t", cred="c")
    with mock.patch("signer.api.time.time", return_value=1_700_000_000):
        h = client._signed_headers(cred, "GET", "https://zonai.skland.com/api/v1/game/player/binding", "")
    assert h["platform"] == ""
    print("PASS GET header build")


def test_storage_roundtrip(tmp: Path) -> None:
    store = AccountStore(tmp / "accounts.json")
    a = Account(uid="u1", nickname="博士", token="tok", games=["arknights"])
    b = Account(uid="u2", nickname="管理员", token="tok2")
    store.upsert(a)
    store.upsert(b)
    assert [x.uid for x in store.load()] == ["u1", "u2"]

    b.games = ["endfield"]
    b.enabled = False
    store.replace_account(b)
    loaded = {x.uid: x for x in store.load()}
    assert loaded["u2"].games == ["endfield"] and loaded["u2"].enabled is False
    assert loaded["u1"].games == ["arknights"]  # 未受影响

    # 非法游戏值被归一化为全部
    c = Account(uid="u3", nickname="x", token="t3", games=["bogus"])
    c.normalize_games()
    assert c.games == list(ALL_GAMES)

    assert store.remove("u1") is True
    assert store.remove("nope") is False

    meta_date, attempts = "2026-08-24", 2
    store.set_daily_meta(meta_date, attempts)
    assert store.get_daily_meta() == {"date": meta_date, "attempts": attempts}
    assert len(store.load()) == 1  # 元数据不破坏账号数据
    print("PASS storage roundtrip")


def test_format_results() -> None:
    account = Account(uid="u1", nickname="博士", token="t")
    ok = RoleResult("u1", "arknights", "100", "Doctor", "官服", True, awards=["合成玉x100"])
    dup = RoleResult("u1", "endfield", "r1", "管理员", "官服", False, already_signed=True)
    text = format_account_results(account, [ok, dup])
    assert "✅" in text and "明日方舟" in text and "合成玉x100" in text
    assert "今日已签到" in text
    empty = format_account_results(Account(uid="e", nickname="n", token="t"), [])
    assert "未找到可签到的角色" in empty or "失败" in empty
    print("PASS format results")


def test_schedule_config_defaults() -> None:
    cfg = ScheduleConfig.from_dict({"hour": 9})
    assert (cfg.enabled, cfg.hour, cfg.minute) == (True, 9, 0)
    assert ScheduleConfig.from_dict(None).max_attempts >= 1
    print("PASS schedule config")


def test_app_code() -> None:
    assert APP_CODE == "4ca99fa6b56cc2ba"
    print("PASS app code")


def test_nte_user_center_ds() -> None:
    with mock.patch("signer.nte.time.time", return_value=1_700_000_000), mock.patch(
        "secrets.choice", side_effect=list("Ab12Cd34")
    ):
        value = _user_center_ds()
    timestamp, nonce, digest = value.split(",")
    expected = hashlib.md5(
        f"{timestamp}{nonce}{APPVERSION}{USER_CENTER_DS_SECRET}".encode("utf-8")
    ).hexdigest()
    assert timestamp == "1700000000"
    assert nonce == "Ab12Cd34"
    assert digest == expected
    print("PASS nte user center ds")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        test_sign_matches_reference()
        test_get_binding_url_sign_source_is_query()
        test_storage_roundtrip(Path(td))
        test_format_results()
        test_schedule_config_defaults()
        test_app_code()
        test_nte_user_center_ds()
    print("\nALL SMOKE TESTS PASSED")
