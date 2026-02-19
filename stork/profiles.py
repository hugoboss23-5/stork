"""
Stork Profiles — Persona templates for agents.
================================================
A profile bundles a system prompt and a timeout.
Built-ins are baked in. Hugo can add custom ones to STORK_HOME/profiles.json.

Hugo & Watty · February 2026
"""

import json
import os
from pathlib import Path

STORK_HOME = Path(os.environ.get("STORK_HOME", os.path.expanduser("~/.stork")))
PROFILES_PATH = STORK_HOME / "profiles.json"

_BUILTIN_PROFILES = {
    "default": {
        "system_prompt": None,
        "timeout": 300,
    },
    "quant": {
        "system_prompt": (
            "You are a quantitative analyst. Be precise with numbers, show your work, "
            "and prefer structured data (tables, lists). Cite data sources."
        ),
        "timeout": 300,
    },
    "research": {
        "system_prompt": (
            "You are a research assistant. Be thorough but concise. Organize findings "
            "with clear sections. Cite sources. Distinguish facts from inferences."
        ),
        "timeout": 600,
    },
    "code": {
        "system_prompt": (
            "You are a senior software engineer. Write clean, tested code. Follow existing "
            "patterns in the codebase. Explain design decisions briefly."
        ),
        "timeout": 300,
    },
    "fast": {
        "system_prompt": "Be brief and direct. No preamble, no fluff. Answer in as few words as possible.",
        "timeout": 60,
    },
}


def load_profiles() -> dict:
    """Load profiles: builtins + any custom overrides from disk."""
    profiles = {k: dict(v) for k, v in _BUILTIN_PROFILES.items()}
    if PROFILES_PATH.exists():
        try:
            custom = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
            if isinstance(custom, dict):
                for name, spec in custom.items():
                    if isinstance(spec, dict):
                        profiles[name] = spec
        except (json.JSONDecodeError, OSError):
            pass
    return profiles


def get_profile(name: str) -> dict | None:
    """Get a profile by name. Returns None if not found."""
    return load_profiles().get(name)


def list_profiles() -> list[str]:
    """List all available profile names."""
    return sorted(load_profiles().keys())
