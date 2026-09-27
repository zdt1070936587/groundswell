import json
import urllib.parse

import httpx

from groundswell.reporting.dingtalk import (
    send_markdown,
    sign_url,
    split_message,
    to_dingtalk_markdown,
)


def test_sign_url_matches_dingtalk_algorithm():
    url = sign_url(
        "https://oapi.dingtalk.com/robot/send?access_token=x", "SEC123", ts_ms=1700000000000
    )
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["timestamp"] == ["1700000000000"]
    # 预先按官方算法计算的签名
    import base64
    import hashlib
    import hmac

    expected = base64.b64encode(
        hmac.new(b"SEC123", b"1700000000000\nSEC123", hashlib.sha256).digest()
    ).decode()
    assert q["sign"] == [expected]
    assert sign_url("https://x/y", None) == "https://x/y"


def test_tables_become_lists():
    md = "## 矩阵\n\n| 话题 | GC | NA |\n|---|---|---|\n| AI 眼镜 | **2.5** | · |\n\n结束"
    out = to_dingtalk_markdown(md)
    assert "|" not in out
    assert "**话题 · GC · NA**" in out
    assert "- AI 眼镜 · **2.5** · ·" in out


def test_split_message_by_heading():
    md = "# 标题\n" + "".join(f"## 区块{i}\n" + "内容\n" * 400 for i in range(4))
    chunks = split_message(md, limit=1500)
    assert len(chunks) > 1
    assert all(len(c) <= 1500 for c in chunks)
    assert "".join(chunks).count("## 区块") == 4


def test_send_markdown_posts_signed_chunks(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"errcode": 0})

    settings.dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=t"
    settings.dingtalk_secret = "S"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sent = send_markdown("日报", "## A\n内容", settings=settings, client=client)
    assert sent == 1
    url, body = calls[0]
    assert "sign=" in url and body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "日报"


def test_send_markdown_skips_without_webhook(settings):
    settings.dingtalk_webhook = None
    assert send_markdown("x", "y", settings=settings) == 0
