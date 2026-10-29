"""The /suggestions page and its POSTs, with the fake LLM behind the route's seam."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Keyword, LlmCall, Suggestion
from sales0.llm import LLMAuthError
from sales0.web.routes.suggestions import get_llm
from tests.integration.suggest.conftest import Seed
from tests.integration.suggest.test_agent_fake import answer
from tests.support.fake_llm import FakeLLMClient


def use(web: TestClient, fake: FakeLLMClient) -> None:
    web.app.dependency_overrides[get_llm] = lambda: fake  # type: ignore[attr-defined]


def test_empty_db_renders(web: TestClient) -> None:
    r = web.get("/suggestions")
    assert r.status_code == 200
    assert (
        "Find suggestions" in r.text
        and "No suggestions waiting" in r.text
        and "No keywords yet" in r.text
    )


def test_generate_accept_dismiss_flow(web: TestClient, seeded: Seed) -> None:
    use(web, FakeLLMClient([answer(seeded)]))
    r = web.post("/suggestions/generate", follow_redirects=True)
    assert r.status_code == 200
    assert "3 new suggestion(s)" in r.text and "AI cost $" in r.text
    assert (
        "prompt engineering workshop" in r.text
        and "Pause keyword" in r.text
        and "Zentora Foods" in r.text
    )
    assert 'id="yield-table"' in r.text and "excel training" in r.text
    with session_scope() as db:
        ids = {s.text: s.id for s in db.scalars(select(Suggestion))}
    r = web.post(
        f"/suggestions/{ids['prompt engineering workshop']}/accept",
        follow_redirects=True,
    )
    assert "Added keyword" in r.text
    r = web.post(f"/suggestions/{ids['excel training']}/accept", follow_redirects=True)
    assert "Paused keyword" in r.text
    r = web.post(
        f"/suggestions/{ids['copilot adoption trainer']}/dismiss", follow_redirects=True
    )
    assert "Dismissed" in r.text and 'id="decided-list"' in r.text
    with session_scope() as db:
        assert (
            db.scalar(select(Keyword.active).where(Keyword.text == "excel training"))
            is False
        )
        assert (
            db.scalar(
                select(Keyword.id).where(
                    Keyword.text_key == "prompt engineering workshop"
                )
            )
            is not None
        )
    again = web.post(
        f"/suggestions/{ids['copilot adoption trainer']}/accept", follow_redirects=True
    )
    assert "already dismissed" in again.text


def test_generate_thin_data_and_llm_error_flash(web: TestClient, seeded: Seed) -> None:
    use(web, FakeLLMClient([LLMAuthError("API key rejected.")]))
    r = web.post("/suggestions/generate", follow_redirects=True)
    assert (
        r.status_code == 200
        and "API key rejected" in r.text
        and 'role="alert"' in r.text
    )
    with session_scope() as db:
        assert db.scalars(select(LlmCall)).one().status == "error"


def test_generate_blocked_by_spend_cap(
    web: TestClient, seeded: Seed, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "1")
    reset_settings()
    with session_scope() as db:
        db.add(
            LlmCall(feature="triage", model="m", status="ok", cost_usd_micros=3_000_000)
        )
    fake = FakeLLMClient([answer(seeded)])
    use(web, fake)
    r = web.post("/suggestions/generate", follow_redirects=True)
    assert r.status_code == 200 and fake.calls == 0 and "cap" in r.text.lower()


def test_unknown_suggestion_is_a_flash_not_a_500(web: TestClient) -> None:
    r = web.post("/suggestions/777/dismiss", follow_redirects=True)
    assert r.status_code == 200 and "no longer exists" in r.text


def test_escapes_html_in_phrase(web: TestClient, db: Session) -> None:
    db.add(
        Suggestion(
            kind="new_keyword",
            text="<script>x</script>",
            dedupe_key="new_keyword:<script>x</script>",
            rationale="<b>r</b>",
        )
    )
    db.commit()
    r = web.get("/suggestions")
    assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text
