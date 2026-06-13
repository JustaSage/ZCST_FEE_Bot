# ZCST Fee Bot — 宿舍费用监控 & 充值机器人
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
bot.py
交互式 Telegram Bot —— 多用户余额查询 + 交互式设置 + 定时预警。
首次使用自动引导配置，所有设置通过 /settings 交互完成，数据完全隔离。
"""
import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

from loguru import logger
from telegram import Update, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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

from fetcher import fetch_balances_async, fetch_index_data_async
from payment import create_alipay_url, convert_to_alipay_scheme_url, convert_to_cashier_url
from sso import sso_fetch_fee_url
from store import UserStore

# ── 会话状态 ─────────────────────────────────────────────────────────────────
(SETTINGS_MENU, AWAITING_INPUT,
 SETUP_URL, SETUP_THRESHOLD, SETUP_INTERVAL,
 SSO_USERNAME, SSO_PASSWORD,
 CHARGE_FEE_TYPE, CHARGE_AMOUNT, CHARGE_CUSTOM_AMOUNT) = range(10)

# ── 常量 ─────────────────────────────────────────────────────────────────────
_LABELS = {
    "electricity": ("⚡ 电费", "kWh"),
    "cold_water":  ("🚰 冷水", "吨"),
    "hot_water":   ("♨️ 热水", "吨"),
}

# 引导设置时阈值的顺序
_THRESHOLD_STEPS = ["electricity", "cold_water", "hot_water"]

# 充值可选固定金额（元）
_CHARGE_AMOUNTS = [10, 20, 30, 50, 100]

# 充值后到账监控：每 15 秒查一次，最多 20 次（5 分钟）
_CHARGE_POLL_INTERVAL = 15
_CHARGE_POLL_MAX_ATTEMPTS = 20

# ── 模块级状态（由 create_bot 初始化） ────────────────────────────────────────
_bot_cfg: dict = {}
_store: UserStore | None = None

_user_caches: dict[str, dict] = {}   # uid → {"balances": {...}, "time": datetime}
_user_locks: dict[str, asyncio.Lock] = {}


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _uid(update: Update) -> str:
    return str(update.effective_user.id)


def _get_lock(uid: str) -> asyncio.Lock:
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    return _user_locks[uid]


def _user_url(uid: str) -> str | None:
    cfg = _store.get(uid) if _store else None
    if cfg and cfg.get("url"):
        return cfg["url"]
    return None


async def _require_url(update: Update) -> bool:
    uid = _uid(update)
    if _user_url(uid):
        return True
    await update.message.reply_text(
        "⚠️ 尚未设置查询链接。\n请先使用 /settings 配置。",
    )
    return False


async def _refresh_user_cache(uid: str) -> dict:
    url = _user_url(uid)
    if not url:
        return {}
    lock = _get_lock(uid)
    async with lock:
        balances = await fetch_balances_async(url)
        if balances:
            _user_caches[uid] = {"balances": balances, "time": datetime.now()}
        cache = _user_caches.get(uid, {})
        return cache.get("balances", {})


def _update_user_cache(uid: str, balances: dict):
    if balances:
        _user_caches[uid] = {"balances": balances, "time": datetime.now()}


def _format_balance_msg(uid: str) -> str:
    cache = _user_caches.get(uid, {})
    balances = cache.get("balances", {})
    updated = cache.get("time")
    cfg = (_store.get(uid) if _store else None) or {}
    thresholds = cfg.get("thresholds", {})
    lines = ["📊 *当前余额*\n"]
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            val = balances[key]
            threshold = thresholds.get(key, 0)
            warn = " ⚠️" if val < threshold else ""
            lines.append(f"{label}：`{val:.2f}` {unit}{warn}")
    if updated:
        lines.append(f"\n🕓 更新于 {updated:%H:%M:%S}")
    return "\n".join(lines)


def _schedule_user_job(app: Application, uid: str, interval: int):
    job_name = f"refresh_{uid}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    if interval > 0:
        app.job_queue.run_repeating(
            _scheduled_user_refresh,
            interval=interval,
            first=10,
            name=job_name,
            data=uid,
        )


def _remove_user_job(app: Application, uid: str):
    for job in app.job_queue.get_jobs_by_name(f"refresh_{uid}"):
        job.schedule_removal()


async def _verify_url_and_cache(uid: str, url: str) -> dict:
    """用给定 URL 拉取余额，成功则更新缓存并返回余额，失败返回空 dict。"""
    lock = _get_lock(uid)
    async with lock:
        balances = await fetch_balances_async(url)
        if balances:
            _user_caches[uid] = {"balances": balances, "time": datetime.now()}
        return balances or {}


def _skip_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ 使用默认值", callback_data=callback_data)]
    ])


# ── /start（首次引导 / 老用户帮助） ──────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    has_url = bool(_user_url(uid))

    if has_url:
        await update.message.reply_text(
            "👋 *宿舍费用机器人*\n\n"
            "/balance — 查询余额\n"
            "/update  — 刷新余额\n"
            "/charge  — 充值\n"
            "/settings — 设置\n"
            "/link    — 获取查询链接\n"
            "/cancel  — 取消当前操作",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # 首次使用 → 自动进入引导设置
    _store.ensure(uid)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 SSO 登录获取链接", callback_data="setup_sso")],
        [InlineKeyboardButton("🔗 手动粘贴链接", callback_data="setup_paste_url")],
        [InlineKeyboardButton("🔍 SSO 仅获取链接（不保存）", callback_data="setup_sso_link_only")],
    ])
    await update.message.reply_text(
        "👋 *欢迎使用宿舍费用机器人！*\n\n"
        "让我们来完成初始配置。\n\n"
        "🔗 *第 1 步*：设置查询链接\n\n"
        "请选择获取链接的方式：",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return SETUP_URL


# ── 引导设置流程 ─────────────────────────────────────────────────────────────

async def on_setup_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 1：用户选择获取链接的方式。"""
    query = update.callback_query
    await query.answer()

    if query.data == "setup_paste_url":
        await query.edit_message_text(
            "🔗 请发送你的 17wanxiao 查询链接\n\n"
            "从学校公众号/小程序获取宿舍费用链接后直接粘贴发送。\n\n"
            "发送 /cancel 取消。"
        )
        return SETUP_URL

    if query.data == "setup_sso":
        context.user_data["sso_origin"] = "setup"
        await query.edit_message_text(
            "🔑 *SSO 统一认证登录*\n\n"
            "请发送你的 SSO 账号（学号/工号）：\n\n"
            "发送 /cancel 取消。",
            parse_mode="Markdown",
        )
        return SSO_USERNAME

    if query.data == "setup_sso_link_only":
        context.user_data["sso_origin"] = "link"
        await query.edit_message_text(
            "🔍 *SSO 仅获取链接*\n\n"
            "请发送你的 SSO 账号（学号/工号）：\n\n"
            "获取到的链接会直接发给你，不会保存到机器人配置中。\n\n"
            "发送 /cancel 取消。",
            parse_mode="Markdown",
        )
        return SSO_USERNAME

    return SETUP_URL


