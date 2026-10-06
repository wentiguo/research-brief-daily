# -*- coding: utf-8 -*-
"""Pull the full 'Generate research brief' step log and summarise what the
scope gate actually admitted: how many candidates were found, which direction
groups matched, and which journals appear.

Usage:
    python .tools/_summarize_run.py <run-id> [--repo owner/name]
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402

GH = rc.gh_exe()
REPO = rc.repo_slug()
RUN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RUN_ID", "")

if not RUN:
    sys.exit("usage: python .tools/_summarize_run.py <run-id> [--repo owner/name]")

if "--repo" in sys.argv:
    REPO = sys.argv[sys.argv.index("--repo") + 1]

STEP = "Generate research brief"

# Patterns worth keeping: selection / gating / counting / scope evidence
PAT = re.compile(
    r"(poster pick|poster digest|traced|quotes unverifiable|fallback|unverifiable|"
    r"sent email|sending email|attached .* file|emailed papers|innovation scoring)",
    re.I,
)


def main() -> None:
    p = subprocess.run(
        [GH, "run", "view", RUN, "--log", "--repo", REPO],
        capture_output=True)
    log = p.stdout.decode("utf-8", "replace")

    lines = []
    for raw in log.splitlines():
        if STEP not in raw:
            continue
        # strip the "Generate research brief\t<ts>Z " prefix
        m = re.match(r"^.*?\t\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?(.*)$", raw)
        body = (m.group(1) if m else raw).strip()
        if body:
            lines.append(body)

    print("step log lines = %d" % len(lines))
    print("=" * 72)
    keep = [l for l in lines if PAT.search(l)]
    seen, uniq = set(), []
    for l in keep:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    print("scope/selection lines = %d" % len(uniq))
    for l in uniq:
        print(l[:200])


if __name__ == "__main__":
    main()
