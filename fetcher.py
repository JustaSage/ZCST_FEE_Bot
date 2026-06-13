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
fetcher.py
使用 17wanxiao 智能水电公开 API 直接获取余额。
纯 API 实现，无头浏览器。
"""
import asyncio
import base64
import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from loguru import logger

# ── 17wanxiao API 常量 ───────────────────────────────────────────────────────
_API_BASE = "https://xqh5.17wanxiao.com/smartWaterAndElectricityService"
_AES_KEY = b"1234567812345678"
_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.44"
)

# 费用类型中文名及显示单位
_LABELS = {
    "electricity": ("电费", "kWh"),
    "cold_water": ("冷水费", "吨"),
    "hot_water": ("热水费", "吨"),
}

# businesstype / accgroup → 内部键名映射
_BUSINESSTYPE_MAP = {
    "0": "electricity",   # 电费
    "1": "cold_water",     # 冷水
    "2": "hot_water",      # 热水
}


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _client(verify: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=verify,
        timeout=httpx.Timeout(60.0, connect=30.0),
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://xqh5.17wanxiao.com/userwaterelecmini/index.html",
        },
        follow_redirects=True,
    )


def _aes_decrypt(data_b64: str) -> str:
    """AES-ECB / PKCS7 解密，用于 loginCheck 返回的 resultdata。"""
    cipher = Cipher(algorithms.AES(_AES_KEY), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    raw = decryptor.update(base64.b64decode(data_b64)) + decryptor.finalize()
    pad_len = raw[-1]
    return raw[:-pad_len].decode("utf-8")


def _extract_raw_params(url: str) -> str:
    """从 final URL 的 fragment 中提取未 URL 解码的 params。"""
    fragment = url.split("#")[1] if "#" in url else ""
    match = re.search(r"[?&]params=([^&]+)", fragment)
    if not match:
        raise ValueError("URL 中未找到 params 参数")
    return match.group(1)


async def _login_check(client: httpx.AsyncClient, params: str) -> dict:
    """调用 loginCheck，解密并返回 body.data。"""
    r = await client.post(f"{_API_BASE}/loginCheck", data={"data": params})
    if r.status_code != 200:
        raise RuntimeError(f"loginCheck 请求失败: HTTP {r.status_code}")
    resp = r.json()
    resultdata = resp.get("resultdata")
    if not resultdata:
        raise RuntimeError(f"loginCheck 未返回 resultdata: {resp}")
    decrypted = _aes_decrypt(resultdata)
    try:
        payload = json.loads(decrypted)
        body = json.loads(payload["body"])
    except Exception as e:
        raise RuntimeError(f"loginCheck 响应解析失败: {decrypted}") from e
    if body.get("result") != "0":
        raise RuntimeError(f"loginCheck 业务失败: {body}")
    return body["data"]


async def _service_api(
    client: httpx.AsyncClient,
    customercode: str,
    command: str,
    account: str,
    payload: dict,
) -> dict:
    """调用 SWAEServlet，自动处理 param/account/timestamp/响应解析。"""
    payload["account"] = account
    payload["timestamp"] = _timestamp()
    body = {
        "param": json.dumps(payload),
        "customercode": customercode,
        "method": payload["cmd"],
        "command": command,
    }
    r = await client.post(f"{_API_BASE}/SWAEServlet", data=body)
    if r.status_code != 200:
        raise RuntimeError(f"SWAEServlet [{payload['cmd']}] 请求失败: HTTP {r.status_code}")
    resp = r.json()
    if resp.get("result_") != "true" or not resp.get("body"):
        raise RuntimeError(f"SWAEServlet [{payload['cmd']}] 业务失败: {resp}")
    try:
        return json.loads(resp["body"])
    except Exception as e:
        raise RuntimeError(f"SWAEServlet [{payload['cmd']}] body 解析失败: {resp['body']}") from e


def _parse_balances(data: dict, field_mapping: dict | None = None) -> dict:
    """从 h5_getstuindexpage 响应中提取余额。"""
    balances: dict = {}

    # 新版接口返回 modlist
    modlist = data.get("modlist") or []
    for item in modlist:
        if not isinstance(item, dict):
            continue
        bt = str(item.get("bussnesstype", "")).strip()
        key = _BUSINESSTYPE_MAP.get(bt)
        if key and key not in balances:
            val = item.get("odd")
            try:
                balances[key] = float(val)
            except (TypeError, ValueError):
                pass

    # 旧版接口可能返回 detaillist
    detaillist = data.get("detaillist") or []
    for item in detaillist:
        if not isinstance(item, dict):
            continue
        bt = str(item.get("businesstype", "")).strip()
        key = _BUSINESSTYPE_MAP.get(bt)
        if key and key not in balances:
            val = item.get("odd")
            try:
                balances[key] = float(val)
            except (TypeError, ValueError):
                pass

    return balances


async def fetch_index_data_async(url: str, debug: bool = False) -> tuple[dict, dict]:
    """
    异步获取 loginCheck 与 h5_getstuindexpage 原始数据。

    Returns:
        (login_data, index_data)
    """
    params = _extract_raw_params(url)
    if debug:
        logger.debug(f"[fetcher] params: {params[:80]}...")

    async with _client() as client:
        await client.get(url)
        if debug:
            logger.debug(f"[fetcher] cookies: {dict(client.cookies)}")

        login_data = await _login_check(client, params)
        if debug:
            logger.debug(f"[fetcher] login_data: {login_data}")

        customercode = login_data.get("customerCode") or login_data.get("customerid")
        command = login_data.get("command")
        account = login_data.get("outid") or login_data.get("userid")
        if not (customercode and command and account):
            raise RuntimeError(f"loginCheck 返回数据不完整: {login_data}")

        index_data = await _service_api(
            client, customercode, command, account,
            {"cmd": "h5_getstuindexpage", "roomverify": ""},
        )
        if debug:
            logger.debug(f"[fetcher] h5_getstuindexpage: {json.dumps(index_data, ensure_ascii=False)[:500]}")

        return login_data, index_data


async def fetch_balances_async(
    url: str,
    field_mapping: dict | None = None,
    debug: bool = False,
) -> dict:
    """
    异步获取余额。

    Args:
        url: 17wanxiao 最终落地页链接（含 params）。
        field_mapping: 保留参数，当前未使用（自动识别已足够）。
        debug: 是否打印调试信息。

    Returns:
        {"electricity": ..., "cold_water": ..., "hot_water": ...}
    """
    _login, index_data = await fetch_index_data_async(url, debug=debug)
    balances = _parse_balances(index_data, field_mapping)
    if debug:
        logger.debug(f"[fetcher] balances: {balances}")
    if not balances:
        logger.warning("未能从 API 响应中识别到余额字段")
    return balances


def fetch_index_data(url: str, debug: bool = False) -> tuple[dict, dict]:
    """同步获取 index 页原始数据（CLI 测试用）。"""
    return asyncio.run(fetch_index_data_async(url, debug=debug))


def fetch_balances(
    url: str,
    field_mapping: dict | None = None,
    debug: bool = False,
) -> dict:
    """同步获取余额（CLI 测试用）。"""
    return asyncio.run(fetch_balances_async(url, field_mapping, debug))
