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
store.py
用户数据持久化 —— 基于 JSON 文件的多用户配置存储。
"""
import json
from pathlib import Path

from loguru import logger

_DEFAULT_THRESHOLDS = {
    "electricity": 5.0,
    "cold_water": 1.0,
    "hot_water": 0.5,
}
_DEFAULT_CHECK_INTERVAL = 300


class UserStore:
    """JSON 文件存储的多用户数据管理器，每次写入使用原子重命名。"""

    def __init__(self, path: str = "users.json"):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"加载用户数据失败：{e}")

    def _save(self):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8"
        )
        tmp.replace(self._path)

    # ── 读取 ──────────────────────────────────────────────────────────────

    def get(self, user_id: str) -> dict | None:
        """获取用户配置，不存在返回 None。"""
        return self._data.get(user_id)

    def all_configured_users(self) -> dict[str, dict]:
        """返回所有已配置 URL 的用户 {uid: cfg}。"""
        return {
            uid: cfg
            for uid, cfg in self._data.items()
            if cfg.get("url")
        }

    # ── 写入 ──────────────────────────────────────────────────────────────

    def ensure(self, user_id: str) -> dict:
        """确保用户记录存在，不存在则创建默认配置。"""
        if user_id not in self._data:
            self._data[user_id] = {
                "url": "",
                "thresholds": dict(_DEFAULT_THRESHOLDS),
                "check_interval": _DEFAULT_CHECK_INTERVAL,
            }
            self._save()
        return self._data[user_id]

    def update(self, user_id: str, key: str, value):
        """更新用户的某个顶层配置项并持久化。"""
        self.ensure(user_id)
        self._data[user_id][key] = value
        self._save()

    def update_threshold(self, user_id: str, fee_type: str, value: float):
        """更新用户的某个预警阈值。"""
        cfg = self.ensure(user_id)
        cfg.setdefault("thresholds", {})[fee_type] = value
        self._save()

    def delete(self, user_id: str) -> bool:
        """删除用户所有数据，返回是否存在并已删除。"""
        if user_id in self._data:
            del self._data[user_id]
            self._save()
            return True
        return False
