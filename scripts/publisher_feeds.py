#!/usr/bin/env python3
"""Publisher-hosted public feeds for closed-access (non-open) physics journals.

Why this module exists
----------------------
Crossref and OpenAlex happily index subscription journals, so a PRL/PRB paper
*is* discoverable through them.  The problem is the **abstract**:

    APS does not deposit abstracts for its subscription journals
    (PRL, PRB, PRApplied, PRMaterials, PRFluids, RMP).
    Verified: Crossref abstract_chars = 0, OpenAlex abstract_inverted_index = None
    for recent PRL/PRB articles, while PRX / PRResearch (fully open) do carry them.

Without an abstract a "priority journal" entry degrades into a title-only row and
can never pass the strict-interest filter.

Publishers do syndicate their own table-of-contents feeds for personal and
research use, and those feeds carry the title, authors, DOI, section and the
**publicly displayed abstract** of each article.

    APS      https://feeds.aps.org/rss/recent/<slug>.xml    (RSS 1.0 / RDF)
    Nature   https://www.nature.com/<journal>.rss           (RSS 1.0 / RDF)
    Science  https://www.science.org/action/showFeed?...    (RSS 1.0 / RDF)

Scope / compliance boundary
---------------------------
Only publicly syndicated metadata and the publicly displayed abstract are read.
No paywall, login, institutional proxy, CAPTCHA or full text is ever touched, and
no PDF is downloaded for a closed-access article.  The feed itself declares
"Personal use only" - which is exactly the use here (private literature alert).

Feed membership differs from the OA-status of a single article: APS journals are
hybrid, so an occasional article inside a subscription journal is open.  The
`open_access` flag below therefore describes the *journal policy*, and any
article that is independently reachable through Unpaywall/OpenAlex keeps its own
OA flag from that source.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

APP_NAME = "research-brief-daily/1.0"

# APS journals that are fully open access even though they publish "Physical Review" titles.
APS_OPEN_ACCESS_SLUGS = {"prx", "prresearch", "prxquantum", "prxenergy", "prper"}

# Publishers' feeds carry the article's section, which is a useful relevance cue.
MAX_ABSTRACT_CHARS = 4000

CACHE_DIR = Path(__file__).resolve().parents[1] / "automation" / ".feed_cache"

# Observed failure mode: feeds.aps.org is reached through a local proxy/VPN
# (resolves into the 198.18.0.0/15 benchmarking range), and that path resets the
# TLS session when several feeds are requested back to back.  The failure is
# therefore transient and burst-shaped, so it is handled with spacing, retries
# with exponential backoff, and a disk cache that keeps a run productive even if
# a feed cannot be reached at all.
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_FEED_DELAY_SECONDS = 1.5
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600

# APS truncates the abstract inside its RSS <description> and appends a citation
# footer.  Measured on PRL 137, 140201: feed 359 chars vs public landing page 820
# chars; on PRL 137, 136603 the feed carried only the 237-char "Physics" synopsis
# while the page carried the true 1265-char abstract.  The feed is therefore used
# as the discovery index and as a fallback, while the *complete* abstract is read
# from the article's public landing page (abstracts are free at APS; only the full
# text is paywalled, and it is never touched).
FEED_FOOTER_RE = re.compile(r"\[\s*(?:Phys\.|Rev\.|PRX|PR[A-Z]|Rev\. Mod\. Phys)[^\]]*\]\s*Published[^.]*\.?\s*$")
TRUNCATION_MARKERS = ("…", "...")

# Below this the abstract cannot support the read the brief promises. The APS measurements
# above bottom out at 237 chars, and a Nature article once reached the mail with a 96-char
# citation line. A summary written from under ~300 characters is not a summary of the
# work; it is a restatement of the title, which is exactly the "亮点理由太简单" the reader
# reported. So a short abstract is treated as a missing one and completed before use.
MIN_USEFUL_ABSTRACT_CHARS = 320

APS_LANDING_TEMPLATE = "https://journals.aps.org/{slug}/abstract/{doi}"
APS_ABSTRACT_SECTION_RE = re.compile(r'<section[^>]*id="abstract-section"[^>]*>(.*?)</section>', re.S | re.I)
APS_ABSTRACT_CONTENT_RE = re.compile(r'<div[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I)


def strip_feed_footer(text: str) -> str:
    """Remove the APS citation footer and the truncation ellipsis from a feed abstract."""
    cleaned = FEED_FOOTER_RE.sub("", text or "").strip()
    for marker in TRUNCATION_MARKERS:
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].strip()
    return cleaned


def is_truncated_feed_abstract(text: str) -> bool:
    raw = text or ""
    if FEED_FOOTER_RE.search(raw):
        return True
    return raw.rstrip().endswith(TRUNCATION_MARKERS)




def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def extract_abstract(raw_markup: str) -> str:
    """Pull the public abstract out of a feed description / content:encoded blob."""
    if not raw_markup:
        return ""
    blocks = re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw_markup)
    kept: list[str] = []
    if blocks:
        for block in blocks:
            text = _clean_text(re.sub(r"(?is)<[^>]+>", " ", block))
            if not text:
                continue
            # APS prefixes the abstract with the author list; Science/Nature add rights lines.
            if re.match(r"(?i)^author\(s\)", text):
                continue
            if re.match(r"(?i)^(published by|©|doi:|copyright|see also|editor'?s suggestion)", text):
                continue
            kept.append(text)
        text = " ".join(kept)
    else:
        text = _clean_text(re.sub(r"(?is)<[^>]+>", " ", raw_markup))
        text = re.sub(r"(?i)^author\(s\)\s*:\s*", "", text)
    # Drop trailing boilerplate that some feeds append to the abstract block.
    text = re.split(r"(?i)\bPublished by the American Physical Society\b", text)[0]
    text = re.split(r"(?i)\b©\s*\d{4}", text)[0]
    text = re.split(r"(?i)\b(All rights reserved|Reprinted with permission)\b", text)[0]
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_ABSTRACT_CHARS]


def normalize_date(value: str) -> str:
    """Best-effort ISO (YYYY-MM-DD) normalization for feed date strings.

    Always returns zero-padded components so the value can be both string-compared
    (the feed-level window) and date-parsed (the main-level window) unambiguously.
    A bare "2026-10-6" from a Crossref date-parts join, or a "2026-10" month-only
    value, is normalized to "2026-10-06" / "2026-10-01". Previously a half-padded
    "2026-10-6" slipped through and was read by parsed_publication_date() as October
    1st (the day group failed to match two digits), which dropped the paper as stale.
    """
    text = _clean_text(value)
    if not text:
        return ""
    iso = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    rfc = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", text)
    if rfc:
        months = {m: i for i, m in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
        month = months.get(rfc.group(2)[:3].lower())
        if month:
            return f"{int(rfc.group(3)):04d}-{month:02d}-{int(rfc.group(1)):02d}"
    loose = re.search(r"(\d{4})-(\d{1,2})", text)
    if loose:
        return f"{int(loose.group(1)):04d}-{int(loose.group(2)):02d}-01"
    return text[:10]


def parse_feed(body: bytes, *, fallback_venue: str = "", feed_slug: str = "") -> list[dict[str, str]]:
    """Parse an RSS 1.0 / RDF feed (namespaces ignored) into flat string records."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"warning: unreadable feed XML ({exc})", file=sys.stderr)
        return []

    items = [
        node for node in root.iter()
        if _local_name(node.tag) in {"item", "entry"} and len(list(node)) > 0
    ]
    records: list[dict[str, str]] = []
    for node in items:
        fields: dict[str, str] = {}
        for child in node:
            name = _local_name(child.tag)
            text = child.text or ""
            if not text and child.attrib:
                # Atom-style <link href="..."/> and <content src="..."/>
                text = child.attrib.get("href") or child.attrib.get("src") or ""
            if name == "title":
                # Keep the inline markup so "<i>Colloquium</i>" is preserved for the stripper.
                fields[name] = text
            elif name not in fields or not fields[name]:
                fields[name] = text

        title = _clean_text(re.sub(r"(?is)<[^>]+>", " ", fields.get("title", "")))
        if not title:
            continue

        abstract = extract_abstract(fields.get("encoded") or fields.get("content") or "")
        if not abstract:
            abstract = extract_abstract(fields.get("description") or fields.get("summary") or "")
        feed_abstract_truncated = is_truncated_feed_abstract(abstract)
        abstract = strip_feed_footer(abstract)

        doi = _clean_text(fields.get("doi") or "")
        if not doi:
            identifier = _clean_text(fields.get("identifier") or "")
            doi = identifier[4:] if identifier.lower().startswith("doi:") else ""
        doi = doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()

        records.append({
            "title": title,
            "abstract": abstract,
            "abstract_truncated": "1" if feed_abstract_truncated else "0",
            "feed_slug": feed_slug,
            "authors": _clean_text(fields.get("creator") or fields.get("author") or ""),
            "doi": doi,
            "venue": _clean_text(fields.get("publicationname") or fields.get("publicationtitle") or fallback_venue),
            "published": normalize_date(fields.get("publicationdate") or fields.get("date") or fields.get("coverdate") or ""),
            "url": _clean_text(fields.get("url") or fields.get("link") or fields.get("id") or ""),
            "section": _clean_text(fields.get("section") or fields.get("subject") or ""),
            "volume": _clean_text(fields.get("volume") or ""),
            "issue": _clean_text(fields.get("number") or fields.get("issue") or ""),
            "start_page": _clean_text(fields.get("startingpage") or fields.get("page") or ""),
            "citation": _clean_text(fields.get("source") or ""),
        })
    return records


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".xml")