async def on_setup_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 1：用户发送 URL → 验证 → 下一步设置阈值。"""
    uid = _uid(update)
    url = update.message.text.strip()

    msg = await update.message.reply_text("⏳ 正在验证链接…")

    try:
        balances = await _verify_url_and_cache(uid, url)
    except Exception as e:
        await msg.edit_text(f"❌ 链接验证失败：{e}\n\n请重新发送正确的链接：")
        return SETUP_URL

    if not balances:
        await msg.edit_text("❌ 无法获取余额数据，链接可能无效或已过期。\n\n请重新发送正确的链接：")
        return SETUP_URL

    # 链接有效，保存
    _store.update(uid, "url", url)

    # 显示余额确认
    lines = ["✅ 链接验证成功！当前余额：\n"]
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            lines.append(f"  {label}：{balances[key]:.2f} {unit}")

    # 进入阈值设置
    context.user_data["setup_threshold_idx"] = 0
    fee_type = _THRESHOLD_STEPS[0]
    label, unit = _LABELS[fee_type]
    cfg = _store.get(uid) or {}
    default = cfg.get("thresholds", {}).get(fee_type, 0)

    lines.append(
        f"\n📊 *第 2 步*：设置预警阈值\n\n"
        f"请发送 {label} 的预警阈值（{unit}）\n"
        f"当前值：{default}"
    )

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_skip_keyboard("setup_skip_threshold"),
    )
    return SETUP_THRESHOLD


async def on_setup_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 2：用户发送阈值数字。"""
    uid = _uid(update)
    text = update.message.text.strip()
    idx = context.user_data.get("setup_threshold_idx", 0)
    fee_type = _THRESHOLD_STEPS[idx]

    try:
        value = float(text)
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的数字，或点击下方按钮跳过：",
            reply_markup=_skip_keyboard("setup_skip_threshold"),
        )
        return SETUP_THRESHOLD
    if value < 0:
        await update.message.reply_text(
            "❌ 阈值不能为负数，请重新发送：",
            reply_markup=_skip_keyboard("setup_skip_threshold"),
        )
        return SETUP_THRESHOLD

    _store.update_threshold(uid, fee_type, value)
    label = _LABELS[fee_type][0]

    # 下一个阈值
    idx += 1
    if idx < len(_THRESHOLD_STEPS):
        context.user_data["setup_threshold_idx"] = idx
        next_type = _THRESHOLD_STEPS[idx]
        next_label, next_unit = _LABELS[next_type]
        cfg = _store.get(uid) or {}
        default = cfg.get("thresholds", {}).get(next_type, 0)
        await update.message.reply_text(
            f"✅ {label} 预警阈值已设为 {value}\n\n"
            f"请发送 {next_label} 的预警阈值（{next_unit}）\n"
            f"当前值：{default}",
            parse_mode="Markdown",
            reply_markup=_skip_keyboard("setup_skip_threshold"),
        )
        return SETUP_THRESHOLD

    # 阈值设置完毕 → 刷新间隔
    context.user_data.pop("setup_threshold_idx", None)
    cfg = _store.get(uid) or {}
    default_interval = cfg.get("check_interval", 300)
    await update.message.reply_text(
        f"✅ {label} 预警阈值已设为 {value}\n\n"
        f"⏱ *第 3 步*：设置定时刷新间隔\n\n"
        f"当前值：{default_interval} 秒\n"
        f"最小 60 秒，设为 0 关闭定时刷新。",
        parse_mode="Markdown",
        reply_markup=_skip_keyboard("setup_skip_interval"),
    )
    return SETUP_INTERVAL


