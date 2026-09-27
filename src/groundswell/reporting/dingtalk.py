"""钉钉自定义机器人（加签模式）推送。钉钉 markdown 不支持表格，推送前转为列表。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse

import httpx
from loguru import logger

from groundswell.config import Settings, get_settings

MAX_CHARS = 3800
_SEPARATOR_ROW = re.compile(r"^\|\s*:?-{2,}")


def sign_url(webhook: str, secret: str | None, ts_ms: int | None = None) -> str:
    if not secret:
        return webhook
    ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
    digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={sign}"


def to_dingtalk_markdown(md: str) -> str:
    """表格 → 列表；折叠多余空行。"""
    out: list[str] = []
    in_table = False
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            out.append(line)
            continue
        if _SEPARATOR_ROW.match(s):
            continue
        cells = " · ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        out.append(f"- {cells}" if in_table else f"**{cells}**")
        in_table = True
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


def split_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """优先按二级/三级标题切分，单段过长再按行切。"""
    sections = re.split(r"(?m)^(?=#{2,3} )", text)
    chunks: list[str] = []
    buf = ""
    for sec in sections:
        if len(sec) > limit:
            for line in sec.splitlines(keepends=True):
                if len(buf) + len(line) > limit and buf:
                    chunks.append(buf)
                    buf = ""
                buf += line[:limit]
            continue
        if len(buf) + len(sec) > limit and buf:
            chunks.append(buf)
            buf = ""
        buf += sec
    if buf.strip():
        chunks.append(buf)
    return chunks


def send_markdown(
    title: str,
    text: str,
    *,
    webhook: str | None = None,
    secret: str | None = None,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> int:
    """推送并返回成功条数；未配置 webhook 时静默跳过。"""
    settings = settings or get_settings()
    webhook = webhook or settings.dingtalk_webhook
    secret = (
        secret if webhook != settings.dingtalk_webhook else (secret or settings.dingtalk_secret)
    )
    if not webhook:
        logger.info("未配置 GS_DINGTALK_WEBHOOK，跳过推送")
        return 0
    chunks = split_message(to_dingtalk_markdown(text))
    own = client is None
    client = client or httpx.Client(timeout=15)
    sent = 0
    try:
        for i, chunk in enumerate(chunks, 1):
            part_title = title if len(chunks) == 1 else f"{title} ({i}/{len(chunks)})"
            payload = {"msgtype": "markdown", "markdown": {"title": part_title, "text": chunk}}
            try:
                resp = client.post(sign_url(webhook, secret), json=payload)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(f"钉钉推送失败: {exc}")
                continue
            if data.get("errcode") == 0:
                sent += 1
            else:
                logger.warning(f"钉钉返回错误: {data}")
            if len(chunks) > 1:
                time.sleep(1.2)  # 机器人限流 20 条/分钟
    finally:
        if own:
            client.close()
    return sent
