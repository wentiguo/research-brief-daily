"""Probe the SMTP account before wiring the daily push.

The research brief pipeline can collect everything and then die on
`RuntimeError: Email is not configured`, which wastes a full run. This script
separates "network reachable" from "credentials accepted" from "actually
delivered", so a bad authorization code is diagnosed in seconds instead of at
the end of a three minute harvest.

Usage (credentials may come from flags or from a local .env, flag wins):

    python scripts/check_smtp.py
    python scripts/check_smtp.py --host smtp.163.com --port 465 --ssl \\
        --username you@example.com --password xxxxxxxxxxxxxxxx
    python scripts/check_smtp.py --send-test      # also deliver one probe mail
    python scripts/check_smtp.py --json           # machine readable

Exit codes: 0 = connected + logged in (+ sent, when --send-test), 1 = failure.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
    "SMTP_TO_EMAIL",
    "SMTP_USE_SSL",
    "SMTP_STARTTLS",
)


def load_dotenv(path: Path) -> None:
    """Minimal `KEY=value` loader; the project only needs flat, quoted strings."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ENV_KEYS and not os.getenv(key):
            os.environ[key] = value


def probe(args: argparse.Namespace, results: dict[str, Any]) -> None:
    host = args.host or os.getenv("SMTP_HOST")
    username = args.username or os.getenv("SMTP_USERNAME")
    password = args.password or os.getenv("SMTP_PASSWORD")
    from_email = args.from_email or os.getenv("SMTP_FROM_EMAIL") or username
    to_email = args.to_email or os.getenv("SMTP_TO_EMAIL") or from_email
    port = args.port if args.port is not None else int(os.getenv("SMTP_PORT") or 465)
    use_ssl = (args.ssl if args.ssl is not None else _tri(args.ssl, "SMTP_USE_SSL", True))
    starttls = (args.starttls if args.starttls is not None else _tri(args.starttls, "SMTP_STARTTLS", False))

    results["host"] = host
    results["port"] = port
    results["ssl"] = use_ssl
    results["starttls"] = starttls
    results["from"] = from_email
    results["to"] = to_email

    missing = [name for name, value in (("SMTP_HOST", host), ("SMTP_USERNAME", username), ("SMTP_PASSWORD", password)) if not value]
    if missing:
        results["error"] = f"missing configuration: {', '.join(missing)} (see .env.example)"
        return

    context = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                results["connect"] = True
                if starttls:
                    server.starttls(context=context)
                    results["starttls"] = True
                results["greeting"] = server.noop()[1].decode("utf-8", "replace")[:120]
                server.login(username, password)
                results["login"] = True
                if args.send_test:
                    results["sent"] = _send_probe(server, from_email, to_email)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                results["connect"] = True
                code, greeting = server.noop()
                results["greeting"] = greeting.decode("utf-8", "replace")[:120]
                if starttls:
                    server.starttls(context=context)
                server.login(username, password)
                results["login"] = True
                if args.send_test:
                    results["sent"] = _send_probe(server, from_email, to_email)
    except smtplib.SMTPAuthenticationError as exc:
        results["login"] = False
        results["error"] = (
            f"username or password rejected (SMTP {exc.smtp_code} {exc.smtp_error!r}). "
            "For 163, the password must be the SMTP client authorization code, "
            "not the account login password."
        )
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        results["connect"] = False
        results["error"] = f"{type(exc).__name__}: {exc}"
    return None


def _tri(cli_value: bool | None, env_key: str, default: bool) -> bool:
    if cli_value is not None:
        return cli_value
    raw = os.getenv(env_key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _send_probe(server: smtplib.SMTP | smtplib.SMTP_SSL, from_email: str, to_email: str) -> dict[str, str]:
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "[research-brief-daily] SMTP probe"
    message["From"] = from_email
    message["To"] = to_email
    message.set_content("SMTP probe OK. This mailbox is now wired to the daily push.")
    message.add_alternative("<p style='font-family:sans-serif'>SMTP probe OK.<br>日常推送已连通。</p>", subtype="html")
    server.send_message(message)
    return {"subject": message["Subject"], "to": to_email}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the SMTP account used by the research brief push.")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--from-email")
    parser.add_argument("--to-email")
    parser.add_argument("--ssl", dest="ssl", action="store_true", default=None)
    parser.add_argument("--no-ssl", dest="ssl", action="store_false")
    parser.add_argument("--startls", dest="starttls", action="store_true", default=None)
    parser.add_argument("--no-startls", dest="starttls", action="store_false")
    parser.add_argument("--send-test", action="store_true", help="deliver one short probe message")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    load_dotenv(Path(args.env_file) if args.env_file else PROJECT_ROOT / ".env")

    results: dict[str, Any] = {}
    probe(args, results)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"host     : {results.get('host')}:{results.get('port')} ssl={results.get('ssl')} starttls={results.get('starttls')}")
        print(f"from/to  : {results.get('from')} -> {results.get('to')}")
        if "greeting" in results:
            print(f"greeting : {results['greeting']}")
        print(f"connect  : {'ok' if results.get('connect') else 'failed'}")
        print(f"login    : {'ok' if results.get('login') else 'failed'}")
        if "sent" in results:
            print(f"sent     : {results['sent']}")
        if results.get("error"):
            print(f"error    : {results['error']}", file=sys.stderr)

    if results.get("error") or not results.get("login"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
