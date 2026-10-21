"""Application settings loaded from config/config.yaml + environment.

Secrets (like the API token) are referenced in the YAML file as
``${ENV_VAR}`` placeholders and expanded from the environment (or .env).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# ${VAR} or ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} placeholders in strings."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str
    temperature: float = 0.2
    vision_model: str = ""


@dataclass(frozen=True)
class AgentSettings:
    skills_path: str = "/.agents/skills/"
    system_prompt: str = "You are a resume analysis assistant."


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    agent: AgentSettings = field(default_factory=AgentSettings)
    project_root: Path = PROJECT_ROOT

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        # Load secrets without ever reading them here: .env first, then
        # .env.local as the override layer (both optional, real env wins).
        load_dotenv(PROJECT_ROOT / ".env")
        load_dotenv(PROJECT_ROOT / ".env.local", override=True)
        config_path = path or DEFAULT_CONFIG_PATH
        raw = yaml.safe_load(config_path.read_text()) or {}
        data = _expand_env(raw)

        llm = data.get("llm", {})
        agent = data.get("agent", {})
        return cls(
            llm=LLMSettings(
                base_url=llm.get("base_url", "https://openrouter.ai/api/v1/"),
                model=llm.get("model", "moonshotai/kimi-k3"),
                api_key=llm.get("api_key", ""),
                temperature=float(llm.get("temperature", 0.2)),
                vision_model=llm.get("vision_model", ""),
            ),
            agent=AgentSettings(
                skills_path=agent.get("skills_path", "/.agents/skills/"),
                system_prompt=agent.get(
                    "system_prompt", "You are a resume analysis assistant."
                ),
            ),
        )
