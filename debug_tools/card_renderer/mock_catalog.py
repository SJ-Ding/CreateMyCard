"""浏览器模拟编辑使用的只读能力目录，不调用真实端侧能力。"""

from __future__ import annotations

from debug_tools.paths import ensure_cloud_import_path
from fastapi import FastAPI, HTTPException


def register_mock_catalog_routes(app: FastAPI) -> None:
    ensure_cloud_import_path()

    from config.config import get_settings
    from services.capability_registry import CapabilityRegistry

    @app.get("/debug/renderer/mock-catalog")
    def mock_catalog(
        registryVersion: str | None = None,
        appVersion: str | None = None,
        romVersion: str | None = None,
    ) -> dict:
        settings = get_settings()
        root = settings.data_root / "capabilities"
        versions: list[str] = []
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir():
                continue
            if (candidate / "data_capabilities.json").is_file():
                versions.append(candidate.name)
        source = "default"
        selected = (
            registryVersion if registryVersion is not None else settings.capability_registry_version
        )
        try:
            if registryVersion is not None:
                source = "artifact_or_manual"
            elif appVersion is not None or romVersion is not None:
                source = "request"
                selected = CapabilityRegistry.from_app_rom_versions(
                    appVersion or settings.default_prd_version,
                    romVersion or settings.default_device_rom_version,
                )
            if selected not in versions:
                raise ValueError("注册表版本不存在，请手动选择可用版本")
            registry = CapabilityRegistry(version=selected)
            definitions: list[dict] = []
            for capability in registry.list_data_capabilities():
                definitions.append({
                    "id": capability.id,
                    "description": capability.description,
                    "defaultWriteResultTo": capability.defaultWriteResultTo,
                    "outputSchema": capability.outputSchema,
                })
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "availableVersions": versions},
            ) from exc
        return {
            "registryVersion": selected,
            "source": source,
            "availableVersions": versions,
            "capabilities": definitions,
        }
