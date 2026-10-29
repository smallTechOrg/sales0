"""The table plan behind ``sales0 push-data`` is derived from the ORM metadata: a new table or FK must not silently
break the import (it fails here until it is ordered / skipped on purpose)."""

from __future__ import annotations

from sales0.db.base import Base
from sales0.services import data_import as plan
from sales0.worker.push_data import batches

SECRET_TABLES = {"gmail_connections", "member_sessions", "password_tokens", "linkedin_session", "team_members"}


def test_every_table_is_planned_or_skipped_on_purpose() -> None:
    names = set(Base.metadata.tables)
    assert set(plan.SKIP) <= names  # no stale entries
    assert {t.name for t in plan.import_order()} == names - set(plan.SKIP)


def test_secret_bearing_tables_are_never_imported() -> None:
    assert SECRET_TABLES <= set(plan.SKIP)


def test_parents_come_before_children_except_deferred_columns() -> None:
    position = {t.name: i for i, t in enumerate(plan.import_order())}
    for table in plan.import_order():
        for col in table.columns:
            if col.name in plan.deferred_columns(table.name):
                assert col.nullable, f"{table.name}.{col.name} is deferred so it must be nullable"
                continue
            for fk in col.foreign_keys:
                target = fk.column.table.name
                if target == table.name or target in plan.SKIP:
                    continue
                assert position[target] < position[table.name], f"{table.name}.{col.name} -> {target} is out of order"


def test_deferred_columns_are_real_foreign_keys() -> None:
    for name, cols in plan.DEFERRED.items():
        for c in cols:
            assert Base.metadata.tables[name].c[c].foreign_keys


def test_external_references_exist() -> None:
    for name in plan.EXTERNAL:
        assert name in Base.metadata.tables


def test_batches_respect_row_and_byte_caps() -> None:
    rows = ({"id": i, "t": "x" * 100} for i in range(25))
    sizes = [len(b) for b in batches(rows, max_rows=10, max_bytes=10_000)]
    assert sizes == [10, 10, 5]
    big = ({"id": i, "t": "x" * 600} for i in range(5))
    assert [len(b) for b in batches(big, max_rows=100, max_bytes=1500)] == [2, 2, 1]
    assert list(batches(iter([]))) == []
