# ZCST Fee Bot 🏠

宿舍电费 / 冷水 / 热水余额监控 & 充值机器人。

通过 Playwright 无头浏览器加载 [17wanxiao](https://www.17wanxiao.com/) H5 页面，自动拦截 API 获取余额数据，并提供 **Telegram Bot** 交互式查询与充值功能。

## ✨ 功能

- **余额查询** — `/balance` 一键查询电费、冷水、热水余额
- **交互式充值** — `/charge` 多步引导，选择类型 → 档位 → 支付方式，生成支付宝/微信支付链接
- **定时预警** — 余额低于阈值时自动推送 Telegram 通知
- **CLI 模式** — `--once` 单次查询 / `--debug` 调试原始数据

## 📋 前置条件

- Python ≥ 3.11
- 一个 17wanxiao 宿舍费用查询链接（从学校公众号/小程序获取）
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

复制示例配置文件并填入你的信息：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

| 字段 | 说明 |
|------|------|
| `url` | 17wanxiao 完整查询链接 |
| `telegram.bot_token` | Telegram Bot Token |
| `telegram.chat_id` | 你的 Telegram Chat ID |
| `telegram.proxy` | *(可选)* HTTP 代理地址，如 `http://127.0.0.1:7890` |
| `telegram.api_base` | *(可选)* 自定义 Telegram API 反代地址 |
| `thresholds.*` | 余额预警阈值 |
| `check_interval` | 定时检查间隔（秒），默认 3600 |

> 💡 获取 `chat_id`：向你的 Bot 发送一条消息，然后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`

## 📖 使用

### 启动 Bot（常驻模式）

```bash
python main.py
```

Bot 启动后支持以下命令：

| 命令 | 功能 |
|------|------|
| `/start` | 显示帮助菜单 |
| `/balance` | 查询当前余额 |
| `/charge` | 开始交互式充值流程 |
| `/cancel` | 取消当前充值操作 |

### CLI 单次查询

```bash
python main.py --once     # 查询一次余额后退出
python main.py --debug    # 调试模式，打印所有拦截到的 API 数据
```

## 🏗️ 项目结构

```
├── main.py          # 入口：CLI 参数解析，启动 Bot
├── bot.py           # Telegram Bot：命令处理、会话管理、定时任务
├── fetcher.py       # Playwright 页面交互、API 拦截、余额解析
├── config.py        # 配置文件加载与合并
├── config.yaml.example  # 配置模板
├── pyproject.toml   # 项目元数据与依赖
└── requirements.txt # pip 依赖列表
```

## 🔧 工作原理

1. **余额获取** — Playwright 加载 17wanxiao H5 页面，监听所有 JSON 响应，从 `detaillist` 结构中按 `businesstype` 识别电费/冷水/热水余额
2. **充值流程** — 通过模拟触屏点击完成：选择充值类型 → 选择金额档位 → 提交订单 → 跳转支付网关 → 选择支付方式 → 拦截支付链接（不消费，保留给用户）
3. **支付链接拦截** — 当浏览器即将跳转到支付宝/微信支付页面时，立即 abort 请求并捕获 URL，确保支付链接未被消费，用户可在手机上正常使用

## 📄 License

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 发布。

本程序是自由软件：您可以按照自由软件基金会发布的 GNU Affero 通用公共许可证第 3 版或（由您选择）更高版本的条款重新分发和/或修改它。本程序不附带任何担保。

如果您修改本程序并通过网络提供服务，您必须向用户提供修改后的完整源代码。
