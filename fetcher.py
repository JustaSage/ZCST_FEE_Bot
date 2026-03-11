"""
fetcher.py
使用 Playwright 加载 17wanxiao H5 页面，拦截 XHR/Fetch 请求，
从 JSON 响应中自动识别电费、冷水费、热水费余额。
"""
import asyncio
import json
import re
from typing import Optional
from loguru import logger
from playwright.async_api import async_playwright, Response

# ── 自动识别：直接字段名匹配 ──────────────────────────────────────────────────
_ELEC_KEYS = {
    "elecBalance", "electricBalance", "electricityBalance", "elec_balance",
    "elecAmt", "electricAmt", "dianfeibalance", "dianfei",
}
_COLD_KEYS = {
    "coldWaterBalance", "coldBalance", "cold_water_balance", "coldwater_balance",
    "coldWaterAmt", "lengshui",
}
_HOT_KEYS = {
    "hotWaterBalance", "hotBalance", "hot_water_balance", "hotwater_balance",
    "hotWaterAmt", "reshui",
}

# ── 自动识别：条目名称匹配（列表结构） ────────────────────────────────────────
_ELEC_NAMES = {"电费", "电", "用电", "电量费"}
_COLD_NAMES = {"冷水", "冷水费", "生活冷水", "自来水"}
_HOT_NAMES = {"热水", "热水费", "生活热水", "洗浴热水"}

# 条目余额字段候选名
_BALANCE_KEYS = ("balance", "amount", "money", "fee", "value", "amt", "余额", "金额", "surplusmoney", "odd")

# 17wanxiao detaillist 中 businesstype 到内部键名的映射
_BUSINESSTYPE_MAP = {
    "0": "electricity",   # 电费
    "1": "cold_water",     # 冷水
    "2": "hot_water",      # 热水
}


# ── 辅助解析函数 ─────────────────────────────────────────────────────────────

