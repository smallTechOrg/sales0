"""Review inbox (slice H): tabs, sort, cards, accept / discard / override, bulk, search,
field edits, retry triage (real Haiku), pagination and state survival."""

from __future__ import annotations

import hashlib
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, EnquirySource, TriageDecision
from sales0.services.dedupe import content_hash
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, add_llm_call, make_enquiry, make_run, triaged

P1_REASON = "Company seeking a training partner for an on-site GenAI workshop"
P2_REASON = "Training company recruiting freelance trainers"


def _card(html: str, enquiry_id: int) -> str:
    start = html.index(f'<article id="enquiry-{enquiry_id}"')
    return html[start : html.index("</article>", start)]


def _link(card_html: str) -> tuple[str, str, str] | None:
    """(data-link-kind, href, label) of the card's ``a.enquiry__link``; None when absent."""
    match = re.search(r'<a class="enquiry__link" href="([^"]*)"[^>]*data-link-kind="([^"]*)">([^<]*)</a>', card_html)
    return (match.group(2), match.group(1), match.group(3)) if match else None


def _card_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'<article id="enquiry-(\d+)"', html)]


def _count(html: str, tab: str) -> int:
    match = re.search(rf'<span data-count="{tab}">(\d+)</span>', html)
    assert match, f"no count for {tab}"
    return int(match.group(1))


def _decisions(enquiry_id: int) -> list[TriageDecision]:
    with session_scope() as db:
        return list(db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == enquiry_id).order_by(TriageDecision.id)))


def _seed_corpus() -> dict[str, int]:
    run_id = make_run("succeeded", posts_seen=4, posts_new=4)
    ids = {
        "P1": make_enquiry(
            "P1", run_id=run_id, keyword="corporate ai training",
            **triaged("genuine", P1_REASON, priority="high", offering_id=1,
                      summary="Acme Logistics - GenAI workshop for 40 managers - Pune/Offline",
                      company="Acme Logistics", email="training@acmelogistics.example", phone="+91 98200 11111",
                      city="Pune", mode="offline", dates_mentioned="week of 17 Nov"),
        ),
        "P2": make_enquiry("P2", run_id=run_id, **triaged("discard", P2_REASON, priority="low", offering_id=None,
                                                          summary="SkillBridge - hiring freelance GenAI trainers")),
        "P3": make_enquiry("P3", run_id=run_id, **triaged("discard", "Seeks an individual freelance trainer at hourly rate",
                                                          priority="low", offering_id=None, summary="Freelance trainer for prompt engineering",
                                                          poster_type="individual_trainer_seeker")),
        "P9": make_enquiry("P9", run_id=run_id, **triaged("discard", "Trainer advertising themselves", priority="low",
                                                          offering_id=None, summary="Certified trainer open to work",
                                                          poster_type="job_seeker_or_self_promo")),
    }
    ids["P6"] = make_enquiry("P6", run_id=run_id, triage_status="failed", triage_error="AI returned an invalid answer twice")
    ids["P10"] = make_enquiry("P10", run_id=run_id)  # triage still pending
    add_llm_call(1_100, enquiry_id=ids["P1"], input_tokens=1_000, output_tokens=300)
    return ids


def test_empty_db_shows_setup_guidance_and_zero_counts(client: TestClient) -> None:
    html = unescape(client.get("/inbox").text)
    assert "<h1>Review inbox</h1>" in html
    assert "No enquiries yet. Add keywords and connect LinkedIn in Setup, then press Run now." in html
    assert '<a class="btn btn--primary" href="/setup">Go to Setup</a>' in html
    assert [_count(html, t) for t in ("review", "accepted", "discarded")] == [0, 0, 0]
    assert 'id="inbox-search"' in html and 'placeholder="Search text, company, author…"' in html
    assert 'id="bulk-bar"' not in html


def test_after_runs_with_nothing_left_shows_all_caught_up_and_tab_empty_states(client: TestClient) -> None:
    make_run("succeeded")
    assert "All caught up — nothing to review." in client.get("/inbox").text
    assert "Accepted enquiries become prospects here." in client.get("/inbox?tab=accepted").text
    assert "Discarded enquiries are kept here and stay searchable." in client.get("/inbox?tab=discarded").text
    assert "All caught up" in client.get("/inbox?tab=bogus&page=-3").text  # bad params fall back, never break


