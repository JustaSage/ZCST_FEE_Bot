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
payment.py
通过逆向 17wanxiao 支付网关，纯 API 生成支付宝 WAP 支付链接。
链路：goPay → 重定向 → getPayInfoByUuid → callWapCashDeskData → prepayOrder。
"""
import json
import random
import re
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx
from loguru import logger

from fetcher import _client, _extract_raw_params, _login_check, _service_api
from utils import sanitize_url, sanitize_for_log

# ── 支付网关常量 ──────────────────────────────────────────────────────────────
_PAY_BASE = "https://xqh5.17wanxiao.com/smartWaterAndElectricityService"
_CLOUD_GATEWAY = "https://cloudpaygateway.59wanmei.com:8087"
_CASH_DESK = "https://dk.zcst.edu.cn/WapCashDesk"
_RETURN_URL = "https://xqh5.17wanxiao.com/userwaterelecmini/index.html#/pages/index/index"

_MERCHANT_NO = "2020080539"
_CHANNEL_NO = "13"
_PAYWAY_ID = "9711"   # 支付宝 WAP
_ACCOUNT_ID = "1"

_TXCODE_MAP = {
    "electricity": "51101",
    "cold_water": "51201",
    "hot_water": "51301",
}


# ── 内部工具 ─────────────────────────────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def _gateway_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _gen_journo() -> str:
    return f"T002{_ts()}{random.randint(100, 999)}"


def _txcode(fee_type: str) -> str:
    code = _TXCODE_MAP.get(fee_type)
    if not code:
        raise ValueError(f"不支持的充值类型: {fee_type}")
    return code


def _find_mod(index_data: dict, txcode: str) -> dict:
    for item in index_data.get("modlist") or []:
        if str(item.get("accode")) == txcode or str(item.get("ecardacccode")) == txcode:
            return item
    raise RuntimeError(f"index 页中未找到充值类型对应的设备信息: {txcode}")


def _room_info(index_data: dict) -> tuple[str, str]:
    roomverify = index_data.get("roomverify") or ""
    roomfullname = index_data.get("roomfullname") or ""
    if not roomverify:
        raise RuntimeError("index 页未返回 roomverify")
    return roomverify, roomfullname


# ── 核心链路 ─────────────────────────────────────────────────────────────────

async def _gopay(
    client: httpx.AsyncClient,
    login_data: dict,
    index_data: dict,
    fee_type: str,
    amount_yuan: float,
) -> dict:
    """调用 17wanxiao goPay，返回解密后的业务响应。"""
    txcode = _txcode(fee_type)
    mod = _find_mod(index_data, txcode)
    roomverify, roomfullname = _room_info(index_data)

    token = login_data.get("token", "")
    outid = login_data.get("outid") or login_data.get("userid", "")
    name = login_data.get("name", "")
    schoolname = login_data.get("schoolname") or login_data.get("school_name", "")
    customercode = login_data.get("customerCode") or login_data.get("customerid", "")
    command = login_data.get("command", "")

    payamt_cents = int(round(amount_yuan * 100))
    if payamt_cents <= 0:
        raise ValueError("充值金额必须大于 0")

    custom_json = json.dumps(
        {"idserial": outid, "username": quote(name), "wxToken": token},
        separators=(",", ":"),
    )

    payload = {
        "cmd": "getPayInfo",
        "thirdsource": "T002",
        "token": token,
        "wxToken": token,
        "idserial": outid,
        "username": quote(name),
        "txcode": txcode,
        "acccode": txcode,
        "payamt": str(payamt_cents),
        "schoolcode": customercode,
        "customerName": quote(schoolname),
        "returnurl": _RETURN_URL,
        "payaccnum": roomverify,
        "payaccname": quote(roomfullname),
        "summary": "",
        "extendinfo": "",
        "paychannel": _CHANNEL_NO,
        "journo": _gen_journo(),
        "roomLimitAmt": None,
        "checkIdentity": "1",
        "flag": "cassso_zhkjxysdZ",
        "customizedPay": customercode,
        "customJson": custom_json,
    }

    body = {
        "param": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "customercode": customercode,
        "method": "getPayInfo",
        "command": command,
    }

    r = await client.post(f"{_PAY_BASE}/goPay", data=body)
    r.raise_for_status()
    wrapper = r.json()
    if not wrapper.get("body"):
        raise RuntimeError(f"goPay 返回异常: {wrapper}")

    gp = json.loads(wrapper["body"])
    if gp.get("result") != 0 and str(gp.get("result")) != "0":
        raise RuntimeError(f"goPay 业务失败: {gp}")
    return gp["data"]


async def _extract_uuid(client: httpx.AsyncClient, go_data: dict) -> str:
    """从重定向 Location 中提取 uuid。"""
    redirect_url = (
        f"{go_data['url']}?jsonData={quote(go_data['jsonData'])}"
        f"&sign={quote(go_data['sign'])}"
    )
    r = await client.get(redirect_url, follow_redirects=False)
    if r.status_code not in (301, 302, 303, 307, 308):
        raise RuntimeError(f"goPay 重定向状态异常: HTTP {r.status_code}")
    location = r.headers.get("location", "")
    m = re.search(r'"uuid":"(T002[^"]+)"', location)
    if not m:
        raise RuntimeError(f"未从重定向地址中提取到 uuid: {location[:200]}")
    return m.group(1)


async def _get_pay_info_by_uuid(client: httpx.AsyncClient, uuid: str) -> dict:
    r = await client.post(
        f"{_CLOUD_GATEWAY}/paygateway/smallpaygateway/trade",
        json={
            "timestamp": _gateway_ts(),
            "method": "getPayInfoByUuid",
            "bizcontent": json.dumps({"uuid": uuid}, separators=(",", ":")),
            "sourceId": 2,
        },
    )
    r.raise_for_status()
    wrapper = r.json()
    business_data = wrapper.get("businessData")
    if isinstance(business_data, str):
        business_data = json.loads(business_data)
    return business_data


async def _call_wap_cash_desk_data(
    client: httpx.AsyncClient,
    login_data: dict,
    info: dict,
    roomverify: str,
    roomfullname: str,
) -> dict:
    """调用 callWapCashDeskData，返回 orderInfo。"""
    token = login_data.get("token", "")
    outid = login_data.get("outid") or login_data.get("userid", "")
    name = login_data.get("name", "")
    customercode = login_data.get("customerCode") or login_data.get("customerid", "")

    custom_json = json.dumps(
        {"idserial": outid, "username": quote(name), "wxToken": token},
        separators=(",", ":"),
    )

    biz = {
        "idserial": outid,
        "thirdaccount": roomverify,
        "paychannel": _CHANNEL_NO,
        "payproid": info.get("discountsTextProjectId") or info.get("id"),
        "payamt": info.get("payamt"),
        "schoolcode": customercode,
        "returnurl": _RETURN_URL,
        "version": "99",
        "journo": info.get("journo"),
        "thirdsource": "T002",
        "notifyurl": "",
        "thirdaccounttype": "0",
        "openid": "",
        "thirdaccountname": roomfullname,
        "customJson": custom_json,
    }

    r = await client.post(
        f"{_CLOUD_GATEWAY}/paygateway/paytrans/gateway",
        json={
            "method": "callWapCashDeskData",
            "timestamp": _gateway_ts(),
            "bizcontent": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
            "sourceId": 2,
        },
    )
    r.raise_for_status()
    wrapper = r.json()
    business_data = wrapper.get("businessData")
    if isinstance(business_data, str):
        business_data = json.loads(business_data)
    return business_data["cashDeskData"]["orderInfo"]


async def _prepay_order(
    client: httpx.AsyncClient,
    order_info: dict,
    token: str,
) -> str:
    """调用 WapCashDesk prepayOrder，返回支付宝支付 URL。"""
    payload = {
        "merchantno": order_info.get("merchantno", _MERCHANT_NO),
        "journo": order_info["journo"],
        "channelno": order_info.get("channelno", _CHANNEL_NO),
        "paywayid": _PAYWAY_ID,
        "accountid": _ACCOUNT_ID,
        "wxToken": token,
        "senceno": json.dumps(
            {"h5_info": {"type": "Wap", "wap_url": "wanxiao://", "wap_name": "wap收银台"}},
            separators=(",", ":"),
        ),
        "device": "android",
        "ip": "",
    }

    r = await client.post(f"{_CASH_DESK}/prepayOrder", json=payload)
    r.raise_for_status()
    resp = r.json()
    if resp.get("returncode") != "SUCCESS":
        raise RuntimeError(f"prepayOrder 业务失败: {resp}")

    paymsg = resp.get("paymsg")
    if isinstance(paymsg, str):
        paymsg = json.loads(paymsg)
    return paymsg["request_url"]


# ── 对外接口 ──────────────────────────────────────────────────────────────────

async def create_alipay_url(
    url: str,
    fee_type: str,
    amount_yuan: float,
) -> dict:
    """
    生成支付宝 WAP 支付链接。

    Args:
        url: 17wanxiao 最终落地页链接。
        fee_type: electricity / cold_water / hot_water。
        amount_yuan: 充值金额（元）。

    Returns:
        {
            "alipay_url": "https://openapi.alipay.com/...",
            "orderno": "...",
            "journo": "...",
            "merchantno": "...",
            "amount_yuan": 50.0,
            "fee_type": "electricity",
            "goodsname": "...",
        }
    """
    params = _extract_raw_params(url)
    async with _client() as client:
        await client.get(url)
        login_data = await _login_check(client, params)
        customercode = login_data.get("customerCode") or login_data.get("customerid")
        command = login_data.get("command")
        account = login_data.get("outid") or login_data.get("userid")
        if not (customercode and command and account):
            raise RuntimeError("loginCheck 返回数据不完整，无法充值")

        index_data = await _service_api(
            client, customercode, command, account,
            {"cmd": "h5_getstuindexpage", "roomverify": ""},
        )

        roomverify, roomfullname = _room_info(index_data)

        go_data = await _gopay(client, login_data, index_data, fee_type, amount_yuan)
        uuid = await _extract_uuid(client, go_data)
        info = await _get_pay_info_by_uuid(client, uuid)
        order_info = await _call_wap_cash_desk_data(
            client, login_data, info, roomverify, roomfullname
        )
        alipay_url = await _prepay_order(client, order_info, login_data.get("token", ""))

        return {
            "alipay_url": alipay_url,
            "orderno": order_info.get("order_no"),
            "journo": order_info.get("journo"),
            "merchantno": order_info.get("merchantno", _MERCHANT_NO),
            "amount_yuan": round(amount_yuan, 2),
            "fee_type": fee_type,
            "goodsname": order_info.get("goodsname", ""),
        }


def convert_to_alipay_scheme_url(target_url: str) -> str:
    """
    把任意 HTTPS 支付链接包装成能在支付宝 App 内打开的链接。
    流程：target_url → alipays://platformapi/startapp?appId=20000067&url=... →
          https://ds.alipay.com/?scheme=...
    用户在 Telegram 里点击后会先打开浏览器，再唤起支付宝并加载收银台。
    """
    alipays = (
        f"alipays://platformapi/startapp?appId=20000067"
        f"&url={quote(target_url, safe='')}"
    )
    return f"https://ds.alipay.com/?scheme={quote(alipays, safe='')}"


# 唤起支付宝 App / 收银台时使用的移动端 UA
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


async def convert_to_cashier_url(alipay_url: str) -> str:
    """
    将 openapi.alipay.com/gateway.do?... WAP 链接通过 POST 提交，
    捕获 302 重定向，返回 mclient.alipay.com/cashier/mobilepay.htm 收银台链接。
    失败时回退到原始链接。
    """
    parsed = urlparse(alipay_url)
    if parsed.netloc != "openapi.alipay.com" or parsed.path != "/gateway.do":
        return alipay_url

    query = parse_qs(parsed.query)
    if not query:
        return alipay_url

    form = {k: v[0] for k, v in query.items()}
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _MOBILE_UA},
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=15.0),
        ) as client:
            r = await client.post("https://openapi.alipay.com/gateway.do", data=form)
            for hist in r.history:
                loc = hist.headers.get("location", "")
                if "mclient.alipay.com/cashier/mobilepay.htm" in loc:
                    return loc
            final = str(r.url)
            if "mclient.alipay.com" in final:
                return final
    except Exception as e:
        logger.warning(f"转换 cashier 链接失败：{sanitize_for_log(str(e))}")

    return alipay_url