async def on_skip_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 2：用户点击跳过按钮。"""
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    idx = context.user_data.get("setup_threshold_idx", 0)

    # 下一个阈值
    idx += 1
    if idx < len(_THRESHOLD_STEPS):
        context.user_data["setup_threshold_idx"] = idx
        next_type = _THRESHOLD_STEPS[idx]
        next_label, next_unit = _LABELS[next_type]
        cfg = _store.get(uid) or {}
        default = cfg.get("thresholds", {}).get(next_type, 0)
        await query.edit_message_text(
            f"请发送 {next_label} 的预警阈值（{next_unit}）\n"
            f"当前值：{default}",
            parse_mode="Markdown",
            reply_markup=_skip_keyboard("setup_skip_threshold"),
        )
        return SETUP_THRESHOLD

    # 阈值设置完毕 → 刷新间隔
    context.user_data.pop("setup_threshold_idx", None)
    cfg = _store.get(uid) or {}
    default_interval = cfg.get("check_interval", 300)
    await query.edit_message_text(
        f"⏱ *第 3 步*：设置定时刷新间隔\n\n"
        f"当前值：{default_interval} 秒\n"
        f"最小 60 秒，设为 0 关闭定时刷新。",
        parse_mode="Markdown",
        reply_markup=_skip_keyboard("setup_skip_interval"),
    )
    return SETUP_INTERVAL


async def on_setup_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 3：用户发送刷新间隔。"""
    uid = _uid(update)
    text = update.message.text.strip()

    try:
        value = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的整数，或点击下方按钮跳过：",
            reply_markup=_skip_keyboard("setup_skip_interval"),
        )
        return SETUP_INTERVAL
    if value != 0 and value < 60:
        await update.message.reply_text(
            "❌ 间隔不能小于 60 秒（设为 0 可关闭），请重新发送：",
            reply_markup=_skip_keyboard("setup_skip_interval"),
        )
        return SETUP_INTERVAL

    _store.update(uid, "check_interval", value)

    # 安排定时任务
    cfg = _store.get(uid) or {}
    interval = cfg.get("check_interval", 300)
    _schedule_user_job(context.application, uid, interval)

    await update.message.reply_text(
        "🎉 *配置完成！*\n\n"
        "现在可以使用以下命令：\n"
        "/balance — 查询余额\n"
        "/update  — 刷新余额\n"
        "/charge  — 充值\n"
        "/settings — 修改设置\n"
        "/cancel  — 取消当前操作",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def on_skip_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """引导步骤 3：用户点击跳过按钮。"""
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)

    # 安排定时任务（使用默认值）
    cfg = _store.get(uid) or {}
    interval = cfg.get("check_interval", 300)
    _schedule_user_job(context.application, uid, interval)

    await query.edit_message_text(
        "🎉 *配置完成！*\n\n"
        "现在可以使用以下命令：\n"
        "/balance — 查询余额\n"
        "/update  — 刷新余额\n"
        "/charge  — 充值\n"
        "/settings — 修改设置\n"
        "/cancel  — 取消当前操作",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /balance ─────────────────────────────────────────────────────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_url(update):
        return

    uid = _uid(update)
    cache = _user_caches.get(uid)

    if not cache:
        msg = await update.message.reply_text("⏳ 首次查询，正在获取余额…")
        try:
            balances = await _refresh_user_cache(uid)
        except Exception as e:
            await msg.edit_text(f"❌ 查询失败：{e}")
            return
        if not balances:
            await msg.edit_text("❌ 未能获取余额数据，请检查链接是否过期。")
            return
        await msg.edit_text(_format_balance_msg(uid), parse_mode="Markdown")
        return

    await update.message.reply_text(
        _format_balance_msg(uid), parse_mode="Markdown"
    )


# ── /update ──────────────────────────────────────────────────────────────────

async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_url(update):
        return

    uid = _uid(update)
    msg = await update.message.reply_text("⏳ 正在刷新余额…")
    try:
        balances = await _refresh_user_cache(uid)
    except Exception as e:
        await msg.edit_text(f"❌ 刷新失败：{e}")
        return
    if not balances:
        await msg.edit_text("❌ 未能获取余额数据，请检查链接是否过期。")
        return
    await msg.edit_text(_format_balance_msg(uid), parse_mode="Markdown")


# ── /settings 交互式设置 ─────────────────────────────────────────────────────

def _settings_text(uid: str) -> str:
    cfg = (_store.get(uid) if _store else None) or {}
    url = cfg.get("url", "")
    url_status = "已设置 ✅" if url else "未设置 ❌"
    thresholds = cfg.get("thresholds", {})
    interval = cfg.get("check_interval", 300)

    lines = [
        "⚙️ *当前设置*\n",
        f"🔗 链接：{url_status}",
        f"⏱ 刷新间隔：{interval} 秒" + ("（已关闭）" if interval == 0 else ""),
        "\n📊 *预警阈值*",
    ]
    for key, (label, unit) in _LABELS.items():
        val = thresholds.get(key, 0)
        lines.append(f"  {label}：{val} {unit}")
    return "\n".join(lines)


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 修改链接", callback_data="set_url"),
            InlineKeyboardButton("🔑 SSO 登录", callback_data="set_sso"),
        ],
        [
            InlineKeyboardButton("⚡ 电费阈值", callback_data="set_threshold_electricity"),
            InlineKeyboardButton("🚰 冷水阈值", callback_data="set_threshold_cold_water"),
        ],
        [
            InlineKeyboardButton("♨️ 热水阈值", callback_data="set_threshold_hot_water"),
            InlineKeyboardButton("⏱ 刷新间隔", callback_data="set_interval"),
        ],
        [InlineKeyboardButton("🗑 清除所有数据", callback_data="reset")],
        [InlineKeyboardButton("✅ 完成", callback_data="done")],
    ])


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    _store.ensure(uid)
    await update.message.reply_text(
        _settings_text(uid),
        parse_mode="Markdown",
        reply_markup=_settings_keyboard(),
    )
    return SETTINGS_MENU


