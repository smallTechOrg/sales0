"""Backoff, auth failures, poller health, non-overlap, thread lifecycle and the listing optimisation."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import get_settings, reset_settings
from sales0.db.models import ReplyPollState
from sales0.integrations.gmail.auth import SCOPES, StoredToken
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.integrations.gmail.errors import GmailAuthError, GmailTransientError
from sales0.jobs.reply_poller import ReplyPoller
from sales0.services import replies
from sales0.services.team import connections
from tests.integration.replies.conftest import Team, all_replies, reply, sent_message
from tests.support.fake_llm import FakeLLMClient

HUMAN = {"kind": "human", "intent": "interested", "reason": "x"}
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class FailingGmail(FakeGmailClient):
    def __init__(self, email: str, error: Exception | None) -> None:
        super().__init__(email)
        self.error = error

    def get_thread(self, thread_id: str):  # type: ignore[no-untyped-def]
        if self.error is not None:
            self.calls.append(("get_thread", (thread_id,)))
            raise self.error
        return super().get_thread(thread_id)


def swap(team: Team, member_id: int, error: Exception | None) -> FailingGmail:
    old = team.gmail[member_id]
    new = FailingGmail(old.email, error)
    new.threads = old.threads
    team.gmail[member_id] = new
    return new


def poll_state(db: Session, member_id: int) -> ReplyPollState:
    db.expire_all()
    return db.scalars(select(ReplyPollState).where(ReplyPollState.member_id == member_id)).one()


def minutes(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setenv("SALES0_REPLY_POLL_MINUTES", str(n))
    reset_settings()


# ------------------------------------------------------------------ auth failures
def test_auth_error_flips_the_connection_records_backoff_and_skips_until_due(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    minutes(monkeypatch, 10)
    sent_message(db, team, team.ann, sent_at=T0 - timedelta(hours=1))
    swap(team, team.ann.id, GmailAuthError("token revoked"))
    r = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)
    assert r.status == "error" and "credentials" in (r.error or "")
    assert connections.connection_status(db, team.ann.id).status == "needs_reauth"
    st = poll_state(db, team.ann.id)
    assert st.consecutive_failures == 1 and st.next_attempt_at is not None and st.last_error
    assert st.next_attempt_at.replace(tzinfo=UTC) == T0 + timedelta(minutes=20)  # 10 min * 2**1
    calls_before = len(team.gmail[team.ann.id].calls)
    skipped = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(minutes=5))
    assert skipped.status == "skipped" and len(team.gmail[team.ann.id].calls) == calls_before


def test_after_backoff_a_disconnected_member_is_reported_not_polled_and_reconnecting_recovers(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann, sent_at=T0 - timedelta(hours=1))
    reply(team, team.ann, msg, date=T0 - timedelta(minutes=30))
    swap(team, team.ann.id, GmailAuthError("revoked"))
    replies.poll_member(team.ann.id, llm=FakeLLMClient([HUMAN]), now=T0)
    later = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(hours=1))
    assert later.status == "skipped" and "reconnect" in (later.error or "").lower()
    # the member reconnects and the mailbox works again
    swap(team, team.ann.id, None)
    connections.store_token(db, team.ann.id, StoredToken("cid", "csecret", "1//new", list(SCOPES), team.ann.email))
    db.commit()
    ok = replies.poll_member(team.ann.id, llm=FakeLLMClient([HUMAN]), now=T0 + timedelta(hours=2))
    assert ok.status == "ok" and ok.new_replies == 1
    st = poll_state(db, team.ann.id)
    assert st.consecutive_failures == 0 and st.next_attempt_at is None and st.last_error is None and st.last_ok_at is not None


def test_one_members_failure_does_not_stop_the_others(db: Session, team: Team) -> None:
    a = sent_message(db, team, team.ann)
    b = sent_message(db, team, team.bob, to="q@beta.example")
    swap(team, team.ann.id, GmailAuthError("revoked"))
    reply(team, team.bob, b, "q@beta.example", snippet="yes")
    results = {r.member_id: r for r in replies.poll_all(llm=FakeLLMClient([HUMAN]))}
    assert results[team.ann.id].status == "error" and results[team.bob.id].status == "ok" and results[team.bob.id].new_replies == 1
    assert a.id and [x.member_id for x in all_replies(db)] == [team.bob.id]


# ------------------------------------------------------------------ transient failures
def test_transient_errors_back_off_exponentially_to_the_cap_without_flipping_the_connection(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    minutes(monkeypatch, 60)
    sent_message(db, team, team.ann, sent_at=T0 - timedelta(hours=1))
    swap(team, team.ann.id, GmailTransientError("503"))
    now, delays = T0, []
    for _ in range(5):
        r = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=now)
        assert r.status == "error"
        st = poll_state(db, team.ann.id)
        delays.append(st.next_attempt_at.replace(tzinfo=UTC) - now)  # type: ignore[union-attr]
        now = st.next_attempt_at.replace(tzinfo=UTC)  # type: ignore[union-attr]
    assert delays == [timedelta(hours=2), timedelta(hours=4), timedelta(hours=6), timedelta(hours=6), timedelta(hours=6)]
    assert poll_state(db, team.ann.id).consecutive_failures == 5
    assert connections.connection_status(db, team.ann.id).status == "connected"
    swap(team, team.ann.id, None)  # recovers: counters reset
    ok = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=now)
    st = poll_state(db, team.ann.id)
    assert ok.status == "ok" and st.consecutive_failures == 0 and st.next_attempt_at is None


def test_force_ignores_the_backoff_window(db: Session, team: Team) -> None:
    sent_message(db, team, team.ann, sent_at=T0 - timedelta(hours=1))
    swap(team, team.ann.id, GmailTransientError("503"))
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)
    assert replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(seconds=1)).status == "skipped"
    assert replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(seconds=1), force=True).status == "error"


# ------------------------------------------------------------------ health
def test_poller_health_levels(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    assert replies.poller_health(db).level == "off" and not replies.poller_health(db).enabled  # tests run with 0
    minutes(monkeypatch, 10)
    h = replies.poller_health(db)
    assert (h.level, h.enabled) == ("idle", True) and "waiting" in h.label
    msg = sent_message(db, team, team.ann)
    replies.poll_all(llm=FakeLLMClient([]))
    h = replies.poller_health(db)
    assert h.level == "ok" and h.label.startswith("Replies: checked")
    swap(team, team.ann.id, GmailTransientError("503"))
    reply(team, team.ann, msg, snippet="a new message makes the thread worth fetching")
    replies.poll_all(llm=FakeLLMClient([]), force=True)
    h = replies.poller_health(db)
    assert h.level == "warn" and "Ann Rao" in h.label and "503" in h.detail


def test_health_warns_for_a_member_whose_gmail_was_never_usable(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    minutes(monkeypatch, 10)
    sent_message(db, team, team.ann)
    connections.disconnect(db, team.ann.id)
    db.commit()
    replies.poll_all(llm=FakeLLMClient([]))
    h = replies.poller_health(db)
    assert h.level == "warn" and "Ann Rao" in h.label


# ------------------------------------------------------------------ non-overlap
class BlockingGmail(FakeGmailClient):
    def __init__(self, email: str) -> None:
        super().__init__(email)
        self.entered, self.release = threading.Event(), threading.Event()

    def get_thread(self, thread_id: str):  # type: ignore[no-untyped-def]
        self.entered.set()
        assert self.release.wait(10)
        return super().get_thread(thread_id)


def test_run_cycle_returns_none_while_another_cycle_holds_the_lock(db: Session, team: Team) -> None:
    sent_message(db, team, team.ann)
    slow = BlockingGmail(team.ann.email)
    slow.threads = team.gmail[team.ann.id].threads
    team.gmail[team.ann.id] = slow
    poller = ReplyPoller(get_settings(), FakeLLMClient([]))
    out: list[object] = []
    t = threading.Thread(target=lambda: out.append(poller.run_cycle()))
    t.start()
    try:
        assert slow.entered.wait(10) and poller.running()
        assert poller.run_cycle() is None
        assert poller.run_cycle(force=True) is None
    finally:
        slow.release.set()
        t.join(10)
    assert not t.is_alive() and not poller.running()
    assert len(out) == 1 and out[0] is not None and len(out[0]) == 1  # type: ignore[arg-type]
    assert poller.run_cycle() is not None  # the lock was released


def test_a_crashing_cycle_returns_empty_and_releases_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = ReplyPoller(get_settings(), FakeLLMClient([]))

    def boom(**_: object) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(replies, "poll_all", boom)
    assert poller.run_cycle() == [] and not poller.running()


# ------------------------------------------------------------------ thread lifecycle
def test_no_thread_is_started_when_polling_is_off() -> None:
    poller = ReplyPoller(get_settings())
    assert not poller.enabled
    poller.start()
    assert poller._thread is None
    poller.stop()


def test_start_runs_cycles_and_stop_is_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    minutes(monkeypatch, 1)
    poller = ReplyPoller(get_settings())
    ran = threading.Event()
    n = {"cycles": 0}

    def fake_cycle(*, force: bool = False) -> list[object]:
        n["cycles"] += 1
        if n["cycles"] >= 3:
            ran.set()
        return []

    class QuickStop(threading.Event):
        """The 5 s boot delay and the 60 s interval collapse to milliseconds."""

        def wait(self, timeout: float | None = None) -> bool:
            return super().wait(0.01 if timeout else timeout)

    poller._stop = QuickStop()  # type: ignore[assignment]
    monkeypatch.setattr(poller, "run_cycle", fake_cycle)
    poller.start()
    thread = poller._thread
    assert thread is not None and thread.daemon and thread.is_alive()
    poller.start()  # second start is a no-op
    assert poller._thread is thread
    assert ran.wait(5)
    t0 = time.monotonic()
    poller.stop()
    assert time.monotonic() - t0 < 2 and not thread.is_alive()


def test_stop_before_the_first_cycle_is_prompt_with_the_real_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    minutes(monkeypatch, 1)
    poller = ReplyPoller(get_settings())
    calls: list[int] = []
    monkeypatch.setattr(poller, "run_cycle", lambda **_: calls.append(1))
    poller.start()
    thread = poller._thread
    assert thread is not None
    t0 = time.monotonic()
    poller.stop()
    assert time.monotonic() - t0 < 2 and not thread.is_alive() and calls == []


# ------------------------------------------------------------------ listing optimisation
def thread_fetches(gm: FakeGmailClient) -> list[str]:
    return [c[1][0] for c in gm.calls if c[0] == "get_thread"]


def three_threads(db: Session, team: Team):  # type: ignore[no-untyped-def]
    return [sent_message(db, team, team.ann, to=f"p{i}@acme.example", sent_at=T0 - timedelta(hours=9)) for i in range(3)]


def test_after_a_good_cycle_only_changed_threads_are_fetched(db: Session, team: Team) -> None:
    a, b, c = three_threads(db, team)
    gm = team.gmail[team.ann.id]
    first = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)
    assert first.threads_checked == 3 and sorted(thread_fetches(gm)) == sorted(m.gmail_thread_id for m in (a, b, c))
    assert not [x for x in gm.calls if x[0] == "list_threads_with_new_messages"]  # first cycle: nothing to list against
    gm.calls.clear()
    reply(team, team.ann, b, "p1@acme.example", date=T0 + timedelta(hours=2), snippet="late reply")
    second = replies.poll_member(team.ann.id, llm=FakeLLMClient([HUMAN]), now=T0 + timedelta(hours=3))
    assert thread_fetches(gm) == [b.gmail_thread_id] and second.threads_checked == 1 and second.new_replies == 1
    listing = [x for x in gm.calls if x[0] == "list_threads_with_new_messages"]
    assert len(listing) == 1 and listing[0][1][0] == T0 - timedelta(hours=1)  # last_ok minus the overlap
    gm.calls.clear()
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(hours=6))
    assert thread_fetches(gm) == []  # nothing changed since: no thread is fetched at all


def test_a_failed_listing_falls_back_to_every_thread(db: Session, team: Team) -> None:
    three_threads(db, team)
    gm = team.gmail[team.ann.id]
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)
    gm.calls.clear()

    def broken(since, *, limit=100):  # type: ignore[no-untyped-def]
        gm.calls.append(("list_threads_with_new_messages", (since,)))
        raise GmailTransientError("listing down")

    gm.list_threads_with_new_messages = broken  # type: ignore[method-assign]
    r = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(hours=3))
    assert r.status == "ok" and len(thread_fetches(gm)) == 3


def test_a_truncated_listing_falls_back_to_every_thread(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_REPLY_MAX_THREADS_PER_POLL", "3")
    reset_settings()
    msgs = three_threads(db, team)
    gm = team.gmail[team.ann.id]
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)
    gm.calls.clear()
    for m in msgs:  # three changed threads == the limit: the listing may be truncated
        reply(team, team.ann, m, f"p{msgs.index(m)}@acme.example", date=T0 + timedelta(hours=2))
    replies.poll_member(team.ann.id, llm=FakeLLMClient([HUMAN, HUMAN, HUMAN]), now=T0 + timedelta(hours=3))
    assert len(thread_fetches(gm)) == 3 and len(all_replies(db)) == 3


def test_an_auth_error_while_listing_is_a_poll_failure(db: Session, team: Team) -> None:
    three_threads(db, team)
    gm = team.gmail[team.ann.id]
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0)

    def revoked(since, *, limit=100):  # type: ignore[no-untyped-def]
        raise GmailAuthError("revoked")

    gm.list_threads_with_new_messages = revoked  # type: ignore[method-assign]
    r = replies.poll_member(team.ann.id, llm=FakeLLMClient([]), now=T0 + timedelta(hours=3))
    assert r.status == "error" and connections.connection_status(db, team.ann.id).status == "needs_reauth"
