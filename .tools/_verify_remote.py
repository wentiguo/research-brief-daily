# -*- coding: utf-8 -*-
"""Verify what the remote `main` actually holds for a few key files.

Uses the GitHub contents API via `gh api` (through urllib to avoid
`--jq` truncation on large files) and compares the remote bytes to the
local file. This decides whether the corrected scope has already been
pushed or whether the cloud is still running the old, narrowed config.
"""
import base64
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402

REPO = rc.repo_slug()
LOCAL_ROOT = str(rc.project_root())
GH = rc.gh_exe()

FILES = [
    "automation/research_brief_config.json",
    "scripts/generate_research_brief.py",
    "scripts/deepseek_client.py",
    "SKILL.md",
    "tests/test_mail_ledger.py",
    "tests/test_poster_priority_and_bridge.py",
    ".tools/local_fetch_posters.py",
    ".tools/_validate_bridge.py",
    ".tools/push_contents.py",
]


def gh_json(path: str):
    """GET a contents path, return decoded bytes or None if missing."""
    proc = subprocess.run(
        [GH, "api", "repos/%s/contents/%s" % (REPO, path)],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        return None
    if "content" not in data:
        return None
    raw = data["content"].encode("ascii", "ignore")
    # contents API returns base64 of the file; GitHub wraps long blobs but
    # for normal text files a single decode is correct.
    return base64.b64decode(raw)


def main() -> None:
    for rel in FILES:
        local_path = os.path.join(LOCAL_ROOT, rel.replace("/", os.sep))
        if not os.path.exists(local_path):
            print("%-46s LOCAL MISSING" % rel)
            continue
        local = open(local_path, "rb").read()
        remote = gh_json(rel)
        if remote is None:
            print("%-46s REMOTE MISSING  (local %d B)" % (rel, len(local)))
            continue
        same = (local == remote)
        print("%-46s %s  local=%d B remote=%d B"
              % (rel, "IN SYNC" if same else "DIFFERS", len(local), len(remote)))


if __name__ == "__main__":
    main()
