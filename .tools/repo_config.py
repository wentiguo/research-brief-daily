# -*- coding: utf-8 -*-
"""Portable repository / credential discovery for the local helper scripts.

Every value here is resolved at runtime instead of being hard-coded, so a clone of
this project works for any user on any machine. Resolution order:

1. Environment variable (highest priority -- use this in CI).
2. Local config file `automation/local_env.json` (written by `configure_project.py`).
3. A sensible default derived from the location of this file.

Secret values (SMTP passwords, API tokens) are NEVER stored here -- they live in
GitHub Secrets and are read by the cloud workflow, not by these local helpers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
LOCAL_ENV = PROJECT_ROOT / "automation" / "local_env.json"

# Default remote. Override with RESEARCH_BRIEF_REPO or automation/local_env.json.
DEFAULT_REPO_SLUG = "research-brief-daily"


def _load_local_env() -> dict:
    if not LOCAL_ENV.is_file():
        return {}
    try:
        with open(LOCAL_ENV, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def repo_slug() -> str:
    """`owner/name` of the GitHub repo this project pushes to and pulls from.

    Set RESEARCH_BRIEF_REPO=owner/name, or put "repo_slug" in local_env.json.
    Falls back to the GitHub login of the current gh auth + the default repo name.
    """
    env = os.environ.get("RESEARCH_BRIEF_REPO")
    if env:
        return env.strip()
    local = _load_local_env()
    value = local.get("repo_slug")
    if value:
        return str(value).strip()
    login = gh_login()
    return f"{login}/{DEFAULT_REPO_SLUG}" if login else DEFAULT_REPO_SLUG


def project_root() -> Path:
    """Absolute path to the project checkout on this machine."""
    env = os.environ.get("RESEARCH_BRIEF_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT


def gh_exe() -> str:
    """Absolute path to the GitHub CLI, or just "gh" when it is on PATH."""
    env = os.environ.get("GH_EXE")
    if env:
        return env
    found = shutil.which("gh")
    if found:
        return found
    # Windows default install location (not on PATH by default).
    win_default = Path(r"C:\Program Files\GitHub CLI\gh.exe")
    if win_default.is_file():
        return str(win_default)
    return "gh"


def gh_login() -> str:
    """Current GitHub login, or "" when gh is absent / not authenticated."""
    exe = gh_exe()
    try:
        out = subprocess.run([exe, "api", "user", "--jq", ".login"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def gh_token() -> str:
    """GitHub token for Contents-API pushes.

    Order: GH_TOKEN / GITHUB_TOKEN env, else the token stored by `gh auth login`.
    Never prompts. Returns "" when unavailable so callers can raise a clear error.
    """
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value.strip()
    try:
        out = subprocess.run([gh_exe(), "auth", "token"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def user_home() -> Path:
    return Path(os.path.expanduser("~"))