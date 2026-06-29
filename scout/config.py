"""
Configuration: loads API keys from .env and the ideal-candidate profile from icp.yaml.

The .env file lives one directory up (in the "Emaar AI Hiring" folder) so the same
keys can be shared across tools. We search a few sensible locations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# talent-scout/ project root (parent of the scout package)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# "Emaar AI Hiring" folder that contains the shared .env
PARENT_DIR = PROJECT_ROOT.parent

DEFAULT_MODEL = "claude-opus-4-8"
# Smaller/cheaper model for high-volume classification passes (kept configurable).
FAST_MODEL = "claude-opus-4-8"


def _load_env() -> None:
    """Load .env from the project dir or the parent 'Emaar AI Hiring' folder."""
    for candidate in (PROJECT_ROOT / ".env", PARENT_DIR / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


@dataclass
class Settings:
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    github_token: str = ""
    x_bearer_token: str = ""

    model: str = DEFAULT_MODEL
    fast_model: str = FAST_MODEL

    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "talent_scout.db")
    icp_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "icp.yaml")
    http_cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".cache")

    @property
    def database_url(self) -> str:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.db_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_env()

    def _clean(value: str | None) -> str:
        # .env values may carry stray spaces / quotes around the '='
        return (value or "").strip().strip('"').strip("'")

    s = Settings(
        anthropic_api_key=_clean(os.getenv("ANTHROPIC_API_KEY")),
        openai_api_key=_clean(os.getenv("OPENAI_API_KEY")),
        gemini_api_key=_clean(os.getenv("GEMINI_API_KEY")),
        github_token=_clean(os.getenv("GITHUB_TOKEN")),
        x_bearer_token=_clean(os.getenv("X_BEARER_TOKEN")),
    )
    if os.getenv("SCOUT_MODEL"):
        s.model = _clean(os.getenv("SCOUT_MODEL"))
    return s


# Runtime overrides (e.g. from the web UI intake) deep-merged over icp.yaml.
_RUNTIME_OVERRIDES: dict = {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_icp_file() -> dict:
    path = get_settings().icp_path
    if not path.exists():
        raise FileNotFoundError(
            f"ICP config not found at {path}. Copy/create config/icp.yaml first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_icp() -> dict:
    """Load the ideal-candidate profile, with any runtime overrides merged on top."""
    return _deep_merge(_load_icp_file(), _RUNTIME_OVERRIDES)


def set_overrides(overrides: dict) -> None:
    """Apply runtime config overrides (merged over icp.yaml on each load_icp())."""
    global _RUNTIME_OVERRIDES
    _RUNTIME_OVERRIDES = overrides or {}


def clear_overrides() -> None:
    global _RUNTIME_OVERRIDES
    _RUNTIME_OVERRIDES = {}


def set_model(model: str | None) -> None:
    """Override the active LLM model for this process (used by the UI's model picker)."""
    if model:
        get_settings().model = model