def test_review_tab_sorts_failed_then_genuine_and_renders_card_details(client: TestClient) -> None:
    ids = _seed_corpus()
    html = unescape(client.get("/inbox").text)
    order = _card_ids(html)
    assert order[0] == ids["P6"]  # failed triage pinned first
    assert order[1] == ids["P1"]  # then AI-genuine
    assert _count(html, "review") == 6

    p1 = _card(html, ids["P1"])
    assert 'data-urn="urn:li:activity:7300000000000000001"' in p1
    assert 'data-verdict="genuine"' in p1 and 'data-review-status="pending_review"' in p1
    assert '<span class="badge badge--genuine">Genuine</span>' in p1 and P1_REASON in p1
    assert "Acme Logistics - GenAI workshop for 40 managers - Pune/Offline" in p1
    assert '<option value="high" selected>High</option>' in p1
    assert '<option value="1" selected>AI corporate workshops</option>' in p1 and ">No match</option>" in p1
    assert 'href="mailto:training@acmelogistics.example"' in p1 and "+91 98200 11111" in p1 and "Offline" in p1
    assert "Contact person" not in p1  # null details are not shown
    assert _link(p1) == ("post", "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/",
                         "View post ↗")
    assert 'target="_blank" rel="noopener"' in p1 and "Open post ↗" not in p1
    assert "<summary>Full post text</summary>" in p1 and "Please send your proposals" in p1
    assert '<span class="chip">corporate ai training</span>' in p1 and "Buyer company" in p1 and "1d ago" in p1
    assert re.search(r"AI \$0\.\d{4} · 1\.3k tok", p1)
    assert ">\n            Accept\n" in p1 or ">Accept<" in p1.replace("\n", "").replace(" ", "")

    p6 = _card(html, ids["P6"])
    assert "AI triage failed — AI returned an invalid answer twice" in p6 and "Retry triage" in p6
    assert "Asking AI…" in p6 and 'name="enquiry_ids"' in p6  # failed cards can still be decided

    p10 = _card(html, ids["P10"])
    assert "AI triage pending…" in p10 and "Accept" not in p10 and 'name="enquiry_ids"' not in p10


def test_accept_moves_card_to_accepted_with_counts_toast_and_decision(client: TestClient) -> None:
    ids = _seed_corpus()
    resp = client.post(
        "/inbox/decide", data={"enquiry_ids": ids["P1"], "decision": "accept", "tab": "review", "q": "", "page": "1"}, headers=HX
    )
    assert resp.status_code == 200
    body = unescape(resp.text)
    assert body.lstrip().startswith('<section id="inbox-list"')
    assert ids["P1"] not in _card_ids(body.split('id="inbox-tabs"')[0])
    tabs = body[body.index('<nav id="inbox-tabs"') :]
    assert 'hx-swap-oob="true"' in tabs[:120]
    assert (_count(tabs, "review"), _count(tabs, "accepted")) == (5, 1)
    assert "Accepted 1" in body and 'hx-swap-oob="beforeend"' in body

    accepted = unescape(client.get("/inbox?tab=accepted").text)
    card = _card(accepted, ids["P1"])
    assert "Prospect · New" in card and "Discard instead" in card and "Overrode AI" not in card
    rows = _decisions(ids["P1"])
    assert [(r.decision, r.is_override) for r in rows] == [("accept", False)]


def test_bulk_discard_with_reason_then_search_discarded(client: TestClient) -> None:
    ids = _seed_corpus()
    resp = client.post(
        "/inbox/decide",
        data={"enquiry_ids": [ids["P2"], ids["P3"]], "decision": "discard", "discard_reason": "Looking for individual trainers", "tab": "review"},
        headers=HX,
    )
    assert "Discarded 2" in resp.text and _count(resp.text, "discarded") == 2

    discarded = unescape(client.get("/inbox?tab=discarded").text)
    assert set(_card_ids(discarded)) == {ids["P2"], ids["P3"]}
    for key in ("P2", "P3"):
        card = _card(discarded, ids[key])
        assert "Discard reason:</span> Looking for individual trainers" in card and "Accept instead" in card

    hits = unescape(client.get("/inbox", params={"tab": "discarded", "q": "freelance"}).text)
    assert set(_card_ids(hits)) == {ids["P2"], ids["P3"]}
    assert 'value="freelance"' in hits
    miss = unescape(client.get("/inbox", params={"tab": "discarded", "q": "zzzz"}).text)
    assert "No enquiries match 'zzzz'." in miss and _card_ids(miss) == []


