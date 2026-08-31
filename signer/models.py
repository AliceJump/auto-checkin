"""数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

GAME_ARKNIGHTS = "arknights"
GAME_ENDFIELD = "endfield"
ALL_GAMES = (GAME_ARKNIGHTS, GAME_ENDFIELD)
GAME_NAMES = {
    GAME_ARKNIGHTS: "明日方舟",
    GAME_ENDFIELD: "终末地",
    "wuthering-waves": "鸣潮",
    "neverness-to-everness": "异环",
    "nte-app": "异环社区",
}

# 支持的平台（账号渠道）
PLATFORM_SKLAND = "skland"   # 森空岛：明日方舟 / 终末地
PLATFORM_KURO = "kuro"       # 库街区：鸣潮
PLATFORM_NTE = "nte"         # 塔吉多：异环
ALL_PLATFORMS = (PLATFORM_SKLAND, PLATFORM_KURO, PLATFORM_NTE)
PLATFORM_NAMES = {
    PLATFORM_SKLAND: "森空岛(明日方舟/终末地)",
    PLATFORM_KURO: "库街区(鸣潮)",
    PLATFORM_NTE: "塔吉多(异环)",
}


@dataclass
class Credential:
    token: str
    cred: str


@dataclass
class Binding:
    app_code: str
    game_id: int
    uid: str
    nickname: str
    channel_name: str
    game_name: str = ""
    roles: list[dict] = field(default_factory=list)


@dataclass
class RoleResult:
    """单个角色的签到结果。"""

    account_uid: str
    game: str
    role_uid: str
    nickname: str
    channel_name: str
    success: bool
    already_signed: bool = False
    awards: list[str] = field(default_factory=list)
    error: str = ""
    info: bool = False  # 纯信息条目（如游戏状态数据），不参与成功/失败统计
    # 结构化状态表（鸣潮小组件等）：[{"name": "结晶波片", "cur": 146, "total": 240}, ...]
    stats: list[dict] | None = None

    @property
    def game_name(self) -> str:
        return GAME_NAMES.get(self.game, self.game)

    @property
    def ok(self) -> bool:
        return self.success or self.already_signed


@dataclass
class Account:
    """一个签到账号的持久化凭证与状态。"""

    uid: str
    nickname: str
    token: str
    platform: str = PLATFORM_SKLAND
    device_id: str = ""
    games: list[str] = field(default_factory=lambda: list(ALL_GAMES))
    enabled: bool = True
    auth_failed_notified: bool = False
    last_run_date: str = ""
    last_success_at: str = ""
    last_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Account":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def normalize_games(self) -> None:
        """仅森空岛平台需要按支持的游戏过滤；其他平台的 games 原样保留。"""
        if self.platform != PLATFORM_SKLAND:
            return
        valid = [g for g in self.games if g in ALL_GAMES]
        self.games = list(dict.fromkeys(valid)) if valid else list(ALL_GAMES)
