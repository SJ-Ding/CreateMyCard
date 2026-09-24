"""服务启动时选择支持异步子进程的事件循环。"""

import asyncio
import sys


def create_server_loop(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """供 Uvicorn 在创建服务循环前调用，热重载子进程同样适用。"""
    loop: asyncio.AbstractEventLoop
    if sys.platform == "win32":
        policy = asyncio.WindowsProactorEventLoopPolicy()
        asyncio.set_event_loop_policy(policy)
        loop = policy.new_event_loop()
    else:
        from uvicorn.loops.auto import auto_loop_factory

        loop = auto_loop_factory(use_subprocess=use_subprocess)()
    asyncio.set_event_loop(loop)
    return loop
