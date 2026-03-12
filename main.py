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


def _check_once(url: str, cfg: dict, debug: bool = False) -> None:
    """执行一次余额检查（CLI 测试用）。"""
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


_LICENSE_NOTICE = """\
本程序是自由软件，您可以按照 GNU Affero 通用公共许可证第 3 版或
（由您选择）更高版本的条款重新分发和/或修改它。
本程序不附带任何担保。详情请参阅 LICENSE 文件。
"""


def main() -> None:
    print(_LICENSE_NOTICE)
    parser = argparse.ArgumentParser(description="宿舍电费/水费余额监控机器人")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--url", help="17wanxiao 查询链接（配合 --once/--debug 使用）")
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
        url = args.url or cfg.get("url", "")
        if not url:
            logger.error(
                "CLI 模式需要指定查询链接。\n"
                "  用法：python main.py --once --url <你的17wanxiao链接>"
            )
            sys.exit(1)
        _check_once(url, cfg, debug=args.debug)
        return

    # 正常模式：启动 Telegram Bot
    from bot import create_bot

    app = create_bot(cfg)
    logger.info("Telegram Bot 已启动，等待用户指令…（Ctrl+C 停止）")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
