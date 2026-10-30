"""Wire format of ``/api/worker/*`` (shared by the hosted app and the laptop worker).

Everything the worker sends is validated here (lengths capped, enums closed) because, although the bearer token
makes the caller trusted, a bug on the laptop must never be able to bloat or corrupt the hosted database.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = 1
MAX_POSTS_PER_UPLOAD = 200
MAX_LINKS_PER_UPLOAD = 400
MAX_RAW_CARD_BYTES = 120_000

LinkKind = Literal["post", "profile", "search", "other"]
LinkedInStatus = Literal["unknown", "connected", "not_connected", "needs_attention"]
CompleteOutcome = Literal[
    "finished",  # every planned keyword was searched (or the worker stopped on a cap it reports)
    "safety_stop",  # AccountSafetyStop: kind in {logout, checkpoint, captcha, rate_limited}
    "session_missing",  # no li_at cookie in the local profile
    "open_failed",  # LinkedIn / the browser could not be opened
    "cap_hit",  # a daily cap ended the run early (cap_kind says which)
    "cancelled",  # an admin pressed Stop
    "crashed",  # unexpected worker error
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class WorkerInfo(_Strict):
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(default="", max_length=40)
    host: str = Field(default="", max_length=120)
    protocol: int = PROTOCOL_VERSION


class LinkedInReport(_Strict):
    status: LinkedInStatus = "unknown"
    detail: str | None = Field(default=None, max_length=300)


class ClaimRequest(_Strict):
    worker: WorkerInfo
    linkedin: LinkedInReport = LinkedInReport()
    # q15 capability negotiation: task kinds this worker can run besides keyword runs (e.g. "company_page").
    # Old workers omit it and are never handed a task; a task is never sent to a worker that did not ask.
    capabilities: list[Literal["company_page"]] = Field(default_factory=list, max_length=8)


class PlannedKeyword(_Strict):
    row_id: int
    text: str
    status: str = "pending"


class RunCaps(_Strict):
    """What the worker needs to size a scan; refreshed after every keyword (``KeywordAck``)."""

    max_posts_per_keyword: int
    posts_left_today: int
    page_loads_left_today: int
    keyword_pause_min_s: float
    keyword_pause_max_s: float
    max_scrolls: int | None = None


class ClaimedRun(_Strict):
    run_id: int
    resumed: bool = False
    keywords: list[PlannedKeyword]
    caps: RunCaps
    page_loads_so_far: int = 0


class ClaimedTask(_Strict):
    """A one-page task (q15). ``slug`` is ``company/<x>``: the worker builds the URL on ITS OWN LinkedIn base
    (live or fixture), so the hosted app can never point the logged-in browser at an arbitrary URL."""

    task_id: int
    kind: Literal["company_page"]
    slug: str = Field(min_length=9, max_length=200)


class ClaimResponse(_Strict):
    run: ClaimedRun | None = None
    reason: str | None = None  # why nothing was handed out (cool-down, cap, nothing queued)
    poll_s: float | None = None
    protocol: int = PROTOCOL_VERSION
    # q15: only ever present for a worker that sent ``capabilities``; the hosted app OMITS the key otherwise, so an
    # old worker (strict model, extra=forbid) keeps parsing the answer.
    task: ClaimedTask | None = None


TaskOutcome = Literal["done", "safety_stop", "session_missing", "open_failed", "page_error", "crashed"]


class TaskResultRequest(_Strict):
    outcome: TaskOutcome
    kind: str | None = Field(default=None, max_length=20)  # safety kind
    title: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=30_000)
    page_loads: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=300)


class TaskResultResponse(_Strict):
    status: str


class HeartbeatRequest(_Strict):
    step: str | None = Field(default=None, max_length=200)
    keyword: str | None = Field(default=None, max_length=100)
    page_loads: int | None = Field(default=None, ge=0)


class HeartbeatResponse(_Strict):
    cancel: bool = False


class PostPayload(_Strict):
    source_ref: str = Field(min_length=1, max_length=300)
    url: str = Field(default="", max_length=2000)
    author_name: str | None = Field(default=None, max_length=300)
    author_profile_url: str | None = Field(default=None, max_length=2000)
    author_headline: str | None = Field(default=None, max_length=600)
    posted_relative: str | None = Field(default=None, max_length=100)
    posted_date: date | None = None
    text: str = Field(default="", max_length=60_000)
    link_kind: LinkKind = "post"
    search_url: str | None = Field(default=None, max_length=2000)
    truncated: bool = False
    # Optional (older workers omit it): innerText, counts, links, media, capped HTML - see scraper.parsing.raw_card_of.
    raw_card: dict[str, Any] | None = None

    @field_validator("raw_card")
    @classmethod
    def _raw_card_bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value, default=str)) > MAX_RAW_CARD_BYTES:
            raise ValueError("raw_card is too large")
        return value


class LayoutSamplePayload(_Strict):
    """``scraper.layout_sample.capture``'s dict (diagnostics; untrusted markup, only ever served as a download)."""

    reason: str = Field(default="", max_length=200)
    strategy: str = Field(default="none", max_length=40)
    html: str = Field(default="", max_length=130_000)
    cards_total: int = Field(default=0, ge=0)
    cards_sampled: int = Field(default=0, ge=0)
    path: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    counts: dict[str, int] = Field(default_factory=dict, max_length=40)


