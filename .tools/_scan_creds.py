# -*- coding: utf-8 -*-
"""Exhaustive credential locator for research-brief-actions.

Scans every readable text file under the skill tree (plus a couple of
sibling locations) for SMTP / SendGrid / GitHub credential assignments.
Covers .env files, JSON, YAML, PowerShell, Markdown, and dotfiles.
Prints file:line, key, and whether the value is REAL or a PLACEHOLDER.
Never prints the secret itself.
"""
import os
import re

KEYS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL", "SMTP_TO_EMAIL", "SMTP_USE_SSL", "SMTP_STARTTLS",
    "SMTP_RETRIES",
    "SENDGRID_API_KEY",
    "RESEARCH_BRIEF_FROM_EMAIL", "RESEARCH_BRIEF_TO_EMAIL",
    "GITHUB_TOKEN", "GH_TOKEN",
]

SECRETISH = re.compile(r"(?i)(smtp_pass|mail_pass|password\s*[=:]|passwd|授权码)")

PLACEHOLDER_MARKERS = (
    "your_", "example", "xxxx", "changeme", "placeholder",
    "configured-via-github-secret", "todo", "***",
)

TEXT_EXT = (
    ".env", ".json", ".py", ".md", ".txt", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".ps1", ".sh", ".bat", ".psm1", ".properties",
    ".rst", ".html", ".log", ".netrc", ".gitignore", ".dockerignore",
)

SKIP_DIR_PARTS = (
    ".git", "node_modules", "__pycache__", "plugins/cache",
    "connectors-marketplace", ".venv", "site-packages",
)

ROOTS = [
    str(PROJECT_ROOT),
    str(Path.home() / ".config" / "gh"),
]

DOTFILES = {".env", ".netrc", ".git-credentials", "credentials", ".env.local"}


def classify(val: str) -> str:
    low = val.lower()
    if (not val) or any(m in low for m in PLACEHOLDER_MARKERS):
        return "PLACEHOLDER"
    if "<" in val or ">" in val:
        return "PLACEHOLDER"
    if len(val) < 6:
        return "TOO_SHORT(%d)" % len(val)
    return "REAL(len=%d)" % len(val)


def main() -> None:
    seen = set()
    rows = []
    scanned = 0
    for root in ROOTS:
        if not os.path.isdir(root):
            print("ROOT MISSING:", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIR_PARTS]
            for fn in filenames:
                low = fn.lower()
                if not (low.endswith(TEXT_EXT) or low in DOTFILES
                        or low.startswith(".env")):
                    continue
                fp = os.path.normpath(os.path.join(dirpath, fn))
                if fp in seen:
                    continue
                seen.add(fp)
                scanned += 1
                try:
                    with open(fp, encoding="utf-8",
                              errors="replace") as fh:
                        lines = fh.read().splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    hit = False
                    for key in KEYS:
                        m = re.search(
                            r"(?i)\b" + re.escape(key) +
                            r"\b\s*[\"']?\s*[=:]\s*[\"']?([^\"'\s,#}]+)",
                            line)
                        if m:
                            val = m.group(1).strip()
                            rows.append((fp, i, key, classify(val)))
                            hit = True
                            break
                    if not hit and SECRETISH.search(line):
                        rows.append((fp, i, "<password-ish>", "CHECK"))

    print("scanned_files = %d" % scanned)
    print("candidate_rows = %d" % len(rows))
    print("-" * 78)
    for fp, i, key, tag in rows:
        print("%s:%d  %-22s %s" % (fp, i, key, tag))
    print("-" * 78)
    real = [r for r in rows if r[3].startswith("REAL")]
    checks = [r for r in rows if r[3] == "CHECK"]
    print("REAL_VALUES = %d   CHECK_LINES = %d" % (len(real), len(checks)))
    for r in real:
        print("  REAL ->", r[0], r[1], r[2])
    for r in checks:
        print("  CHECK->", r[0], r[1])


if __name__ == "__main__":
    main()
