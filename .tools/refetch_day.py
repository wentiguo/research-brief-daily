"""Re-fetch one day's full texts locally, for the mail the cloud could not attach to.

Why this exists: the cloud mail only reuses a hand-over that sits under
`research_briefs/attachments/<run_date>/`, and `run_date` is today - there is no
`--date` switch on `generate_research_brief.py`. Only a local run, on a machine that
holds the publisher's session, can fill that folder. Re-fetching is mandatory, not a
copy: the hand-over must be dated for the day that is being mailed, and a folder that
borrowed yesterday's bytes would be a date lie on top of a provenance one.

The article list is read from the day's **committed archive**,
`reference_push_archive/<week>/<date>/papers.json`, which is what the cloud run wrote
when it picked those papers. Anything else is guesswork: the previous day's citation
card describes a different mailing list, and attaching its files to today's mail would
break the one promise the citation cards exist to keep - that an attachment belongs to
the paper in front of the reader.

`--fresh` empties the target folder first. Two days must never share a folder: reuse
offers whatever the manifest declares, so a mixed folder mails one paper's full text
under another paper's citation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "automation" / "research_brief_config.json"
CARD_RE = re.compile(r"^\[(\d+)\]\s+(.+)$")
DOI_RE = re.compile(r"^DOI[：:]\s*(\S+)", re.M)


def papers_from_archive(path: Path) -> list[dict[str, str]]:
    """The day's picked articles, the way the cloud run archived them."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload if isinstance(payload, list) else (payload.get("papers") or [])
    return [{"title": str(item.get("title", "")), "doi": str(item.get("doi", "")),
             "venue": str(item.get("venue", ""))} for item in items if item.get("doi")]


def papers_from_card(card_path: Path) -> list[dict[str, str]]:
    """Fallback input: the day's citation card, with the article title and DOI per entry."""
    text = card_path.read_text(encoding="utf-8", errors="replace")
    papers: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        head = CARD_RE.match(raw.strip())
        if head:
            if current:
                papers.append(current)
            current = {"title": head.group(2).strip(), "doi": "", "venue": ""}
            continue
        if current is None:
            continue
        if raw.strip().startswith("期刊/来源："):
            current["venue"] = raw.split("：", 1)[1].strip()
        match = DOI_RE.search(raw)
        if match and not current["doi"]:
            current["doi"] = match.group(1).strip()
    if current:
        papers.append(current)
    return [paper for paper in papers if paper.get("doi")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", required=True,
                        help="the day's papers.json (archive) or citation card")
    parser.add_argument("--date", required=True, help="the folder to fill, YYYY-MM-DD")
    parser.add_argument("--canonical", help="manifest.json of the shas that may ship")
    parser.add_argument("--fresh", action="store_true",
                        help="empty the target folder before fetching")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    source = Path(args.papers)
    papers = (papers_from_archive(source) if source.suffix == ".json"
              else papers_from_card(source))
    print(f"{len(papers)} article(s) from {source.name}")
    for paper in papers:
        print(f"  {paper['doi']}  {paper['title'][:64]}")

    options = config.get("paper_attachments") or {}
    out_dir = Path(str(options.get("dir", "research_briefs/attachments"))) / args.date
    if args.fresh and out_dir.exists():
        # Moved, never deleted: a stale hand-over is evidence, and an empty target
        # folder is what makes this run's manifest the only one the cloud can read.
        stale = ROOT / ".tools" / "tmp" / f"stale_{args.date}"
        shutil.move(str(out_dir), str(stale))
        print(f"moved the previous hand-over out of the way: {stale}")
    out_dir.mkdir(parents=True, exist_ok=True)

    from fetch_paper_attachments import _content_digest, fetch_paper_attachments  # noqa: PLC0415

    run_date = dt.date.fromisoformat(args.date)
    files, _cards, _note = fetch_paper_attachments(papers, run_date, config)
    allowed = set()
    if args.canonical:
        payload = json.loads(Path(args.canonical).read_text(encoding="utf-8-sig"))
        allowed = {str(entry.get("sha256")) for entry in payload.get("files") or []}
        print(f"canonical shas from {Path(args.canonical).name}: {len(allowed)}")
    print(f"\n{len(files)} file(s) in {out_dir}")
    for label, path in files:
        digest = _content_digest(path)
        flag = "" if not allowed or digest in allowed else "   <-- NOT canonical"
        print(f"  {label:<28} {path.name[:56]:<56} {digest[:12]}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
