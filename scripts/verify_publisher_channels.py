#!/usr/bin/env python3
"""Verify that closed-access journals (PRL/PRB/Nature/Science) are reachable.

Run this before trusting a change to the publisher-feed layer, or when a run reports
fewer papers than expected.  It answers three questions with numbers:

  1. Are the publisher ToC feeds reachable, and do they expose the abstract?
  2. Do Crossref / OpenAlex really lack the abstract for APS subscription titles?
  3. Is the APS feed abstract truncated, and does the public abstract page complete it?

Usage:
    python scripts/verify_publisher_channels.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from publisher_feeds import (  # noqa: E402
    _feed_settings,
    extract_aps_page_abstract,
    http_bytes,
    parse_feed,
)

CONFIG_PATH = SCRIPT_DIR.parent / "automation" / "research_brief_config.json"
UA = "research-brief-verify/1.0 (mailto:verify@example.com)"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"    (lookup failed: {exc})")
        return None


def crossref_abstract_chars(doi: str) -> int | None:
    data = get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    if not data:
        return None
    return len(data.get("message", {}).get("abstract") or "")


def openalex_abstract_words(doi: str) -> tuple[int, bool | None] | None:
    data = get_json("https://api.openalex.org/works/doi:" + doi + "?select=abstract_inverted_index,open_access")
    if not data:
        return None
    index = data.get("abstract_inverted_index")
    is_oa = (data.get("open_access") or {}).get("is_oa")
    return (len(index) if index else 0), is_oa


def main() -> int:
    config = load_config()
    settings = _feed_settings(config)
    block = config.get("publisher_feeds") or {}

    print("=" * 78)
    print("1) Publisher ToC feeds")
    print("=" * 78)
    feed_targets: list[tuple[str, str, str]] = []
    aps_base = (block.get("aps") or {}).get("base", "https://feeds.aps.org/rss/recent/")
    for slug in (block.get("aps") or {}).get("journals", {}):
        feed_targets.append((f"APS {slug}", aps_base + slug + ".xml", slug))
    for url, venue in (block.get("nature") or {}).get("feeds", {}).items():
        feed_targets.append((f"Nature {venue}", url, ""))
    for url, venue in (block.get("science") or {}).get("feeds", {}).items():
        feed_targets.append((f"Science {venue}", url, ""))

    ok = 0
    sample_slug = ""
    sample_doi = ""
    for label, url, slug in feed_targets:
        payload, status = http_bytes(url, user_agent=UA, timeout=settings["timeout"],
                                     retries=settings["retries"], backoff_seconds=settings["backoff"],
                                     cache_ttl_seconds=settings["cache_ttl"])
        if not payload:
            print(f"  [FAIL] {label:26s} {status}")
            continue
        records = parse_feed(payload, feed_slug=slug)
        with_abstract = sum(1 for r in records if r.get("abstract"))
        # `abstract_truncated` is decided on the raw feed text before the footer/ellipsis
        # are stripped, so it must be read from the record rather than re-tested here.
        truncated = sum(1 for r in records if r.get("abstract_truncated") == "1")
        print(f"  [ OK ] {label:26s} items={len(records):3d} with_abstract={with_abstract:3d} truncated={truncated:3d}")
        ok += 1
        if slug == "prl" and records and not sample_doi:
            sample_doi = records[0].get("doi", "")
            sample_slug = slug
    print(f"  -> {ok}/{len(feed_targets)} feeds reachable")

    if not sample_slug:
        print("\nPRL feed unreachable; cannot compare channels.")
        return 1

    print()
    print("=" * 78)
    print("2) Abstract availability by channel, for a fresh PRL article")
    print(f"   DOI {sample_doi}")
    print("=" * 78)
    print(f"  Crossref  abstract_chars = {crossref_abstract_chars(sample_doi)}")
    oa = openalex_abstract_words(sample_doi)
    print(f"  OpenAlex  abstract_words = {oa[0] if oa else None} (is_oa={oa[1] if oa else None})")

    feed_payload, _ = http_bytes(aps_base + sample_slug + ".xml", user_agent=UA, timeout=settings["timeout"],
                                 retries=settings["retries"], backoff_seconds=settings["backoff"],
                                 cache_ttl_seconds=settings["cache_ttl"])
    feed_len = 0
    feed_abstract = ""
    feed_truncated = "?"
    if feed_payload:
        for record in parse_feed(feed_payload, feed_slug=sample_slug):
            if record.get("doi") == sample_doi:
                feed_abstract = record.get("abstract", "")
                feed_len = len(feed_abstract)
                feed_truncated = record.get("abstract_truncated", "?")
                break
    print(f"  APS feed  abstract_chars = {feed_len}  (truncated_in_feed={feed_truncated})")

    page_payload, _ = http_bytes(
        f"https://journals.aps.org/{sample_slug}/abstract/{sample_doi}",
        user_agent=UA, timeout=settings["timeout"], retries=settings["retries"],
        backoff_seconds=settings["backoff"], cache_ttl_seconds=settings["cache_ttl"],
    )
    page_abstract = extract_aps_page_abstract(page_payload.decode("utf-8", errors="replace")) if page_payload else ""
    print(f"  APS page  abstract_chars = {len(page_abstract)}")

    print()
    if len(page_abstract) > feed_len:
        print(f"  PASS: the public abstract page completes the truncated feed abstract "
              f"({feed_len} -> {len(page_abstract)} chars).")
        return 0
    print("  WARN: could not confirm feed truncation; rerun or inspect the article manually.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
