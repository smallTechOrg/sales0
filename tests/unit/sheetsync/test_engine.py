from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sales0.services.sheetsync import (
    InMemorySheetRepository,
    SheetFetchError,
    StaticSource,
    SyncEngine,
)
from tests.unit.sheetsync import fixtures as fx

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def make(texts: dict[str, str | Exception] | None = None, **kw):
    src = StaticSource(texts or fx.all_tabs_csv())
    repo = InMemorySheetRepository(clock=lambda: NOW)
    engine = SyncEngine(src, repo, fx.TABS, today=lambda: fx.TODAY, now=lambda: NOW, **kw)
    return src, repo, engine


def counts(report):
    return {n: (t.added, t.updated, t.unchanged, t.removed) for n, t in report.tabs.items()}


def test_first_sync_adds_everything_and_never_aborts_on_bad_rows() -> None:
    _, repo, engine = make()
    rep = engine.run()
    assert rep.ok and not rep.dry_run
    t = rep.tabs
    assert t["Active Enquiries"].added == 7 and len(t["Active Enquiries"].errors) == 1 and t["Active Enquiries"].rows == 8
    assert t["Discarded Enquiries"].added == 2
    assert t["Competitors"].added == 3 and t["Groups"].added == 4
    assert t["Weekly"].added == 3 and t["Secondary"].added == 4
    assert rep.total_changes == len(repo.events) > 0
    assert all(e["actor"] == "sheet-sync" for e in repo.events)
    assert {e["action"] for e in repo.events} == {"created", "updated"}  # "updated": 2nd tab of a shared person


def test_idempotent_second_run_changes_nothing() -> None:
    _, repo, engine = make()
    engine.run()
    events_before = len(repo.events)
    snapshot = {k: (dict(v.fields), dict(v.sources)) for k, v in repo.records.items()}
    rep = engine.run()
    assert rep.total_changes == 0
    assert all(t.added == t.updated == t.removed == 0 for t in rep.tabs.values())
    assert sum(t.unchanged for t in rep.tabs.values()) > 20
    assert len(repo.events) == events_before
    assert snapshot == {k: (dict(v.fields), dict(v.sources)) for k, v in repo.records.items()}


def test_dry_run_reports_the_same_diff_without_applying() -> None:
    _, repo, engine = make()
    dry = engine.run(dry_run=True)
    assert dry.dry_run and repo.records == {} and repo.events == []
    real = engine.run()
    assert counts(dry) == counts(real)
    # and dry-run after apply shows nothing to do
    assert engine.run(dry_run=True).total_changes == 0


def test_dry_run_matches_apply_for_edits_and_removals() -> None:
    src, repo, engine = make()
    engine.run()
    rows = list(fx.ACTIVE_ROWS)
    rows[0] = fx.active_row(**{**{"posted": "09 July", "added": "12 July", "summary": "Acme Corp - GenAI workshop - Pune | Online",
                                  "link": "https://www.linkedin.com/posts/x-activity-7000000000000000001-AbCd", "status": "Closed"}})
    src.texts["Active Enquiries"] = fx.active_csv(rows[:1] + rows[1:2])  # edit row 1, drop the rest
    dry = engine.run(dry_run=True)
    real = engine.run()
    assert counts(dry) == counts(real)
    assert real.tabs["Active Enquiries"].updated == 1


