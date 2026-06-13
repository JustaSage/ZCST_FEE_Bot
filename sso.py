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
sso.py
通过 ZCST SSO 统一认证获取 17wanxiao 水电费查询直连链接。
纯 API 实现，无头浏览器。
"""
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from loguru import logger

# 学校 SSO / 17wanxiao 相关常量
_CAS_REST_URL = "https://sos.zcst.edu.cn/v1/tickets"
_SERVICE_URL = (
    "https://hub.17wanxiao.com/bsacs/light.action"
    "?flag=cassso_zhkjxysdZ&ecardFunc=index"
)
_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/100.0.0.0 Mobile Safari/537.36 iPortal/30"
)


def _client() -> httpx.AsyncClient:
    """构造复用的异步 HTTP 客户端。"""
    return httpx.AsyncClient(
        verify=False,
        timeout=httpx.Timeout(60.0, connect=30.0),
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    )


async def _get_tgt(client: httpx.AsyncClient, username: str, password: str) -> str:
    r = await client.post(
        _CAS_REST_URL,
        data={"username": username, "password": password},
    )
    if r.status_code != 201:
        raise RuntimeError(f"CAS TGT 获取失败: HTTP {r.status_code} {r.text[:200]}")
    tgt = r.headers.get("location", "").replace("http://", "https://")
    if not tgt:
        raise RuntimeError("CAS 未返回 TGT URL")
    return tgt


async def _get_st(client: httpx.AsyncClient, tgt_url: str, service_url: str) -> str:
    r = await client.post(tgt_url, data={"service": service_url})
    if r.status_code != 200:
        raise RuntimeError(f"Service Ticket 获取失败: HTTP {r.status_code} {r.text[:200]}")
    st = r.text.strip()
    if not st.startswith("ST-"):
        raise RuntimeError(f"Service Ticket 格式异常: {st}")
    return st


async def _get_hub_data(client: httpx.AsyncClient, service_url: str, st: str):
    r = await client.get(f"{service_url}&ticket={st}")
    if r.status_code != 200:
        raise RuntimeError(f"hub 页面访问失败: HTTP {r.status_code}")
    import re
    m = re.search(r"data[:\s]*['\"]([^'\"]+)['\"]", r.text)
    if not m:
        raise RuntimeError("未在 hub 页面中提取到 data 参数")
    return r.url, m.group(1)


async def _get_auth_url(client: httpx.AsyncClient, hub_url: str, data_encoded: str) -> str:
    r = await client.post(
        urljoin(str(hub_url), "/bsacs/redirect.action"),
        data=data_encoded,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://hub.17wanxiao.com",
            "Referer": str(hub_url),
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"redirect.action 请求失败: HTTP {r.status_code} {r.text[:200]}")
    result = r.json()
    if result.get("error") or not result.get("result_"):
        raise RuntimeError(f"redirect.action 业务失败: {result}")
    auth_url = result.get("url")
    if not auth_url:
        raise RuntimeError("redirect.action 未返回 url")
    return auth_url


async def _get_final_url(client: httpx.AsyncClient, auth_url: str) -> str:
    r = await client.get(auth_url)
    final_url = str(r.url)
    if "xqh5.17wanxiao.com" in final_url and "params=" in final_url:
        return final_url
    raise RuntimeError(f"未到达最终落地页: {final_url}")


async def sso_fetch_fee_url(username: str, password: str) -> str:
    """
    通过 SSO 统一认证登录并获取 17wanxiao 水电费查询直连链接。

    Returns:
        形如 https://xqh5.17wanxiao.com/userwaterelecmini/index.html#/?params=... 的链接
    Raises:
        RuntimeError: 登录失败或无法获取链接
    """
    async with _client() as client:
        try:
            tgt_url = await _get_tgt(client, username, password)
            st = await _get_st(client, tgt_url, _SERVICE_URL)
            hub_url, data = await _get_hub_data(client, _SERVICE_URL, st)
            auth_url = await _get_auth_url(client, hub_url, data)
            return await _get_final_url(client, auth_url)
        except httpx.HTTPError as e:
            logger.error(f"SSO 请求异常：{e}")
            raise RuntimeError(f"网络请求异常：{e}") from e
