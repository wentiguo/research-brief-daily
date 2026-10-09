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

Run by the daily local automation (see the recurring WorkBuddy automation). Safe to run
by hand too.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
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
LOG_PATH = LOCAL_ROOT / ".tools" / "local_bridge.log"
LOG_KEEP_BYTES = 400_000


def _log(message: str) -> None:
    """Print and persist one line so a scheduled run leaves a trail we can read.

    A silent failure is the whole problem this bridge has had: it downloaded the PDF,
    then could not push it, and every later run said "already synced" so nobody noticed.
    """
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_KEEP_BYTES:
            tail = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-LOG_KEEP_BYTES:]
            LOG_PATH.write_text(tail, encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # logging must never break the bridge


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
    last_exc = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError:
            raise
        except Exception as exc:  # urllib.error.URLError / ssl.SSLError (sandbox TLS reset)
            last_exc = exc
            if attempt < 5:
                time.sleep(2 * (attempt + 1))
                continue
            break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{method} /{path} -> unknown transport failure")


def _gh_api(method: str, path: str, body: dict | None = None) -> Any:
    """Call the REST API through the gh CLI instead of urllib.

    Two things the urllib path lacks: gh carries its own TLS stack (a multi-megabyte
    base64 PUT from here is regularly reset mid-flight by a transport error) and it
    supplies the stored credential itself. The request body goes through stdin so a
    several-megabyte payload never lands on a command line.

    Returns None when gh is unavailable, a dict carrying "__error__" when GitHub
    refused, otherwise the decoded JSON.
    """
    if not Path(GH_EXE).exists():
        return None
    endpoint = path if path.startswith("http") else f"https://api.github.com/{path}"
    args = [GH_EXE, "api", "-X", method, endpoint,
            "-H", "Accept: application/vnd.github+json"]
    stdin_bytes = None
    if body is not None:
        args += ["--input", "-"]
        stdin_bytes = json.dumps(body).encode("utf-8")
    try:
        proc = subprocess.run(args, input=stdin_bytes, capture_output=True, timeout=300)
    except Exception as exc:  # noqa: BLE001 - gh blocked or gone; caller falls back
        return {"__error__": f"gh transport failed: {exc}"}
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:400]
        return {"__error__": detail or f"gh exited {proc.returncode}"}
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw__": raw}


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


def remote_folder_names(folder: str, token: str = "") -> dict:
    """Map filename -> size for `<folder>` on origin/main ({} when absent or unreadable).

    This is what makes the idempotency check honest. The old guard asked only whether
    the files existed *here*, so a download that failed to push looked permanently
    synced while the repository - the thing the cloud actually reads - stayed empty.
    """
    data = _gh_api("GET", f"repos/{REPO}/contents/{quote(folder)}")
    if isinstance(data, list):
        return {str(item.get("name")): int(item.get("size") or 0) for item in data}
    if data is None and token:
        try:
            listed = _api("GET", f"repos/{REPO}/contents/{quote(folder)}", token)
        except Exception:  # noqa: BLE001 - absence and failure both mean "not there yet"
            return {}
        if isinstance(listed, list):
            return {str(item.get("name")): int(item.get("size") or 0) for item in listed}
    return {}


def find_cached_pdf(doi: str, title: str, exclude_date: str = "") -> Path | None:
    """Return a PDF this machine already downloaded for this same article, if any.

    Re-downloading a full text we already have is a pointless hit on the publisher, and
    it is also the only way this bridge can serve a day whose publisher routes are
    temporarily blocked. Newest folder first, and the identity check is the same
    one `verify_pdf` applies to a fresh download.
    """
    root = LOCAL_ROOT / "research_briefs" / "attachments"
    if not root.exists() or not (doi or title):
        return None
    for day in sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda p: p.name, reverse=True):
        if day.name == exclude_date:
            continue
        for pdf in sorted(day.glob("*.pdf")):
            try:
                blob = pdf.read_bytes()[:6_000_000]
            except OSError:
                continue
            ok, _reason = fl.verify_pdf(blob, doi, title)
            if ok:
                return pdf
    return None


