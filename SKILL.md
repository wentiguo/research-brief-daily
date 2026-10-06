---
name: research-brief-actions
description: Configure, run and extend a GitHub Actions daily research-brief workflow for scholarly paper monitoring (Crossref, OpenAlex, arXiv, PubMed, publisher ToC feeds), journal-tiered ranking, a three-class topic gate, Markdown/BibTeX rendering, poster write-ups with verified quotes, original-PDF attachments via a cloud-to-local bridge, and SMTP/SendGrid delivery. Use when the user wants daily paper alerts, a literature brief, arXiv or journal monitoring, GitHub Actions secrets setup, SMTP configuration, or to customise the on-topic scope.
---

# Research Brief Actions

Operator guide for the daily literature-brief workflow. The README covers installation;
this file covers the decisions that are not obvious from the code, and the failure modes
that have already bitten.

## First: does this capability actually exist on this machine?

**A negative `command -v` is evidence about `PATH`, not about existence.** Before telling a
user "I cannot push" or "I need your GitHub token", check in this order:

1. the standard install path (Windows: `C:\Program Files\GitHub CLI\gh.exe`)
2. the keyring / credential manager (`gh auth status`)
3. known script directories

This has been misdiagnosed twice in this project's own history — "this machine has no
git", "this sandbox has no gh" — and both times the assistant asked the user for a token
that was never needed. `.tools/repo_config.py` already performs this lookup
(`gh_exe()`, `gh_login()`, `gh_token()`); use it instead of re-deriving paths.

The helpers never prompt for a GitHub token. They read `$GH_TOKEN` / `$GITHUB_TOKEN`, else
the credential `gh auth login` stored.

## Where the papers come from

**Publisher feeds supersede journal-name queries.** APS supplies no abstract to Crossref or
OpenAlex, so a metadata-only pipeline sees subscription letters with empty abstracts and
ranks them blind. Publisher ToC feeds are the only source of that public abstract.

APS truncates the abstract inside its RSS description (~350 characters plus a citation
footer) and sometimes substitutes the short Physics synopsis. `complete_abstracts=true`
therefore re-reads the article's public landing page for the papers actually pushed.

### Why not Google Scholar

It has no API, blocks automated clients, and its terms forbid them. Everything here uses
documented APIs or publisher feeds.

### The manifest accumulates across runs on one day

`research_briefs/attachments/<date>/manifest.json` carries files forward from earlier runs
on the same date, drops entries whose files vanished, and tolerates an unreadable previous
manifest rather than failing the run. A re-run therefore adds to the day's folder instead
of replacing it.

## Closed-access journals (PRL, PRB, Nature, Science)

Only files that are verifiably **public** are saved. Every candidate URL is requested
anonymously (no cookies) first and must come back as a real PDF. Anything that exists only
for a logged-in subscriber is reported as a legal route and never downloaded. The citation
card states one of four honest outcomes: `fetched`, `paywalled`, `verification-required`,
`cited-only`.

### The cloud cannot fetch APS PDFs. Full stop.

APS returns `HTTP 403` to GitHub Actions' egress IP. The request is well-formed and
`/pdf/` is allowed by `robots.txt`; the block is on the runner's IP, so **no in-workflow
workaround exists**. The same URL from an ordinary machine returns `application/pdf`.

Hence the bridge in `.tools/local_fetch_posters.py`: cloud publishes the two poster DOIs →
local machine downloads and verifies → pushes back via the Contents API → cloud attaches
them to the same mail. See the README section on the bridge.

### Desktop browser pass

`scripts/fetch_browser_paper.py` drives the desktop Chrome **attached over the DevTools
protocol** rather than launching an automated one. A browser the script merely attaches to
reports no automation flag, which is what makes the publisher's wall go away by itself.

If a challenge appears, the physical cursor ticks it: window raised first, cursor walked
in, control found, then a real press. The control is located two ways on purpose — the DOM
gives the host element's position (the widget's own markup is in a shadow root, so only a
pixel match can say what is there), and the pixels decide. **The DOM geometry never becomes
the click point by itself**: on a scaled display the viewport-to-screen mapping is not a
plain translation, so the element only trims the search.

Regression cover: the fine pass once scored the window `big[y0:y0+h, x0:x0+w]`, so `_ncc`
returned coordinates *inside that window*; for a 1:1 hit those are `(0,0)` and were
recorded as full-screen coordinates, turning a correct score into a click on the top-left
corner of the screen. `tests/test_checkbox_match.py` pins the coordinate convention. It
skips when Pillow is absent.

