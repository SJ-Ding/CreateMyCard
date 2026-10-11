"""同一事件循环内，交互部署与批量截图共享设备占用。"""

import asyncio
import weakref

_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def device_lock(serial: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _LOCKS.setdefault(loop, {})
    return locks.setdefault(serial, asyncio.Lock())
