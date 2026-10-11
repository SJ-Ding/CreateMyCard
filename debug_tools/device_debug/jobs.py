"""可恢复状态的串行构建任务；仅在工程副本中写入。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from debug_tools.postprocess_plugins._io import atomic_write_json

from .commands import DeviceError, command, redact
from .config import Config, Project, read_object
from .conversion import convert
from .devices import Devices, Target
from .locks import device_lock

LOGGER = logging.getLogger(__name__)
TERMINAL = {"success", "failed", "cancelled", "interrupted"}


class Jobs:
    def __init__(self, config: Config, devices: Devices) -> None:
        self.config = config
        self.devices = devices
        self.states: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.build_lock = asyncio.Lock()

    async def start(self) -> None:
        self.config.output.mkdir(parents=True, exist_ok=True)
        for path in self.config.output.glob("*/state.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("id") != path.parent.name:
                    continue
                job_id = path.parent.name
                if state.get("status") not in TERMINAL:
                    state.update(status="interrupted", error="后端重启，任务已中断，请重新提交")
                    atomic_write_json(path, state)
                self.states[job_id] = state
            except (OSError, ValueError) as exc:
                LOGGER.warning("读取设备任务失败 %s: %s", type(exc).__name__, exc)

    def get(self, job_id: str) -> dict[str, Any]:
        state = self.states.get(job_id)
        if state is None:
            raise DeviceError("任务不存在", 404)
        return state

    def update(self, job_id: str, **values: Any) -> None:
        state = self.get(job_id)
        state.update(values, updatedAt=datetime.now(UTC).isoformat())
        atomic_write_json(self.config.output / job_id / "state.json", state)

    async def submit(self, target_id: str, source: str, input_format: str, size: str) -> dict:
        target = await self.devices.target(target_id)
        converted = await asyncio.to_thread(convert, source, input_format, size)
        project = Project.read(self.config.project)
        if target.kind != "emulator" and not project.signed:
            raise DeviceError("真机或未确认类型的设备需要签名，请先在 DevEco 中配置工程签名")
        job_id = uuid.uuid4().hex
        directory = self.config.output / job_id
        directory.mkdir(parents=True)
        (directory / "input.txt").write_text(source, encoding="utf-8")
        (directory / "a2ui.json").write_text(converted.text, encoding="utf-8")
        state = {
            "id": job_id,
            "targetId": target.id,
            "device": target.name,
            "serial": target.serial,
            "size": size,
            "format": converted.format,
            "status": "queued",
            "stage": "排队",
            "error": None,
            "installed": False,
        }
        self.states[job_id] = state
        self.update(job_id)
        self.tasks[job_id] = asyncio.create_task(self._execute(job_id, target))
        return state

    async def _execute(self, job_id: str, target: Target) -> None:
        directory = self.config.output / job_id
        try:
            async with self.build_lock:
                await self._build_install(job_id, target, directory)
            self.update(job_id, status="success", stage="完成")
        except asyncio.CancelledError:
            if self.get(job_id).get("status") != "interrupted":
                self.update(job_id, status="cancelled", error="任务已取消；已完成的安装不会撤销")
            raise
        except Exception as exc:
            LOGGER.exception("设备任务失败：%s", job_id)
            self.update(job_id, status="failed", error=redact(str(exc)) or type(exc).__name__)
        finally:
            try:
                await asyncio.to_thread(self._clean_project, directory)
            except OSError as exc:
                LOGGER.warning("工程副本清理失败 %s: %s", type(exc).__name__, exc)

    def _clean_project(self, directory: Path) -> None:
        project = (directory / "render_project").resolve()
        if not project.is_relative_to(self.config.output.resolve()):
            raise DeviceError("工程副本路径越界")
        if project.is_dir():
            shutil.rmtree(project)

    def _prepare(self, directory: Path) -> None:
        project = directory / "render_project"
        source = self.config.project.resolve()
        if project.resolve().is_relative_to(source):
            raise DeviceError("输出目录不能位于源工程内")
        shutil.copytree(
            source,
            project,
            ignore=shutil.ignore_patterns("build", ".hvigor", ".idea", ".git", "node_modules"),
        )
        destination = project / "entry/src/main/resources/rawfile/test.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / "a2ui.json", destination)
        profile_path = project / "build-profile.json5"
        profile = read_object(profile_path)
        app = profile.get("app")
        if not isinstance(app, dict):
            raise DeviceError("工程缺少 app 配置")
        configs = app.get("signingConfigs", [])
        for config in configs:
            material = config.get("material") if isinstance(config, dict) else None
            if not isinstance(material, dict):
                continue
            for key in ("certpath", "profile", "storeFile"):
                value = material.get(key)
                if isinstance(value, str) and value:
                    material[key] = str((source / value).resolve())
        # 无签名的模拟器构建不能引用不存在的 default 签名项。
        if not configs:
            for product in app.get("products", []):
                if isinstance(product, dict):
                    product.pop("signingConfig", None)
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _build_install(self, job_id: str, target: Target, directory: Path) -> None:
        self.update(job_id, status="running", stage="准备工程")
        # 复制在线程内运行，取消时必须等复制结束才清理，防止遗留后台写入。
        prepare = asyncio.create_task(asyncio.to_thread(self._prepare, directory))
        try:
            await asyncio.shield(prepare)
        except asyncio.CancelledError:
            await prepare
            raise
        project = directory / "render_project"
        info = Project.read(project)
        log = directory / "build.log"
        self.update(job_id, stage="编译")
        await command(
            [
                str(self.config.node),
                str(self.config.hvigor),
                "--mode",
                "module",
                "-p",
                "product=default",
                "-p",
                "module=entry@default",
                "-p",
                "buildMode=debug",
                "assembleHap",
                "--no-daemon",
            ],
            cwd=project,
            env=self.config.environment(),
            timeout=self.config.build_timeout,
            log=log,
        )
        name = "entry-default-signed.hap" if info.signed else "entry-default-unsigned.hap"
        built = project / "entry/build/default/outputs/default" / name
        if not built.is_file():
            raise DeviceError(f"编译未生成预期 HAP：{name}")
        hap = directory / name
        shutil.copyfile(built, hap)
        self.update(job_id, stage="等待设备占用")
        async with device_lock(target.serial):
            current = await self.devices.target(target.id)
            if current.kind != "emulator" and not info.signed:
                raise DeviceError("设备类型发生变化，未签名 HAP 不允许推送")
            self.update(job_id, stage="安装")
            result = await self.devices.hdc(
                target.serial,
                ["install", "-r", str(hap)],
                timeout=120.0,
                log=log,
            )
            self._require_success(result, "安装")
            self.update(job_id, installed=True, stage="启动")
            await self.devices.hdc(
                target.serial,
                ["shell", "aa", "force-stop", info.bundle],
                log=log,
            )
            result = await self.devices.hdc(
                target.serial,
                ["shell", "aa", "start", "-a", info.ability, "-b", info.bundle, "-m", info.module],
                log=log,
            )
            self._require_success(result, "启动")

    @staticmethod
    def _require_success(output: str, action: str) -> None:
        lowered = output.lower()
        if "success" not in lowered or "failed" in lowered or "error:" in lowered:
            raise DeviceError(f"{action}未确认成功：{output[-2000:]}")

    async def cancel(self, job_id: str) -> dict[str, Any]:
        state = self.get(job_id)
        task = self.tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if state.get("status") not in TERMINAL:
                self.update(job_id, status="cancelled", error="任务已取消")
        return state

    def logs(self, job_id: str, offset: int) -> dict[str, Any]:
        self.get(job_id)
        path = self.config.output / job_id / "build.log"
        data = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        text = data[offset : offset + 32000]
        return {"text": text, "offset": offset + len(text)}

    async def close(self) -> None:
        for job_id, task in self.tasks.items():
            if not task.done():
                self.update(job_id, status="interrupted", error="后端关闭，任务已中断")
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
