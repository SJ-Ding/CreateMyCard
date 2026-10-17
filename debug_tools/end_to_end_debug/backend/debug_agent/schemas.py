from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DeviceDebugContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str = "debug-user"
    odid: str = "debug-device"
    deviceId: str = "debug-device"
    phoneType: str = "ALN-AL00"
    appVersion: str = "11.9.9.342"
    romVersion: str = "ALN-AL00 7.0.0.100"
    locale: str = "zh-CN"
    countryCode: str = "CN"
    deviceFormation: str = "phone"
    deviceType: int = 0
    sysVer: str = "HarmonyOS"
    paginationLimit: int = 5
    paginationStart: str = ""


class ArtifactPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runId: str
    artifactUrl: str
    artifactDigest: str = ""
    digestMatches: bool | None = None
    genui: str = ""
    cardSpec: dict[str, Any] = Field(default_factory=dict)
    taskSpec: dict[str, Any] = Field(default_factory=dict)
    effectiveCapabilities: dict[str, Any] = Field(default_factory=dict)
    removedCapabilities: list[Any] = Field(default_factory=list)
    generationPlan: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    designToken: str | None = None