def test_sheet_edit_propagates_and_only_sheet_fields_change() -> None:
    src, repo, engine = make()
    engine.run()
    rid = ("enquiry", "post:7000000000000000001")
    stored = repo.get_by_key(*rid)
    assert stored.fields["sheet_status"] == "Email Sent"
    # the CRM owns these; a sync must never touch them
    stored.crm.update({"stage": "proposal", "ai_verdict": "genuine", "app_notes": ["called"], "flags": ["hot"]})

    rows = list(fx.ACTIVE_ROWS)
    rows[0] = list(rows[0])
    rows[0][5] = "Sent Pricing"  # Status
    rows[0][6] = "Pricing sent 14/07"  # Notes
    src.texts["Active Enquiries"] = fx.active_csv(rows)
    rep = engine.run()

    assert rep.tabs["Active Enquiries"].updated == 1 and rep.tabs["Active Enquiries"].added == 0
    stored = repo.get_by_key(*rid)
    assert (stored.fields["sheet_status"], stored.fields["sheet_notes"]) == ("Sent Pricing", "Pricing sent 14/07")
    assert stored.crm == {"stage": "proposal", "ai_verdict": "genuine", "app_notes": ["called"], "flags": ["hot"]}
    assert repo.events[-1]["action"] == "updated" and repo.events[-1]["actor"] == "sheet-sync"
    # moving a row without editing it is not an update, but the source row number follows
    rows2 = [fx.active_row(summary="Inserted - Row - Pune | Online", email="i@i.example"), *rows]
    src.texts["Active Enquiries"] = fx.active_csv(rows2)
    rep2 = engine.run()
    assert rep2.tabs["Active Enquiries"].added == 1 and rep2.tabs["Active Enquiries"].updated == 0
    assert repo.get_by_key(*rid).sources[(fx.SID, "0")].row_number == 3


def test_cleared_cell_clears_the_sheet_field() -> None:
    src, repo, engine = make()
    engine.run()
    rows = list(fx.ACTIVE_ROWS)
    rows[0] = list(rows[0])
    rows[0][6] = ""  # notes cleared
    src.texts["Active Enquiries"] = fx.active_csv(rows)
    engine.run()
    assert repo.get_by_key("enquiry", "post:7000000000000000001").fields["sheet_notes"] is None


def test_removal_is_soft_then_restored_when_the_row_returns() -> None:
    src, repo, engine = make()
    engine.run()
    full = src.texts["Active Enquiries"]
    src.texts["Active Enquiries"] = fx.active_csv([fx.ACTIVE_ROWS[0], *fx.ACTIVE_ROWS[2:]])  # Beta row gone
    rep = engine.run()
    assert rep.tabs["Active Enquiries"].removed == 1 and rep.tabs["Active Enquiries"].updated == 0
    gone = repo.get_by_key("enquiry", "post:7000000000000000002")
    assert gone is not None and gone.sheet_removed_at == NOW  # soft: record kept
    assert gone.fields["company"] == "Beta Ltd"
    assert repo.events[-1]["action"] == "removed"
    # a second run does not re-remove
    assert engine.run().tabs["Active Enquiries"].removed == 0
    src.texts["Active Enquiries"] = full
    rep = engine.run()
    assert rep.tabs["Active Enquiries"].added == 1  # no live source for the tab -> counted as added
    back = repo.get_by_key("enquiry", "post:7000000000000000002")
    assert back.sheet_removed_at is None and repo.events[-1]["action"] == "restored"


def test_same_person_in_outreach_and_prospects_is_one_entity_with_two_sources() -> None:
    src, repo, engine = make()
    rep = engine.run()
    assert rep.cross_tab_collisions == 1
    rid = ("prospect", "li:pat-example-123")
    pat = repo.get_by_key(*rid)
    assert set(pat.sources) == {(fx.SID, "12"), (fx.SID, "13")}
    assert pat.fields["sheet_status"] == "Message sent"  # Prospects' blank status does not erase Outreach's
    assert "also prospect" in pat.fields["sheet_notes"]  # later tab wins where it has a value
    # per-tab tracking: each tab reports its own add
    assert rep.tabs["Outreach"].added == 3 and rep.tabs["Prospects"].added == 2

    # removing him from Prospects keeps him alive via Outreach
    src.texts["Prospects"] = fx.to_csv([fx.PERSON_HEADER, ["New Person", "https://www.linkedin.com/in/new-person-9/", "", "", "", "", "", "n"]])
    rep = engine.run()
    assert rep.tabs["Prospects"].removed == 1
    pat = repo.get_by_key(*rid)
    assert pat.sheet_removed_at is None and set(pat.sources) == {(fx.SID, "12")}
    # ...and removing him from Outreach too finally soft-removes him
    src.texts["Outreach"] = fx.to_csv([fx.PERSON_HEADER, ["Sam Sample", "", "Sam Inc", "", "Connection request sent", "", "", ""],
                                              ["Lee Link", "https://lnkd.in/p/abc", "Lee LLC", "", "Closed", "", "", "x"]])
    engine.run()
    assert repo.get_by_key(*rid).sheet_removed_at == NOW


