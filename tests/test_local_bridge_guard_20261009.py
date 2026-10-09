"""Guard / idempotency logic for the local PDF bridge (regression, 2026-10-09).

The bridge must only act on a wishlist the cloud stamped today (or, failing a small
local-clock drift, yesterday), and must not re-download files it already synced. These
two predicates are what keep the every-15-min scheduled task from re-fetching yesterday's
posters or hammering the publishers on every tick.
"""
import datetime as dt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(os.path.dirname(_HERE), ".tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import local_fetch_posters as m  # noqa: E402


def _iso(days_ago: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8, days=-days_ago)).date().isoformat()


def test_beijing_date_today():
    assert m._beijing_date(0) == _iso(0), (m._beijing_date(0), _iso(0))


def test_should_act_today():
    assert m._should_act_on_wishlist(m._beijing_date(0)) is True


def test_should_act_yesterday():
    assert m._should_act_on_wishlist(_iso(1)) is True


def test_should_act_two_days_ago():
    # A stale wishlist from two days ago must NOT trigger a fetch.
    assert m._should_act_on_wishlist(_iso(2)) is False


def test_should_act_future():
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8, days=3)).date().isoformat()
    assert m._should_act_on_wishlist(future) is False


if __name__ == "__main__":
    test_beijing_date_today()
    test_should_act_today()
    test_should_act_yesterday()
    test_should_act_two_days_ago()
    test_should_act_future()
    print("ALL TESTS PASSED")