async def on_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    data = query.data

    if data == "done":
        await query.edit_message_text("✅ 设置完成。")
        return ConversationHandler.END

    if data == "set_url":
        context.user_data["setting_key"] = "url"
        await query.edit_message_text(
            "🔗 请发送新的查询链接：\n\n"
            "从学校公众号/小程序获取 17wanxiao 宿舍费用链接后粘贴发送。\n\n"
            "发送 /cancel 取消。"
        )
        return AWAITING_INPUT

    if data == "set_sso":
        context.user_data["sso_origin"] = "settings"
        await query.edit_message_text(
            "🔑 *SSO 统一认证登录*\n\n"
            "请发送你的 SSO 账号（学号/工号）：\n\n"
            "发送 /cancel 取消。",
            parse_mode="Markdown",
        )
        return SSO_USERNAME

    if data.startswith("set_threshold_"):
        fee_type = data.removeprefix("set_threshold_")
        if fee_type not in _LABELS:
            return SETTINGS_MENU
        label, unit = _LABELS[fee_type]
        context.user_data["setting_key"] = f"threshold_{fee_type}"
        await query.edit_message_text(
            f"📊 请发送 {label} 的预警阈值（{unit}）：\n\n"
            "发送 /cancel 取消。"
        )
        return AWAITING_INPUT

    if data == "set_interval":
        context.user_data["setting_key"] = "interval"
        await query.edit_message_text(
            "⏱ 请发送刷新间隔（秒）：\n\n"
            "最小 60 秒，设为 0 关闭定时刷新。\n\n"
            "发送 /cancel 取消。"
        )
        return AWAITING_INPUT

    if data == "reset":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ 确认清除", callback_data="reset_confirm")],
            [InlineKeyboardButton("↩️ 返回", callback_data="settings_back")],
        ])
        await query.edit_message_text(
            "⚠️ *确认清除所有个人数据？*\n\n"
            "链接、阈值、缓存等将全部删除。",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return SETTINGS_MENU

    if data == "reset_confirm":
        _remove_user_job(context.application, uid)
        _user_caches.pop(uid, None)
        _user_locks.pop(uid, None)
        if _store:
            _store.delete(uid)
        await query.edit_message_text("✅ 所有个人数据已清除。")
        return ConversationHandler.END

    if data == "settings_back":
        _store.ensure(uid)
        await query.edit_message_text(
            _settings_text(uid),
            parse_mode="Markdown",
            reply_markup=_settings_keyboard(),
        )
        return SETTINGS_MENU

    return SETTINGS_MENU


async def on_awaiting_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    text = update.message.text.strip()
    key = context.user_data.get("setting_key", "")

    if key == "url":
        # 先验证链接
        msg = await update.message.reply_text("⏳ 正在验证链接…")
        try:
            balances = await _verify_url_and_cache(uid, text)
        except Exception as e:
            await msg.edit_text(f"❌ 链接验证失败：{e}\n\n请重新发送正确的链接：")
            return AWAITING_INPUT
        if not balances:
            await msg.edit_text("❌ 无法获取余额数据，链接可能无效或已过期。\n\n请重新发送正确的链接：")
            return AWAITING_INPUT

        _store.ensure(uid)
        _store.update(uid, "url", text)
        cfg = _store.get(uid)
        interval = cfg.get("check_interval", 300)
        _schedule_user_job(context.application, uid, interval)

        # 显示余额确认
        lines = ["✅ 链接验证成功！当前余额：\n"]
        for bk, (label, unit) in _LABELS.items():
            if bk in balances:
                lines.append(f"  {label}：{balances[bk]:.2f} {unit}")
        lines.append(f"\n{_settings_text(uid)}")
        context.user_data.pop("setting_key", None)
        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_settings_keyboard(),
        )
        return SETTINGS_MENU

    elif key.startswith("threshold_"):
        fee_type = key.removeprefix("threshold_")
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字，重新发送：")
            return AWAITING_INPUT
        if value < 0:
            await update.message.reply_text("❌ 阈值不能为负数，重新发送：")
            return AWAITING_INPUT
        _store.update_threshold(uid, fee_type, value)

    elif key == "interval":
        try:
            value = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的整数，重新发送：")
            return AWAITING_INPUT
        if value != 0 and value < 60:
            await update.message.reply_text("❌ 间隔不能小于 60 秒（设为 0 可关闭），重新发送：")
            return AWAITING_INPUT
        _store.update(uid, "check_interval", value)
        if _user_url(uid):
            _schedule_user_job(context.application, uid, value)

    else:
        await update.message.reply_text("❌ 未知设置项，请重试。")
        return ConversationHandler.END

    context.user_data.pop("setting_key", None)
    await update.message.reply_text(
        f"✅ 已保存！\n\n{_settings_text(uid)}",
        parse_mode="Markdown",
        reply_markup=_settings_keyboard(),
    )
    return SETTINGS_MENU