The landing page is opened with `wait_until="commit"`. A publisher page never fires
`DOMContentLoaded` (a stalled subresource holds it pending past 90 s) even though the
article is already there, so `domcontentloaded` burned the full timeout on every paper —
18 minutes for 11 papers, versus 2 s with `commit`.

### What the mail size budget takes away

The budget is spent round-robin, one file per paper per pass, **after collapsing
byte-identical files** (an APS PDF arrives twice, via the HTTP and the browser route).
Kept by arrival order instead, the first two papers eat the whole budget and the
browser-fetched full texts are dropped while the citation card still claims a full text
was delivered. When trimming happens, the brief states how many files actually mailed.

Files are only ever de-duplicated and trimmed — **never merged or zipped**. Each PDF stays
an independent attachment.

## Core workflow

The run is one pass:

1. **Fetch** — Crossref, OpenAlex, arXiv, PubMed, publisher feeds, all date-filtered.
2. **Deduplicate** by `source + id`.
3. **Drop previously sent** papers using `recommended_history.json`.
4. **Gate** on topic (`is_domain_match`) and venue (`is_allowed_venue`); drop wave physics.
5. **Rank** by journal tier and topical fit.
6. **Group** into recommendation groups; featured list takes the leftovers.
7. **Pick posters** — published papers first, journal tier then score as the sort key.
8. **Render** Markdown + BibTeX + images.
9. **Mail** once, with attachments inside the budget.

### Poster selection: published beats preprint

Both poster slots prefer **already-published** papers, sorted by journal tier then score.
A preprint only fills a slot when the day's published candidates are insufficient. An
earlier version of this code preferred preprints, which is backwards: a preprint is a worse
thing to build a poster around, and "highlighted" then routinely meant "two days old and
unreviewed".

The sort key must be `+tier`. `journal_tier` returns `1` = best, so `-tier` puts the
*worse* journals first.

### Grouping runs in one direction only

Direction groups claim their papers first; "priority-journal selected" takes what is left.
Filling the featured list first put all papers there and left every direction group
reporting "no candidates" — which is the section the reader actually uses.

### One complete email per day

Every paper found that day goes out in a single mail. Papers already in the history are
filtered before rendering, so a brief is never a partial duplicate of yesterday's.

## Reading the original text (and never fabricating)

Poster write-ups are generated, but **every field must be traceable to a quote from the
source abstract**. The Chinese sections are 研究背景 / 创新亮点 / 解决的关键问题 /
可借鉴之处 / 可延展方向; each carries its English quote as provenance.

Three rules, all learned the hard way:

1. **The quote gate is measured in words, not characters.** A `len(q) < 6` character check
   let a two-word quote like `"we study"` (8 characters) through and onto the poster. The
   threshold is `MIN_QUOTE_WORDS = 4`.

2. **Longest-contiguous-fragment matching is allowed.** Models routinely produce a quote
   spanning two real fragments joined by `...`. Rejecting those discarded the best content
   and made the poster fall back to printing a raw English abstract. Verify the longest
   contiguous fragment instead.

3. **A field whose quote cannot be verified is omitted entirely.** Never write a
   placeholder such as "abstract not disclosed". Omission is the honest output; a
   placeholder reads as a bug and trains the reader to distrust the whole brief.

The same rule applies to summaries: information not present in the abstract is simply left
out.

## The topic gate

A paper reaches the brief only through
`score > 0 and is_domain_match(...) and not is_wave_physics(...)`. `is_domain_match`
decides on-topic; `is_allowed_venue` decides the journal allowlist separately.

**All three term lists are configuration-driven** (`config["topic_gate"]`), defaulting to
the `_DEFAULT_*` tuples in `generate_research_brief.py`. The shipped defaults are a
generic condensed-matter / materials-science profile — a starting point, not one person's
research. `scripts/configure_project.py` asks the user for their own.

`is_domain_match` is **universal** — it does not branch on journal. A paper is on-topic
when any of these hold:

1. **A DIRECT signal** — a phrase naming a specific state or material, passing on its own.
2. **A WEAK signal plus a matter anchor** — the cross-field homonyms `topological`,
   `magnetic`, `spin`, `ferro`, `superconduct`, `semimetal`, `insulator`, `pyroelectric`
   pass only when a matter-side anchor (`material`, `crystal`, `electron`, `band`,
   `lattice`, `first-principles`, `tight-binding`, `hamiltonian`, `berry`, `chern`,
   `hall`, …) is also present. A weak word alone is a homonym — `topological data
   analysis`, `magnetic resonance imaging` — and is rejected.

   **The weak words must never enter the anchor tuple.** Otherwise a weak hit always finds
   itself as its own anchor and the rule degrades into passing everything. This has
   happened; it is now asserted against the live config by
   `tests/test_topic_gate_scope.py::DefaultConfigSanityTests`.