def test_accepting_an_ai_discard_is_an_override_and_discard_instead_appends(client: TestClient) -> None:
    ids = _seed_corpus()
    client.post("/inbox/decide", data={"enquiry_ids": ids["P9"], "decision": "accept"}, headers=HX)
    card = _card(client.get("/inbox?tab=accepted").text, ids["P9"])
    assert "Overrode AI" in card
    assert _decisions(ids["P9"])[0].is_override is True

    # Discard instead with "Other — use AI reason" (empty) falls back to the AI's reason.
    client.post("/inbox/decide", data={"enquiry_ids": ids["P9"], "decision": "discard", "discard_reason": "", "tab": "accepted"}, headers=HX)
    rows = _decisions(ids["P9"])
    assert [r.decision for r in rows] == ["accept", "discard"]
    assert rows[1].reason == "Trainer advertising themselves" and rows[1].is_override is False


def test_priority_and_offering_edits_persist_and_keep_ai_values(client: TestClient) -> None:
    ids = _seed_corpus()
    resp = client.post(f"/inbox/{ids['P1']}/fields", data={"priority": "medium", "tab": "review"}, headers=HX)
    card = unescape(resp.text)
    assert card.lstrip().startswith(f'<article id="enquiry-{ids["P1"]}"')
    assert '<option value="medium" selected>Medium</option>' in card and "Priority updated." in card
    assert '<option value="1" selected>' in card  # offering untouched by a priority-only change

    cleared = unescape(client.post(f"/inbox/{ids['P1']}/fields", data={"offering_id": "none"}, headers=HX).text)
    assert '<option value="none" selected>No match</option>' in cleared

    reloaded = _card(unescape(client.get("/inbox").text), ids["P1"])
    assert '<option value="medium" selected>Medium</option>' in reloaded
    with session_scope() as db:
        e = db.get(Enquiry, ids["P1"])
        assert e is not None and e.priority == "medium" and e.ai_priority == "high" and e.offering_id is None and e.ai_offering_id == 1

    bad = client.post(f"/inbox/{ids['P1']}/fields", data={"priority": "urgent"}, headers=HX)
    assert bad.headers.get("HX-Retarget") == "#toasts" and "Priority must be High, Medium or Low." in unescape(bad.text)


def test_decisions_on_missing_or_pending_enquiries_are_toasts_not_breakage(client: TestClient) -> None:
    ids = _seed_corpus()
    missing = client.post("/inbox/decide", data={"enquiry_ids": 99999, "decision": "accept"}, headers=HX)
    assert missing.status_code == 200 and missing.headers["HX-Retarget"] == "#toasts"
    assert "That item no longer exists — refresh the page." in unescape(missing.text)
    garbage = client.post("/inbox/decide", data={"enquiry_ids": "abc", "decision": "accept"}, headers=HX)
    assert garbage.headers["HX-Retarget"] == "#toasts"
    bad_decision = client.post("/inbox/decide", data={"enquiry_ids": ids["P1"], "decision": "maybe"}, headers=HX)
    assert "Choose Accept or Discard for at least one enquiry." in unescape(bad_decision.text)
    pending = client.post("/inbox/decide", data={"enquiry_ids": ids["P10"], "decision": "accept"}, headers=HX)
    assert "AI triage is still pending" in unescape(pending.text)
    assert _count(client.get("/inbox").text, "review") == 6  # nothing changed
    assert client.post("/inbox/99999/fields", data={"priority": "low"}, headers=HX).headers["HX-Retarget"] == "#toasts"


