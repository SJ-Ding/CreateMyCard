"""AI Widget 调试工作台统一启动入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

if not __package__:
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from debug_tools.static_site import create_frontend_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 AI Widget 浏览器调试工作台")
    parser.add_argument(
        "--mode",
        choices=("frontend", "full"),
        default="full",
        help="frontend 仅托管前端；full 同时启用 Main Agent 后端",
    )
    parser.add_argument("--host", default="127.0.0.1", help="只建议绑定回环地址")
    parser.add_argument("--port", type=int, default=8888)
    return parser


def create_application(args) -> Any:
    print(f"Starting debug tools in {args.mode} mode...")
    print(f"Server listening on http://{args.host}:{args.port}/debug")
    if args.mode == "frontend":
        return create_frontend_app()
    if args.mode == "full":
        from debug_tools.end_to_end_debug.backend.server import create_app
        return create_app()
    raise ValueError(f"unsupported debug tools mode: {args.mode}")


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    app = create_application(args)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