# ── /charge 会话 ─────────────────────────────────────────────────────────────

async def cmd_charge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/charge：交互式选择充值类型与金额，纯 API 生成支付宝支付链接。"""
    if not await _require_url(update):
        return ConversationHandler.END

    uid = _uid(update)
    msg = await update.message.reply_text("⏳ 正在获取最新余额…")

    try:
        balances = await _refresh_user_cache(uid)
    except Exception as e:
        await msg.edit_text(f"❌ 刷新余额失败：{e}")
        return ConversationHandler.END

    if not balances:
        await msg.edit_text("❌ 未能获取余额数据，请检查链接是否过期。")
        return ConversationHandler.END

    lines = ["📊 当前余额：\n"]
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            lines.append(f"  {label}：{balances[key]:.2f} {unit}")
    lines.append("\n请选择要充值的类型：")

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"charge_type:{key}")]
        for key, (label, _) in _LABELS.items()
    ]

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHARGE_FEE_TYPE


async def on_charge_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择充值类型后，展示金额选项。"""
    query = update.callback_query
    await query.answer()

    uid = str(query.from_user.id)
    fee_type = query.data.split(":", 1)[1]
    context.user_data["charge_fee_type"] = fee_type

    url = _user_url(uid)
    msg = await query.edit_message_text("⏳ 正在读取价格信息…")

    try:
        _login, index_data = await fetch_index_data_async(url)
    except Exception as e:
        await msg.edit_text(f"❌ 读取宿舍信息失败：{e}\n\n请重试 /charge")
        context.user_data.pop("charge_fee_type", None)
        return ConversationHandler.END

    txcode = {"electricity": "51101", "cold_water": "51201", "hot_water": "51301"}[fee_type]
    mod = None
    for item in index_data.get("modlist") or []:
        if str(item.get("accode")) == txcode or str(item.get("ecardacccode")) == txcode:
            mod = item
            break

    if mod is None:
        await msg.edit_text("❌ 未找到该类型的充值设备信息。")
        context.user_data.pop("charge_fee_type", None)
        return ConversationHandler.END

    price = float(mod.get("price") or 0)
    context.user_data["charge_price"] = price

    label, unit = _LABELS[fee_type]
    lines = [
        f"*已选择：{label}*",
        f"单价：`{price:.3f}` 元/{unit}\n",
        "请选择充值金额：",
    ]

    keyboard = []
    for amt in _CHARGE_AMOUNTS:
        est = amt / price if price else 0
        keyboard.append([
            InlineKeyboardButton(
                f"¥{amt}（约 {est:.2f} {unit}）",
                callback_data=f"charge_amt:{amt * 100}",
            )
        ])
    keyboard.append([
        InlineKeyboardButton("✏️ 其他金额", callback_data="charge_custom"),
        InlineKeyboardButton("❌ 取消", callback_data="charge_cancel"),
    ])

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHARGE_AMOUNT


