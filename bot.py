"""
bot.py
交互式 Telegram Bot —— 余额查询 + 分步充值 + 定时预警。
"""
import asyncio

from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from fetcher import RechargeSession, fetch_balances_async

# ── 会话状态 ─────────────────────────────────────────────────────────────────
CHOOSE_TYPE, CHOOSE_AMOUNT, CHOOSE_PAYMENT = range(3)

# ── 常量 ─────────────────────────────────────────────────────────────────────
_LABELS = {
    "electricity": ("⚡ 电费", "kWh"),
    "cold_water":  ("🚰 冷水", "吨"),
    "hot_water":   ("♨️ 热水", "吨"),
}

_FEE_TYPES = [
    ("electricity", "⚡ 电费"),
    ("cold_water",  "🚰 冷水"),
    ("hot_water",   "♨️ 热水"),
]

# 模块级配置，由 create_bot() 注入
_cfg: dict = {}


# ── 工具函数 ─────────────────────────────────────────────────────────────────

async def _cleanup_session(context: ContextTypes.DEFAULT_TYPE):
    """关闭并清理用户的浏览器会话。"""
    session: RechargeSession | None = context.user_data.pop("session", None)
    if session:
        await session.close()


# ── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *宿舍费用机器人*\n\n"
        "/balance — 查询余额\n"
        "/charge  — 充值\n"
        "/cancel  — 取消当前操作",
        parse_mode="Markdown",
    )


# ── /balance ─────────────────────────────────────────────────────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ 正在查询余额…")
    try:
        balances = await fetch_balances_async(_cfg["url"])
    except Exception as e:
        await msg.edit_text(f"❌ 查询失败：{e}")
        return

    if not balances:
        await msg.edit_text("❌ 未能获取余额数据，请检查链接是否过期。")
        return

    thresholds = _cfg.get("thresholds", {})
    lines = ["📊 *当前余额*\n"]
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            val = balances[key]
            threshold = thresholds.get(key, 0)
            warn = " ⚠️" if val < threshold else ""
            lines.append(f"{label}：`{val:.2f}` {unit}{warn}")

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


# ── /charge 会话 ─────────────────────────────────────────────────────────────

async def charge_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """入口：加载主页，展示费用类型选择。"""
    await _cleanup_session(context)

    msg = await update.message.reply_text("⏳ 正在加载页面，请稍候…")

    session = RechargeSession()
    try:
        balances = await session.start(_cfg["url"])
    except Exception as e:
        await msg.edit_text(f"❌ 页面加载失败：{e}")
        await session.close()
        return ConversationHandler.END

    context.user_data["session"] = session

    lines = ["📊 当前余额：\n"]
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            lines.append(f"  {label}：{balances[key]:.2f} {unit}")
    lines.append("\n请选择充值类型：")

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"type_{key}")]
        for key, label in _FEE_TYPES
    ]
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    await msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_TYPE


