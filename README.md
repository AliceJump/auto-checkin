# auto-checkin 自动签到服务

参考 [MAA1999/37Bot](https://github.com/MAA1999/37Bot) 的 `plugins/skland` 插件剥离实现的通用自动签到定时服务，不依赖 QQ/NapCat。

**当前支持平台：**

| 平台 | 游戏 | 登录方式 |
| --- | --- | --- |
| 森空岛 | 明日方舟 / 终末地 | 鹰角 token / 短信 / 密码 |
| 库街区 | 鸣潮 | 社区 token（抓包获取） |
| 塔吉多 | 异环 | refreshToken / 完美世界短信 |

多账号管理、每日定时（默认北京时间 06:00 前）、失败限次重试、token 失效告警、多渠道推送。

## 功能

- 多账号管理：token 或 手机短信验证码 登录，一次登录长期有效
- 每账号可选签到的游戏（明日方舟 / 终末地），终末地自动遍历所有角色
- 每日定时签到：可配置时间点；服务重启错过时间点可补签；失败自动限次重试
- token 失效只告警一次，恢复后自动清除告警状态
- 通知渠道：日志 / Telegram / Server酱 / PushPlus（可同时启用多个）
- 凭证原子落盘 `accounts.json`，日志不记录明文 token

## 快速开始

```bash
# Python 3.10+
pip install -r requirements.txt

cp config.yaml.example config.yaml   # Windows: copy

# 登录并添加账号（推荐短信登录）
python main.py login

# 手动立即签到
python main.py sign

# 启动每日定时守护进程
python main.py run
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `login [--token XXX] [--games arknights,endfield]` | 登录并保存账号（不带参数进入交互向导） |
| `sign [--uid UID] [--force]` | 立即签到；`--force` 忽略今日已签标记 |
| `list` | 查看账号、游戏选择与状态 |
| `remove <uid>` | 删除账号 |
| `enable` / `disable <uid>` | 启用 / 禁用账号 |
| `test-notify` | 发送测试通知 |
| `run` | 启动定时守护进程 |

## 登录各平台

```bash
python main.py login          # 先选平台，再按向导登录
```

**森空岛**：短信验证码（推荐）/ 密码 / 粘贴 token。token 获取：浏览器登录 https://user.hypergryph.com/ 后控制台执行：

```js
JSON.parse(localStorage.getItem("USER_TOKEN")).content.token
```

**库街区（鸣潮）**：粘贴 token。获取方式：手机抓包或电脑浏览器登录库街区后，在 devtools Network 中找 `api.kurobbs.com` 请求头里的 `token` 字段。

**塔吉多（异环）**：完美世界账号短信登录，或粘贴 refreshToken（可用 [NTE-Auto-Sign](https://github.com/Candy-QAQ/NTE-Auto-Sign) 的 add_account 工具获取）。refreshToken 会自动轮换并回写保存。

## 配置说明（config.yaml）

```yaml
schedule:
  enabled: true      # 是否启用每日定时
  timezone: Asia/Shanghai  # 签到时刻按此时区计算，默认北京时间（服务器非东八区也准点）
  hour: 5            # 默认北京时间 06:00 前（05:00）
  minute: 0
  catch_up: true     # 服务启动时已错过今天的时间点则立即补签
accounts_file: accounts.json
notify:
  telegram:
    enabled: true
    bot_token: "123456:ABC..."
    chat_id: "你的chat_id"
  serverchan:
    enabled: false
    send_key: ""
  pushplus:
    enabled: false
    token: ""
```

## 服务器部署（systemd）

```bash
sudo mkdir -p /opt/auto-checkin && sudo chown $USER /opt/auto-checkin
# 上传项目文件到 /opt/auto-checkin 后：
cd /opt/auto-checkin
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml && vim config.yaml
.venv/bin/python main.py login        # 先完成登录
sudo cp deploy/auto-checkin.service /etc/systemd/system/   # 检查其中路径与用户
sudo systemctl daemon-reload
sudo systemctl enable --now auto-checkin
journalctl -u auto-checkin -f
```

注意：

- 签到时刻按 `schedule.timezone` 计算，默认北京时间 `Asia/Shanghai`；服务器时区无关
- `accounts.json` 含登录凭证，请限制文件权限（`chmod 600 accounts.json`）并做好备份

## 项目结构

```
main.py                  # CLI 入口
config.yaml.example      # 配置模板
accounts.json            # 凭证存储（运行时生成）
signer/
├── api.py               # 森空岛渠道（HMAC-SHA256+MD5 签名 / OAuth / 双游戏签到）
├── kuro.py              # 库街区渠道（鸣潮签到）
├── nte.py               # 塔吉多渠道（异环社区+游戏签到）
├── deviceid.py          # 数美设备指纹注册（登录风控）
├── models.py            # Account / Binding / RoleResult 数据模型
├── storage.py           # accounts.json 原子读写 + 每日调度标记
├── service.py           # 签到编排与结果汇总（按平台分发）
├── scheduler.py         # 每日定时循环（补签 / 限次重试）
├── notifier.py          # Telegram / Server酱 / PushPlus 通知
├── config.py            # 配置加载
└── logsetup.py          # 日志（控制台 + 滚动文件）
deploy/
└── auto-checkin.service  # systemd 单元
```

## 致谢与参考项目

本项目站在以下开源项目的肩膀上，感谢这些作者的贡献：

| 项目 | 借鉴内容 | 协议 |
| --- | --- | --- |
| [MAA1999/37Bot](https://github.com/MAA1999/37Bot) | 森空岛签到算法与整体流程的原始实现（本项目前身） | GPL-3.0 |
| [YueHen14/skyland-auto-sign](https://github.com/YueHen14/skyland-auto-sign) / [UKMeng/nonebot-plugin-skland-arksign](https://github.com/UKMeng/nonebot-plugin-skland-arksign) | 森空岛 API 签名算法参考 | - |
| [NoelZong/skland-auto-sign](https://github.com/NoelZong/skland-auto-sign) | 登录请求头风格、密码登录接口 | MIT |
| [mxyooR/Kuro_login](https://github.com/mxyooR/Kuro_login) | 库街区极验滑块破解模块移植（`signer/geetest.py`）与短信登录流程 | MIT |
| [TomyJan/Kuro-API-Collection](https://github.com/TomyJan/Kuro-API-Collection) | 库街区 API 文档（角色列表 / 签到 v2 / 小组件状态） | - |
| [leeezep/kurobbs_auto_checkin](https://github.com/leeezep/kurobbs_auto_checkin) | 签到参数格式（reqMonth 等）修正参考 | MIT |
| [Candy-QAQ/NTE-Auto-Sign](https://github.com/Candy-QAQ/NTE-Auto-Sign) | 异环塔吉多认证链路与签到接口 | MIT |

本项目遵循 GPL-3.0 协议开源；引用了 GPL 项目的部分即整项目以 GPL-3.0 提供，引用 MIT 项目的部分遵循其原协议并在此致谢。

## 已支持 / 未接入的能力

**已接入：**

- 明日方舟 / 终末地：每日签到
- 鸣潮：每日签到 + 游戏状态日报（体力、活跃度、结晶单质、深渊层数、周本次数等，来自库街区小组件）
- 异环：社区签到 + 游戏签到

**暂未接入（欢迎 PR）：**

- 抽卡记录导出：鸣潮需从游戏客户端抓取抽卡链接；异环可参考 [NTE_Gacha_Exporter](https://github.com/Anong0u0/NTE_Gacha_Exporter)（MIT）的本地解析方案
- 云异环每日时长领取：需额外的云异环登录态，可参考 NTE-Auto-Sign 的实现
- 明日方舟理智/公招等状态查询：森空岛暂无公开稳定的组件接口

- 签名算法与接口流程参考 [MAA1999/37Bot](https://github.com/MAA1999/37Bot)
- 并参考了 [YueHen14/skyland-auto-sign](https://github.com/YueHen14/skyland-auto-sign) 与 [UKMeng/nonebot-plugin-skland-arksign](https://github.com/UKMeng/nonebot-plugin-skland-arksign)

## License

GPL-3.0（沿用上游项目许可证）