async def on_charge_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择固定金额或点击自定义/取消。"""
    query = update.callback_query
    await query.answer()

    uid = str(query.from_user.id)
    data = query.data

    if data == "charge_cancel":
        context.user_data.pop("charge_fee_type", None)
        context.user_data.pop("charge_price", None)
        await query.edit_message_text("✅ 已取消充值。")
        return ConversationHandler.END

    if data == "charge_custom":
        await query.edit_message_text(
            "✏️ 请发送充值金额（元）：\n\n"
            "例如：`10`\n\n"
            "发送 /cancel 取消。",
            parse_mode="Markdown",
        )
        return CHARGE_CUSTOM_AMOUNT

    if not data.startswith("charge_amt:"):
        return CHARGE_AMOUNT

    cents = int(data.split(":", 1)[1])
    amount_yuan = cents / 100
    await _process_charge(query, context, uid, amount_yuan)
    return ConversationHandler.END


async def on_charge_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户发送自定义充值金额。"""
    uid = _uid(update)
    text = update.message.text.strip()

    try:
        amount_yuan = float(text)
    except ValueError:
        await update.message.reply_text("❌ 请输入有效的数字，例如 `10`：")
        return CHARGE_CUSTOM_AMOUNT

    if amount_yuan <= 0:
        await update.message.reply_text("❌ 充值金额必须大于 0：")
        return CHARGE_CUSTOM_AMOUNT

    await _process_charge(update.message, context, uid, amount_yuan)
    return ConversationHandler.END


async def _process_charge(
    update_or_query: Update | CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    uid: str,
    amount_yuan: float,
):
    """调用 payment 模块生成支付宝 WAP 支付链接，包装成支付宝 App 可直接点开的链接后发送给用户，并安排到账监控。"""
    url = _user_url(uid)
    fee_type = context.user_data.get("charge_fee_type")
    if not fee_type:
        if isinstance(update_or_query, CallbackQuery):
            await update_or_query.edit_message_text("❌ 会话已过期，请重新使用 /charge")
        return

    label, unit = _LABELS[fee_type]

    if isinstance(update_or_query, CallbackQuery):
        msg = await update_or_query.edit_message_text(
            f"⏳ 正在为 {label} 生成 ¥{amount_yuan:.2f} 的支付链接…"
        )
    else:
        msg = await update_or_query.reply_text(
            f"⏳ 正在为 {label} 生成 ¥{amount_yuan:.2f} 的支付链接…"
        )

    # ── 生成 17wanxiao 支付宝 WAP 链接 ────────────────────────────────────
    try:
        result = await create_alipay_url(url, fee_type, amount_yuan)
    except Exception as e:
        logger.error(f"为用户 {uid} 生成支付链接失败：{e}")
        await msg.edit_text(f"❌ 生成支付链接失败：{e}\n\n请稍后重试 /charge")
        context.user_data.pop("charge_fee_type", None)
        context.user_data.pop("charge_price", None)
        return

    # ── 先拿到 mclient.alipay.com 收银台链接，再包装成 App scheme 链接 ───────
    try:
        cashier_url = await convert_to_cashier_url(result["alipay_url"])
        pay_url = convert_to_alipay_scheme_url(cashier_url)
    except Exception as e:
        logger.warning(f"用户 {uid} 转换支付宝收银台/scheme 链接失败，使用原始链接：{e}")
        pay_url = result["alipay_url"]

    pre_balance = _user_caches.get(uid, {}).get("balances", {}).get(fee_type, 0)

    # 安排到账监控
    job_name = f"charge_{uid}_{result['orderno']}"
    context.application.job_queue.run_repeating(
        _monitor_charge,
        interval=_CHARGE_POLL_INTERVAL,
        first=_CHARGE_POLL_INTERVAL,
        name=job_name,
        data={
            "uid": uid,
            "url": url,
            "fee_type": fee_type,
            "pre_balance": pre_balance,
            "amount_yuan": amount_yuan,
            "orderno": result["orderno"],
            "attempts": 0,
        },
    )

    # ── 5. 把支付链接发给用户 ─────────────────────────────────────────────────
    await msg.edit_text(
        f"✅ *支付链接已生成*\n\n"
        f"{label}：¥{amount_yuan:.2f}\n"
        f"订单号：`{result['orderno']}`\n\n"
        f"点击下方按钮前往 *支付宝* 完成支付。支付完成后约 1–5 分钟到账。",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 去支付宝支付", url=pay_url)]
        ]),
    )

    context.user_data.pop("charge_fee_type", None)
    context.user_data.pop("charge_price", None)


