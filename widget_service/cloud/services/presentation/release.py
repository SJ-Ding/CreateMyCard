"""仅通过服务配置开启或关闭动态展示，不扩展公开请求字段。"""

from config.config import get_settings


def presentation_enabled() -> bool:
    return get_settings().enable_dynamic_presentation
