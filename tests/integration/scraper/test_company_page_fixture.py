"""The worker's company-page reader with the REAL ``LinkedInSearcher`` (headless Chromium) against the FIXTURE
LinkedIn only (fake companies; the real linkedin.com is never opened)."""

from __future__ import annotations

import pytest

from sales0.config import Settings
from sales0.scraper.company_page import read_company_page
from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.search import LinkedInSearcher
from sales0.worker import protocol as p
from sales0.worker.runner import Worker

pytestmark = pytest.mark.browser


def test_reads_about_and_recent_posts_text_with_one_page_load(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings, page_load_budget=1) as searcher:
        page = read_company_page(searcher, "company/watch-brightfield")
        assert searcher.page_loads == 1
    assert "Brightfield Pharma" in page.text and "quality-control lab" in page.text and "About" in page.text
    assert page.url.endswith("/company/watch-brightfield/")


def test_checkpoint_on_the_company_page_is_a_safety_stop(connected_settings: Settings) -> None:
    with pytest.raises(AccountSafetyStop) as stop, LinkedInSearcher(connected_settings, page_load_budget=1) as searcher:
        read_company_page(searcher, "company/watch-checkpoint-test")
    assert stop.value.kind == "checkpoint"


def test_unknown_company_is_a_page_error_and_the_budget_is_respected(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings, page_load_budget=1) as searcher:
        with pytest.raises(ScrapeError):
            read_company_page(searcher, "company/watch-unknown")
        with pytest.raises(ScrapeError, match="budget"):
            read_company_page(searcher, "company/watch-brightfield")  # a second page in the same task is refused


def test_the_worker_task_path_uses_the_real_reader(connected_settings: Settings) -> None:
    class Client:
        sent: list = []

        def task_result(self, task_id: int, req: p.TaskResultRequest) -> p.TaskResultResponse:
            self.sent.append((task_id, req))
            return p.TaskResultResponse(status="done")

    client = Client()
    w = Worker(connected_settings, client, sleep=lambda _s: None)  # type: ignore[arg-type]
    w.execute_task(p.ClaimedTask(task_id=7, kind="company_page", slug="company/watch-brightfield"))
    (tid, req), = client.sent
    assert tid == 7 and req.outcome == "done" and "Brightfield" in (req.text or "") and req.page_loads == 1
