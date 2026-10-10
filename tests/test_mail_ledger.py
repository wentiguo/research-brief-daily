"""One mail per day per kind.

The job has two schedulers now: the GitHub cron (`0 0 * * *`, i.e. 00:00 UTC = 08:00
Asia/Shanghai) and the local 08:00 automation that fires the same workflow by hand.
Nothing stopped both from landing in the same day, and the second mail would arrive
while the first one was still unread. The ledger in `automation/mail_ledger.json`
makes the guarantee belong to the code instead of to scheduling luck.

Two rules the tests pin down:

* same date + same kind (daily vs weekly) is mailed once;
* an unreadable ledger reads as "nothing was sent", never as "already sent" - a
  duplicate is a nuisance, silently losing the day's mail is not.
"""

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

sys_path = str(Path(__file__).resolve().parents[1] / "scripts")
import sys  # noqa: E402

sys.path.insert(0, sys_path)
import generate_research_brief as grb  # noqa: E402

RUN_DATE = dt.date(2026, 10, 4)


class MailLedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mail-ledger-"))
        self.original = grb.MAIL_LEDGER_PATH
        grb.MAIL_LEDGER_PATH = self.tmp / "mail_ledger.json"
        self.dispatched: list[str] = []
        self.original_smtp = grb.send_email_smtp
        grb.send_email_smtp = lambda *a, **kw: self.dispatched.append(kw.get("subject") or "")

    def tearDown(self):
        grb.MAIL_LEDGER_PATH = self.original
        grb.send_email_smtp = self.original_smtp
        shutil.rmtree(self.tmp, ignore_errors=True)

    def send(self, **kwargs):
        return grb.send_email("body", {"language": "zh-CN"}, RUN_DATE, **kwargs)

    def test_missing_ledger_sends_first_mail(self):
        self.assertFalse(grb.mail_already_sent(RUN_DATE))
        self.assertEqual(self.send(), "SMTP")
        self.assertTrue(grb.mail_already_sent(RUN_DATE))

    def test_second_call_same_day_same_kind_is_skipped(self):
        self.assertEqual(self.send(), "SMTP")
        self.assertIsNone(self.send())
        self.assertEqual(len(self.dispatched), 1, "the same day may not be mailed twice")

    def test_force_send_overrides_the_ledger(self):
        self.assertEqual(self.send(), "SMTP")
        self.assertIsNone(self.send())
        self.assertEqual(self.send(force=True), "SMTP")
        self.assertEqual(len(self.dispatched), 2)

    def test_kinds_are_independent_on_the_same_day(self):
        self.assertEqual(self.send(kind="daily"), "SMTP")
        self.assertIsNone(self.send(kind="daily"))
        self.assertEqual(self.send(kind="weekly"), "SMTP", "the weekly workbook is a different mail")
        self.assertEqual(len(self.dispatched), 2)

    def test_a_different_day_is_not_treated_as_sent(self):
        self.assertEqual(self.send(), "SMTP")
        self.assertFalse(grb.mail_already_sent(RUN_DATE + dt.timedelta(days=1)))

    def test_unreadable_ledger_reads_as_nothing_sent(self):
        grb.MAIL_LEDGER_PATH.write_text("{ not json", encoding="utf-8")
        self.assertFalse(grb.mail_already_sent(RUN_DATE), "corrupt ledger must not suppress the mail")
        self.assertEqual(self.send(), "SMTP")
        self.assertEqual(len(self.dispatched), 1)

    def test_ledger_drops_a_duplicate_entry_on_rewrite(self):
        grb.MAIL_LEDGER_PATH.write_text(json.dumps({"sent": [
            {"date": "2026-10-04", "kind": "daily", "provider": "SMTP"},
            {"date": "2026-10-04", "kind": "daily", "provider": "SMTP"},
        ]}), encoding="utf-8")
        self.assertEqual(len(grb.load_mail_ledger()), 2)
        self.assertIsNone(self.send(), "the seeded row alone already counts as sent today")
        self.assertEqual(self.send(force=True), "SMTP")
        entries = grb.load_mail_ledger()
        self.assertEqual(len(entries), 1, "marking a second mail must not duplicate the row")
        self.assertEqual([e["date"] for e in entries], [RUN_DATE.isoformat()])

    def test_marking_records_provider_and_time(self):
        self.assertEqual(self.send(), "SMTP")
        entry = grb.load_mail_ledger()[0]
        self.assertEqual(entry["kind"], "daily")
        self.assertEqual(entry["provider"], "SMTP")
        # The ledger stamps at_utc with the real send time, so assert it starts with
        # today's date rather than a hard-coded one (the test was originally pinned to the
        #
        # Compare in UTC, not local time: mark_mail_sent records dt.datetime.now(UTC),
        # so on a machine whose local date has already rolled over (or rolls over during
        # the run) `date.today()` disagrees with the stamp for up to a full day.
        self.assertTrue(
            entry["at_utc"].startswith(dt.datetime.now(dt.UTC).date().isoformat()),
            f"at_utc {entry['at_utc']!r} is not today's UTC date",
        )


if __name__ == "__main__":
    unittest.main()
