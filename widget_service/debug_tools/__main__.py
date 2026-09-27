"""Python-only launcher for the prebuilt browser debug workbench."""

from __future__ import annotations

import argparse

import uvicorn

from .end_to_end_debug.backend.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 AI Widget 浏览器调试工作台")
    parser.add_argument("--host", default="127.0.0.1", help="只建议绑定回环地址")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