def http_bytes(
    url: str,
    *,
    user_agent: str,
    timeout: int = 35,
    retries: int = DEFAULT_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> tuple[bytes | None, str]:
    """Fetch a feed with cache, retry/backoff and stale-cache fallback.

    Returns (payload, status) where status is one of:
      "cache" / "fresh" / "stale-cache" / "failed"
    """
    cache_file = _cache_path(url)
    cached: bytes | None = None
    if cache_file.exists():
        try:
            cached = cache_file.read_bytes()
            if time.time() - cache_file.stat().st_mtime < cache_ttl_seconds and cached:
                return cached, "cache"
        except OSError:
            cached = None

    last_error: Exception | None = None
    for attempt in range(1, max(retries, 1) + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            if payload:
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(payload)
                except OSError as exc:
                    print(f"warning: could not cache feed {url}: {exc}", file=sys.stderr)
                return payload, "fresh"
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 4xx means the resource will not appear on a retry; only 429/5xx are transient.
            if exc.code < 500 and exc.code != 429:
                break
            if attempt < retries:
                wait = backoff_seconds * attempt + random.uniform(0.0, 0.6)
                print(f"note: feed attempt {attempt}/{retries} failed for {url} ({exc}); retrying in {wait:.1f}s",
                      file=sys.stderr)
                time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            # Connection resets through a local proxy/VPN land here and are worth retrying.
            last_error = exc
            if attempt < retries:
                wait = backoff_seconds * attempt + random.uniform(0.0, 0.6)
                print(f"note: feed attempt {attempt}/{retries} failed for {url} ({exc}); retrying in {wait:.1f}s",
                      file=sys.stderr)
                time.sleep(wait)

    if cached:
        print(f"warning: using stale cached feed for {url} (last error: {last_error})", file=sys.stderr)
        return cached, "stale-cache"
    print(f"warning: publisher feed unavailable {url}: {last_error}", file=sys.stderr)
    return None, "failed"


def _feed_settings(config: dict[str, Any]) -> dict[str, Any]:
    block = config.get("publisher_feeds") or {}
    return {
        "timeout": int(block.get("timeout_seconds", 35)),
        "retries": int(block.get("retries", DEFAULT_RETRIES)),
        "backoff": float(block.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)),
        "delay": float(block.get("feed_delay_seconds", DEFAULT_FEED_DELAY_SECONDS)),
        "cache_ttl": int(block.get("cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS)),
    }


def _record_health(health: list[dict[str, str]] | None, label: str, status: str) -> None:
    if health is not None:
        health.append({"feed": label, "status": status})


def _paper_from_feed_record(record: dict[str, str], *, source: str, open_access: bool) -> dict[str, Any]:
    subjects = ", ".join(x for x in (record.get("section", ""), record.get("citation", "")) if x)
    return {
        "title": record["title"],
        "authors": record.get("authors", ""),
        "affiliations": "",
        "venue": record.get("venue", ""),
        "source": source,
        "published": record.get("published", ""),
        "doi": record.get("doi", ""),
        "url": record.get("url", "") or (f"https://doi.org/{record['doi']}" if record.get("doi") else ""),
        "abstract": record.get("abstract", ""),
        "abstract_source": "publisher feed" if record.get("abstract") else "",
        "subjects": subjects,
        "publisher_section": record.get("section", ""),
        "volume": record.get("volume", ""),
        "issue": record.get("issue", ""),
        "start_page": record.get("start_page", ""),
        "open_access": open_access,
        "feed_source": source,
        "feed_slug": record.get("feed_slug", ""),
        "abstract_truncated": record.get("abstract_truncated") == "1",
    }


def _within_window(published: str, cutoff: str) -> bool:
    if not published:
        return True
    return published >= cutoff


def _collect_feed(
    *,
    url: str,
    venue: str,
    label: str,
    source: str,
    open_access: bool,
    cutoff: str,
    user_agent: str,
    settings: dict[str, Any],
    health: list[dict[str, str]] | None,
    papers: list[dict[str, Any]],
    feed_slug: str = "",
) -> None:
    payload, status = http_bytes(
        url,
        user_agent=user_agent,
        timeout=settings["timeout"],
        retries=settings["retries"],
        backoff_seconds=settings["backoff"],
        cache_ttl_seconds=settings["cache_ttl"],
    )
    _record_health(health, label, status)
    if not payload:
        return
    records = parse_feed(payload, fallback_venue=venue, feed_slug=feed_slug)
    kept = 0
    for record in records:
        if not _within_window(record.get("published", ""), cutoff):
            continue
        if venue and not record.get("venue"):
            record["venue"] = venue
        record_oa = open_access
        if not record_oa and record.get("venue", "").lower() in _OA_VENUES:
            record_oa = True
        papers.append(_paper_from_feed_record(record, source=source, open_access=record_oa))
        kept += 1
    print(f"publisher feed: {label} -> {kept}/{len(records)} items in window ({status})")
    time.sleep(settings["delay"])


# Venue names that are fully open access and therefore flagged as such even when
# they appear inside a subscription-oriented feed family.
_OA_VENUES = {"nature communications", "science advances", "prx", "physical review x",
              "physical review research", "physical review physics education research",
              "prx quantum", "prx energy"}


def fetch_aps_feeds(
    config: dict[str, Any], *, cutoff: str, user_agent: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    block = (config.get("publisher_feeds") or {}).get("aps") or {}
    if not block.get("enabled", True):
        return []
    base = block.get("base", "https://feeds.aps.org/rss/recent/")
    journals: dict[str, str] = block.get("journals") or {}
    oa_slugs = {slug.lower() for slug in (block.get("open_access_slugs") or APS_OPEN_ACCESS_SLUGS)}
    settings = _feed_settings(config)

    papers: list[dict[str, Any]] = []
    for slug, venue in journals.items():
        _collect_feed(
            url=base + slug + ".xml",
            venue=venue,
            label=f"APS {slug}",
            source="APS feed",
            open_access=slug.lower() in oa_slugs,
            cutoff=cutoff,
            user_agent=user_agent,
            settings=settings,
            health=health,
            papers=papers,
            feed_slug=slug.lower(),
        )
    return papers


def fetch_nature_feeds(
    config: dict[str, Any], *, cutoff: str, user_agent: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    block = (config.get("publisher_feeds") or {}).get("nature") or {}
    if not block.get("enabled", True):
        return []
    feeds: dict[str, str] = block.get("feeds") or {}
    oa_venues = {v.lower() for v in (block.get("open_access_venues") or [])}
    settings = _feed_settings(config)

    papers: list[dict[str, Any]] = []
    for url, venue in feeds.items():
        _collect_feed(
            url=url,
            venue=venue,
            label=f"Nature {venue}",
            source="Nature feed",
            open_access=venue.lower() in oa_venues,
            cutoff=cutoff,
            user_agent=user_agent,
            settings=settings,
            health=health,
            papers=papers,
        )
    return papers


def fetch_science_feeds(
    config: dict[str, Any], *, cutoff: str, user_agent: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    block = (config.get("publisher_feeds") or {}).get("science") or {}
    if not block.get("enabled", True):
        return []
    feeds: dict[str, str] = block.get("feeds") or {}
    oa_venues = {v.lower() for v in (block.get("open_access_venues") or [])}
    settings = _feed_settings(config)

    papers: list[dict[str, Any]] = []
    for url, venue in feeds.items():
        _collect_feed(
            url=url,
            venue=venue,
            label=f"Science {venue}",
            source="Science feed",
            open_access=venue.lower() in oa_venues,
            cutoff=cutoff,
            user_agent=user_agent,
            settings=settings,
            health=health,
            papers=papers,
        )
    return papers


def extract_aps_page_abstract(page: str) -> str:
    """Read the abstract out of an APS article landing page.

    APS serves article abstracts for free; only the full text is paywalled.  This
    function reads the `#abstract-section` block and nothing else - no PDF, no
    supplementary material, no body text.
    """
    if not page:
        return ""
    section = APS_ABSTRACT_SECTION_RE.search(page)
    body = section.group(1) if section else ""
    if not body:
        return ""
    content = APS_ABSTRACT_CONTENT_RE.search(body)
    fragment = content.group(1) if content else body
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    text = re.sub(r"(?is)</?(?:p|div|br|li|h[1-6])[^>]*>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = _clean_text(text)
    text = re.sub(r"(?i)^(abstract)\b[\s:]*", "", text).strip()
    return text[:MAX_ABSTRACT_CHARS]


def fetch_aps_page_abstract(doi: str, slug: str, *, user_agent: str, settings: dict[str, Any]) -> str:
    if not doi or not slug:
        return ""
    url = APS_LANDING_TEMPLATE.format(slug=slug, doi=doi)
    payload, _status = http_bytes(
        url,
        user_agent=user_agent,
        timeout=settings["timeout"],
        retries=max(settings["retries"], 2),
        backoff_seconds=settings["backoff"],
        cache_ttl_seconds=settings["cache_ttl"],
    )
    if not payload:
        return ""
    return extract_aps_page_abstract(payload.decode("utf-8", errors="replace"))


# The generic counterpart of `extract_aps_page_abstract`. APS renders one stable
# `#abstract-section`, and the brief used to read nothing but that block, so a Nature or
# Science article arrived in the mail with the publisher's citation line - journal name,
# publication date, DOI - wearing the title of an abstract. The LLM then had to write an
# "innovation" summary from a DOI string, and every such summary began with the same
# honest-but-pointless phrase: the full text has to be read to confirm anything.
# These patterns cover the abstract block of the venues the allowlist tracks (Nature
# family, Science family, Springer/Elsevier article pages).
_ABS_ID_RE = re.compile(r'(?is)<[^>]*?id="(Abs\d*|abstract|-abstract)"[^>]*>(.*?)</(?:section|div)>')
_ABS_CONTENT_RE = re.compile(r'(?is)<[^>]*?id="(?:Abs\d*-?content?|abstract-content)"[^>]*>(.*?)</div>')
_ABS_META_KEYS = ("dc.description", "description", "og:description", "citation_abstract")


def extract_landing_abstract(page: str) -> str:
    """Read the abstract out of any publisher article page.

    The abstract block is searched by id first (Nature's `Abs1-content`, Elsevier's
    `-abstract`), then by the descriptive metadata. Only the abstract is taken: a
    publisher page also carries the whole body, and feeding the body in would make the
    mail claim to have read more than it has.
    """
    if not page:
        return ""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    for key in _ABS_META_KEYS:
        match = re.search(r'(?is)<meta[^>]+name="%s"[^>]+content="([^"]+)"' % re.escape(key), body)
        if not match:
            match = re.search(r'(?is)<meta[^>]+content="([^"]+)"[^>]+name="%s"' % re.escape(key), body)
        if match:
            text = _clean_text(match.group(1))
            if len(text) > 80:
                return text[:MAX_ABSTRACT_CHARS]
    # `id="Abs1-content"` (Nature) names the block directly; `id="Abs1"` names the section
    # around it. Both readers take the widest fragment they can find, but neither may walk
    # into the article body: an abstract is ~1500 chars, a full text is a hundred times
    # that, and the brief only ever claims to have read the abstract.
    def _read(fragment: str) -> str:
        text = re.sub(r"(?is)</?(?:p|div|br|li|h[1-6])[^>]*>", " ", fragment)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = _clean_text(text)
        text = re.sub(r"(?i)^abstract\b[\s:]*", "", text).strip()
        # The block may run into the section's own boilerplate (rights, "Published online");
        # cut there so the abstract does not end with the publisher's footer.
        return re.split(r"(?i)\b(©|Published online|Published by|All rights reserved)\b", text)[0]

    best = ""
    for match in _ABS_CONTENT_RE.finditer(body):
        text = _read(match.group(1))
        if len(text) > len(best):
            best = text
        if len(best) >= 200:
            break
    if len(best) < 200:
        for match in _ABS_ID_RE.finditer(body):
            text = _read(match.group(2))
            if len(text) > len(best):
                best = text
            if len(best) >= 200:
                break
    return best[:MAX_ABSTRACT_CHARS]


def fetch_landing_abstract(doi: str, *, user_agent: str, settings: dict[str, Any]) -> str:
    """The complete public abstract from whatever the DOI resolves to."""
    if not doi:
        return ""
    payload, _status = http_bytes(
        "https://doi.org/" + urllib.parse.quote(doi, safe=""),
        user_agent=user_agent,
        timeout=settings["timeout"],
        retries=max(settings["retries"], 2),
        backoff_seconds=settings["backoff"],
        cache_ttl_seconds=settings["cache_ttl"],
    )
    if not payload:
        return ""
    return extract_landing_abstract(payload.decode("utf-8", errors="replace"))


def crossref_abstract_by_doi(doi: str, *, user_agent: str, settings: dict[str, Any]) -> str:
    """Cheap JSON lookup for a complete abstract; the publisher page is the fallback.

    Coverage is partial for APS subscription titles (measured 4/25 = 16% on a recent
    PRL sample), so this is an optimisation and a resilience path, not the primary source.
    """
    if not doi:
        return ""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    payload, _status = http_bytes(
        url,
        user_agent=user_agent,
        timeout=settings["timeout"],
        retries=2,
        backoff_seconds=settings["backoff"],
        cache_ttl_seconds=settings["cache_ttl"],
    )
    if not payload:
        return ""
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ""
    raw = (data.get("message") or {}).get("abstract") or ""
    return extract_abstract(raw) or strip_feed_footer(_clean_text(re.sub(r"<[^>]+>", " ", raw)))


def complete_truncated_abstracts(
    papers: list[dict[str, Any]], config: dict[str, Any], *, contact: str,
    max_pages: int = 40,
) -> int:
    """Replace truncated or missing publisher-feed abstracts with the complete public one.

    Order: Crossref (cheap, but only ~16% coverage for PRL) then the article's public
    abstract page (complete, one request each).

    Which papers get revisited used to be " APS feed items flagged `abstract_truncated`".
    That gate read as a fidelity rule but behaved like a hole: the flag is set by the APS
    feed parser, so every other source - the Nature family, Science, Crossref metadata,
    PubMed - could never be completed, and a Nature article whose feed carried only the
    citation line ("Nature Communications, Published online: 03 October 2026; doi:...")
    reached the mailbox with no abstract at all. The LLM was then asked for an
    "创新点" summary and could only answer in circles, so the brief's headline paper read
    like a placeholder. The question to answer is not "which feed gave us this" but
    "does the abstract we hold actually describe the paper", so the gate is now the
    abstract itself: missing, footer-truncated, or too short to analyse.
    """
    block = config.get("publisher_feeds") or {}
    if not block.get("complete_abstracts", True):
        return 0
    settings = _feed_settings(config)
    user_agent = f"{APP_NAME} (mailto:{contact})"
    completed = 0
    attempts = 0
    for paper in papers:
        current = paper.get("abstract", "") or ""
        if len(current) >= MIN_USEFUL_ABSTRACT_CHARS and not is_truncated_feed_abstract(current):
            continue
        doi = paper.get("doi", "")
        if not doi or attempts >= max_pages:
            continue
        attempts += 1
        full = crossref_abstract_by_doi(doi, user_agent=user_agent, settings=settings)
        if len(full) < MIN_USEFUL_ABSTRACT_CHARS:
            full = fetch_landing_abstract(doi, user_agent=user_agent, settings=settings)
        aps_slug = paper.get("feed_slug", "")
        if aps_slug and str(paper.get("feed_source", "")).lower() == "aps feed":
            if len(full) < MIN_USEFUL_ABSTRACT_CHARS:
                full = fetch_aps_page_abstract(doi, aps_slug, user_agent=user_agent, settings=settings)
        if len(full) > len(current):
            paper["abstract"] = full
            paper["abstract_source"] = "publisher page"
            paper["abstract_truncated"] = False
            completed += 1
        time.sleep(settings["delay"])
    if attempts:
        print(f"completed {completed}/{attempts} truncated or missing publisher abstracts")
    return completed


def _venue_agrees(expected: str, observed: str) -> bool:
    """True when a Crossref container-title really belongs to the journal we asked for."""
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

    want, got = norm(expected), norm(observed)
    if not want or not got:
        return False
    return want == got or want.startswith(got + " ") or got.startswith(want + " ")


def fetch_crossref_journal_feeds(
    config: dict[str, Any], *, cutoff: str, user_agent: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Cover titles whose publisher RSS blocks non-browser clients (ACS, and similar).

    ACS returned HTTP 403 for every ETOCS feed request on 2026-10-02, including with a
    browser user-agent, so the journal is queried through Crossref by ISSN instead.
    Every returned item still has to declare a container-title that matches the requested
    journal; anything else is dropped and reported, so a wrong or stale ISSN in the config
    can never smuggle an unrelated journal into the brief.
    """
    block = (config.get("publisher_feeds") or {}).get("crossref_journals") or {}
    if not block.get("enabled", True):
        return []
    rows = str(int(block.get("rows", 60)))

    import datetime as _dt
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    papers: list[dict[str, Any]] = []

    for entry in block.get("journals", []):
        name = entry.get("name", "")
        issn = entry.get("issn", "")
        label = f"Crossref {name or '(unnamed)'}"
        if not name or not issn:
            _record_health(health, label, "skipped: name/issn missing")
            continue
        url = (
            "https://api.crossref.org/journals/" + urllib.parse.quote(issn) + "/works"
            "?filter=from-pub-date:" + cutoff + ",until-pub-date:" + today
            + ",type:journal-article"
            "&sort=published&order=desc&rows=" + rows
        )
        payload, status = http_bytes(
            url, user_agent=user_agent, timeout=35,
            retries=2, backoff_seconds=2.0, cache_ttl_seconds=21600,
        )
        _record_health(health, label, status)
        if not payload:
            continue
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            print(f"publisher feed: {label} -> unreadable JSON ({exc})")
            continue
        items = (data.get("message") or {}).get("items", [])
        in_window = 0
        accepted = 0
        for item in items:
            pub = item.get("published-online") or item.get("published-print") or item.get("issued") or {}
            pieces = pub.get("date-parts") or [[]]
            published = ""
            if pieces and pieces[0]:
                # Crossref returns date-parts as raw integers (e.g. [2026, 10, 6]); joining
                # them with "-" yields "2026-10-6" (single-digit day), which
                # parsed_publication_date() later reads as October 1st and drops as stale.
                # Zero-pad every component so the value is an unambiguous ISO date.
                try:
                    nums = [int(x) for x in pieces[0] if x not in (None, "")]
                except (TypeError, ValueError):
                    nums = []
                if nums:
                    year = nums[0]
                    month = nums[1] if len(nums) > 1 else 1
                    day = nums[2] if len(nums) > 2 else 1
                    published = f"{year:04d}-{month:02d}-{day:02d}"
            if published and published < cutoff[:10]:
                continue
            container = " ".join(item.get("container-title") or [])
            in_window += 1
            if not _venue_agrees(name, container):
                continue
            accepted += 1
            authors = _clean_text("; ".join(
                " ".join(x for x in (a.get("given", ""), a.get("family", "")) if x).strip()
                for a in (item.get("author") or [])
            ))
            papers.append({
                "title": _clean_text(" ".join(item.get("title") or [])),
                "authors": authors,
                "affiliations": "",
                "venue": name,
                "source": "Crossref journal",
                "published": published,
                "doi": _clean_text(item.get("DOI", "")),
                "url": _clean_text(item.get("URL", "")) or (f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else ""),
                "abstract": extract_abstract(item.get("abstract") or ""),
                "abstract_source": "publisher feed" if item.get("abstract") else "",
                "subjects": ", ".join(item.get("subject", [])[:8]) if isinstance(item.get("subject"), list) else "",
                "publisher_section": "",
                "volume": str(item.get("volume", "") or ""),
                "issue": "",
                "start_page": str(item.get("page", "") or ""),
                "open_access": None,
                "feed_source": "crossref journal",
                "feed_slug": "",
                "abstract_truncated": False,
            })
        print(f"publisher feed: {label} -> {accepted}/{in_window} items matched ({status})")
        time.sleep(1.0)
    return papers


def _openalex_abstract(work: dict[str, Any]) -> str:
    """OpenAlex serves the abstract as an inverted index; put the sentence back together."""
    index = work.get("abstract_inverted_index")
    if not isinstance(index, dict) or not index:
        return ""
    positions: dict[int, str] = {}
    for token, slots in index.items():
        for slot in slots or []:
            if isinstance(slot, int):
                positions[slot] = token
    if not positions:
        return ""
    return _clean_text(" ".join(positions[key] for key in sorted(positions)))


def fetch_openalex_keyword_search(
    config: dict[str, Any], *, cutoff: str, user_agent: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Search by research topic across every journal at once, not one title at a time.

    The four feeds above are tables of contents: they walk a fixed list of titles and
    read whatever that publisher has just put online. What they cannot do is the thing a
    keyword search does - find me anything published in the last two days that talks
    about the reader's topic, in whichever journal it appeared. That gap is what the reader
    notices first. On 2026-10-03 all seven pushed papers came from one Nature
    Communications table of contents, three of them about AI and virology, because a
    table of contents carries its own journal's decisions about what is new and knows
    nothing about the reader's direction.

    OpenAlex answers it through a documented API that welcomes programmatic use and no
    key, verified 2026-10-04. It hands the abstract over as an inverted index, so the
    abstract is rebuilt from that index rather than left empty - an entry with no
    abstract degrades into a title-only row and can never pass the strict-interest
    filter, which is exactly what made the keyword search sound useless until it worked.
    """
    block = (config.get("publisher_feeds") or {}).get("openalex_keyword_search") or {}
    if not block.get("enabled", True):
        return []
    import datetime as _dt
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    profile = config.get("research_profile") or {}
    terms = block.get("terms") or profile.get("priority_topics") or []
    per_page = str(int(block.get("per_page", 10)))
    if not terms:
        print("openalex search: no terms configured; skipping")
        return []

    papers: list[dict[str, Any]] = []
    seen_doi: set[str] = set()
    for term in terms:
        label = f"OpenAlex “{term}”"
        phrase = f'"{term}"' if " " in str(term) else str(term)
        query = urllib.parse.quote(phrase)
        # `title_and_abstract.search` is a filter field, not a bare query parameter:
        # `?title_and_abstract.search=x` is answered with HTTP 400. Passing it as a
        # filter also narrows the match to the title and abstract, where a bare `search`
        # would extend into the reference list and hand back a paper that merely cites
        # the topic. Multi-word terms are quoted so the phrase is matched as a unit
        # rather than as loose parts.
        url = (
            "https://api.openalex.org/works?filter=title_and_abstract.search:" + query
            + ",type:article,from_publication_date:" + cutoff
            + "&sort=publication_date:desc&per-page=" + per_page
        )
        payload, status = http_bytes(
            url, user_agent=user_agent, timeout=35,
            retries=2, backoff_seconds=2.0, cache_ttl_seconds=21600,
        )
        _record_health(health, label, status)
        if not payload:
            continue
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            print(f"openalex search: {term} -> unreadable JSON ({exc})")
            continue
        kept = 0
        for work in data.get("results") or []:
            published = str(work.get("publication_date") or "")
            if not published or published < cutoff or published > today:
                continue
            doi = _clean_text(str(work.get("doi") or "").replace("https://doi.org/", ""))
            if not doi or doi in seen_doi:
                continue
            seen_doi.add(doi)
            location = work.get("primary_location") or {}
            source = (location.get("source") or {}) if isinstance(location, dict) else {}
            venue = _clean_text(str(source.get("display_name") or "")) if isinstance(source, dict) else ""
            authors = _clean_text("; ".join(
                str(((a or {}).get("author") or {}).get("display_name", "")).strip()
                for a in (work.get("authorships") or [])
            ))
            papers.append({
                "title": _clean_text(str(work.get("title") or work.get("display_name") or "")),
                "authors": authors,
                "affiliations": "",
                "venue": venue,
                "source": "OpenAlex keyword",
                "published": published,
                "doi": doi,
                "url": f"https://doi.org/{doi}",
                "abstract": _openalex_abstract(work),
                "abstract_source": "OpenAlex" if _openalex_abstract(work) else "",
                "subjects": ", ".join(
                    str((c or {}).get("display_name", "")) for c in (work.get("concepts") or [])[:6]
                    if isinstance(c, dict) and c.get("display_name")
                ),
                "publisher_section": "",
                "volume": "",
                "issue": "",
                "start_page": "",
                "open_access": (work.get("open_access") or {}).get("is_oa")
                    if isinstance(work.get("open_access"), dict) else None,
                "feed_source": "openalex keyword",
                "feed_slug": "",
                "abstract_truncated": False,
            })
            kept += 1
        print(f"openalex search: {label} -> {kept} items ({status})")
        time.sleep(1.0)
    return papers


def fetch_publisher_feeds(
    config: dict[str, Any], days_back: int, *, contact: str,
    health: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Collect closed-access journal metadata + public abstracts from publisher ToC feeds."""
    block = config.get("publisher_feeds") or {}
    if not block.get("enabled", True):
        print("publisher feeds disabled by config")
        return []

    import datetime as dt
    # Feed-level window: keep a paper whose `published` date falls within
    # (days_back + tolerance) days. The tolerance compensates for publisher feeds
    # that lag the real publication date by a day or two (observed for PRB and for
    # Crossref journal records), so genuinely recent papers are not pruned before
    # the precise date gate in filter_recent_publications() ever evaluates them.
    try:
        tolerance = int(config.get("published_window_tolerance_days", 1) or 1)
    except (TypeError, ValueError):
        tolerance = 1
    cutoff = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days_back + tolerance)).isoformat()
    user_agent = f"{APP_NAME} (mailto:{contact})"

    papers: list[dict[str, Any]] = []
    papers.extend(fetch_aps_feeds(config, cutoff=cutoff, user_agent=user_agent, health=health))
    papers.extend(fetch_nature_feeds(config, cutoff=cutoff, user_agent=user_agent, health=health))
    papers.extend(fetch_science_feeds(config, cutoff=cutoff, user_agent=user_agent, health=health))
    papers.extend(fetch_crossref_journal_feeds(config, cutoff=cutoff, user_agent=user_agent, health=health))
    papers.extend(fetch_openalex_keyword_search(config, cutoff=cutoff, user_agent=user_agent, health=health))
    return papers

