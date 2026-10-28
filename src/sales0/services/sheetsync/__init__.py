"""Google Sheet read-only sync engine (spec/capabilities/sheet-sync.md).

Public API: ``SyncEngine``, ``SheetFetcher``, ``tabs_from_settings``, ``SheetRepository``,
``InMemorySheetRepository``, ``SheetSyncRunner``, ``SqlStateStore``, the ``Sheet*`` records.
"""

from sales0.services.sheetsync.engine import SyncEngine
from sales0.services.sheetsync.fetch import (
    ReadOnlyTransport,
    SheetFetcher,
    SheetFetchError,
    SheetWriteBlocked,
    StaticSource,
    make_client,
)
from sales0.services.sheetsync.parsers import ParsedRow, RowIssue, TabParse, parse_tab
from sales0.services.sheetsync.records import (
    SheetCompetitor,
    SheetDiscarded,
    SheetEnquiry,
    SheetGroup,
    SheetMetricRow,
    SheetMetricWeek,
    SheetOutreachPerson,
    SheetProspect,
    SheetRecord,
)
from sales0.services.sheetsync.registry import get_repository, register_repository_factory, repository_installed
from sales0.services.sheetsync.report import SyncReport, TabReport
from sales0.services.sheetsync.repository import (
    InMemorySheetRepository,
    RecordId,
    SheetRepository,
    SheetSource,
    StoredRecord,
)
from sales0.services.sheetsync.runner import InlineExecutor, RunnerStatus, SheetSyncRunner
from sales0.services.sheetsync.state import InMemoryStateStore, SqlStateStore
from sales0.services.sheetsync.tabs import TabSpec, default_tabs, tabs_from_settings

__all__ = [
    "InMemorySheetRepository",
    "InMemoryStateStore",
    "InlineExecutor",
    "ParsedRow",
    "ReadOnlyTransport",
    "RecordId",
    "RowIssue",
    "RunnerStatus",
    "SheetCompetitor",
    "SheetDiscarded",
    "SheetEnquiry",
    "SheetFetchError",
    "SheetFetcher",
    "SheetGroup",
    "SheetMetricRow",
    "SheetMetricWeek",
    "SheetOutreachPerson",
    "SheetProspect",
    "SheetRecord",
    "SheetRepository",
    "SheetSource",
    "SheetSyncRunner",
    "SheetWriteBlocked",
    "SqlStateStore",
    "StaticSource",
    "StoredRecord",
    "SyncEngine",
    "SyncReport",
    "TabParse",
    "TabReport",
    "TabSpec",
    "default_tabs",
    "get_repository",
    "make_client",
    "parse_tab",
    "register_repository_factory",
    "repository_installed",
    "tabs_from_settings",
]