def test_enquiry_and_discarded_with_same_content_do_not_collide() -> None:
    row = fx.active_row(summary="Same - Topic - Pune | Online", email="s@s.example")
    src, repo, engine = make({**fx.all_tabs_csv(), "Active Enquiries": fx.active_csv([row])})
    src.texts["Discarded Enquiries"] = fx.to_csv([fx.DISCARDED_HEADER, row[:13]])
    engine.run()
    kinds = {rid[0] for rid in repo.records}
    assert {"enquiry", "discarded"} <= kinds
    assert sum(1 for rid in repo.records if rid[0] in ("enquiry", "discarded")) == 2


def test_fetch_failure_isolates_the_tab_and_never_removes() -> None:
    src, repo, engine = make()
    engine.run()
    n_before = sum(1 for r in repo.records.values() if r.sheet_removed_at is None)
    src.texts["Active Enquiries"] = SheetFetchError("http-500")
    src.texts["Groups"] = RuntimeError("boom")
    rep = engine.run()
    assert not rep.ok
    assert rep.tabs["Active Enquiries"].fetch_error == "http-500"
    assert rep.tabs["Groups"].fetch_error == "fetch-failed:RuntimeError"
    assert rep.tabs["Competitors"].unchanged == 3  # other tabs still synced
    assert sum(1 for r in repo.records.values() if r.sheet_removed_at is None) == n_before


def test_unreadable_header_is_reported_and_removes_nothing() -> None:
    src, repo, engine = make()
    engine.run()
    src.texts["Competitors"] = "totally,different\nstuff,here\n"
    rep = engine.run()
    assert rep.tabs["Competitors"].fetch_error == "header-not-found" and rep.tabs["Competitors"].removed == 0
    assert repo.list_keys_for_tab(fx.SID, "15")


def test_mass_removal_guard() -> None:
    rows = [fx.active_row(summary=f"Co{i} - Topic - Pune | Online", email=f"c{i}@x.example") for i in range(12)]
    src, repo, engine = make({**fx.all_tabs_csv(), "Active Enquiries": fx.active_csv(rows)})
    engine.run()
    src.texts["Active Enquiries"] = fx.active_csv(rows[:2])  # sheet suddenly nearly empty
    rep = engine.run()
    t = rep.tabs["Active Enquiries"]
    assert t.removed == 0 and t.removal_blocked == "10-of-12"
    assert all(r.sheet_removed_at is None for r in repo.records.values())
    # an operator can opt in
    engine2 = SyncEngine(src, repo, fx.TABS, today=lambda: fx.TODAY, now=lambda: NOW, allow_mass_removal=True)
    assert engine2.run().tabs["Active Enquiries"].removed == 10


def test_only_tab_and_unknown_tab() -> None:
    _, repo, engine = make()
    rep = engine.run(only_tab="competitors")
    assert list(rep.tabs) == ["Competitors"]
    with pytest.raises(KeyError):
        engine.run(only_tab="Nope")


def test_report_is_json_safe_and_carries_no_cell_content() -> None:
    import json

    _, _, engine = make()
    d = engine.run().to_dict()
    blob = json.dumps(d)
    for secret in ("@acme", "acme", "Pat Example", "linkedin.com", "+91"):
        assert secret.lower() not in blob.lower()
    assert d["tabs"]["Active Enquiries"]["errors"][0]["row"] == 11
    assert all(line.count("@") == 0 for line in engine.run().summary_lines())


def test_year_inference_is_frozen_by_row_hash() -> None:
    """'09 July' stays 2026 after New Year: unchanged hash => no rewrite."""
    src, repo, engine = make()
    engine.run()
    later = SyncEngine(src, repo, fx.TABS, today=lambda: fx.TODAY.replace(year=2027, month=1, day=3), now=lambda: NOW)
    rep = later.run()
    assert rep.total_changes == 0
    assert repo.get_by_key("enquiry", "post:7000000000000000001").fields["date_posted"].year == 2026
