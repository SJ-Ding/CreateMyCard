"""端到端调试 Agent 的应用工厂。

统一入口由 :mod:`debug_tools.end_to_end_debug.backend.server` 提供；保留这个模块名
是为了让调试脚本能够按包内路径导入，而不再暴露旧的 ``/api/v1`` 入口。
"""

from __future__ import annotations

from ..server import create_app

__all__ = ["create_app"]
