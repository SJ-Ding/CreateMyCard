"""受控子进程执行、取消及日志脱敏。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from debug_tools.subprocesses import (
    ProcessExecutionError,
    run_process,
    terminate_process_tree,
)
from debug_tools.subprocesses import redact as redact


class DeviceError(RuntimeError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


async def terminate(process: asyncio.subprocess.Process) -> None:
    await terminate_process_tree(process)


async def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
    log: Path | None = None,
) -> str:
    try:
        return await run_process(args, cwd=cwd, env=env, timeout=timeout, log=log)
    except ProcessExecutionError as exc:
        raise DeviceError(f"命令退出码 {exc.returncode}：{exc.output}") from exc
