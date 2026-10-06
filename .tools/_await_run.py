# -*- coding: utf-8 -*-
"""Poll a GitHub Actions run until it completes, then print the tail of the
job log so the email-sending lines are visible.

Usage:
    python .tools/_await_run.py <run-id> [--repo owner/name] [--minutes 25]

`gh` is located via .tools/repo_config.py (env var -> PATH -> Windows default
install path), and `--jq` is avoided in favour of parsing full JSON, because
truncation hides large output.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402

GH = rc.gh_exe()
REPO = rc.repo_slug()
RUN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RUN_ID", "")
DEADLINE_S = 25 * 60

if not RUN:
    sys.exit("usage: python .tools/_await_run.py <run-id> [--repo owner/name]")

if "--repo" in sys.argv:
    REPO = sys.argv[sys.argv.index("--repo") + 1]
if "--minutes" in sys.argv:
    DEADLINE_S = int(float(sys.argv[sys.argv.index("--minutes") + 1]) * 60)


def gh_json(args):
    p = subprocess.run([GH] + args + ["--repo", REPO],
                       capture_output=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def main() -> None:
    t0 = time.time()
    status = conclusion = None
    while time.time() - t0 < DEADLINE_S:
        d = gh_json(["run", "view", RUN, "--json",
                     "status,conclusion,updatedAt"])
        if d:
            status = d.get("status")
            conclusion = d.get("conclusion")
            if status == "completed":
                break
        print("[%4ds] status=%s conclusion=%s"
              % (int(time.time() - t0), status, conclusion), flush=True)
        time.sleep(30)

    print("=" * 70)
    print("FINAL status=%s conclusion=%s" % (status, conclusion))

    # Pull the job log and keep only the lines that prove the run behaved:
    # scope gate, paper counts, and the SMTP delivery verdict.
    p = subprocess.run(
        [GH, "run", "view", RUN, "--log", "--repo", REPO],
        capture_output=True)
    log = p.stdout.decode("utf-8", "replace")
    print("log bytes = %d" % len(log))

    KEEP = ("sent email", "sending email", "smtp", "SMTP", "skip",
            "paper", "Paper", "brief", "recommend", "domain",
            "topic", "force_send", "ledger", "inbox", "publisher feed",
            "stale", "fresh", "abstract", "ERR", "WARN", "Traceback",
            "error", "no papers", "selected", "group")
    out = []
    for line in log.splitlines():
        # strip the runner timestamp prefix for readability
        for sep in ("\t", "Z ", "Z\t"):
            if sep in line:
                line = line.split(sep, 1)[-1]
                break
        if any(k in line for k in KEEP):
            out.append(line.strip())
    # de-dup while preserving order
    seen = set()
    uniq = []
    for l in out:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    print("-" * 70)
    print("interesting lines: %d (showing last 120)" % len(uniq))
    for l in uniq[-120:]:
        print(l)


if __name__ == "__main__":
    main()
