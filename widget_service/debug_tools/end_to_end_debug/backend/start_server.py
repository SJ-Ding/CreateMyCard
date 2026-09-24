"""启动端到端调试平台，不启动外部工具服务。"""

from __future__ import annotations

import os

import uvicorn

from .server import app


def main() -> None:
    if app is None:
        raise RuntimeError("debug app failed to initialize")
    uvicorn.run(
        app,
        host=os.getenv("DEBUG_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("DEBUG_AGENT_PORT", "8888")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
