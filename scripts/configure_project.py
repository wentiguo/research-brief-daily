#!/usr/bin/env python3
"""Interactive setup for Research Brief Actions.

Asks for everything that is specific to you and writes it into
`automation/research_brief_config.json` (gitignored) and, optionally, into GitHub
Secrets. Nothing personal is stored in the repository itself.

    python scripts/configure_project.py

Two phases:

1. Non-secret preferences -> `automation/research_brief_config.json`
   (email, timezone, send time, research topics, journals, topic gate)
2. Secrets -> GitHub Secrets, only if you answer yes
   (SMTP password, SendGrid key, DeepSeek key. The GitHub token is never asked for:
   reuse the one `gh auth login` already stored.)

Re-running is safe: every answer defaults to your current value, so you can come back
and change a single field.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "automation" / "research_brief_config.json"
EXAMPLE = ROOT / "automation" / "research_brief_config.example.json"
LOCAL_ENV = ROOT / "automation" / "local_env.json"
WORKFLOW = ROOT / ".github" / "workflows" / "research-brief.yml"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def ask_list(prompt: str, default: list[str], *, hint: str = "") -> list[str]:
    """Multi-line list entry, one item per line; a blank line finishes."""
    if hint:
        print(f"  {DIM}{hint}{RESET}")
    shown = ", ".join(default[:8]) + (" ..." if len(default) > 8 else "")
    print(f"  {DIM}current: {shown}{RESET}")
    print(f"  {DIM}one per line, blank line to finish (or type 'keep' to skip){RESET}")
    items: list[str] = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            break
        if line.lower() in {"keep", "k", "保留"}:
            return list(default)
        items.append(line)
    return items or list(default)


def ask_bool(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        value = input(f"{prompt} {DIM}({suffix}){RESET}: ").strip().lower()
    except EOFError:
        return default
    if not value:
        return default
    return value.startswith("y")


def ask_int(prompt: str, default: int) -> int:
    value = ask(prompt, str(default))
    try:
        return int(value)
    except ValueError:
        print(f"  {DIM}not a number, keeping {default}{RESET}")
        return default


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print("-" * len(title))


def local_time_to_utc_cron(time_text: str, timezone: str) -> str:
    """GitHub Actions cron is always UTC. Convert a local wall-clock send time.

    The offset is taken for *today*, so a schedule that crosses a DST boundary can be
    one hour off until the script is re-run. That is the honest trade-off for a static
    cron string -- re-running after a DST change fixes it.
    """
    hour, minute = [int(x) for x in time_text.split(":")]
    now = dt.datetime.now(ZoneInfo(timezone))
    local = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    utc = local.astimezone(dt.UTC)
    return f"{utc.minute} {utc.hour} * * *"


def update_workflow_cron(cron: str) -> bool:
    if not WORKFLOW.is_file():
        print(f"  {DIM}workflow not found at {WORKFLOW}; set the schedule in the GitHub UI{RESET}")
        return False
    text = WORKFLOW.read_text(encoding="utf-8")
    lines, replaced = [], False
    for line in text.splitlines():
        if line.strip().startswith("- cron:") and not replaced:
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f'{indent}- cron: "{cron}"')
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        print(f"  {DIM}no '- cron:' line found; set the schedule in the GitHub UI{RESET}")
        return False
    WORKFLOW.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_repo_default() -> str:
    try:
        out = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                              "--jq", ".nameWithOwner"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def set_secret(name: str, value: str, repo: str | None) -> bool:
    cmd = ["gh", "secret", "set", name]
    if repo:
        cmd.extend(["--repo", repo])
    try:
        subprocess.run(cmd, input=value, text=True, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:140]
        print(f"  {DIM}could not set {name}: {detail}{RESET}")
        return False
    print(f"  set {name}")
    return True


def is_placeholder(value: str) -> bool:
    low = (value or "").lower()
    return (not low
            or "example" in low
            or "configured-via-github-secret" in low)


def _default_gate_terms():
    """Read the shipped gate defaults so the wizard can show them for editing."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import generate_research_brief as grb
    except ImportError:
        return [], [], []
    return (list(grb._DEFAULT_TOPIC_SIGNAL_DIRECT),
            list(grb._DEFAULT_TOPIC_SIGNAL_WEAK),
            list(grb._DEFAULT_TOPIC_MATTER_ANCHOR))


