from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

import uvicorn

from debug_agent.app import create_app
from debug_agent.config import DebugSettings, load_debug_config
from debug_agent.logging_utils import configure_debug_logging


from config.config import get_settings

production = get_settings()
config_path = (Path(production.repo_root) / "cloud" / "debug_agent.yaml")
if not config_path.is_file():
    config_path = Path(production.repo_root) / "widget_service" / "cloud" / "debug_agent.yaml"
config = load_debug_config(config_path, production.repo_root)
settings = DebugSettings.from_config(
    config,
    production,
    profile=os.getenv("DEBUG_AGENT_PROFILE"),
    port=8888,
    log_level=os.getenv("DEBUG_AGENT_LOG_LEVEL"),
    trace=True,
)
overrides = {}
if os.getenv("DEBUG_AGENT_UPSTREAM_URL"):
    overrides["upstream_base_url"] = os.environ["DEBUG_AGENT_UPSTREAM_URL"]
if os.getenv("DEBUG_AGENT_SKILL_ROOT"):
    overrides["skill_root"] = Path(os.environ["DEBUG_AGENT_SKILL_ROOT"]).resolve()
if overrides:
    settings = replace(settings, **overrides)
configure_debug_logging(settings.log_level)
app = create_app(settings, production_settings=production)



if __name__ == "__main__":
    print(f"Debug Agent Server running on http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
