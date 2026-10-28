"""Server restart: the app's startup hook marks an interrupted run aborted (no cool-down) (state survival)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.services import runs
from sales0.web.app import create_app
from tests.integration.services.helpers import make_run


def test_app_startup_marks_a_running_run_aborted_interrupted_by_restart(db_url: str) -> None:
    interrupted = make_run("running")
    finished = make_run("succeeded")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
    with session_scope() as db:
        assert runs.get_run(db, interrupted).run.status == "aborted"
        assert runs.get_run(db, interrupted).run.abort_reason is None
        assert runs.get_run(db, interrupted).run.error_message == runs.INTERRUPTED_MESSAGE
        assert runs.get_run(db, finished).run.status == "succeeded"
