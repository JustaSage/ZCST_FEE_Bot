"""
main.py  —  宿舍费用机器人入口

用法：
  python main.py              # 启动 Telegram Bot（交互式充值 + 定时预警）
  python main.py --once       # 只查询一次余额后退出（测试用）
  python main.py --debug      # 调试模式：打印全部拦截到的 API 数据
"""
import argparse
import sys

from loguru import logger

from config import load_config
from fetcher import fetch_balances

# ── 日志配置 ─────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="INFO", colorize=True,
           format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add("bot.log", level="DEBUG", rotation="5 MB", retention="7 days",
           encoding="utf-8", enqueue=True)

# 费用类型中文名及显示单位
_LABELS = {
    "electricity": ("电费",  "kWh"),
    "cold_water":  ("冷水费", "吨"),
    "hot_water":   ("热水费", "吨"),
}


def _check_once(cfg: dict, debug: bool = False) -> None:
    """执行一次余额检查（CLI 测试用）。"""
    url = cfg["url"]
    field_mapping: dict = cfg.get("field_mapping") or {}

    logger.info("开始检查余额…")
    try:
        balances = fetch_balances(url, field_mapping=field_mapping, debug=debug)
    except Exception as e:
        logger.error(f"获取余额时发生异常：{e}")
        return

    if not balances:
        logger.warning("本次未获取到任何余额数据，请检查链接或开启 --debug 排查")
        return

    thresholds: dict = cfg.get("thresholds", {})
    for key, (label, unit) in _LABELS.items():
        if key in balances:
            val = balances[key]
            threshold = thresholds.get(key, 0)
            warn = " ⚠️ 低于预警值！" if val < threshold else ""
            logger.info(f"  {label}：{val:.2f} {unit}{warn}")


def main() -> None:
    parser = argparse.ArgumentParser(description="宿舍电费/水费余额监控机器人")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只查询一次余额后退出")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    if args.debug:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG", colorize=True,
                   format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        sys.exit(1)

    # --once / --debug：仅查询余额并退出
    if args.once or args.debug:
        _check_once(cfg, debug=args.debug)
        return

    # 正常模式：启动 Telegram Bot
    token = cfg.get("telegram", {}).get("bot_token", "").strip()
    if not token:
        logger.error(
            "请先在 config.yaml 中配置 telegram.bot_token。\n"
            "  1. 在 Telegram 搜索 @BotFather 创建 Bot\n"
            "  2. 将获取到的 token 填入 config.yaml"
        )
        sys.exit(1)

    from bot import create_bot

    app = create_bot(cfg)
    logger.info("Telegram Bot 已启动，等待用户指令…（Ctrl+C 停止）")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