def main() -> int:
    if not CONFIG.is_file():
        if EXAMPLE.is_file():
            shutil.copyfile(EXAMPLE, CONFIG)
            print(f"{DIM}created automation/research_brief_config.json from the template{RESET}")
        else:
            print(f"error: {EXAMPLE} is missing; cannot create a config", file=sys.stderr)
            return 1

    with CONFIG.open(encoding="utf-8-sig") as fh:
        config = json.load(fh)

    print(f"\n{BOLD}Research Brief Actions setup{RESET}")
    print("Answers are saved to automation/research_brief_config.json (gitignored).")
    print("Passwords and API keys go to GitHub Secrets only, and only if you say so.")

    # ------------------------------------------------------------------ delivery
    section("1. Delivery -- where the brief goes")
    stored = str(config.get("recipient_email", ""))
    config["recipient_email"] = ask(
        "Your email (the brief is sent here)" if is_placeholder(stored) else "Your email",
        stored)
    config["contact_email"] = ask(
        "Contact email for scholarly APIs (Crossref/OpenAlex polite pool)",
        str(config.get("contact_email", "")) or str(config["recipient_email"]))
    config["timezone"] = ask("Timezone (IANA name)", str(config.get("timezone", "UTC")))
    config["send_time_local"] = ask("Daily send time, local HH:MM",
                                    str(config.get("send_time_local", "08:00")))
    config["max_papers"] = ask_int("Maximum papers per brief", int(config.get("max_papers", 20)))
    config["language"] = ask("Brief language (zh-CN or en)", str(config.get("language", "zh-CN")))

    # ------------------------------------------------------------------ research
    section("2. Research scope -- what counts as on-topic")
    profile = config.setdefault("research_profile", {})
    profile["summary"] = ask("Describe your research in a sentence or two",
                             str(profile.get("summary", "")))
    print()
    profile["core_topics"] = ask_list(
        "Core topics", list(profile.get("core_topics", [])),
        hint="Specific states, material classes or phenomena. Each becomes a DIRECT gate phrase.")
    print()
    profile["method_keywords"] = ask_list(
        "Methods you care about", list(profile.get("method_keywords", [])),
        hint="e.g. first-principles, tight-binding, machine learning, thin-film growth")
    print()
    profile["objective_keywords"] = ask_list(
        "Properties / observables you care about", list(profile.get("objective_keywords", [])),
        hint="e.g. anomalous Hall effect, band gap, cycling stability")
    print()
    profile["seed_papers"] = ask_list(
        "Seed papers (2-5 titles that represent your taste)",
        list(profile.get("seed_papers", [])),
        hint="These anchor the ranking. Plain titles are fine.")
    print()
    profile["query_templates"] = ask_list(
        "Search queries", list(profile.get("query_templates", [])),
        hint="Sent to OpenAlex/Crossref/arXiv. Keep each short and specific.")

    # ------------------------------------------------------------------ journals
    section("3. Journals -- where to look")
    config["journals"] = ask_list(
        "Journals / venues to deliver", list(config.get("journals", [])),
        hint="Names must match what Crossref / the publisher feeds return.")
    policy = config.setdefault("journal_policy", {})
    policy["tier1"] = ask_list("Tier 1 (your top venues)", list(policy.get("tier1", [])))
    print()
    policy["tier2"] = ask_list("Tier 2 (also worth reading)", list(policy.get("tier2", [])))
    print()
    policy["exclude"] = ask_list("Journals to exclude", list(policy.get("exclude", [])))
    policy["allow_preprints"] = ask_bool("Include arXiv preprints?",
                                         bool(policy.get("allow_preprints", True)))

    print(f"\n  {DIM}Journal zones drive the 'N \u533a' badge. Optional: a journal with a tier but "
          f"no zone record renders '\u5927\u7c7b\u672a\u5efa\u6863', not an empty cell.{RESET}")
    if ask_bool("Add or edit zone records now?", False):
        policy["journal_zones"] = {}
        while True:
            name = ask("  Journal name (blank to finish)", "").strip()
            if not name:
                break
            policy["journal_zones"][name] = {
                "zone": ask_int("    Zone (1 or 2)", 1),
                "category": ask("    Category", ""),
            }
    else:
        policy.pop("journal_zones", None)

    # ------------------------------------------------------------------ topic gate
    section("4. Topic gate -- the on-topic rule (optional)")
    gate = config.setdefault("topic_gate", {})
    gate.pop("_comment", None)
    print(f"  {DIM}The gate decides whether a paper is on-topic. Defaults live in "
          f"scripts/generate_research_brief.py and cover generic condensed matter / "
          f"materials. Override only the lists you want to change.{RESET}")
    if ask_bool("Customise the topic gate now?", False):
        default_direct, default_weak, default_anchor = _default_gate_terms()
        gate["direct_signals"] = ask_list(
            "  DIRECT phrases (pass on their own)",
            list(gate.get("direct_signals") or default_direct),
            hint="Specific states / materials / phenomena of YOUR field.")
        print()
        gate["weak_signals"] = ask_list(
            "  WEAK words (need a matter anchor)",
            list(gate.get("weak_signals") or default_weak),
            hint="Cross-field homonyms. These MUST NOT also appear in the anchors below.")
        print()
        anchors = ask_list(
            "  Matter anchors (what makes a weak hit legitimate)",
            list(gate.get("matter_anchors") or default_anchor),
            hint="Host-system words: material, crystal, electron, band, first-principles, ...")
        weak = {w.lower() for w in gate.get("weak_signals", [])}
        gate["matter_anchors"] = [a for a in anchors if a.lower() not in weak]
        dropped = sorted(set(anchors) - set(gate["matter_anchors"]))
        if dropped:
            print(f"  {DIM}dropped from anchors (they are weak signals, and a weak hit "
                  f"must not find itself): {', '.join(dropped)}{RESET}")
        print()
        gate["exclude_wave_physics"] = ask_bool(
            "  Exclude wave physics (topological photonics/acoustics)?",
            bool(gate.get("exclude_wave_physics", True)))

    # ------------------------------------------------------------------ attachments
    section("5. Poster PDFs (optional)")
    attach = config.setdefault("paper_attachments", {})
    attach["enabled"] = ask_bool("Attach the original PDF of each poster paper?",
                                 bool(attach.get("enabled", True)))
    if attach["enabled"]:
        attach["poster_count"] = ask_int("  How many poster papers?",
                                         int(attach.get("poster_count", 2)))
        print(f"  {DIM}Some publisher sites block cloud runners (APS answers HTTP 403 to "
              f"GitHub Actions). For those journals run .tools/local_fetch_posters.py on "
              f"your own machine after the daily run: it downloads the PDFs and pushes them "
              f"back so the cloud can attach them to the next mail.{RESET}")

    # ------------------------------------------------------------------ write
    with CONFIG.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"\n{BOLD}Wrote{RESET} automation/research_brief_config.json")

    # ------------------------------------------------------------------ schedule
    section("6. Schedule")
    try:
        cron = local_time_to_utc_cron(str(config["send_time_local"]), str(config["timezone"]))
    except (ValueError, KeyError, ZoneInfo) as exc:
        print(f"  {DIM}could not convert the time ({exc}); set the cron in the GitHub UI{RESET}")
    else:
        print(f"  {config['send_time_local']} {config['timezone']}  ->  cron \"{cron}\" (UTC)")
        if update_workflow_cron(cron):
            print(f"  {DIM}updated .github/workflows/research-brief.yml{RESET}")

    # ------------------------------------------------------------------ secrets
    section("7. Secrets (optional)")
    print(f"  {DIM}Needed to actually send mail. Stored in GitHub Secrets, never in the repo. "
          f"Skip this if you only ever run --dry-run.{RESET}")

    repo = ""
    if gh_available():
        default_repo = gh_repo_default()
        repo = ask("GitHub repo (owner/name)", default_repo) if default_repo else ""
        if ask_bool("Set GitHub email secrets now?", False):
            backend = ask("Email backend: smtp or sendgrid", "smtp").lower()
            ok = True
            if backend == "sendgrid":
                ok &= set_secret("SENDGRID_API_KEY", getpass.getpass("SendGrid API key: "), repo)
                ok &= set_secret("RESEARCH_BRIEF_FROM_EMAIL", ask("Verified sender email"), repo)
                ok &= set_secret("RESEARCH_BRIEF_TO_EMAIL", str(config["recipient_email"]), repo)
            else:
                ok &= set_secret("SMTP_HOST", ask("SMTP host"), repo)
                ok &= set_secret("SMTP_PORT", ask("SMTP port", "587"), repo)
                ok &= set_secret("SMTP_USERNAME", ask("SMTP username"), repo)
                ok &= set_secret("SMTP_PASSWORD",
                                 getpass.getpass("SMTP password / app password: "), repo)
                ok &= set_secret("SMTP_FROM_EMAIL",
                                 ask("From email", str(config["recipient_email"])), repo)
                ok &= set_secret("SMTP_TO_EMAIL", str(config["recipient_email"]), repo)
                ok &= set_secret("SMTP_USE_SSL", ask("SMTP_USE_SSL true/false", "false"), repo)
                ok &= set_secret("SMTP_STARTTLS", ask("SMTP_STARTTLS true/false", "true"), repo)
            if ok:
                print(f"  {DIM}email secrets written to {repo or 'the current repo'}{RESET}")
        else:
            print(f"  skipped. Later: gh secret set SMTP_PASSWORD --repo "
                  f"{repo or '<owner/name>'}")
        print(f"\n  {DIM}The GitHub token is never asked for here: the helpers reuse the one "
              f"`gh auth login` already stored.{RESET}")
    else:
        print("  {DIM}GitHub CLI (gh) not found -- install it, then re-run to set secrets.{RESET}")

    if ask_bool("\nConfigure the DeepSeek key too (poster text and scoring)?", False):
        key = getpass.getpass("DeepSeek API key: ")
        if key and repo:
            set_secret("DEEPSEEK_API_KEY", key, repo)

    if repo:
        with LOCAL_ENV.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump({"repo_slug": repo}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"\n{BOLD}Wrote{RESET} automation/local_env.json  (repo_slug = {repo})")

    section("Next steps")
    print(f"  1. Review:   {CONFIG.relative_to(ROOT)}")
    print(f"  2. Dry run:  python scripts/generate_research_brief.py --dry-run")
    print(f"  3. Commit the workflow + example config. Your own config stays untracked.")
    print(f"  4. Enable the schedule in the Actions tab, or run it once: "
          f"gh workflow run research-brief.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())