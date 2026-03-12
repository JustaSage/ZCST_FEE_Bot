# ZCST Fee Bot 🏠

宿舍电费 / 冷水 / 热水余额监控 & 充值机器人。

通过 Playwright 无头浏览器加载 [17wanxiao](https://www.17wanxiao.com/) H5 页面，自动拦截 API 获取余额数据，并提供 **Telegram Bot** 交互式查询与充值功能。

支持**多用户**：每位用户通过 Telegram 命令独立配置自己的查询链接、预警阈值和刷新间隔，数据完全隔离。

## ✨ 功能

- **多用户支持** — 每位用户独立配置，数据隔离，部署一次服务多人
- **余额查询** — `/balance` 即时返回缓存余额，无需等待
- **手动刷新** — `/update` 立即拉取最新余额
- **交互式充值** — `/charge` 多步引导，选择类型 → 档位 → 支付方式，生成支付宝/微信支付链接
- **充值到账监控** — 生成支付链接后自动监控余额变化，到账后即时通知
- **SSO 登录** — 通过学校统一认证自动获取查询链接，无需手动抓包
- **首次引导设置** — 新用户发送 `/start` 自动进入逐步配置，链接即时验证
- **定时预警** — 按用户独立定时刷新，低于阈值时推送 Telegram 通知
- **获取链接** — `/link` 获取或通过 SSO 登录获取查询链接
- **CLI 模式** — `--once` 单次查询 / `--debug` 调试原始数据

## 📋 前置条件

- Python ≥ 3.11
- 一个 17wanxiao 宿舍费用查询链接（从学校公众号/小程序获取），或学校 SSO 统一认证账号
- 一个 Telegram Bot Token（通过 [@BotFather](https://t.me/BotFather) 创建）

## 🚀 安装

### 使用 uv（推荐）

```bash
uv sync
uv run playwright install chromium
```

### 使用 pip

```bash
pip install -r requirements.txt
playwright install chromium
```

## ⚙️ 配置

复制示例配置文件并填入 Bot Token：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

| 字段 | 说明 |
|------|------|
| `telegram.bot_token` | **必填** Telegram Bot Token |
| `telegram.proxy` | *(可选)* HTTP 代理地址，如 `http://127.0.0.1:7890` |
| `telegram.api_base` | *(可选)* 自定义 Telegram API 反代地址 |

> 💡 其他设置（查询链接、预警阈值、刷新间隔）由每位用户通过 Telegram 命令自行配置。

## 📖 使用

### 启动 Bot（常驻模式）

```bash
python main.py
```

Bot 启动后，用户发送 `/start` 开始使用。

### 用户首次配置

新用户首次发送 `/start` 时，Bot 会自动进入引导式设置：

1. **获取查询链接** — 三种方式可选：
   - 🔑 SSO 登录自动获取（推荐）
   - 🔗 手动粘贴链接
   - 🔍 SSO 仅获取链接（不保存到配置）
2. **预警阈值**（可跳过，使用默认值）
3. **刷新间隔**（可跳过，使用默认值）

设置完成后即可正常使用。后续可随时通过 `/settings` 修改配置。

### 充值流程

1. 发送 `/charge`，Bot 加载页面并显示当前余额
2. 选择充值类型（电费/冷水/热水）
3. 选择充值档位
4. 选择支付方式（支付宝/微信）
5. Bot 返回支付链接，在手机浏览器中打开支付
6. Bot 自动监控余额变化，充值到账后即时通知

### 完整命令列表

| 命令 | 功能 |
|------|------|
| `/start` | 开始使用 / 首次引导设置 |
| `/settings` | 交互式设置（链接、阈值、间隔等） |
| `/balance` | 查询当前余额（缓存） |
| `/update` | 立即刷新余额 |
| `/charge` | 开始交互式充值流程 |
| `/link` | 获取查询链接 / SSO 登录获取链接 |
| `/cancel` | 取消任何进行中的操作 |

### CLI 单次查询

```bash
python main.py --once --url <链接>    # 查询一次余额后退出
python main.py --debug --url <链接>   # 调试模式，打印所有拦截到的 API 数据
```

## 🏗️ 项目结构

```
├── main.py          # 入口：CLI 参数解析，启动 Bot
├── bot.py           # Telegram Bot：多用户命令处理、会话管理、定时任务
├── fetcher.py       # Playwright 页面交互、API 拦截、余额解析
├── sso.py           # SSO 统一认证登录，自动获取查询链接
├── config.py        # 配置文件加载（仅 Bot 连接配置）
├── store.py         # 多用户数据持久化（JSON 存储）
├── config.yaml.example  # 配置模板
├── pyproject.toml   # 项目元数据与依赖
└── requirements.txt # pip 依赖列表
```

## 🔧 工作原理

1. **多用户架构** — 每位用户的配置（URL、阈值、间隔）存储在 `users.json` 中，运行时余额缓存按用户隔离，定时任务按用户独立调度
2. **余额获取** — Playwright 加载 17wanxiao H5 页面，监听所有 JSON 响应，从 `detaillist` 结构中按 `businesstype` 识别电费/冷水/热水余额
3. **充值流程** — 通过模拟触屏点击完成：选择充值类型 → 选择金额档位 → 提交订单 → 跳转支付网关 → 选择支付方式 → 拦截支付链接（不消费，保留给用户）
4. **支付链接拦截** — 当浏览器即将跳转到支付宝/微信支付页面时，立即 abort 请求并捕获 URL，确保支付链接未被消费，用户可在手机上正常使用
5. **充值监控** — 生成支付链接后启动后台轮询，每隔几秒检查余额变化，检测到余额增加后自动通知用户到账
6. **SSO 登录** — 通过 Playwright 自动化完成学校 SSO 统一认证登录，跟随 CAS 跳转获取 17wanxiao 水电费查询直连链接

## 📄 License

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 发布。

本程序是自由软件：您可以按照自由软件基金会发布的 GNU Affero 通用公共许可证第 3 版或（由您选择）更高版本的条款重新分发和/或修改它。本程序不附带任何担保。

如果您修改本程序并通过网络提供服务，您必须向用户提供修改后的完整源代码。
