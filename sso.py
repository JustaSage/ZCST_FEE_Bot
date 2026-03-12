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
使用 Playwright 无头浏览器自动完成登录并跟随 CAS 跳转。
"""
import hashlib
import uuid
from urllib.parse import urlencode, urlparse

import httpx
from loguru import logger
from playwright.async_api import async_playwright

# ── 配置常量 ──────────────────────────────────────────────────────────────────
_BASE_URL = "https://my.zcst.edu.cn"
_SSO_HOSTS = {"sos.zcst.edu.cn"}
_APP_VERSION = "1.3.7"
_CLIENT_TYPE = "android"

_TARGET_APP_URL = (
    "https://sos.zcst.edu.cn/login"
    "?service=https%3A%2F%2Fhub.17wanxiao.com%2Fbsacs%2Flight.action"
    "%3Fflag%3Dcassso_zhkjxysdZ%26ecardFunc%3Dindex"
)

_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/100.0.0.0 Mobile Safari/537.36 iPortal/30"
)


# ── 内部函数 ──────────────────────────────────────────────────────────────────

async def _init_client_config() -> dict:
    """初始化客户端配置，获取 IDS 服务器地址。"""
    device_key = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()
    url = f"{_BASE_URL}/mobile/initClientConfig21_1.mo"
    data = {
        "deviceKey": device_key,
        "version": _APP_VERSION,
        "clientType": _CLIENT_TYPE,
        "isFirst": "1",
        "os": "12",
        "mobileType": "Pixel 6",
    }
    # 学校 SSL 证书可能不规范，此处仅连接校内已知服务
    async with httpx.AsyncClient(verify=False, headers={"User-Agent": _UA}) as client:
        resp = None
        for attempt in range(2):
            try:
                resp = await client.post(url, data=data, timeout=30)
                resp.raise_for_status()
                break
            except Exception:
                if attempt == 0:
                    continue
                raise RuntimeError("无法连接学校服务器")

        result = resp.json()

    if str(result.get("result")) != "1":
        raise RuntimeError(result.get("failReason", "服务端配置初始化失败"))

    config_data = result.get("data", {})
    config = config_data.get("config", config_data)
    return {
        "mi_ssl": config.get("mi_sll", config.get("mi_ssl", "")),
        "mi_host": config.get("mi_host", config.get("MI_Host", "")),
        "device_key": device_key,
    }


async def _find_visible(page, selectors: list):
    """按优先级查找第一个可见元素。"""
    for sel in selectors:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            return el
    return None


async def _detect_login_error(page) -> str | None:
    """检测页面上的登录错误提示文字。"""
    for sel in [
        ".error-msg", ".login-error", ".alert-danger",
        "[class*='error']", "[class*='tip']",
    ]:
        for el in await page.query_selector_all(sel):
            if await el.is_visible():
                txt = (await el.text_content() or "").strip()
                if txt and len(txt) < 100:
                    return txt
    return None


async def _login_and_get_url(page, login_url: str, username: str, password: str) -> str:
    """在已创建的 page 上完成 SSO 登录 + CAS 跳转，返回水电费直连链接。"""
    # appWebLogin.jsp 会多次重定向，先导航再用 locator 等待最终表单
    await page.goto(login_url, timeout=60_000)

    # 等待登录表单渲染：尝试多种密码输入框选择器
    _pwd_selectors = [
        "input[type='password']",
        "input[placeholder*='密码']",
        "input[name='password']",
        "input#password",
    ]
    pwd_el = None
    for _ in range(60):  # 最多等 30 秒（每次 500ms）
        for sel in _pwd_selectors:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                pwd_el = el
                break
        if pwd_el:
            break
        await page.wait_for_timeout(500)

    if not pwd_el:
        # 截图辅助调试
        try:
            await page.screenshot(path="_debug_sso_login.png")
            logger.error(f"SSO 登录页截图已保存为 _debug_sso_login.png，当前 URL: {page.url}")
        except Exception:
            pass
        raise RuntimeError(
            f"登录页面未找到密码输入框（当前 URL: {page.url}）\n"
            "可能原因：学校 SSO 页面结构已更新或链接无法访问"
        )

    await page.wait_for_timeout(500)

    # ── 切到「用户名密码」Tab（某些学校默认手机验证码 Tab） ──
    for selector in [
        "[class*='tab-item']", "[class*='login-tab']",
        "[class*='way-item']", "[class*='login-way']",
    ]:
        tabs = page.locator(selector)
        count = await tabs.count()
        for i in range(count):
            tab = tabs.nth(i)
            txt = await tab.text_content() or ""
            if any(kw in txt for kw in ("用户名", "密码", "账号")):
                await tab.click()
                await page.wait_for_timeout(500)
                break

    # ── 填写用户名 ──
    username_el = await _find_visible(page, [
        "input[placeholder*='账号']", "input[placeholder*='用户名']",
        "input[placeholder*='学号']", "input[placeholder*='工号']",
        "input[name='username']", "input#username",
        "input[type='text']:not([readonly]):not([disabled])",
    ])
    if not username_el:
        raise RuntimeError("未找到用户名输入框，登录页结构可能已变更")

    # ── 填写密码（已在上方找到 pwd_el） ──
    await username_el.fill(username)
    await page.wait_for_timeout(200)
    await pwd_el.fill(password)
    await page.wait_for_timeout(200)

    # ── 点击登录 ──
    submit_el = await _find_visible(page, [
        "button[type='submit']", "input[type='submit']",
        "button[class*='login']", "button[class*='submit']",
        "button[class*='btn-primary']", ".login-btn", ".submit-btn",
    ])
    if not submit_el:
        btns = page.locator("button")
        for i in range(await btns.count()):
            btn = btns.nth(i)
            txt = await btn.text_content() or ""
            if "登录" in txt and await btn.is_visible():
                submit_el = btn
                break
    if not submit_el:
        raise RuntimeError("未找到登录按钮，登录页结构可能已变更")

    await submit_el.click()
    logger.debug("SSO 登录表单已提交，等待认证…")

    # ── 等待登录成功（从 SSO 域跳回） ──
    for _ in range(60):
        await page.wait_for_timeout(1000)
        host = urlparse(page.url).netloc
        if host not in _SSO_HOSTS:
            await page.wait_for_timeout(2000)
            break
        error_text = await _detect_login_error(page)
        if error_text:
            raise RuntimeError(f"登录失败：{error_text}")
    else:
        raise RuntimeError("登录超时，请检查账号密码是否正确")

    # ── 跳转到智能水电页面 ──
    logger.debug("SSO 登录成功，正在跟随 CAS 跳转获取水电费链接…")
    await page.goto(_TARGET_APP_URL, wait_until="domcontentloaded", timeout=30_000)

    for _ in range(40):
        current = page.url
        if "params=" in current or "xqh5.17wanxiao.com" in current:
            await page.wait_for_timeout(1000)
            return page.url
        await page.wait_for_timeout(1000)

    if "17wanxiao.com" in page.url:
        return page.url

    raise RuntimeError("未能获取水电费链接，CAS 跳转异常")


# ── 公开接口 ──────────────────────────────────────────────────────────────────

async def sso_fetch_fee_url(username: str, password: str) -> str:
    """
    通过 SSO 统一认证登录并获取 17wanxiao 水电费查询直连链接。

    Returns:
        直连链接字符串
    Raises:
        RuntimeError: 登录失败或无法获取链接
    """
    config = await _init_client_config()
    ids_base = config.get("mi_ssl") or config.get("mi_host") or _BASE_URL

    cas_login_url = f"{ids_base}/_web/appWebLogin.jsp"
    params = {
        "serialNo": config["device_key"],
        "os": _CLIENT_TYPE,
        "deviceName": "Pixel 6",
        "name": "Pixel 6",
        "apnsKey": "",
        "miApnsKey": "",
        "_p": "YXM9MTAwMDAwMCZwPTEmbT1OJg__",
    }
    full_login_url = f"{cas_login_url}?{urlencode(params)}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 500, "height": 750},
            ignore_https_errors=True,
            has_touch=True,
        )
        page = await ctx.new_page()
        try:
            return await _login_and_get_url(page, full_login_url, username, password)
        finally:
            await browser.close()
