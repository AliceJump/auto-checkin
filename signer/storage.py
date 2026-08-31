"""accounts.json 凭证存储（原子写入）。

文件结构：
{
  "accounts": [ {...Account...} ],
  "daily": { "date": "2026-08-24", "attempts": 1 }   # 调度器每日标记
}
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Account


class AccountStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------------ 基础

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = self.path.with_suffix(".json.corrupt")
            self.path.replace(backup)
            raise RuntimeError(f"凭证文件损坏，已备份到 {backup}，请重新登录账号")

    def _write_raw(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 账号

    def load(self) -> list[Account]:
        raw = self._load_raw().get("accounts", [])
        accounts = [Account.from_dict(item) for item in raw]
        for account in accounts:
            account.normalize_games()
        return accounts

    def save_accounts(self, accounts: list[Account]) -> None:
        payload = self._load_raw()
        payload["accounts"] = [a.to_dict() for a in accounts]
        self._write_raw(payload)

    def upsert(self, account: Account) -> None:
        accounts = [a for a in self.load() if a.uid != account.uid]
        accounts.append(account)
        self.save_accounts(accounts)

    def remove(self, uid: str) -> bool:
        accounts = self.load()
        remaining = [a for a in accounts if a.uid != uid]
        if len(remaining) == len(accounts):
            return False
        self.save_accounts(remaining)
        return True

    def get(self, uid: str) -> Account | None:
        for account in self.load():
            if account.uid == uid:
                return account
        return None

    def replace_account(self, account: Account) -> None:
        """用给定状态覆盖同名账号（保留其他账号与元数据）。"""
        accounts = self.load()
        replaced = False
        for i, a in enumerate(accounts):
            if a.uid == account.uid:
                accounts[i] = account
                replaced = True
                break
        if not replaced:
            accounts.append(account)
        self.save_accounts(accounts)

    # ------------------------------------------------------------------ 元数据

    def get_daily_meta(self) -> dict:
        meta = self._load_raw().get("daily") or {}
        return {"date": str(meta.get("date") or ""), "attempts": int(meta.get("attempts") or 0)}

    def set_daily_meta(self, date: str, attempts: int) -> None:
        payload = self._load_raw()
        payload["daily"] = {"date": date, "attempts": attempts}
        self._write_raw(payload)
