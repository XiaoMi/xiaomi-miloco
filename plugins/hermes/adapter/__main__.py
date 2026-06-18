"""入站适配进程入口。

从环境变量读取配置，启动 aiohttp server 监听 miloco 的 ``POST /miloco/webhook``。

环境变量：
- ``ADAPTER_HOST``: 监听地址，默认 ``0.0.0.0``（容器/远端部署友好；本机调试可设 127.0.0.1）
- ``ADAPTER_PORT``: 监听端口，默认 ``18789``（对齐 OpenClaw 默认，使 miloco 默认
  ``agent.webhook_url`` 不用改即可指向本适配器）
- ``HERMES_API_URL``: Hermes api_server 根 URL，如 ``http://127.0.0.1:8642``
- ``HERMES_API_KEY``: Hermes api_server 的 API_SERVER_KEY，用于 Bearer 鉴权
- ``ADAPTER_AUTH_BEARER``: miloco 调本适配器时用的 Bearer token（对应 miloco
  ``AgentSettings.auth_bearer``）；空则不校验

用法：``python -m plugins.hermes.adapter``（需在 xiaomi-miloco 仓库根目录，使
``plugins.hermes.adapter`` 可作为包导入）。
"""

from __future__ import annotations

import logging
import os
import sys

from aiohttp import web

from .hermes_client import HermesClient
from .server import create_app

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 18789


def _build() -> web.Application:
    auth_bearer = os.getenv("ADAPTER_AUTH_BEARER", "").strip()
    hermes_url = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").strip()
    hermes_key = os.getenv("HERMES_API_KEY", "").strip()

    if not hermes_url:
        print("HERMES_API_URL is required", file=sys.stderr)
        sys.exit(2)

    client = HermesClient(api_url=hermes_url, api_key=hermes_key)
    app = create_app(auth_bearer=auth_bearer, hermes_client=client)

    # 启动日志：不打印 secret
    logger.info(
        "hermes adapter listening host=%s port=%s hermes_url=%s auth=%s",
        os.getenv("ADAPTER_HOST", _DEFAULT_HOST),
        os.getenv("ADAPTER_PORT", str(_DEFAULT_PORT)),
        hermes_url,
        "on" if auth_bearer else "off",
    )
    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("ADAPTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.getenv("ADAPTER_HOST", _DEFAULT_HOST)
    port = int(os.getenv("ADAPTER_PORT", str(_DEFAULT_PORT)))
    web.run_app(_build(), host=host, port=port)


if __name__ == "__main__":
    main()