async def on_choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第一步：用户选择费用类型 → 展示档位。"""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await _cleanup_session(context)
        await query.edit_message_text("已取消充值。")
        return ConversationHandler.END

    fee_type = query.data.removeprefix("type_")
    session: RechargeSession | None = context.user_data.get("session")
    if not session:
        await query.edit_message_text("❌ 会话已过期，请重新 /charge")
        return ConversationHandler.END

    context.user_data["fee_type"] = fee_type
    await query.edit_message_text("⏳ 正在获取充值档位…")

    try:
        amounts = await session.get_amounts(fee_type)
    except Exception as e:
        await query.edit_message_text(f"❌ 获取档位失败：{e}")
        await _cleanup_session(context)
        return ConversationHandler.END

    if not amounts:
        await query.edit_message_text("❌ 未找到可用充值档位。")
        await _cleanup_session(context)
        return ConversationHandler.END

    context.user_data["amounts"] = amounts

    keyboard = [
        [InlineKeyboardButton(a["text"], callback_data=f"amount_{a['index']}")]
        for a in amounts
    ]
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    await query.edit_message_text(
        "请选择充值档位：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_AMOUNT


async def on_choose_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第二步：用户选择档位 → 提交订单 → 展示支付方式。"""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await _cleanup_session(context)
        await query.edit_message_text("已取消充值。")
        return ConversationHandler.END

    amount_index = int(query.data.removeprefix("amount_"))
    session: RechargeSession | None = context.user_data.get("session")
    if not session:
        await query.edit_message_text("❌ 会话已过期，请重新 /charge")
        return ConversationHandler.END

    await query.edit_message_text("⏳ 正在提交订单，请稍候…")

    try:
        methods = await session.confirm_and_get_pay_methods(amount_index)
    except Exception as e:
        await query.edit_message_text(f"❌ 提交订单失败：{e}")
        await _cleanup_session(context)
        return ConversationHandler.END

    if not methods:
        await query.edit_message_text("❌ 未找到可用支付方式。")
        await _cleanup_session(context)
        return ConversationHandler.END

    context.user_data["methods"] = methods

    _icons = {"支付宝": "💳", "微信": "💚"}
    keyboard = []
    for m in methods:
        icon = next((v for k, v in _icons.items() if k in m["name"]), "💳")
        keyboard.append(
            [InlineKeyboardButton(f"{icon} {m['name']}", callback_data=f"pay_{m['index']}")]
        )
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    await query.edit_message_text(
        "请选择支付方式：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSE_PAYMENT


async def on_choose_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """第三步：用户选择支付方式 → 获取支付链接 → 发送给用户。"""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await _cleanup_session(context)
        await query.edit_message_text("已取消充值。")
        return ConversationHandler.END

    method_index = int(query.data.removeprefix("pay_"))
    session: RechargeSession | None = context.user_data.get("session")
    if not session:
        await query.edit_message_text("❌ 会话已过期，请重新 /charge")
        return ConversationHandler.END

    await query.edit_message_text("⏳ 正在生成支付链接…")

    try:
        pay_url = await session.select_payment(method_index)
    except Exception as e:
        await query.edit_message_text(f"❌ 获取支付链接失败：{e}")
        await _cleanup_session(context)
        return ConversationHandler.END

    await _cleanup_session(context)

    keyboard = [[InlineKeyboardButton("💰 去支付", url=pay_url)]]
    await query.edit_message_text(
        "✅ 支付链接已生成！\n\n点击下方按钮或复制链接完成支付：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def on_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理会话中发送的 /cancel 命令。"""
    await _cleanup_session(context)
    await update.message.reply_text("已取消充值。")
    return ConversationHandler.END


async def on_unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """会话中收到意外消息时提示。"""
    await update.message.reply_text("请点击上方按钮继续操作，或发送 /cancel 取消。")


# ── 定时余额检查 ─────────────────────────────────────────────────────────────

async def _scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    """定时检查余额，低于阈值时推送预警。"""
    chat_id = _cfg.get("telegram", {}).get("chat_id", "").strip()
    if not chat_id:
        return

    try:
        balances = await fetch_balances_async(_cfg["url"])
    except Exception as e:
        logger.error(f"定时检查失败：{e}")
        return

    if not balances:
        return

    thresholds = _cfg.get("thresholds", {})
    alerts = []
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            val = balances[key]
            threshold = thresholds.get(key, 0)
            if val < threshold:
                alerts.append(
                    f"*{label}*：`{val:.2f}` {unit}（预警值 {threshold:.2f}）"
                )

    if not alerts:
        return

    text = "⚠️ *余额预警*\n\n" + "\n".join(alerts) + "\n\n使用 /charge 快速充值"
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"发送预警失败：{e}")


# ── 全局错误处理 ─────────────────────────────────────────────────────────

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")


# ── 构建应用 ─────────────────────────────────────────────────────────────────

def create_bot(config: dict) -> Application:
    """根据配置创建并返回 Telegram Bot Application。"""
    global _cfg
    _cfg = config

    token = config["telegram"]["bot_token"]
    proxy = config["telegram"].get("proxy", "").strip() or None
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        connection_pool_size=8,
        proxy=proxy,
    )
    builder = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(HTTPXRequest(
            connect_timeout=30.0, read_timeout=30.0, proxy=proxy,
        ))
    )

    api_base = config["telegram"].get("api_base", "").strip()
    if api_base:
        api_base = api_base.rstrip("/")
        builder = builder.base_url(f"{api_base}/bot")
        builder = builder.base_file_url(f"{api_base}/file/bot")

    app = builder.build()

    # 会话处理器（/charge 多步充值流程）
    conv = ConversationHandler(
        entry_points=[CommandHandler("charge", charge_start)],
        states={
            CHOOSE_TYPE:    [CallbackQueryHandler(on_choose_type)],
            CHOOSE_AMOUNT:  [CallbackQueryHandler(on_choose_amount)],
            CHOOSE_PAYMENT: [CallbackQueryHandler(on_choose_payment)],
        },
        fallbacks=[
            CommandHandler("cancel", on_cancel_cmd),
            MessageHandler(filters.ALL, on_unexpected_message),
        ],
        conversation_timeout=300,  # 5 分钟超时自动结束
        per_chat=True,
        per_user=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(conv)
    app.add_error_handler(_error_handler)

    # 定时余额检查
    interval = int(config.get("check_interval", 3600))
    if interval > 0:
        app.job_queue.run_repeating(
            _scheduled_check,
            interval=interval,
            first=10,
        )

    return app
