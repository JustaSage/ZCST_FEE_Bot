import os
import yaml
from loguru import logger

_DEFAULTS = {
    "check_interval": 3600,
    "thresholds": {
        "electricity": 5.0,
        "cold_water": 1.0,
        "hot_water": 0.5,
    },
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        "api_base": "",
    },
    "field_mapping": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在：{path}")

    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    url = user_cfg.get("url", "")
    if not url or "YOUR_PARAMS_HERE" in url:
        raise ValueError("请先在 config.yaml 中填写正确的 url 链接")

    cfg = _deep_merge(_DEFAULTS, user_cfg)

    tg = cfg.get("telegram", {})
    if not tg.get("bot_token") or not tg.get("chat_id"):
        logger.warning("Telegram bot_token 或 chat_id 未配置，余额不足时将只打印日志而不发送通知")

    return cfg
