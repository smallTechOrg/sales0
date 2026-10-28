"""RealGmailClient.get_thread / list_threads_with_new_messages against a fake Google (MockTransport)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sales0.integrations.gmail.auth import SCOPE_MODIFY, SCOPES, StoredToken, TokenProvider
from sales0.integrations.gmail.client import RealGmailClient
from sales0.integrations.gmail.errors import GmailNotFound, GmailScopeError, GmailAuthError
from sales0.integrations.gmail.threads import METADATA_HEADERS


def hdrs(**kv: str) -> list[dict[str, str]]:
    return [{"name": k.replace("_", "-"), "value": v} for k, v in kv.items()]


def raw_msg(mid: str, headers: list[dict[str, str]], *, internal: str | None = None, labels: list[str] | None = None, snippet: str = "snip") -> dict[str, Any]:
    out: dict[str, Any] = {"id": mid, "threadId": "t1", "snippet": snippet, "labelIds": labels or ["INBOX"], "payload": {"headers": headers}}
    if internal is not None:
        out["internalDate"] = internal
    return out


class Google:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []
        self.thread: dict[str, Any] = {"id": "t1", "messages": []}
        self.pages: list[dict[str, Any]] = []
        self.token_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"AT{self.token_calls}", "expires_in": 3600})
        if self.script:
            return self.script.pop(0)
        if req.url.path.endswith("/threads"):
            return httpx.Response(200, json=self.pages.pop(0) if self.pages else {})
        return httpx.Response(200, json=self.thread)

    @property
    def api(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != "oauth2.googleapis.com"]


def make(scopes: list[str] | None = None) -> tuple[RealGmailClient, Google]:
    g = Google()
    http = httpx.Client(transport=httpx.MockTransport(g))
    tok = StoredToken("cid", "csec", "RT", list(SCOPES if scopes is None else scopes), "me@example.com")
    return RealGmailClient(TokenProvider(tok, http), http, sleep=lambda s: None), g


# ------------------------------------------------------------------ parsing
def test_get_thread_parses_every_header_and_label() -> None:
    c, g = make()
    g.thread["messages"] = [
        raw_msg(
            "m1",
            hdrs(
                From='"Priya Menon" <Priya.Menon@Acme.Example>', To="Ann <ANN@team.example>, bob@team.example", Cc="Cc1@X.example, Dee <dee@y.example>",
                Subject="Re: Workshop", Date="Tue, 27 Oct 2026 10:00:00 +0000", Message_ID="<abc@acme>", In_Reply_To="<orig@team>",
                References="<orig@team> <abc@acme>", Auto_Submitted="auto-replied", X_Autoreply="yes", Precedence="bulk",
                Content_Type="multipart/report; report-type=delivery-status", X_Failed_Recipients="gone@x.example",
            ),
            internal="1793095200000", labels=["INBOX", "UNREAD"], snippet="Thanks!",
        )
    ]
    (m,) = c.get_thread("t1")
    assert m.id == "m1" and m.thread_id == "t1"
    assert m.from_addr == "priya.menon@acme.example" and m.from_name == "Priya Menon"
    assert m.to_addrs == ("ann@team.example", "bob@team.example")
    assert m.cc_addrs == ("cc1@x.example", "dee@y.example")
    assert m.subject == "Re: Workshop" and m.snippet == "Thanks!"
    assert m.message_id_header == "<abc@acme>" and m.in_reply_to == "<orig@team>" and m.references == "<orig@team> <abc@acme>"
    assert (m.auto_submitted, m.x_autoreply, m.precedence) == ("auto-replied", "yes", "bulk")
    assert m.content_type.startswith("multipart/report") and m.x_failed_recipients == "gone@x.example"
    assert m.labels == ("INBOX", "UNREAD") and not m.is_sent and not m.is_draft


def test_internal_date_wins_and_date_header_is_the_fallback() -> None:
    c, g = make()
    g.thread["messages"] = [
        raw_msg("a", hdrs(From="a@x.example", Date="Mon, 01 Jan 2024 00:00:00 +0000"), internal="1793095200000"),
        raw_msg("b", hdrs(From="b@x.example", Date="Tue, 27 Oct 2026 12:30:00 +0530")),
        raw_msg("c", hdrs(From="c@x.example", Date="garbage")),
    ]
    a, b, cc = sorted(c.get_thread("t1"), key=lambda m: m.id)
    assert a.date == datetime.fromtimestamp(1793095200, UTC)
    assert b.date == datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
    assert cc.date == datetime.fromtimestamp(0, UTC)  # unparseable: epoch, never a crash


def test_messages_are_sorted_oldest_first_and_header_case_is_ignored() -> None:
    c, g = make()
    g.thread["messages"] = [
        raw_msg("late", [{"name": "FROM", "value": "L@x.example"}], internal="2000000000000"),
        raw_msg("early", [{"name": "from", "value": "E@x.example"}], internal="1000000000000"),
        raw_msg("mid", [{"name": "From", "value": "M@x.example"}], internal="1500000000000"),
    ]
    got = c.get_thread("t1")
    assert [m.id for m in got] == ["early", "mid", "late"]
    assert got[0].from_addr == "e@x.example"


def test_sent_and_draft_labels_are_exposed_and_empty_thread_is_fine() -> None:
    c, g = make()
    g.thread["messages"] = [raw_msg("s", hdrs(From="me@example.com"), labels=["SENT"]), raw_msg("d", hdrs(From="me@example.com"), labels=["DRAFT"])]
    d, s = sorted(c.get_thread("t1"), key=lambda m: m.id)
    assert s.is_sent and not s.is_draft and d.is_draft and not d.is_sent
    g.thread = {"id": "t1"}
    assert c.get_thread("t1") == []


def test_x_autorespond_counts_as_x_autoreply() -> None:
    c, g = make()
    g.thread["messages"] = [raw_msg("m", hdrs(From="a@x.example", X_Autorespond="yes"))]
    assert c.get_thread("t1")[0].x_autoreply == "yes"


# ------------------------------------------------------------------ request shape
def test_get_thread_asks_for_metadata_only_with_metadata_headers() -> None:
    c, g = make()
    c.get_thread("thr-9")
    (req,) = g.api
    assert req.method == "GET" and req.url.path.endswith("/users/me/threads/thr-9")
    q = req.url.params
    assert q["format"] == "metadata" and q.get_list("metadataHeaders") == list(METADATA_HEADERS)
    assert "format=full" not in str(req.url) and "format=raw" not in str(req.url)
    assert req.headers["authorization"] == "Bearer AT1"


def test_list_uses_after_epoch_query() -> None:
    c, g = make()
    g.pages = [{"threads": [{"id": "t1"}, {"id": "t2"}]}]
    since = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    assert c.list_threads_with_new_messages(since) == ["t1", "t2"]
    (req,) = g.api
    assert req.url.params["q"] == f"after:{int(since.timestamp())}" and req.url.params["maxResults"] == "100"


def test_list_follows_next_page_token_and_honours_limit() -> None:
    c, g = make()
    g.pages = [
        {"threads": [{"id": f"a{i}"} for i in range(3)], "nextPageToken": "P2"},
        {"threads": [{"id": f"b{i}"} for i in range(3)], "nextPageToken": "P3"},
        {"threads": [{"id": "c0"}]},
    ]
    ids = c.list_threads_with_new_messages(datetime.now(UTC), limit=5)
    assert ids == ["a0", "a1", "a2", "b0", "b1"]
    assert len(g.api) == 2 and g.api[1].url.params["pageToken"] == "P2" and g.api[1].url.params["maxResults"] == "2"
    g.requests.clear()
    g.pages = [{"threads": [{"id": "x"}], "nextPageToken": "N"}, {"threads": [{"id": "y"}]}]
    assert c.list_threads_with_new_messages(datetime.now(UTC)) == ["x", "y"]


def test_list_with_no_threads_key_is_empty() -> None:
    c, g = make()
    g.pages = [{"resultSizeEstimate": 0}]
    assert c.list_threads_with_new_messages(datetime.now(UTC)) == []


# ------------------------------------------------------------------ errors and scopes
def test_404_is_gmail_not_found() -> None:
    c, g = make()
    g.script = [httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})]
    with pytest.raises(GmailNotFound):
        c.get_thread("gone")


def test_401_refreshes_the_token_once_then_succeeds() -> None:
    c, g = make()
    g.script = [httpx.Response(401, json={"error": {"message": "expired"}})]
    g.thread["messages"] = [raw_msg("m", hdrs(From="a@x.example"))]
    assert [m.id for m in c.get_thread("t1")] == ["m"]
    assert g.token_calls == 2 and [r.headers["authorization"] for r in g.api] == ["Bearer AT1", "Bearer AT2"]


def test_persistent_401_is_an_auth_error() -> None:
    c, g = make()
    g.script = [httpx.Response(401, json={}), httpx.Response(401, json={})]
    with pytest.raises(GmailAuthError):
        c.list_threads_with_new_messages(datetime.now(UTC))


def test_403_insufficient_scope_is_scope_error() -> None:
    c, g = make()
    g.script = [httpx.Response(403, json={"error": {"message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}})]
    with pytest.raises(GmailScopeError):
        c.get_thread("t1")


@pytest.mark.parametrize("call", ["get_thread", "list"])
def test_openid_only_token_is_refused_before_any_network_call(call: str) -> None:
    c, g = make(scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"])
    with pytest.raises(GmailScopeError):
        c.get_thread("t1") if call == "get_thread" else c.list_threads_with_new_messages(datetime.now(UTC))
    assert g.requests == []


@pytest.mark.parametrize("scope", [SCOPE_MODIFY, "https://www.googleapis.com/auth/gmail.readonly"])
def test_modify_or_readonly_scope_is_enough(scope: str) -> None:
    c, g = make(scopes=[scope])
    assert c.get_thread("t1") == []
    assert c.list_threads_with_new_messages(datetime.now(UTC)) == []


def test_compose_only_token_cannot_read() -> None:
    c, g = make(scopes=["https://www.googleapis.com/auth/gmail.compose"])
    with pytest.raises(GmailScopeError):
        c.get_thread("t1")
    assert g.requests == []


# ------------------------------------------------------------------ read-only guard
def test_reply_tracking_methods_only_ever_issue_get_requests() -> None:
    c, g = make()
    g.thread["messages"] = [raw_msg("m", hdrs(From="a@x.example"))]
    g.pages = [{"threads": [{"id": "t1"}], "nextPageToken": "n"}, {"threads": [{"id": "t2"}]}]
    c.get_thread("t1")
    c.list_threads_with_new_messages(datetime.now(UTC))
    api = g.api
    assert len(api) == 3
    assert {r.method for r in api} == {"GET"}
    assert not any(seg in r.url.path for r in api for seg in ("/modify", "/trash", "/send", "/drafts", "/labels", "/untrash"))
    assert all(r.content == b"" for r in api)
