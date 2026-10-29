"""告警：运行出错或对账不一致时推送到手机。

设置环境变量 ALERT_NTFY_URL（例如 https://ntfy.sh/<你自己起的随机主题名>），
在手机上装 ntfy App 订阅同一个主题即可。没设置就只写日志。
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def send(title: str, body: str) -> None:
    log.warning("%s：%s", title, body)
    url = os.environ.get("ALERT_NTFY_URL")
    if not url:
        return
    try:
        httpx.post(url, content=f"{title}\n{body}".encode(), timeout=10)
    except httpx.HTTPError as e:  # 告警失败不能影响主流程
        log.error("告警发送失败：%s", e)
