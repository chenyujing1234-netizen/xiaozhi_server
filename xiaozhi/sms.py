"""阿里云短信验证码（移植自 huanDa）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import random
import string
import time
import urllib.parse

import aiohttp

from config import config

logger = logging.getLogger("auth.sms")

SMS_GATEWAY = "https://dysmsapi.aliyuncs.com/"


def generate_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def _generate_signature(params: dict, secret: str) -> str:
    sorted_keys = sorted(params.keys())
    canonical = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(params[k], safe='')}"
        for k in sorted_keys
    )
    string_to_sign = (
        f"GET&{urllib.parse.quote('/', safe='')}&"
        f"{urllib.parse.quote(canonical, safe='')}"
    )
    sig = hmac.new(
        f"{secret}&".encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(sig).decode("utf-8")


async def send_sms_code(phone: str, code: str) -> bool:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    params: dict[str, str] = {
        "AccessKeyId": config.ALIYUN_ACCESS_KEY_ID,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": config.SMS_SIGN_NAME,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": nonce,
        "SignatureVersion": "1.0",
        "TemplateCode": config.SMS_TEMPLATE_CODE,
        "TemplateParam": f'{{"code":"{code}"}}',
        "Timestamp": timestamp,
        "Version": "2017-05-25",
    }
    params["Signature"] = _generate_signature(params, config.ALIYUN_ACCESS_KEY_SECRET)
    query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in params.items()
    )
    url = f"{SMS_GATEWAY}?{query}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                result = await resp.json(content_type=None)
        if result.get("Code") == "OK":
            logger.info("[SMS] 验证码发送成功 phone=%s", phone)
            return True
        logger.warning(
            "[SMS] 发送失败 code=%s msg=%s phone=%s sms_code=%s",
            result.get("Code"), result.get("Message"), phone, code,
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("[SMS] 发送异常 phone=%s code=%s err=%s", phone, code, e)
        return False
