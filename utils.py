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
utils.py
通用工具函数：日志脱敏、文本处理等。
"""
import re
from urllib.parse import urlparse, urlunparse


def mask_token(text: str | None, prefix_len: int = 6, suffix_len: int = 4) -> str:
    """对敏感 token 进行掩码，只保留前后若干字符。"""
    if not text:
        return ""
    if len(text) <= prefix_len + suffix_len + 3:
        return "***"
    return f"{text[:prefix_len]}***{text[-suffix_len:]}"


def _sanitize_query(query: str) -> str:
    """对查询字符串中的敏感参数进行脱敏。"""
    if not query:
        return query

    sensitive_keys = {
        "params", "ticket", "sign", "token", "st", "auth_code",
        "access_token", "refresh_token", "code", "password",
    }
    query_pairs = []
    for part in query.split("&"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key.lower() in sensitive_keys:
                value = mask_token(value, prefix_len=4, suffix_len=4)
            query_pairs.append(f"{key}={value}")
        else:
            query_pairs.append(part)
    return "&".join(query_pairs)


def sanitize_url(url: str | None) -> str:
    """
    对 URL 中的敏感参数（如 params、ticket、sign、token）进行脱敏，
    避免在日志中泄露会话凭证。
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)

        # 处理标准查询参数
        new_query = _sanitize_query(parsed.query)

        # 处理 fragment 中的查询参数（如 17wanxiao 链接 #/?params=...）
        new_fragment = parsed.fragment
        if new_fragment and "?" in new_fragment:
            frag_path, _, frag_query = new_fragment.partition("?")
            new_fragment = f"{frag_path}?{_sanitize_query(frag_query)}"

        return urlunparse(parsed._replace(query=new_query, fragment=new_fragment))
    except Exception:
        # 解析失败时返回截断后的原始 URL，避免泄露完整内容
        return url[:80] + "..." if len(url) > 80 else url


def sanitize_for_log(text: str | None) -> str:
    """
    对任意文本中的 URL 进行脱敏，用于记录异常或调试信息。
    """
    if not text:
        return ""

    def _replace_url(match: re.Match) -> str:
        return sanitize_url(match.group(0))

    # 匹配 http/https 链接
    return re.sub(r"https?://[^\s\)\]<>\"]+", _replace_url, text)