def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _try_parse_body(data) -> None:
    """递归将响应中值为 JSON 字符串的 body 字段就地解析为字典/列表。"""
    if isinstance(data, dict):
        for k, v in list(data.items()):
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, (dict, list)):
                        data[k] = parsed
                        _try_parse_body(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                _try_parse_body(v)
    elif isinstance(data, list):
        for item in data:
            _try_parse_body(item)


def _parse_detaillist(data) -> dict:
    """识别 17wanxiao 的 detaillist 结构：按 businesstype 区分类型，odd 为余额。"""
    balances: dict = {}

    def _search(obj):
        if isinstance(obj, dict):
            dl = obj.get("detaillist")
            if isinstance(dl, list):
                for item in dl:
                    if not isinstance(item, dict):
                        continue
                    bt = str(item.get("businesstype", "")).strip()
                    key = _BUSINESSTYPE_MAP.get(bt)
                    if key and key not in balances:
                        val = _to_float(item.get("odd"))
                        if val is not None:
                            balances[key] = val
            for v in obj.values():
                _search(v)
        elif isinstance(obj, list):
            for item in obj:
                _search(item)

    _search(data)
    return balances


def _find_by_key(data, key_set: set) -> Optional[float]:
    """递归按字段名查找余额数值。"""
    if isinstance(data, dict):
        for k, v in data.items():
            if k in key_set or k.lower() in {s.lower() for s in key_set}:
                result = _to_float(v)
                if result is not None:
                    return result
            result = _find_by_key(v, key_set)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_by_key(item, key_set)
            if result is not None:
                return result
    return None


def _find_by_name(data, name_set: set) -> Optional[float]:
    """在列表条目中按名称字段匹配，再取余额字段。"""
    name_fields = ("name", "itemName", "typeName", "type", "title", "category", "cateName")
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            for nf in name_fields:
                raw = str(item.get(nf, "")).strip()
                if raw and (raw in name_set or any(n in raw for n in name_set)):
                    for bk in _BALANCE_KEYS:
                        result = _to_float(item.get(bk))
                        if result is not None:
                            return result
    elif isinstance(data, dict):
        for v in data.values():
            result = _find_by_name(v, name_set)
            if result is not None:
                return result
    return None


def _parse_with_mapping(data, mapping: dict) -> Optional[float]:
    """使用 config.yaml 中用户自定义的 field_mapping 解析。"""
    key = mapping.get("key")
    if key:
        return _find_by_key(data, {key})
    name = mapping.get("name")
    balance_key = mapping.get("balance_key", "balance")
    if name:
        return _find_by_name(data, {name}) or _find_by_key(data, {balance_key})
    return None


def _parse_response(data, field_mapping: dict) -> dict:
    """从单个 API 响应中提取余额，field_mapping 为空时全自动识别。"""
    # 先将 body 等 JSON 字符串就地解析
    _try_parse_body(data)

    balances: dict = {}

    # ── 优先：尝试 detaillist / businesstype / odd 结构 ──
    dl_balances = _parse_detaillist(data)
    balances.update(dl_balances)
    if len(balances) >= 3:
        return balances

    # ── 次选：用户自定义 field_mapping ──
    fm = field_mapping or {}
    for internal_key, fm_key in [("electricity", "electricity"), ("cold_water", "cold_water"), ("hot_water", "hot_water")]:
        if internal_key in balances:
            continue
        mapping = fm.get(fm_key)
        if mapping:
            val = _parse_with_mapping(data, mapping)
            if val is not None:
                balances[internal_key] = val
    if len(balances) >= 3:
        return balances

    # ── 回退：通用字段名 / 名称匹配 ──
    if "electricity" not in balances:
        elec = _find_by_key(data, _ELEC_KEYS) or _find_by_name(data, _ELEC_NAMES)
        if elec is not None:
            balances["electricity"] = elec
    if "cold_water" not in balances:
        cold = _find_by_key(data, _COLD_KEYS) or _find_by_name(data, _COLD_NAMES)
        if cold is not None:
            balances["cold_water"] = cold
    if "hot_water" not in balances:
        hot = _find_by_key(data, _HOT_KEYS) or _find_by_name(data, _HOT_NAMES)
        if hot is not None:
            balances["hot_water"] = hot

    return balances


# ── 常量 ─────────────────────────────────────────────────────────────────────

# businesstype 索引 → 充值按钮在主页中的顺序
_BT_TO_BTN_INDEX = {"0": 0, "1": 1, "2": 2}

_JS_HOOK_SHOW_MODAL = """() => {
    if (window.uni && window.uni.showModal) {
        window.uni.showModal = function(opts) {
            if (opts.success) {
                setTimeout(() => opts.success({confirm: true, cancel: false}), 100);
            }
        };
    }
}"""

# 支付链接拦截模式：捕获 URL 但不访问，保留给用户使用
_PAY_URL_INTERCEPT_RE = re.compile(
    r"mclient\.alipay\.com/h5pay"
    r"|wx\.tenpay\.com"
    r"|pay\.weixin\.qq\.com",
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.44"
)


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _extract_balances(captured: list[dict], field_mapping: dict | None = None) -> dict:
    """从截获的 API 响应数据列表中提取余额。"""
    for item in captured:
        _try_parse_body(item)
    balances: dict = {}
    for item in captured:
        parsed = _parse_response(item, field_mapping or {})
        balances.update(parsed)
        if len(balances) >= 3:
            break
    return balances


# ── RechargeSession（交互式充值会话） ────────────────────────────────────────

class RechargeSession:
    """管理一个浏览器会话，支持分步骤交互式充值流程。

    使用方式：
        session = RechargeSession()
        balances = await session.start(url)          # 1. 加载主页，获取余额
        amounts  = await session.get_amounts("cold_water")  # 2. 获取充值档位
        methods  = await session.confirm_and_get_pay_methods(0)  # 3. 确认并获取支付方式
        pay_url  = await session.select_payment(0)   # 4. 选择支付方式，获取支付链接
        await session.close()
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ── 生命周期 ──

    async def start(self, url: str) -> dict:
        """启动浏览器，加载主页，返回余额字典。"""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=_UA,
            viewport={"width": 390, "height": 844},
            has_touch=True,
        )
        self._page = await self._context.new_page()

        captured: list[dict] = []

        async def on_resp(resp: Response):
            if resp.status != 200:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                data = await resp.json()
                captured.append(data)
            except Exception:
                pass

        self._page.on("response", on_resp)

        try:
            await self._page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"页面加载提前结束：{e}")

        await asyncio.sleep(3)
        self._page.remove_listener("response", on_resp)

        # Hook uni.showModal 以自动确认弹窗
        await self._page.evaluate(_JS_HOOK_SHOW_MODAL)

        return _extract_balances(captured)

    async def close(self):
        """关闭浏览器，释放资源。"""
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    # ── 步骤 ──

    async def get_amounts(self, fee_type: str) -> list[dict]:
        """点击对应充值按钮，返回可选的充值档位列表。

        返回: [{"index": 0, "text": "10度 （￥6.46）"}, ...]
        """
        type_to_bt = {v: k for k, v in _BUSINESSTYPE_MAP.items()}
        bt = type_to_bt.get(fee_type)
        if bt is None or bt not in _BT_TO_BTN_INDEX:
            raise ValueError(f"未知的费用类型：{fee_type}")

        btn_index = _BT_TO_BTN_INDEX[bt]
        page = self._page

        recharge_btns = page.locator(".recharge-btn")
        btn_count = await recharge_btns.count()
        if btn_index >= btn_count:
            raise RuntimeError(f"充值按钮数量 {btn_count}，索引 {btn_index} 越界")

        await recharge_btns.nth(btn_index).tap()
        await asyncio.sleep(3)

        try:
            await page.locator(".money-item").first.wait_for(
                state="visible", timeout=8000
            )
        except Exception:
            raise RuntimeError("充值页面未正常加载（未找到金额选项）")

        items: list[dict] = []
        count = await page.locator(".money-item").count()
        for i in range(count):
            text = (await page.locator(".money-item").nth(i).inner_text()).strip()
            text = text.replace("\n", " ")
            items.append({"index": i, "text": text})
        return items

    async def confirm_and_get_pay_methods(self, amount_index: int) -> list[dict]:
        """选择档位、确认购买、一路跳转到支付方式选择页面。

        返回: [{"index": 0, "name": "支付宝支付"}, {"index": 1, "name": "微信支付"}]
        """
        page = self._page

        # 选择档位
        await page.locator(".money-item").nth(amount_index).tap()
        await asyncio.sleep(0.5)

        # 重新注入弹窗自动确认 Hook（SPA 页内导航后可能丢失）
        await page.evaluate(_JS_HOOK_SHOW_MODAL)

        # 点击确认
        await page.locator(".submit-btn").tap()

        # 等待 cloudpaygateway 页面（使用 Playwright 内置的 URL 等待，避免竞态）
        try:
            await page.wait_for_url("**/cloudpaygateway**", timeout=30000)
        except Exception:
            logger.warning(f"等待支付网关时当前 URL: {page.url}")
            raise RuntimeError("等待支付网关跳转超时")

        # 等待 cloudpaygateway "立即支付" 按钮渲染
        pay_btn = page.locator("a.pay-botton")
        try:
            await pay_btn.wait_for(state="visible", timeout=25000)
        except Exception:
            raise RuntimeError("支付网关页面未正常加载")

        # 点击"立即支付"，等待 payways 支付方式选择页
        await pay_btn.tap()

        try:
            await page.wait_for_url("**payways**", timeout=30000)
        except Exception:
            logger.warning(f"等待支付方式页面时当前 URL: {page.url}")
            raise RuntimeError("等待支付方式页面跳转超时")

        # 等待支付方式列表渲染
        links = page.locator("a.item-link.item-content")
        try:
            await links.first.wait_for(state="visible", timeout=15000)
        except Exception:
            raise RuntimeError("支付方式页面未正常加载")

        methods: list[dict] = []
        count = await links.count()
        for i in range(count):
            text = (await links.nth(i).inner_text()).strip().replace("\n", " ")
            methods.append({"index": i, "name": text})
        return methods

    async def select_payment(self, method_index: int) -> str:
        """点击支付方式，拦截支付链接并返回。

        拦截到 alipay / wx.tenpay 的请求时立即 abort，
        只捕获 URL 不让无头浏览器访问，确保支付链接未被消费。
        """
        page = self._page
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[str] = loop.create_future()

        async def _intercept_pay_url(route):
            url = route.request.url
            logger.debug(f"[支付] 拦截到: {url[:120]}")
            if not result_future.done():
                result_future.set_result(url)
            try:
                await route.abort()
            except Exception:
                pass

        await page.route(_PAY_URL_INTERCEPT_RE, _intercept_pay_url)

        links = page.locator("a.item-link.item-content")
        await links.nth(method_index).tap()

        try:
            url = await asyncio.wait_for(result_future, timeout=45)
        except asyncio.TimeoutError:
            logger.warning(f"等待支付链接时当前 URL: {page.url}")
            raise RuntimeError("等待支付链接超时")
        finally:
            try:
                await page.unroute(_PAY_URL_INTERCEPT_RE, _intercept_pay_url)
            except Exception:
                pass

        return url


# ── 余额查询（快速模式，不保留浏览器） ──────────────────────────────────────

async def fetch_balances_async(
    url: str,
    field_mapping: dict | None = None,
    debug: bool = False,
) -> dict:
    """异步获取余额，适合在已有事件循环中调用。"""
    captured: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()

        async def on_response(resp: Response):
            if resp.status != 200:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                data = await resp.json()
                captured.append(data)
                if debug:
                    logger.debug(f"[API] {resp.url}")
            except Exception:
                pass

        page.on("response", on_response)

        logger.info("正在加载页面，请稍候…")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"页面加载提前结束：{e}")

        await asyncio.sleep(3)

        if debug:
            logger.info(f"共拦截到 {len(captured)} 个 JSON 接口响应")
            for item in captured:
                _try_parse_body(item)
                preview = json.dumps(item, ensure_ascii=False)[:600]
                logger.info(f"  数据: {preview}\n")

        await browser.close()

    balances = _extract_balances(captured, field_mapping)

    if not balances:
        if captured:
            logger.warning(
                "未能自动识别余额字段。请使用 --debug 查看原始 API 数据，"
                "然后在 config.yaml 的 field_mapping 中手动指定字段名。"
            )
        else:
            logger.error(
                "未拦截到任何 JSON 接口响应，可能原因：\n"
                "  1. URL 中的 params 已过期，请重新从微信获取链接\n"
                "  2. 网络无法访问 17wanxiao 服务器\n"
                "  3. 页面结构已更新，请提交 issue"
            )

    return balances


def fetch_balances(
    url: str,
    field_mapping: dict | None = None,
    debug: bool = False,
) -> dict:
    """同步获取余额（CLI 测试用）。"""
    return asyncio.run(fetch_balances_async(url, field_mapping, debug))
