# astrbot_plugin_heybox_daily

让 AstrBot 里的 AI 角色在夜间「自己刷小黑盒」，只把**它觉得有趣**的帖子用自己的话讲出来，顺带复述有意思的评论。

> 这是一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，人格化闲聊类，不提供任何绕过平台限制的能力。

## 特色

- **按时间逛，不按条数**：每天在设定的时间窗里随机挑一个时刻开始，连逛 `session_minutes` 分钟（默认 60），逛够就停。
- **自己判断值不值得说**：每屏先让模型打分（0–10），低于 `interest_threshold`（默认 6）就不分享，宁可不说也不凑数。
- **用自己的话复述**：读帖子正文 + 热门评论后重写，而不是转发原文。
- **带首图**：分享时附上帖子首图。
- **两天记忆**：看过的帖子在 `memory_days`（默认 2 天）内不再出现，过期后可重新出现。
- **喜好分区加权**：`preferred_topics` 里填关键词，命中的帖子排序加权、并且在判断时降低门槛。
- **扫码登录**：正文与评论需要小黑盒账号，插件支持聊天内扫码登录（`/小黑盒扫码`），未登录时自动降级为只用首页摘要。

## 依赖

本插件通过子进程调用 [AaronZ345/xiaoheihe-cli](https://github.com/AaronZ345/xiaoheihe-cli) 读取小黑盒 feed / 正文 / 评论。

```bash
git clone https://github.com/AaronZ345/xiaoheihe-cli
cd xiaoheihe-cli
python -m venv .venv
.venv/Scripts/pip install -e .      # Windows
# 或 .venv/bin/pip install -e .
```

装完记下 `cli.py` 与 venv 里 python 的绝对路径，填进插件配置的 `cli_script` / `cli_python`（这两项默认为空，不填插件无法工作）：

AstrBot 侧还需要（一般已自带）：

```bash
pip install pillow
```

`pillow` 仅用于把 CLI 生成的二维码重编码成真正的 PNG，缺失时会退化为直接发送原文件。

## 安装

1. 把本目录放到 `AstrBot/data/plugins/astrbot_plugin_heybox_daily/`
2. 重启 AstrBot，或在 WebUI 插件页里重载
3. 打开插件配置，填好 `cli_python` / `cli_script` 两个路径（默认为空）
4. 在聊天里发 `/小黑盒扫码`，用小黑盒 App 扫码登录

## 配置项

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否开启定时闲逛 | `true` |
| `window_start` / `window_end` | 允许开始的时间窗（可跨零点） | `18:00` / `09:00` |
| `session_minutes` | 一次逛多久 | `60` |
| `interval_minutes` | 翻屏间隔（区间随机） | `[4, 9]` |
| `interest_threshold` | 有趣门槛（0–10） | `6.0` |
| `judge_batch` | 每次先看几条再决定 | `5` |
| `memory_days` | 帖子记忆保留天数 | `2.0` |
| `send_image` | 是否带首图 | `true` |
| `fetch_comments` | 是否读热门评论（需登录） | `true` |
| `comment_limit` | 读多少条评论 | `8` |
| `preferred_topics` | 喜好分区/关键词列表 | `[]` |
| `preferred_boost` | 命中喜好的热度加权倍数 | `1.6` |
| `preferred_threshold_discount` | 命中喜好时门槛降低量 | `1.5` |
| `auto_login_qr` | 启动未登录时自动推二维码 | `true` |
| `target_umos` | 推送目标 UMO 列表 | — |
| `provider_id` | 生成点评用的模型 | 当前默认 |
| `topic_blocklist` | 屏蔽关键词 | 抽奖/广告/代练/出售账号 |

## 命令

| 命令 | 作用 |
| --- | --- |
| `/刷小黑盒` | 手动逛一屏，只会发她觉得有趣的那条 |
| `/小黑盒扫码` | 生成登录二维码 |
| `/小黑盒登录` | 查看登录状态 |

UMO 格式：`<平台实例>:<消息类型>:<会话ID>`，例如 `snowluma:FriendMessage:1234567`。

## 说明与免责

- 只读操作：浏览 feed、读正文、读评论，**不点赞、不评论、不发帖**。
- 登录凭证由 xiaoheihe-cli 自行保存在本地，本插件不读取、不上传。
- 请自行控制使用频率，遵守小黑盒的用户协议；因使用产生的账号风险由使用者自行承担。
- 与小黑盒官方无关。

## 致谢

本插件**没有自己实现小黑盒的签名与接口**，全部数据获取都来自并借鉴了：

- [AaronZ345/xiaoheihe-cli](https://github.com/AaronZ345/xiaoheihe-cli) —— 小黑盒命令行工具（MIT License）。请求签名（`xhh_operator/signing.py`）、接口参数与数据结构均来自该项目，本插件只是通过子进程调用它的 CLI 并组织输出。

本插件与 xiaoheihe-cli 作者无隶属关系；如原作者有任何异议，欢迎提 issue，我会立刻调整或下线相关功能。

## License

MIT
