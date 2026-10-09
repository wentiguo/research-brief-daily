"""Fetch the day's poster PDFs on this machine and push them to the repo.

Why this exists: the scheduled cloud run cannot download APS (or other paywalled)
publisher PDFs - APS blocks the Actions runner's egress IP with HTTP 403. But a normal
machine can. The cloud run publishes `research_briefs/latest_poster_wishlist.json`
(the two poster DOIs); this script reads it, downloads the original journal PDFs from
this machine, writes a `manifest.json` the cloud's `reuse_synced_attachments` understands,
and pushes the dated folder to the repo. The cloud then folds those files into the
single daily email.

Gitless by design: the working copy on this machine is populated through the GitHub
Contents API, so it is NOT a git checkout. We therefore read the wishlist and write
the PDFs back through the Contents API (same path push_contents.py uses - handles PDFs
up to ~90 MB per file without trouble).

Run this right after the daily workflow finishes, or schedule it on your own machine.
Safe to run by hand too.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _beijing_date(days_offset: int = 0) -> str:
    """Beijing (UTC+8) calendar date, computed without zoneinfo (Windows lacks tzdata)."""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8, days=days_offset)).date().isoformat()


def _should_act_on_wishlist(date_str: str) -> bool:
    """Only fetch when the cloud's wishlist is dated today or yesterday (Beijing).

    A wishlist from two or more days ago means the cloud run has not published today's
    yet (or the local clock is badly off); acting on a stale one would re-fetch papers
    that already shipped. Yesterday is allowed so a small local-clock drift does not
    skip a run that is still valid.
    """
    return date_str in (_beijing_date(0), _beijing_date(-1))

# The script lives in .tools/ next to fetch_pdfs_local.py, so import it directly.
_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import fetch_pdfs_local as fl  # noqa: E402
import repo_config as rc  # noqa: E402

REPO = rc.repo_slug()
LOCAL_ROOT = rc.project_root()
WISHLIST_LOCAL = LOCAL_ROOT / "research_briefs" / "latest_poster_wishlist.json"
WISHLIST_REMOTE = "research_briefs/latest_poster_wishlist.json"
GH_EXE = rc.gh_exe()
CONTENTS_LIMIT_MB = 90.0


def local_date_str() -> str:
    return _beijing_date(0)


def resolve_target_date(wish_doc: Any) -> str:
    """Cloud-stamped run date wins; fall back to the local clock only if it is absent.

    The local machine clock can drift (observed +2 days vs the runner); trusting it
    would write the poster PDFs into the wrong dated folder and the cloud would never
    find them. The cloud stamps its authoritative run date into the wishlist, and the
    local run must honour that exact date.
    """
    if isinstance(wish_doc, dict):
        stamped = (wish_doc.get("run_date") or "").strip()
        if stamped:
            return stamped
    return local_date_str()


# --------------------------------------------------------------------------- #
# GitHub Contents API (gitless)                                                 #
# --------------------------------------------------------------------------- #
def _api(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "research-brief-local-fetch",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.github.com/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} /{path} -> HTTP {exc.code}: {detail}") from exc


def gh_token() -> str:
    """Token from env, else from the gh CLI (never prompt the user)."""
    env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    for candidate in (GH_EXE, "gh"):
        try:
            proc = subprocess.run([candidate, "auth", "token"],
                                  capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return ""


def fetch_wishlist(token: str) -> Any | None:
    """Read the wishlist from origin/main (preferred) and fall back to local disk."""
    if token:
        try:
            node = _api("GET", f"repos/{REPO}/contents/{quote(WISHLIST_REMOTE)}", token)
            content = base64.b64decode(node["content"])
            return json.loads(content.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - fall back to local copy
            print(f"  could not read wishlist from origin/main ({exc}); trying local disk")
    if WISHLIST_LOCAL.exists():
        return json.loads(WISHLIST_LOCAL.read_text(encoding="utf-8-sig"))
    return None


def upload(token: str, rel: str, blob: bytes, message: str) -> str:
    """Create or update one file on origin/main via the Contents API. Returns commit sha."""
    if len(blob) > CONTENTS_LIMIT_MB * 1_000_000:
        raise RuntimeError(f"{rel} is {len(blob)/1_000_000:.0f} MB, past the contents API limit")
    encoded = base64.b64encode(blob).decode("ascii")
    try:
        existing = _api("GET", f"repos/{REPO}/contents/{quote(rel)}", token)
        sha = existing.get("sha")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        sha = None  # new file
    body = {"message": message, "content": encoded}
    if sha:
        body["sha"] = sha
    result = _api("PUT", f"repos/{REPO}/contents/{quote(rel)}", token, body)
    return str(result["commit"]["sha"])


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    token = gh_token()
    if not token:
        print("no GitHub token available (set GH_TOKEN or run `gh auth login`); cannot "
              "read the cloud wishlist or push PDFs. Aborting.")
        return 1

    wish = fetch_wishlist(token)
    if wish is None:
        print("no poster wishlist available (remote or local) - the cloud run may not "
              "have published it yet, or today produced no poster. Nothing to do.")
        return 0
    # New format: {"run_date": "...", "papers": [...]}. Older format was a bare list.
    if isinstance(wish, dict):
        papers = wish.get("papers", []) or []
    else:
        papers = wish or []
    # Always target the cloud's authoritative run date, not this machine's clock (which
    # can drift). The cloud stamps run_date into the wishlist for exactly this reason.
    date_str = resolve_target_date(wish)
    if not papers:
        print("wishlist is empty - no poster to fetch.")
        return 0

    if not _should_act_on_wishlist(date_str):
        today, yesterday = _beijing_date(0), _beijing_date(-1)
        print(f"wishlist run_date {date_str} is neither today ({today}) nor yesterday "
              f"({yesterday}); the cloud run has not published today's poster wishlist "
              f"yet. Nothing to do (retrying on the next scheduled run).")
        return 0

    out_dir = LOCAL_ROOT / "research_briefs" / "attachments" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Idempotency: if every poster PDF for this date was already fetched and verified,
    # do not re-download or re-push on the next scheduled run.
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if old.get("date") == date_str and not any(
                    not (out_dir / f["name"]).exists() for f in old.get("files", [])):
                print(f"already synced {len(old.get('files', []))} poster PDF(s) for "
                      f"{date_str}; nothing to do")
                return 0
        except Exception:
            pass

    files: list[dict] = []
    for idx, entry in enumerate(papers[:2]):
        doi = (entry.get("doi") or "").strip()
        title = (entry.get("title") or "").strip()
        venue = (entry.get("venue") or "").strip()
        if not doi:
            print(f"  [{idx + 1}] skipped: no DOI")
            continue
        if not fl.doi_exists(doi):
            print(f"  [{idx + 1}] SKIPPED {doi}: Crossref does not know this DOI "
                  f"(often reassembled from a masked log line)")
            continue
        if not venue:
            venue = fl.crossref_journal(doi)
        path, note = fl.try_routes(doi, title, venue, out_dir)
        if path:
            label = f"{idx + 1}_{fl.slug(title, 40)}"
            files.append({
                "label": label,
                "name": path.name,
                "doi": doi,
                "kind": "fulltext",
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            })
            print(f"  [{idx + 1}] OK {doi} -> {path.name} [{note}]")
        else:
            print(f"  [{idx + 1}] FAILED {doi}: {note}")

    if not files:
        print("no poster PDF fetched; not pushing an empty folder.")
        return 0

    manifest = {"date": date_str, "files": files}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote manifest with {len(files)} file(s) for {date_str}")

    # Push the manifest and each PDF to origin/main via the Contents API.
    folder = f"research_briefs/attachments/{date_str}"
    pushed = 0
    try:
        blob = (out_dir / "manifest.json").read_bytes()
        upload(token, f"{folder}/manifest.json", blob,
               f"Add poster manifest for {date_str}")
        pushed += 1
        for f in files:
            p = out_dir / f["name"]
            if not p.exists():
                print(f"  warning: {f['name']} missing on disk; skipped")
                continue
            blob = p.read_bytes()
            upload(token, f"{folder}/{f['name']}", blob,
                   f"Add poster PDF {f['name']} for {date_str}")
            pushed += 1
        print(f"pushed {pushed} file(s) for {date_str} to origin/main")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"push failed after {pushed} file(s): {exc}")
        print(f"PDFs are ready in {out_dir} but could not be pushed to the repo; "
              f"the cloud email for {date_str} will not carry them.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