async def _monitor_charge(context: ContextTypes.DEFAULT_TYPE):
    """轮询余额，检测充值是否到账。"""
    data = context.job.data
    data["attempts"] += 1
    uid = data["uid"]
    fee_type = data["fee_type"]
    pre_balance = data["pre_balance"]

    if data["attempts"] > _CHARGE_POLL_MAX_ATTEMPTS:
        await context.bot.send_message(
            chat_id=int(uid),
            text="⏰ 充值到账监控已超时。如已支付，通常几分钟后到账，请使用 /balance 查询。",
        )
        context.job.schedule_removal()
        return

    try:
        balances = await fetch_balances_async(data["url"])
    except Exception as e:
        logger.warning(f"充值监控查询余额失败（{uid}/{fee_type}）：{e}")
        return

    _update_user_cache(uid, balances)
    new_balance = balances.get(fee_type)
    if new_balance is None:
        return

    if new_balance > pre_balance + 0.001:
        label, unit = _LABELS[fee_type]
        await context.bot.send_message(
            chat_id=int(uid),
            text=(
                f"🎉 *充值到账！*\n\n"
                f"{label}：`{pre_balance:.2f}` → `{new_balance:.2f}` {unit}\n"
                f"增加：`{new_balance - pre_balance:.2f}` {unit}"
            ),
            parse_mode="Markdown",
        )
        context.job.schedule_removal()


# ── SSO 登录流程 ─────────────────────────────────────────────────────────────

async def on_sso_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSO 步骤 1：用户发送用户名 → 询问密码。"""
    context.user_data["sso_username"] = update.message.text.strip()
    await update.message.reply_text(
        "🔒 请发送 SSO 密码：\n\n"
        "（密码将在读取后立即从聊天记录中删除，不会被存储）",
    )
    return SSO_PASSWORD


async def on_sso_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSO 步骤 2：用户发送密码 → 执行 SSO 登录 → 获取链接。"""
    uid = _uid(update)
    chat_id = update.effective_chat.id
    password = update.message.text.strip()
    username = context.user_data.pop("sso_username", "")
    origin = context.user_data.pop("sso_origin", "setup")

    # 立即删除包含密码的消息
    try:
        await update.message.delete()
    except Exception:
        pass

    if not username:
        await context.bot.send_message(chat_id, "❌ 未知错误，请重新使用 /settings")
        return ConversationHandler.END

    msg = await context.bot.send_message(
        chat_id,
        "⏳ 正在通过 SSO 登录并获取链接，请稍候…\n"
        "（此过程可能需要 30-60 秒）",
    )

    try:
        url = await sso_fetch_fee_url(username, password)
    except Exception as e:
        await msg.edit_text(f"❌ SSO 登录失败：{e}")
        if origin == "setup":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 重试 SSO 登录", callback_data="setup_sso")],
                [InlineKeyboardButton("🔗 手动粘贴链接", callback_data="setup_paste_url")],
                [InlineKeyboardButton("🔍 SSO 仅获取链接（不保存）", callback_data="setup_sso_link_only")],
            ])
            await context.bot.send_message(
                chat_id, "请重新选择获取链接的方式：",
                reply_markup=keyboard,
            )
            return SETUP_URL
        elif origin == "link":
            return ConversationHandler.END
        else:
            _store.ensure(uid)
            await context.bot.send_message(
                chat_id, _settings_text(uid),
                parse_mode="Markdown",
                reply_markup=_settings_keyboard(),
            )
            return SETTINGS_MENU

    # 更新提示：链接已获取，正在验证
    await msg.edit_text("✅ 链接已获取，正在验证有效性…")

    # /link 模式：不保存，直接发送链接给用户
    if origin == "link":
        await msg.edit_text(
            f"✅ *链接获取成功！*\n\n`{url}`\n\n"
            "可以直接在浏览器中打开此链接查询水电费。",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # 保存链接并验证
    _store.ensure(uid)
    _store.update(uid, "url", url)

    try:
        balances = await _verify_url_and_cache(uid, url)
    except Exception:
        balances = {}

    if origin == "setup":
        lines = ["✅ SSO 登录成功！链接已自动配置。\n"]
        if balances:
            lines.append("当前余额：")
            for key, (label, unit) in _LABELS.items():
                if key in balances:
                    lines.append(f"  {label}：{balances[key]:.2f} {unit}")

        context.user_data["setup_threshold_idx"] = 0
        fee_type = _THRESHOLD_STEPS[0]
        label, unit = _LABELS[fee_type]
        cfg = _store.get(uid) or {}
        default = cfg.get("thresholds", {}).get(fee_type, 0)
        lines.append(
            f"\n📊 *第 2 步*：设置预警阈值\n\n"
            f"请发送 {label} 的预警阈值（{unit}）\n"
            f"当前值：{default}"
        )
        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_skip_keyboard("setup_skip_threshold"),
        )
        return SETUP_THRESHOLD
    else:
        cfg = _store.get(uid) or {}
        interval = cfg.get("check_interval", 300)
        _schedule_user_job(context.application, uid, interval)

        lines = ["✅ SSO 登录成功！链接已自动配置。\n"]
        if balances:
            lines.append("当前余额：")
            for key, (label, unit) in _LABELS.items():
                if key in balances:
                    lines.append(f"  {label}：{balances[key]:.2f} {unit}")
        lines.append(f"\n{_settings_text(uid)}")
        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_settings_keyboard(),
        )
        return SETTINGS_MENU


