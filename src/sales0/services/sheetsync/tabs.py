"""Which sheets/tabs to read: tab layout from PHASE2, sheet ids opt-in through settings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sales0.config import Settings

CLIENT_SOURCING_ID = "client-sourcing-sheet-id"  # placeholder; the real ids come from SALES0_SHEET_*_ID
METRICS_ID = "metrics-sheet-id"

TAB_KINDS = ("enquiries", "discarded", "outreach", "prospects", "working", "competitors", "groups", "weekly", "secondary")


@dataclass(frozen=True)
class TabSpec:
    name: str
    kind: str
    sheet_id: str
    gid: str

    def __post_init__(self) -> None:
        if self.kind not in TAB_KINDS:
            raise ValueError(f"unknown tab kind {self.kind!r}")

    @property
    def tab_key(self) -> tuple[str, str]:
        return (self.sheet_id, self.gid)


def default_tabs(client_sourcing_id: str = CLIENT_SOURCING_ID, metrics_id: str = METRICS_ID) -> tuple[TabSpec, ...]:
    c, m = client_sourcing_id, metrics_id
    return (
        TabSpec("Active Enquiries", "enquiries", c, "0"),
        TabSpec("Discarded Enquiries", "discarded", c, "1948969217"),
        TabSpec("Outreach", "outreach", c, "219858330"),
        TabSpec("Prospects", "prospects", c, "1516649000"),
        TabSpec("Working Sheet", "working", c, "490067020"),
        TabSpec("Competitors", "competitors", c, "945694185"),
        TabSpec("Groups", "groups", c, "194345607"),
        TabSpec("Weekly", "weekly", m, "1915564504"),
        TabSpec("Secondary", "secondary", m, "569887510"),
    )


def tabs_from_settings(settings: Settings) -> tuple[TabSpec, ...]:
    """Defaults, with ids from settings; ``SALES0_SHEET_TABS`` (JSON list) replaces the list."""
    raw = getattr(settings, "sheet_tabs_json", None)
    if raw:
        items: list[dict[str, Any]] = json.loads(raw)
        return tuple(
            TabSpec(i["name"], i["kind"], i.get("sheet_id") or settings.sheet_client_sourcing_id, str(i["gid"]))
            for i in items
        )
    # Opt-in: a sheet whose id is blank is not synced (no real sheet ids ship as defaults).
    return tuple(t for t in default_tabs(settings.sheet_client_sourcing_id, settings.sheet_metrics_id) if t.sheet_id)


def select_tab(tabs: tuple[TabSpec, ...], name: str) -> tuple[TabSpec, ...]:
    wanted = name.strip().casefold()
    picked = tuple(t for t in tabs if t.name.casefold() == wanted)
    if not picked:
        raise KeyError(name)
    return picked