def fetch_wishlist(token: str) -> Any | None:
    """Read the wishlist from origin/main (preferred) and fall back to local disk."""
    node = _gh_api("GET", f"repos/{REPO}/contents/{quote(WISHLIST_REMOTE)}")
    if isinstance(node, dict) and node.get("content") and "__error__" not in node:
        try:
            return json.loads(base64.b64decode(node["content"]).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - fall through to the other transport
            pass
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


def _same_doi(left: str, right: str) -> bool:
    """Compare DOIs ignoring case, surrounding space and any https://doi.org/ form."""
    def clean(value: str) -> str:
        text = (value or "").strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text.strip()
    left_c, right_c = clean(left), clean(right)
    return bool(left_c) and left_c == right_c


def _push(token: str, folder: str, date_str: str, out_dir: Path, files: list[dict]) -> int:
    """Write manifest + every listed PDF to origin/main. Returns how many landed."""
    pushed = 0
    try:
        upload(token, f"{folder}/manifest.json", (out_dir / "manifest.json").read_bytes(),
               f"Add poster manifest for {date_str}")
        pushed += 1
        for f in files:
            target = out_dir / str(f["name"])
            if not target.exists():
                print(f"  warning: {f['name']} missing on disk; skipped")
                continue
            upload(token, f"{folder}/{f['name']}", target.read_bytes(),
                   f"Add poster PDF {f['name']} for {date_str}")
            pushed += 1
        _log(f"pushed {pushed} file(s) for {date_str} to origin/main")
        return pushed
    except Exception as exc:  # noqa: BLE001
        _log(f"PUSH FAILED for {date_str} after {pushed} file(s): {exc}")
        print(f"PDFs are ready in {out_dir} but did not reach the repository; the cloud "
              f"email for {date_str} cannot carry them until this succeeds.")
        raise


def existing_sha(api_path: str, token: str) -> str | None:
    """Blob sha of an existing remote file, None when it is not there yet.

    The update needs the current sha, otherwise the Contents API answers 422.
    """
    node = _gh_api("GET", api_path)
    if isinstance(node, dict) and node.get("sha") and "__error__" not in node:
        return str(node["sha"])
    try:
        existing = _api("GET", api_path, token)
    except urllib.error.HTTPError as exc:
        # urllib hands HTTPError straight back instead of wrapping it; a 404 is simply
        # "the file is not there yet", so it is the one benign answer here. Anything
        # else - 401 with the wrong token, 403 without write scope - must surface.
        if getattr(exc, "code", None) == 404:
            return None
        raise
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        return None
    return existing.get("sha")


def upload(token: str, rel: str, blob: bytes, message: str) -> str:
    """Create or update one file on origin/main via the Contents API. Returns commit sha."""
    if len(blob) > CONTENTS_LIMIT_MB * 1_000_000:
        raise RuntimeError(f"{rel} is {len(blob)/1_000_000:.0f} MB, past the contents API limit")
    encoded = base64.b64encode(blob).decode("ascii")
    api_path = f"repos/{REPO}/contents/{quote(rel)}"
    sha = existing_sha(api_path, token)
    body = {"message": message, "content": encoded}
    if sha:
        body["sha"] = sha

    # gh first: rolling its own TLS stack, it puts multi-megabyte bodies through here
    # while urllib gets reset mid-upload - which is exactly how a month of downloads
    # ended up sitting on this disk and never reaching the repository.
    result = _gh_api("PUT", api_path, body)
    if isinstance(result, dict) and result.get("commit"):
        return str(result["commit"]["sha"])
    gh_err = (result or {}).get("__error__") if isinstance(result, dict) else None
    try:
        written = _api("PUT", api_path, token, body)
        return str(written["commit"]["sha"])
    except Exception as exc:  # noqa: BLE001 - report both transports, the push is what failed
        both = f"gh said {gh_err!r}; urllib said {exc}" if gh_err else str(exc)
        raise RuntimeError(f"both transports failed writing {rel}: {both}")


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
        _log("no poster wishlist available (remote or local) - the cloud may not have "
             "published today's yet. Nothing to do.")
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
    folder = f"research_briefs/attachments/{date_str}"
    manifest_path = out_dir / "manifest.json"

    # Repair before anything else. A previous run may have downloaded the PDFs and then
    # failed to push them - that is what happened on 2026-10-09 - so the completion test
    # has to be made against the repository the cloud actually reads, not this disk. The
    # old local-only test answered "already synced" forever afterwards, so nothing was
    # ever retried and no trace of the failure surfaced.
    local_manifest = None
    if manifest_path.exists():
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a damaged manifest counts as none at all
            candidate = None
        if isinstance(candidate, dict) and candidate.get("date") == date_str:
            local_manifest = candidate
            wanted_names = [f for f in (candidate.get("files") or [])
                            if str(f.get("name", ""))]
            here = [f for f in wanted_names if (out_dir / str(f["name"])).exists()]
            if wanted_names and len(here) == len(wanted_names):
                remote = remote_folder_names(folder, token)
                missing = [str(f["name"]) for f in here if str(f["name"]) not in remote]
                if not missing:
                    _log(f"already synced {len(here)} poster PDF(s) for {date_str} "
                         f"on origin/main; nothing to do")
                    return 0
                _log(f"{date_str}: {len(missing)} file(s) on disk but MISSING on "
                     f"origin/main ({', '.join(missing)}) - retrying the push now")
                _push(token, folder, date_str, out_dir, here)
                return 0

    files: list[dict] = []
    for idx, entry in enumerate(papers[:2]):
        doi = (entry.get("doi") or "").strip()
        title = (entry.get("title") or "").strip()
        venue = (entry.get("venue") or "").strip()
        if not doi:
            print(f"  [{idx + 1}] skipped: no DOI")
            continue
        # Crossref only decides whether the DOI is real; it says nothing about the bytes
        # we end up with, and `verify_pdf` below inspects those. Treating a Crossref
        # hiccup as "unknown DOI" used to drop every paper of a run, so it is a warning.
        if not fl.doi_exists(doi):
            print(f"  [{idx + 1}] warning {doi}: Crossref lookup failed or does not know "
                  f"this DOI; continuing, identity will be checked on the bytes")
        if not venue:
            venue = fl.crossref_journal(doi)

        # 1. A file this run itself already fetched and could not push.
        path = None
        for prior in (local_manifest or {}).get("files") or []:
            if _same_doi(str(prior.get("doi", "")), doi):
                candidate = out_dir / str(prior.get("name", ""))
                if candidate.exists():
                    path, note = candidate, "already downloaded here"
                    break

        # 2. The same article already downloaded on a previous day. Re-fetching it is a
        #    pointless hit on the publisher, and it is how a day whose routes are
        #    blocked still gets its full text.
        if path is None:
            cached = find_cached_pdf(doi, title, exclude_date=date_str)
            if cached is not None:
                destination = out_dir / cached.name
                try:
                    if not destination.exists():
                        shutil.copy2(cached, destination)
                    blob = destination.read_bytes()
                except OSError as exc:
                    print(f"  [{idx + 1}] warning: could not reuse cached {cached.name} ({exc})")
                    blob = b""
                ok, reason = fl.verify_pdf(blob, doi, title) if blob else (False, "unreadable")
                if ok:
                    path, note = destination, f"reused local copy from {cached.parent.name}"
                else:
                    print(f"  [{idx + 1}] cached copy rejected: {reason}")

        # 3. Only then ask the publisher for it.
        if path is None:
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
        _log(f"{date_str}: no poster PDF could be fetched; not pushing an empty folder")
        return 0

    manifest = {"date": date_str, "files": files}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote manifest with {len(files)} file(s) for {date_str}")

    try:
        _push(token, folder, date_str, out_dir, files)
        return 0
    except Exception:  # noqa: BLE001 - the traceback has already been logged by _push
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