# ── /cancel 全局取消 ─────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("setting_key", None)
    context.user_data.pop("setup_threshold_idx", None)
    context.user_data.pop("sso_username", None)
    context.user_data.pop("sso_origin", None)
    context.user_data.pop("charge_fee_type", None)
    context.user_data.pop("charge_price", None)
    await update.message.reply_text("✅ 已取消当前操作。")
    return ConversationHandler.END


async def cmd_cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ 当前没有进行中的操作。")


async def on_unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("请点击上方按钮继续操作，或发送 /cancel 取消。")


# ── /link 获取链接 ─────────────────────────────────────────────────────────────

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """如果用户已有链接则直接发送，否则引导 SSO 登录获取。"""
    uid = _uid(update)
    url = _user_url(uid)

    if url:
        await update.message.reply_text(
            f"🔗 *你的查询链接：*\n\n`{url}`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data["sso_origin"] = "link"
    await update.message.reply_text(
        "🔑 *通过 SSO 登录获取链接*\n\n"
        "请发送你的 SSO 账号（学号/工号）：\n\n"
        "发送 /cancel 取消。",
        parse_mode="Markdown",
    )
    return SSO_USERNAME


# ── 定时刷新（按用户隔离） ───────────────────────────────────────────────────

async def _scheduled_user_refresh(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data
    url = _user_url(uid)
    if not url:
        return

    try:
        await _refresh_user_cache(uid)
    except Exception as e:
        logger.error(f"定时刷新用户 {uid} 失败：{e}")
        return

    cache = _user_caches.get(uid, {})
    balances = cache.get("balances", {})
    if not balances:
        return

    cfg = (_store.get(uid) if _store else None) or {}
    thresholds = cfg.get("thresholds", {})
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
            chat_id=int(uid), text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"发送预警给用户 {uid} 失败：{e}")


# ── 全局错误处理 ─────────────────────────────────────────────────────────

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")


# ── 构建应用 ─────────────────────────────────────────────────────────────────

def create_bot(config: dict) -> Application:
    global _bot_cfg, _store
    _bot_cfg = config
    _store = UserStore()

    tg = config["telegram"]
    token = tg["bot_token"]
    proxy = (
        tg.get("proxy", "").strip()
        or os.getenv("HTTPS_PROXY", "").strip()
        or os.getenv("HTTP_PROXY", "").strip()
        or None
    )
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

    api_base = tg.get("api_base", "").strip()
    if api_base:
        api_base = api_base.rstrip("/")
        builder = builder.base_url(f"{api_base}/bot")
        builder = builder.base_file_url(f"{api_base}/file/bot")

    app = builder.build()

    # 统一会话处理器：引导设置 + 设置菜单 + 充值
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("settings", settings_start),
            CommandHandler("link", cmd_link),
            CommandHandler("charge", cmd_charge),
        ],
        states={
            SETTINGS_MENU:   [CallbackQueryHandler(on_settings_menu)],
            AWAITING_INPUT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, on_awaiting_input)],
            SETUP_URL: [
                CallbackQueryHandler(on_setup_method, pattern=r"^setup_(sso|paste_url|sso_link_only)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_setup_url),
            ],
            SSO_USERNAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_sso_username)],
            SSO_PASSWORD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_sso_password)],
            SETUP_THRESHOLD: [
                CallbackQueryHandler(on_skip_threshold, pattern="^setup_skip_threshold$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_setup_threshold),
            ],
            SETUP_INTERVAL: [
                CallbackQueryHandler(on_skip_interval, pattern="^setup_skip_interval$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_setup_interval),
            ],
            CHARGE_FEE_TYPE: [CallbackQueryHandler(on_charge_type, pattern=r"^charge_type:")],
            CHARGE_AMOUNT:   [CallbackQueryHandler(on_charge_amount, pattern=r"^charge_")],
            CHARGE_CUSTOM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_charge_custom_amount)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.ALL, on_unexpected_message),
        ],
        conversation_timeout=300,
        per_chat=True,
        per_user=True,
    )

    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cmd_cancel_idle))
    app.add_error_handler(_error_handler)

    # 为已有用户安排定时刷新任务
    for uid, cfg in _store.all_configured_users().items():
        interval = cfg.get("check_interval", 300)
        if interval > 0:
            _schedule_user_job(app, uid, interval)

    return app