class KeywordResultMeta(_Strict):
    """Scan-level facts, sent with the keyword's last batch (``final=True``)."""

    scrolls: int = Field(default=0, ge=0)
    parse_errors: int = Field(default=0, ge=0)
    drift_warning: bool = False
    cards_found: int = Field(default=0, ge=0)
    no_results: bool = False
    field_misses: dict[str, int] = Field(default_factory=dict)
    strategy: str = "none"
    passes: int = Field(default=1, ge=0)
    expanded: int = Field(default=0, ge=0)
    stop_reason: str = "cap"
    page_loads: int = Field(default=0, ge=0)  # this scan's own page loads
    page_loads_total: int | None = Field(default=None, ge=0)  # the browser session's cumulative count
    scan_error: str | None = Field(default=None, max_length=300)  # a ScrapeError: the keyword ends 'error'
    duration_ms: int | None = Field(default=None, ge=0)  # the worker's scan + upload time for this keyword
    layout_sample: LayoutSamplePayload | None = None  # cleaned result cards when the scan ended with a layout warning


class UploadPostsRequest(_Strict):
    """One batch of raw posts for one keyword. ``seq`` (1, 2, ...) makes a retry harmless."""

    seq: int = Field(ge=1)
    posts: list[PostPayload] = Field(default_factory=list, max_length=MAX_POSTS_PER_UPLOAD)
    final: bool = False
    result: KeywordResultMeta | None = None


class KeywordAck(_Strict):
    duplicate: bool = False
    accepted: int = 0
    new: int = 0
    dupes: int = 0
    link_refs: list[str] = Field(default_factory=list)  # posts that want a "Copy link to post" capture
    continue_run: bool = True  # False: stop searching (fatal AI error / a daily cap) and call complete
    stop_kind: Literal["post", "page", "llm", "none"] = "none"
    caps: RunCaps | None = None


class UploadLinksRequest(_Strict):
    links: dict[str, str] = Field(default_factory=dict, max_length=MAX_LINKS_PER_UPLOAD)
    attempts: int = Field(default=0, ge=0)


class UploadLinksResponse(_Strict):
    attached: int = 0


class CompleteRequest(_Strict):
    outcome: CompleteOutcome
    kind: str | None = Field(default=None, max_length=20)  # safety kind
    url: str | None = Field(default=None, max_length=2000)
    cap_kind: Literal["post", "page", "visit"] | None = None
    page_loads: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=500)
    row_id: int | None = None  # the keyword being searched when a safety stop hit (it ends 'error')


class CompleteResponse(_Strict):
    status: str
    summary: str | None = None


# --------------------------------------------------------------------------- bulk data import (sales0 push-data)
class ImportBatchRequest(_Strict):
    table: str = Field(min_length=1, max_length=64)
    phase: Literal["insert", "link"] = "insert"
    rows: list[dict[str, Any]] = Field(max_length=1000)
    member_emails: dict[str, str] = {}  # local team_members.id -> e-mail, to re-map actor references


class ImportBatchResponse(_Strict):
    received: int
    inserted: int
    updated: int


class ImportDeckChunkRequest(_Strict):
    """One piece of a deck PDF (``decks.content``), base64; pieces are sent in order."""

    deck_id: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    offset: int = Field(ge=0)
    total: int = Field(ge=1)
    data_b64: str = Field(max_length=7 * 1024 * 1024)


class ImportDeckChunkResponse(_Strict):
    stored: int
    complete: bool
    skipped: bool = False


class ImportFinalizeRequest(_Strict):
    counts: dict[str, int]  # table -> rows the laptop read


class ImportTableReport(_Strict):
    local: int
    remote: int
    ok: bool


class ImportFinalizeResponse(_Strict):
    tables: dict[str, ImportTableReport]
    ok: bool


# --------------------------------------------------------------------------- read-only export (sales0 pull-data)
class ExportTable(BaseModel):
    name: str
    rows: int
    pk: list[str]


class ExportManifest(BaseModel):
    head: str | None  # the hosted database's Alembic revision
    code_head: str | None = None
    tables: list[ExportTable]  # parents before children
    excluded: list[str] = Field(default_factory=list)
    max_batch_rows: int = 1000
    max_batch_bytes: int = 4 * 1024 * 1024


class ExportBatch(BaseModel):
    table: str
    columns: list[str]
    rows: list[dict[str, Any]]
    next: str | None = None  # cursor of the following page; None = this was the last one
    trimmed: int = 0  # rows whose raw_card/raw_text were dropped because the row alone exceeded the page cap
