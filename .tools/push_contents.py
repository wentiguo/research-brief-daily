"""Push local files through the contents API (the tree API stored base64 verbatim).

The git tree API echoed our base64 `content` back as file bytes on the runner, which
turned every script into `SyntaxError: invalid decimal literal`. The contents API
explicitly decodes `content` and is the documented path for editing a single file.

Two jobs live here, and only one of them:

* with no flag, the list of source-controlled `FILES` (scripts, tests, docs);
* with `--dir`, one day's fetched attachment folder pushed under
  `research_briefs/attachments/<date>/`, which is the hand-over the cloud mail reads
  back (see `fetch_paper_attachments.reuse_synced_attachments`).

A second implementation of the same hand-over used to live in `scripts/sync_attachments.py`
on the git tree API; it was deleted rather than kept side by side, because the tree API is
the broken one and a 15 MB PDF is nowhere near the contents API's 100 MB ceiling.

One commit per file keeps the blame readable; credentials never leave the process.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_config as rc  # noqa: E402

REPO = rc.repo_slug()
LOCAL_ROOT = rc.project_root()
ATTACHMENT_PREFIX = "research_briefs/attachments"
CONTENTS_LIMIT_MB = 90.0  # the contents API takes 100 MB per file; stay clear of the edge

FILES = [
    "scripts/deepseek_client.py",  # summary fallback prompt (was missing -> edits never reached the runner)
    "scripts/generate_research_brief.py",
    "scripts/publisher_feeds.py",
    "scripts/render_digest_images.py",
    "scripts/fetch_paper_attachments.py",
    "scripts/fetch_browser_paper.py",
    "scripts/mouse_operator.py",  # the physical-cursor driver the browser pass aims with
    "scripts/verify_publisher_channels.py",
    "scripts/check_smtp.py",
    "tests/test_checkbox_match.py",  # regression cover for the matcher's coordinate bug
    "tests/test_attachment_pruning.py",  # one survivor per duplicate digest
    "tests/test_attachment_kinds.py",  # one full text per article, whatever the route
    "tests/test_attachment_identity.py",  # a cited report must not be mailed as the paper
    "tests/test_attachment_reuse.py",  # the manifest hand-over, and the card is not a paper
    "tests/test_domain_match.py",  # why an abstract-less PRL used to be refused outright
    "tests/test_journal_scope.py",  # the zone-1 scope add, and poster-only attachments
    "tests/test_topic_gate.py",  # 2026-10-04 agreed four-rule gate regression
    "tests/test_topic_scope_20261006.py",  # 2026-10-06 full-scope re-alignment regression
    "tests/test_poster_published_and_quotes_20261006.py",  # poster prefers published; quote gate
    "tests/test_mail_ledger.py",  # date-rot fix: today-based idempotency
    "tests/test_openalex_keyword_search.py",  # keyword-search term coverage regression
    "tests/test_brief_content_quality.py",  # brief content/briefing-quality regression
    "tests/test_manifest_carryover.py",  # manifest hand-over across runs
    "tests/test_poster_priority_and_bridge.py",  # poster priority + cloud/local date guard
    "tests/test_local_bridge_guard_20261009.py",  # local bridge only acts on today/yesterday's wishlist
    "tests/test_nature_oa_pdf_derive_20261009.py",
    "tests/test_manifest_not_mailed_20261010.py",  # a manifest is not reading material; it also faked the poll success
    "tests/test_arxiv_preprint_route_20261010.py",  # arXiv must serve DOI-carrying papers too, and not be re-rejected on bytes  # root-cause regression: Nature OA omits url_for_pdf
    "automation/research_brief_config.json",
    ".github/workflows/research-brief.yml",
    "SKILL.md",
    "README.md",
    "LICENSE",
    ".gitignore",
    "assets/turnstile_checkbox_unchecked.png",  # the control the physical cursor aims at
    ".env.example",
    "docs/AI_SETUP_PROMPT.md",
    # ops helpers: verify the remote byte-for-byte, run tests, watch a run.
    # .tools/_scan_creds.py and _hunt_gh_token.py are deliberately NOT listed:/n    # they are diagnostic probes, not part of the delivered project.
    ".tools/_verify_remote.py",  # push succeeded != change reached the runner
    ".tools/fetch_pdfs_local.py",  # APS blocks the Actions IP; fetch from a normal machine
    ".tools/local_fetch_posters.py",  # local run fetches the poster PDFs the cloud cannot
    ".tools/_validate_bridge.py",  # proves the local->cloud PDF hand-over end to end
    ".tools/push_contents.py",  # the deploy tool itself: keep its whitelist in sync
    ".tools/_run_tests.py",
    ".tools/_await_run.py",
    ".tools/_summarize_run.py",
    "docs/EMAIL_PROVIDERS.md",
]

BASE_MESSAGE = "Five-dimension ranking (topical fit dominant, journal == innovation), no-score poster with English title and traceable Chinese briefings, plus a desktop-browser pass for subscription journals and a privacy-clean, placeholder-only config"


def api(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "research-brief-actions-sync",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"{method} /{path} -> HTTP {exc.code}: {detail}") from exc


def _encoded_path(rel: str) -> str:
    """The API wants percent-encoded paths; the citation card is full of CJK characters."""
    return quote(rel, safe="/")


def upload(token: str, rel: str, blob: bytes, message: str) -> str:
    encoded = base64.b64encode(blob).decode("ascii")
    try:
        existing = api("GET", f"repos/{REPO}/contents/{_encoded_path(rel)}", token)
        sha = existing.get("sha")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        sha = None  # new file: no blob to replace
        print(f"{rel}: new file")
    body = {"message": message, "content": encoded}
    if sha:
        body["sha"] = sha
    result = api("PUT", f"repos/{REPO}/contents/{_encoded_path(rel)}", token, body)
    return str(result["commit"]["sha"])


def _attachment_targets(local_dir: Path, run_date: str, root: Path) -> list[str]:
    """The day's files as repo-relative paths, straight from the hand-over manifest.

    The manifest is written after duplicate pruning, so every name it lists exists and
    nothing else in the folder is claimed. The manifest and the citation card travel too:
    the cloud run that reads this folder back wants both.
    """
    manifest_path = local_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"no manifest.json in {local_dir}; run the fetch pass first")
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    stamped = payload.get("date")
    if stamped and stamped != run_date:
        raise RuntimeError(f"manifest is dated {stamped!r}, not {run_date!r}; refusing to push")
    try:
        relative = local_dir.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"{local_dir} is outside {root}; pass a path inside the workspace") from exc
    wanted = [local_dir / str(entry.get("name", "")) for entry in payload.get("files") or []]
    wanted += [manifest_path, local_dir / f"{run_date}_题录与获取指引.txt"]

    rels: list[str] = []
    seen: set[str] = set()
    for path in wanted:
        if not path.exists():
            print(f"warning: manifest names {path.name} but it is not on disk; skipped")
            continue
        rel = f"{relative}/{path.name}"
        if rel in seen:
            continue
        seen.add(rel)
        rels.append(rel)
    return rels


def main() -> int:
    ap = argparse.ArgumentParser(description="Push local files into the repository.")
    ap.add_argument("--only", help="push a single path from FILES (used for verification)")
    ap.add_argument("--dir", help="push a whole attachment folder instead of the file list")
    ap.add_argument("--date", help="date of the folder to push; defaults to today")
    ap.add_argument("--push", action="store_true", help="write to the repository")
    args = ap.parse_args()

    if args.dir:
        local_dir = Path(args.dir)
        if not local_dir.is_absolute():
            local_dir = LOCAL_ROOT / local_dir
        run_date = args.date or dt.date.today().isoformat()
        targets = _attachment_targets(local_dir, run_date, LOCAL_ROOT)
    else:
        run_date = None
        targets = [p for p in FILES if args.only is None or p == args.only]
        if not targets:
            raise RuntimeError(f"--only {args.only!r} matched nothing; expected one of {FILES}")

    # Either mode names its files by repo-relative path, so one root covers both.
    total = 0
    sizes: list[tuple[str, int]] = []
    for rel in targets:
        local = LOCAL_ROOT / rel
        if not local.exists():
            raise RuntimeError(f"local file missing: {local}")
        size = local.stat().st_size
        sizes.append((rel, size))
        total += size
        if size > CONTENTS_LIMIT_MB * 1_000_000:
            raise RuntimeError(f"{rel} is {size / 1_000_000:.0f} MB, past the contents API limit")

    print(f"would push {len(sizes)} file(s), {total / 1_000_000:.1f} MB"
          + ("" if args.push else " (dry run; re-run with --push to write)"))
    for rel, size in sizes:
        print(f"  · {rel} ({size // 1024} KB)")

    if not args.push:
        print("dry run: nothing was written")
        return 0

    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN is not set; nothing can be pushed")

    for index, (rel, size) in enumerate(sizes, start=1):
        blob = (LOCAL_ROOT / rel).read_bytes()
        if run_date:
            message = f"Sync {run_date} attachments: {Path(rel).name} ({index}/{len(sizes)})"
        else:
            message = f"{BASE_MESSAGE} ({index}/{len(sizes)})"
        sha = upload(token, rel, blob, message)
        print(f"{rel:60s} {size:>8d} bytes -> commit {sha[:10]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
