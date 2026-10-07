# Research Brief Actions

A GitHub Actions workflow that searches scholarly metadata sources every day, ranks the
results against **your** research profile, writes a Markdown/BibTeX brief, and emails it
to you — with the original PDFs of the two poster papers attached when the publisher
allows it.

**This repository ships no personal data.** No email address, no SMTP credential, no API
key, no research profile, no journal list of one particular person. Every one of those is
asked for interactively the first time you run:

```bash
python scripts/configure_project.py
```

Your answers land in `automation/research_brief_config.json`, which is gitignored.

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [How the topic gate works](#how-the-topic-gate-works)
- [Attaching the real PDFs](#attaching-the-real-pdfs-the-cloud--local-bridge)
- [Configuration reference](#configuration-reference)
- [Secrets](#secrets)
- [Dependencies](#dependencies)
- [Daily operations](#daily-operations)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Privacy](#privacy)

---

## What it does

**Finds papers.** Searches Crossref, OpenAlex, arXiv and PubMed, plus publisher-hosted
table-of-contents feeds for subscription journals. The feeds matter more than they sound:
APS supplies **no abstract** to Crossref or OpenAlex, so a metadata-only pipeline sees PRL
titles with empty abstracts and ranks them blind. Reading the publisher feed is the only
way to get the public abstract of a subscription letter.

**Decides what is on-topic.** A topic gate, not a keyword filter. See
[How the topic gate works](#how-the-topic-gate-works).

**Ranks by journal and by fit.** A 1区/顶刊 venue outranks a 2区 paper of equal topical
fit; topic fit outranks venue within a tier. Subscribed letters are labelled as such
rather than presented as open access.

**Recommends two papers as posters.** Two slots, and both prefer **already-published**
papers over preprints — a preprint is a worse thing to build a poster around. Each poster
gets a five-section Chinese write-up (研究背景 / 创新亮点 / 解决的关键问题 / 可借鉴之处 /
可延展方向), every field of which carries an English quote from the abstract as
provenance. A field whose quote cannot be verified is **omitted**, never invented.

**Attaches the originals.** When the publisher permits it, the PDF of each poster paper
travels with the email, plus a citation card stating honestly how it was obtained
(`fetched` / `paywalled` / `verification-required` / `cited-only`).

**Never sends the same paper twice.** A dedup ledger keyed on `source + id` means a paper
that already appeared in a brief is not pushed again, even if it stays relevant for weeks.

---

## Quick start

### 1. Fork and clone

```bash
gh repo fork <owner>/research-brief-daily --clone
cd research-brief-daily
```

### 2. Answer the setup questions

```bash
python scripts/configure_project.py
```

It asks for:

| Group | What it asks |
|---|---|
| Delivery | your email, contact email for the scholarly APIs, timezone, send time, max papers, brief language |
| Research scope | a sentence describing your work, core topics, methods, observables, 2–5 seed papers, search queries |
| Journals | which venues to deliver, tier 1 / tier 2 / excluded, preprint policy |
| Topic gate | optionally replace the shipped on-topic vocabulary with your own |
| Poster PDFs | whether to attach originals, and how many posters |
| Schedule | converts your local send time into the UTC cron the workflow needs |
| Secrets | optionally writes SMTP / SendGrid / DeepSeek keys into GitHub Secrets |

It is safe to re-run: every answer defaults to what you chose last time.

### 3. Dry run locally

```bash
python scripts/generate_research_brief.py --days-back 3 --dry-run
```

Writes `research_briefs/latest.md` and `.bib`, sends nothing. On a fresh clone this works
immediately — the config is materialised from the example template on first load.

### 4. Run the tests

```bash
python -m unittest discover -s tests -t .
```

### 5. Enable the schedule

Commit the workflow (the cron was rewritten by the wizard), then either use the Actions
tab or trigger it once:

```bash
gh workflow run research-brief.yml -f days_back=3 -f send_email=false
```

Test with `send_email=false` first, then with `send_email=true`.

---

## How the topic gate works

Naive keyword filtering is the failure mode this replaces. "topological" appears in
biology ("topological data analysis"), medicine ("magnetic resonance imaging") and
engineering ("topological photonics"); "machine learning" appears in every field. A gate
built from bare keywords either admits all of that or, once tightened, silently drops
in-scope papers.

The gate has three term classes:

| Class | Behaviour |
|---|---|
| `direct_signals` | A specific state, material or phenomenon. **Passes on its own.** |
| `weak_signals` | A cross-field homonym (`topological`, `magnetic`, `spin`, `ferro`, …). Passes **only** together with a matter anchor. |
| `matter_anchors` | Host-system words (`material`, `crystal`, `electron`, `band`, `first-principles`, …) that make a weak hit legitimate. |

So *"topological data analysis for single-cell omics"* is rejected (weak word, no matter
host), while *"Berry curvature of a topological insulator"* passes (direct phrase).

**The one invariant not to break:** a weak signal must never also appear in the anchor
tuple. Otherwise a weak hit finds itself as its own anchor and the rule collapses into
"pass everything" — which is exactly how a gate silently disables itself. The test suite
checks this against your live config, so the mistake is caught at test time rather than in
a brief full of papers you did not ask for.

A method word (`machine learning`, `active learning`, `inverse design`) is **not** a
direction on its own. ML applied to an in-topic subject passes because the topic phrase
carries it.

Papers with **no abstract** — which is most subscription letters — are judged on the
title alone, rather than being discarded for lack of an abstract.

Replace any list from your config:

```json
"topic_gate": {
  "direct_signals":  ["your specific states and materials", "..."],
  "weak_signals":    ["cross-field homonyms"],
  "matter_anchors":  ["host-system words"],
  "exclude_wave_physics": true
}
```

Omit a key to keep the shipped default. `tests/test_topic_gate_scope.py` pins all of the
semantics above.

---

## Attaching the real PDFs: the cloud ↔ local bridge

Some publishers refuse to serve their PDFs to a cloud runner. APS answers
`HTTP 403` to GitHub Actions' egress IP — the request is well-formed and the route is
allowed, so **there is no workaround inside the workflow**. Your own machine is not
blocked.

The bridge:

```
08:00  cloud run        fetch metadata -> pick 2 posters -> publish
                        research_briefs/latest_poster_wishlist.json  {run_date, papers[]}
                        then wait (default up to 30 min)
08:15  your machine     .tools/local_fetch_posters.py
                        read the wishlist -> download the real PDFs
                        -> verify each one -> push to the repo
                        via the GitHub Contents API
                        -> the cloud run picks them up and attaches them
```

Run the local half once, right after the daily mail:

```bash
python .tools/local_fetch_posters.py
```

**Why the API and not `git push`.** The working copy this runs in is populated through
the Contents API, so it is not a git checkout; `git pull`/`git push` cannot work there.
The Contents API also handles PDFs up to ~90 MB per file without trouble.

**Verification is mandatory, not optional.** Every downloaded file must (a) start with the
`%PDF-` magic number, (b) clear a size floor, and (c) contain its own DOI in the bytes, or
match the title by ≥60 % of its tokens. A wrong-paper check exists because an arXiv
lookup once returned a 2020 paper in place of a brand-new PRL; a PDF that fails
verification is discarded rather than mailed.

**The cloud stamps the date.** `local_fetch_posters.py` decides which dated folder to write
from `run_date` **inside the wishlist**, never from the local clock — a drifting local
clock silently writes PDFs where the cloud will never look, and the mail goes out without
them with nothing in the logs to say why.

---

## Configuration reference

`automation/research_brief_config.json` (gitignored, yours alone):

| Key | Meaning |
|---|---|
| `recipient_email` | where the brief is sent |
| `contact_email` | contact address passed to Crossref/OpenAlex (polite pool) |
| `timezone`, `send_time_local` | IANA zone and local `HH:MM`; the wizard converts to UTC cron |
| `max_papers`, `language` | size of the brief, `zh-CN` or `en` |
| `journals` | every venue you accept |
| `journal_policy.tier1` / `tier2` | ranking tiers; `journal_zones` adds an optional category badge |
| `journal_policy.exclude` | never delivered |
| `research_profile.summary` | one paragraph describing your taste |
| `research_profile.core_topics` | topic phrases; these become DIRECT gate signals |
| `research_profile.seed_papers` | 2–5 titles that anchor the ranking |
| `research_profile.query_templates` | queries sent to the metadata APIs |
| `topic_gate` | overrides for the on-topic vocabulary |
| `publisher_feeds` | ToC feed URLs and Crossref ISSNs; the reusable part of the template |
| `paper_attachments` | poster PDF behaviour |

`automation/research_brief_config.example.json` is the committed template.

### Journal tiers must be exact

Tier resolution is **exact-match first, prefix-match second**. Without that order,
`"Nature"` in tier 1 claims `"Nature Nanotechnology"` by prefix before tier 2 is consulted,
silently promoting every Nature sub-journal to the top tier. Put every journal you care
about in exactly one tier.

### Journal zones are yours to audit

`journal_policy.journal_zones` maps a journal to `{"zone": 1|2, "category": "..."}` for the
badge in the email. **The template ships none of these on purpose**: CAS-style journal
partitioning differs per institution and per year, so the correct values depend on your own
institution's current audit. Without a zone record a journal still renders — as
`N 区 · 大类未建档` — never as an empty cell.

---

## Secrets

Never put these in a file. `gh secret set` writes them to GitHub Secrets:

```bash
gh secret set SMTP_HOST       --body "smtp.example.com"
gh secret set SMTP_PORT       --body "587"
gh secret set SMTP_USERNAME   --body "you@example.com"
gh secret set SMTP_PASSWORD                      # prompts, not echoed
gh secret set SMTP_FROM_EMAIL --body "you@example.com"
gh secret set SMTP_TO_EMAIL   --body "you@example.com"
gh secret set SMTP_USE_SSL    --body "false"
gh secret set SMTP_STARTTLS   --body "true"
```

Providers and their ports: [docs/EMAIL_PROVIDERS.md](docs/EMAIL_PROVIDERS.md).

Optional: `DEEPSEEK_API_KEY` (poster write-ups and scoring), `SENDGRID_API_KEY` instead of
the SMTP set, `IEEE_XPLORE_API_KEY`, `ZOTERO_API_KEY` + `ZOTERO_USER_ID` /
`ZOTERO_GROUP_ID`.

**The GitHub token is never asked for.** The local helpers reuse the credential `gh auth
login` already stored, or `$GH_TOKEN` if you export one.

---

## Daily operations

```bash
# Trigger manually (the wizard can rewrite the cron)
gh workflow run research-brief.yml -f days_back=3 -f send_email=true

# Fetch the poster PDFs that the cloud could not get
python .tools/local_fetch_posters.py

# Run the tests
python -m unittest discover -s tests -t .

# Push the local tree to the repo (Contents API, for a non-git checkout)
GH_TOKEN=... python .tools/push_contents.py --push
```

Watch a run:

```bash
python .tools/_await_run.py <run-id>
```

### How the "one complete email" behaviour works

The daily run delivers **every** paper it found that day in a single mail. Papers already
present in `automation/recommended_history.json` are filtered out before rendering, so a
brief is never a partial duplicate of yesterday's. The ledger is committed back to the
repo after a successful send.

---

## Architecture

```
.github/workflows/research-brief.yml   scheduled run (cron, UTC)
scripts/generate_research_brief.py      the whole pipeline: fetch -> gate -> rank -> render -> mail
scripts/publisher_feeds.py              publisher ToC feeds (APS / Nature / Crossref ISSNs)
scripts/fetch_paper_attachments.py      PDF hunt + attachment pruning under the mail budget
scripts/fetch_browser_paper.py          desktop-browser pass for bot-walled publishers
scripts/render_digest_images.py         the long image / poster cards
scripts/configure_project.py            the interactive setup wizard
scripts/setup_github_secrets.ps1        PowerShell secret setup
.tools/repo_config.py                   portable repo + gh discovery (no hard-coded paths)
.tools/privacy_audit.py                 scans the tree for personal data before publishing
.tools/_check_core_imports.py           proves the core needs no third-party packages
.tools/local_fetch_posters.py           cloud -> local PDF bridge
.tools/push_contents.py                 Contents-API push for a non-git checkout
tests/                                  167 tests
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| No papers at all | the gate rejected everything — check `topic_gate.direct_signals` contains your own vocabulary |
| Too many irrelevant papers | a weak signal is missing from `matter_anchors`, or a weak signal is *also* in the anchors |
| Everything passes the gate | a weak signal is in `matter_anchors`; `tests/test_topic_gate_scope.py` catches this |
| Wrong journal tier | a name is a prefix of another (`Nature` vs `Nature Nanotechnology`) — list both explicitly |
| Emails not sending | secrets not set, or `SMTP_USE_SSL` / `SMTP_STARTTLS` mismatch the provider port |
| Run finishes at the wrong hour | cron is UTC; re-run the wizard to recompute it after a DST change |
| Poster PDFs missing from the mail | the publisher blocked the runner — run `.tools/local_fetch_posters.py` locally |
| `gh: command not found` | install GitHub CLI; the helpers also try the standard install path and `automation/local_env.json` |
| A paper keeps reappearing | `recommended_history.json` was not committed back; re-push it |

---

## Dependencies

**The core pipeline uses the Python standard library only.** No `pip install` is needed
to fetch metadata, run the topic gate, rank papers, render Markdown/BibTeX, or send the
email. Python 3.11+ is enough.

Four third-party packages exist, all confined to optional channels:

| Package | Needed for | Required? |
|---|---|---|
| `pillow` | the long digest image and the poster image | no — the brief degrades to text and says so |
| `numpy`, `playwright` | the desktop-browser pass for bot-walled publishers | no — only if you enable that pass |
| `pyautogui` | physical cursor control, if a challenge needs a real press | no — **this one moves your mouse** |

```bash
pip install -r requirements.txt          # no-op: the core needs nothing
pip install -r requirements-optional.txt # only if you want the channels above
```

`requirements-optional.txt` documents each one and what it actually does. Note that
`scripts/mouse_operator.py` moves the OS cursor and presses it — read it before running,
and don't run it unattended.

The Chinese poster text also needs a CJK font. On Debian/Ubuntu:

```bash
sudo apt-get install -y fonts-noto-cjk fonts-noto-cjk-extra
```

The renderer **skips the image and warns** when no CJK font is present, rather than
emitting boxes.

You can verify the no-dependency claim yourself:

```bash
python .tools/_check_core_imports.py
```

It blocks the four packages, then imports the core modules and runs a dry run. It is how
this claim is kept honest — a new `import` that reaches for a third-party package fails
that check instead of quietly working on one contributor's machine.

---

## Contributing

Issues and pull requests are welcome.

```bash
python -m unittest discover -s tests -t .   # 167 tests, must stay green
```

Useful things to contribute:

- **Verified publisher feeds or ISSNs.** The template's value is that each entry was
  checked. A new journal needs its feed URL (or ISSN) *and* a note on how it was verified.
- **Topic-gate improvements.** The three-class rule is the delicate part; a change that
  widens it without a matching test is a regression.
- **A field's honesty invariant.** Fields that cannot be sourced are omitted rather than
  filled in. Please keep that.

If you change the gate semantics, update `tests/test_topic_gate_scope.py` in the same
commit — the docstring there explains which invariant each test pins.

---

## Privacy

Never commit SMTP passwords, authorization codes, API keys, your inbox, or generated
briefs that reveal your research interests.

Everything personal lives in gitignored files
(`automation/research_brief_config.json`, `automation/local_env.json`,
`automation/mail_ledger.json`, `automation/recommended_history.json`) or in GitHub Secrets.
If you want the daily archives committed, use a private repository and remove the
`research_briefs/*` ignore rules deliberately.

**Fetching is limited to publicly reachable files.** Each candidate URL is requested
anonymously (no cookies) and must return a real PDF. Anything that exists only for a
logged-in subscriber is reported as a legal route in the citation card and never
downloaded.

## License

MIT — see [LICENSE](LICENSE).