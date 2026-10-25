"""按已验收客户端组合失效关闭；不扩展公开请求字段。"""

from contextvars import ContextVar
from functools import wraps

from config.config import get_settings

_VERIFIED: ContextVar[bool | None] = ContextVar("presentation_verified_client", default=None)


def presentation_enabled() -> bool:
    return get_settings().enable_dynamic_presentation and _VERIFIED.get() is not False


def verified_client_scope(method):
    @wraps(method)
    async def run(service, request, policy, *args, **kwargs):
        verified = False
        for client in get_settings().dynamic_presentation_verified_clients:
            same_app = client.get("appVersion") == request.prdVer
            same_rom = client.get("romVersion") == request.device.romVersion
            same_profile = client.get("protocolProfileId") == policy.protocol_profile_id
            if same_app and same_rom and same_profile:
                verified = True
                break
        token = _VERIFIED.set(verified)
        try:
            return await method(service, request, policy, *args, **kwargs)
        finally:
            _VERIFIED.reset(token)

    return run