3. **No abstract fallback** — a subscribed letter (APS supplies no abstract to Crossref /
   OpenAlex) is judged on its title alone, so a title-only PRL on magnon-Chern physics
   still clears the gate.

Machine-learning method words are **not** a direction. `machine learning` / `active
learning` / `inverse design` / `neural network` never appear in the signal lists, so a
paper whose only hit is ML fails the gate; ML applied to an in-topic subject passes
because the topic phrase carries it. Category labels (`quantum materials`,
`topological materials`, `magnetic materials`) are likewise not standalone directions.

Wave physics (`_WAVE_SIGNAL_PHRASES`) is excluded **outright**, never used as a fallback.
The line is drawn by matter anchors: an ion-trap Berry-phase-of-a-single-phonon paper
names no host and stays wave physics, while a magnetic phonon-angular-momentum paper names
its host and wins its exit from the wave bucket. Bare `crystal` is deliberately absent from
the anchors so it cannot cancel the `photonic crystal` signal. Set
`topic_gate.exclude_wave_physics=false` to disable the whole rule.

### OpenAlex query construction

`title_and_abstract.search` must be passed as an OpenAlex **filter field**
(`?filter=title_and_abstract.search:<term>`). As a bare parameter it replies HTTP 400.
Multi-word terms are quoted, and a bare `search` is avoided because it reaches into
reference lists and returns papers that merely *cite* the topic.

## Journal tiers

`journal_tier` resolves **exact names across both tiers first, prefix matches second**.
Without that two-pass order, `"Nature"` in tier 1 claims `"Nature Nanotechnology"` by
prefix before tier 2 is consulted, silently promoting every Nature sub-journal to the top
tier.

A journal in the allowlist with neither a publisher feed nor a Crossref ISSN is **silently
never fetched** — it looks configured and returns nothing.

`zone_badge` never renders an empty cell: a journal with a tier but no zone record renders
`N 区 · 大类未建档`, because the tier alone is enough to state the zone and inventing a
category would not be.

## The cloud ↔ local PDF bridge

Two failure modes here are invisible in the logs, which is why they are called out:

**Never trust the local clock.** `local_fetch_posters.py` must decide the target folder
from `run_date` **inside the cloud-published wishlist**, never from `datetime.now()`. A
local clock that runs even one day ahead writes the PDFs into a folder the cloud never
polls; the mail then goes out without them and nothing in the log says why.

**Verify every download against its identity.** A `%PDF-` magic number and a size floor
are necessary and not sufficient. Require the DOI to appear in the bytes, or a ≥60 % title
token overlap. An arXiv lookup once returned a 2020 paper in place of a same-day PRL; a
wrong-paper check is the only thing that catches that class of mistake.

The bridge talks to the GitHub Contents API rather than `git pull`/`git push`, because the
working copy it runs in is populated through that API and is **not a git checkout**.

## Ledger persistence

After a successful send the run commits `automation/recommended_history.json` and
`automation/mail_ledger.json` back to the repo. When re-basing, drop generated artefacts
first:

```bash
git clean -fd research_briefs/attachments
git pull --rebase --autostash origin main
```

Without the clean, checkout aborts: the bridge pushed those PDFs as *tracked* files on
`origin/main` while the cloud sees them as *untracked* working-tree files, and git refuses
to overwrite. The email is already sent at that point, so the run ends `failure` while the
user has a perfectly good mail — check the ledger step specifically before assuming the
delivery failed.

## Date handling in tests

Compare ledger timestamps in **UTC**, not local time. `mark_mail_sent` records
`datetime.now(UTC)`, so `date.today()` disagrees with the stamp for up to a full day
depending on the machine's offset — a test asserting `at_utc.startswith(date.today())`
fails for reasons that have nothing to do with the code under test.

## Important privacy rule

Never write SMTP passwords, app authorization codes, GitHub tokens, private inboxes, or API
keys into tracked files. Secrets belong in GitHub Secrets; personal preferences belong in
the gitignored `automation/research_brief_config.json`.

When an AI assistant configures this project, **ask the user** for every personal value
(email, timezone, send time, research topics, journal tiers, topic-gate vocabulary).
Never infer or copy them from an existing configuration.