def test_non_htmx_decide_redirects_back_and_state_survives_restart(db_url: str) -> None:
    with TestClient(create_app()) as c:
        ids = _seed_corpus()
        resp = c.post("/inbox/decide", data={"enquiry_ids": ids["P1"], "decision": "accept", "tab": "review", "q": "acme"}, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/inbox?tab=review&q=acme"
        c.post("/inbox/decide", data={"enquiry_ids": ids["P2"], "decision": "discard", "discard_reason": "Training agency / marketplace"}, headers=HX)
    with TestClient(create_app()) as c:  # a fresh app process over the same DB file
        assert ids["P1"] in _card_ids(c.get("/inbox?tab=accepted").text)
        assert ids["P2"] in _card_ids(c.get("/inbox?tab=discarded").text)


def test_pagination_shows_25_per_page_with_prev_next(client: TestClient) -> None:
    make_run("succeeded")
    for n in range(27):
        make_enquiry("P1", text=f"Company {n} needs an AI workshop for its team, contact us {n}", added_offset_s=n,
                     **triaged("genuine", "Buyer wants a workshop", summary=f"Company {n} workshop"))
    first = client.get("/inbox").text
    assert len(_card_ids(first)) == 25 and "Page 1 of 2 · 27 enquiries" in first and "Next →" in first
    second = client.get("/inbox?tab=review&page=2").text
    assert len(_card_ids(second)) == 2 and "← Prev" in second
    beyond = client.get("/inbox?tab=review&page=9").text
    assert len(_card_ids(beyond)) == 2  # an out-of-range page shows the last real page


@pytest.mark.llm
def test_retry_triage_runs_one_real_haiku_call_and_updates_the_card(client: TestClient) -> None:
    run_id = make_run("succeeded")
    eid = make_enquiry("P1", run_id=run_id, triage_status="failed", triage_error="Anthropic timed out")
    resp = client.post(f"/inbox/{eid}/retry", data={"tab": "review"}, headers=HX)
    assert resp.status_code == 200
    body = unescape(resp.text)
    assert "HX-Retarget" not in resp.headers, body[-500:]
    card = body[: body.index("</article>")]
    assert 'data-verdict="genuine"' in card
    assert '<span class="badge badge--genuine">Genuine</span>' in card
    assert 'href="mailto:training@acmelogistics.example"' in card
    assert "Acme" in card and re.search(r"AI \$0\.\d{4} · [\d.]+k? tok", card)
    assert "AI triage finished." in body
    badge = re.search(r'id="cost-badge" class="cost-badge" hx-swap-oob="true".*?AI today <strong>\$(\d+\.\d+)</strong>', body, re.S)
    assert badge and float(badge.group(1)) > 0
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None and e.triage_status == "done" and e.ai_verdict == "genuine"


def test_retry_triage_on_missing_enquiry_is_a_not_found_toast(client: TestClient) -> None:
    resp = client.post("/inbox/424242/retry", headers=HX)
    assert resp.headers["HX-Retarget"] == "#toasts" and "That item no longer exists" in unescape(resp.text)


@pytest.mark.parametrize(
    "urn",
    ["urn:li:ugcPost:7311111111111111111", "urn:li:share:7322222222222222222", "urn:li:activity:7333333333333333333"],
)
def test_card_urn_falls_back_to_permalink_for_every_post_urn_kind(client: TestClient, urn: str) -> None:
    # No EnquirySource row: data-urn must come from parsing the permalink (scraper accepts all three kinds).
    raw = f"We need a corporate GenAI workshop for our team ({urn})"
    with session_scope() as db:
        e = Enquiry(content_hash=content_hash(raw), raw_text=raw, author_name="Asha Rao", author_headline="L&D Head",
                    source_url=f"https://www.linkedin.com/feed/update/{urn}/", date_added=utcnow())
        db.add(e)
        db.flush()
        enquiry_id = e.id
    card = _card(unescape(client.get("/inbox").text), enquiry_id)
    assert f'data-urn="{urn}"' in card
    assert f'href="https://www.linkedin.com/feed/update/{urn}/"' in card


# ---------------------------------------------------------------------- Phase 1.2: link labels
LINK_CASES = [
    ("post", "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000005/", "View post ↗"),
    ("post", "https://www.linkedin.com/posts/arjun-rao-fx_ai-agents-workshop-AbCd/", "View post ↗"),
    ("profile", "https://www.linkedin.com/in/priya-menon-acme/", "Author profile (post link unavailable) ↗"),
    ("profile", "https://www.linkedin.com/company/briltus-academy/", "Author profile (post link unavailable) ↗"),
    ("search", "https://www.linkedin.com/search/results/content/?keywords=ai%20workshop",
     "Search results (post link unavailable) ↗"),
    ("other", "https://example.invalid/elsewhere", "Open link ↗"),
]


def _enquiry_with_link(url: str | None, ref: str | None = None, text: str | None = None) -> int:
    raw = text or f"We need a corporate GenAI workshop for our team, link {url}"
    with session_scope() as db:
        unique = hashlib.sha256(f"{raw}|{url}|{ref}".encode()).hexdigest()
        e = Enquiry(content_hash=unique, raw_text=raw, author_name="Asha Rao",
                    author_headline="L&D Head", source_url=url, date_added=utcnow(),
                    **triaged("genuine", P1_REASON))
        db.add(e)
        db.flush()
        if ref:
            db.add(EnquirySource(enquiry_id=e.id, source_ref=ref, source_url=None, first_seen_at=utcnow()))
        return e.id


@pytest.mark.parametrize(("kind", "url", "label"), LINK_CASES)
def test_card_link_label_and_data_link_kind_follow_the_url_shape(client: TestClient, kind: str, url: str,
                                                                 label: str) -> None:
    eid = _enquiry_with_link(url)
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card) == (kind, url, label)


