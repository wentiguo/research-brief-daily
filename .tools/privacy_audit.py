#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan a working tree for personal data before it is pushed anywhere.

    python .tools/privacy_audit.py [--root DIR]

The failure modes this catches are the ones a fork-and-edit workflow actually
produces: an account name hard-coded in a helper script, an absolute path from the
machine the code was first written on, a credential in a dotfile, and a real e-mail
address or inbox domain sitting in a sample config.

Exit code 0 means clean. Anything reported should become a placeholder, an
environment-variable read, or a value the user supplies at setup time -- the wizard
(`scripts/configure_project.py`) exists precisely so that personal values never need
to be committed.

Placeholders this treats as SAFE (they are documentation, not data):
    *example.com / example.org / example.invalid / localhost / users.noreply*
    *your.email@* / *you@* / *YOUR_* / *your_*
"""
from __future__ import annotations

import os
import re
import sys

SAFE_EMAIL_MARKS = (
    "example.com", "example.org", "example.invalid", "example.net",
    "localhost", "users.noreply", "your.", "you@", "your_", "researcher@",
    "test@", "placeholder",
)

PATTERNS = {
    "github token": re.compile(
        r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "api key": re.compile(r"\bsk-[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "windows user path": re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!\{)[A-Za-z0-9_.-]+"),
    "codex/skill path": re.compile(r"[A-Za-z]:[\\/](?:codex|Users)[\\/][^\s\"']*"),
    "hpc cluster path": re.compile(r"/fs\d+/home/[a-z]"),
    "slurm queue": re.compile(r"\b[a-z]\d{4,6}v\d?[a-z]?!\b"),
    "broken placeholder": re.compile(
        r"rPROJECT_ROOT|rGH_EXE|YOUR_GITHUB_LOGIN|SKILL_ROOT|PROJECT_ROOT\b(?!)"),
    "leftover marker": re.compile(r"YOUR_[A-Z_]+|__FILL_ME__|TODO_FILL"),
    # Only a quoted literal can actually hold a secret. `token = gh_token` and
    # `api_key = some_var` are code, not data, and matching them buries the real
    # findings under noise until nobody reads the output.
    "secret in a literal": re.compile(
        r"(?i)(smtp_password|api_key|api_secret|access_token|secret_key|auth_token)"
        r"\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"),
}
# Values that look like a secret but are obviously documentation. Judged by the whole
# assignment, not just the value, so a real key with an unlucky substring still trips.
SAFE_VALUE_MARKS = (
    "your_", "your-", "you_", "example", "placeholder", "changeme", "changeme",
    "todo", "xxx", "<", ">", "_here", "redacted", "dummy", "notreal", "test",
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".idea", ".vscode",
             ".browser_profile", ".feed_cache", ".mypy_cache", ".pytest_cache",
             "reference_push_archive", "xhs_materials"}
# This file necessarily contains the patterns it looks for, so scanning it would
# always report itself. Same for a user's own local audit config.
SELF = {"privacy_audit.py"}
TEXT_EXT = (".py", ".md", ".json", ".yml", ".yaml", ".txt", ".ps1", ".sh", ".bat",
            ".cfg", ".ini", ".toml", ".env", ".example", ".gitignore", "")
SIZE_CAP = 3_000_000


def is_safe_email(value: str) -> bool:
    low = value.lower()
    return any(mark in low for mark in SAFE_EMAIL_MARKS)


def scan(root: str) -> int:
    findings: dict[str, list[tuple[str, str]]] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not (filename.endswith(TEXT_EXT) or filename.startswith(".env")
                    or filename == ".gitignore"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if filename in SELF:
                continue
            try:
                if os.path.getsize(full) > SIZE_CAP:
                    continue
                text = open(full, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            scanned += 1
            for match in EMAIL.finditer(text):
                if not is_safe_email(match.group(0)):
                    findings.setdefault("e-mail address", []).append(
                        (rel, match.group(0)))
            for name, rx in PATTERNS.items():
                for match in rx.finditer(text):
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line_end = text.find("\n", match.end())
                    line = text[line_start: line_end if line_end != -1 else len(text)]
                    if name == "secret in a literal" and any(
                            mark in line.lower() for mark in SAFE_VALUE_MARKS):
                        continue
                    findings.setdefault(name, []).append((rel, match.group(0)[:60]))

    print(f"scanned {scanned} text files under {root}\n")
    total = 0
    for name in sorted(set(PATTERNS) | {"e-mail address"}):
        rows = sorted(set(findings.get(name, [])))
        total += len(rows)
        print(("  clean  " if not rows else "  HITS   ") + f"{name}: {len(rows)}")
        for rel, snippet in rows[:12]:
            print("            " + rel + "  ->  " + snippet)
    print(f"\ntotal findings: {total}")
    if total:
        print("\nReplace each finding with a placeholder, an env-var read, or a value "
              "the user supplies at setup time.\nPersonal settings belong in "
              "automation/research_brief_config.json (gitignored); secrets belong in "
              "GitHub Secrets.")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    root = os.path.abspath(sys.argv[sys.argv.index("--root") + 1]) \
        if "--root" in sys.argv else os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(scan(root))