def test_fixture_mode_links_are_classified_after_the_fixture_prefix(client: TestClient) -> None:
    base = "http://127.0.0.1:8788/__fixtures/linkedin"
    post_id = _enquiry_with_link(f"{base}/feed/update/urn:li:activity:7300000000000000005/")
    profile_id = _enquiry_with_link(f"{base}/in/meera-iyer-orbit/")
    html = unescape(client.get("/inbox").text)
    assert _link(_card(html, post_id))[::2] == ("post", "View post ↗")  # type: ignore[index]
    assert _link(_card(html, profile_id))[::2] == ("profile", "Author profile (post link unavailable) ↗")  # type: ignore[index]


def test_card_without_source_url_shows_no_link(client: TestClient) -> None:
    eid = _enquiry_with_link(None, ref="ck:NoLinkKey01")
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card) is None and "enquiry__link" not in card
    assert 'data-urn="ck:NoLinkKey01"' in card  # data-urn may hold a ck: ref (ui.md test hooks)


def test_link_label_upgrades_after_a_later_run_finds_the_permalink(client: TestClient) -> None:
    from sales0.services import dedupe
    from tests.integration.services.helpers import sdui_post

    text = ("Orbit Health wants a partner to run a Build your first AI agent workshop for 25 engineers, fully "
            "online, in December. Reach out if you do this kind of hands-on session for product teams.")
    run1, run2 = make_run("succeeded"), make_run("succeeded")
    with session_scope() as db:
        _, eid = dedupe.upsert_post(db, sdui_post("KeyP6a", text, slug="in/meera-iyer-orbit"), run1, None, "ai workshop")
    html = unescape(client.get("/inbox").text)
    assert _link(_card(html, eid)) == ("profile", "https://www.linkedin.com/in/meera-iyer-orbit/",
                                       "Author profile (post link unavailable) ↗")
    assert 'data-urn="ck:KeyP6a"' in _card(html, eid)
    urn = "urn:li:activity:7300000000000000006"
    with session_scope() as db:
        kind, again = dedupe.upsert_post(db, sdui_post("KeyP6b", text, slug="in/meera-iyer-orbit", urn=urn),
                                         run2, None, "ai workshop")
    assert (kind, again) == ("dupe_content", eid)
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card) == ("post", f"https://www.linkedin.com/feed/update/{urn}/", "View post ↗")
    assert 'data-urn="ck:KeyP6a"' in card  # the first source ref is unchanged


def test_link_href_is_escaped(client: TestClient) -> None:
    eid = _enquiry_with_link('https://www.linkedin.com/in/x"onmouseover="alert(1)/')
    raw_html = client.get("/inbox").text
    card = raw_html[raw_html.index(f'<article id="enquiry-{eid}"'):]
    card = card[: card.index("</article>")]
    assert 'onmouseover="alert(1)' not in card


# ---------------------------------------------------------------------- Phase 1.4: post link / posted-ago
def test_card_with_profile_link_shows_posted_ago_hint(client: TestClient) -> None:
    eid = _enquiry_with_link("https://www.linkedin.com/in/priya-menon-acme/")
    with session_scope() as db:
        db.get(Enquiry, eid).posted_relative = "3h"
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card) == ("profile", "https://www.linkedin.com/in/priya-menon-acme/",
                           "Author profile (post link unavailable) ↗")
    assert "· posted ~3h before sales0 found it" in card


def test_card_with_captured_post_link_shows_view_post_and_no_hint(client: TestClient) -> None:
    eid = _enquiry_with_link("https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000077/")
    with session_scope() as db:
        db.get(Enquiry, eid).posted_relative = "3h"
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card)[::2] == ("post", "View post ↗")  # type: ignore[index]
    assert "posted ~" not in card


def test_card_without_source_url_falls_back_to_the_author_profile(client: TestClient) -> None:
    eid = _enquiry_with_link(None, ref="ck:NoLinkKey02")
    with session_scope() as db:
        row = db.get(Enquiry, eid)
        row.author_profile_url, row.posted_relative = "https://www.linkedin.com/in/asha-rao/", "2d"
    card = _card(unescape(client.get("/inbox").text), eid)
    assert _link(card) == ("profile", "https://www.linkedin.com/in/asha-rao/", "Author profile (post link unavailable) ↗")
    assert "posted ~2d before sales0 found it" in card
