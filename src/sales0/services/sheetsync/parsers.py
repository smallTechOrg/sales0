"""CSV text -> canonical records, per tab kind.

Never raises on data: every problem becomes a :class:`RowIssue` (row number + stable code;
messages carry NO cell content). ``errors`` rows are skipped; ``warnings`` rows are kept
with the offending field set to ``None``.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date

from sales0.services.sheetsync import values as v
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
from sales0.services.sheetsync.tabs import TabSpec

MAX_CELL_CHARS = 20000
HEADER_SCAN_ROWS = 10


@dataclass(frozen=True)
class RowIssue:
    row: int
    code: str
    message: str = ""


@dataclass(frozen=True)
class ParsedRow:
    record: SheetRecord
    row_number: int
    row_hash: str


@dataclass
class TabParse:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)
    data_rows: int = 0  # non-blank rows after the header
    blank_rows: int = 0
    header_row: int | None = None
    fatal: str | None = None  # e.g. "header-not-found": the engine must not remove anything


# ----------------------------------------------------------------------------- headers
def norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", v.fix_mojibake(h or "")).strip().strip(":*").strip().casefold()


ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "enquiries": {
        "date_posted": ("date on which enquiry was posted", "date posted", "posted on", "date of post"),
        "date_added": ("date enquiry added", "date added"),
        "requirement_summary": ("requirement summary", "summary", "requirement"),
        "email": ("e-mail", "email", "e mail", "email id", "e-mail id", "emails"),
        "priority": ("priority",),
        "status": ("status",),
        "notes": ("notes", "note"),
        "company": ("company", "company name"),
        "date_email_sent": ("date e-mail sent", "date email sent", "date message sent", "date mail sent"),
        "date_proposal_sent": ("date proposal sent",),
        "phone": ("contact number", "phone", "phone number", "contact no", "mobile", "contact no."),
        "link": ("link", "post link", "url"),
        "full_enquiry": ("full enquiry", "enquiry"),
        "keyword_used": ("keyword used", "keyword", "keywords"),
        "update": ("update", "updates"),
        "contact_person": ("contact person", "contact name"),
    },
    "person": {
        "name": ("name",),
        "linkedin": ("linkedin id", "linkedin", "linkedin url", "linkedin profile"),
        "company": ("company name", "company", "comapany", "company/org"),
        "website": ("website", "web site"),
        "status": ("status",),
        "reason": ("reason of adding", "reason", "reason for adding"),
        "notes": ("notes", "note"),
    },
    "competitors": {
        "website": ("website",),
        "name": ("name",),
        "approx_clients": ("approx no of clients", "approx. no of clients", "no of clients"),
        "client_names": ("some client names", "client names"),
        "contact": ("contact",),
        "email": ("e-mail", "email"),
        "notes": ("notes", "note"),
    },
    "groups": {
        "name": ("name",),
        "platform": ("platform",),
        "link": ("link", "url"),
        "details": ("details", "detail"),
    },
}
ALIASES["discarded"] = ALIASES["enquiries"]
for _k in ("outreach", "prospects", "working"):
    ALIASES[_k] = ALIASES["person"]

# Minimum recognised headers for a row to count as the header row.
MIN_MATCH = {"enquiries": 3, "discarded": 3, "outreach": 2, "prospects": 2, "working": 2, "competitors": 2, "groups": 2}
# The header-less spare column some tabs carry.
BLANK_HEADER_FIELD = {"outreach": "extra", "prospects": "extra"}
PLACEHOLDER_FIELDS = frozenset({"status", "priority"})
REQUIRED_ANY = {  # a row needs at least one of these to be a real record
    "enquiries": ("requirement_summary", "link", "email", "company", "full_enquiry", "phone", "contact_person"),
    "discarded": ("requirement_summary", "link", "email", "company", "full_enquiry", "phone", "contact_person"),
    "outreach": ("name", "linkedin", "company"),
    "prospects": ("name", "linkedin", "company"),
    "working": ("name", "company", "reason"),
    "competitors": ("website", "name"),
    "groups": ("name", "link"),
}


def read_csv(text: str) -> list[list[str]]:
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    return list(csv.reader(io.StringIO(text, newline="")))


def _build_colmap(header: list[str], kind: str) -> tuple[dict[str, int], int]:
    """field -> column index (first occurrence wins). Returns (map, duplicate_header_count)."""
    lookup = {a: f for f, names in ALIASES[kind].items() for a in names}
    colmap: dict[str, int] = {}
    dups = 0
    for i, h in enumerate(header):
        n = norm_header(h)
        if not n:
            fld = BLANK_HEADER_FIELD.get(kind)
            if fld and fld not in colmap:
                colmap[fld] = i
            continue
        fld = lookup.get(n)
        if fld is None:
            continue
        if fld in colmap:
            dups += 1
        else:
            colmap[fld] = i
    return colmap, dups


def _find_header(rows: list[list[str]], kind: str) -> tuple[int, dict[str, int], int] | None:
    need = MIN_MATCH[kind]
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        colmap, dups = _build_colmap(row, kind)
        recognised = len([f for f in colmap if f != "extra"])
        if recognised >= need:
            return i, colmap, dups
    return None


def _cell(row: list[str], idx: int | None) -> tuple[str, bool]:
    if idx is None or idx >= len(row):
        return "", False
    return v.clean_text(row[idx], max_chars=MAX_CELL_CHARS)


# ----------------------------------------------------------------------------- generic driver
Builder = Callable[[dict[str, str], int, str, "_Ctx"], SheetRecord]


@dataclass
class _Ctx:
    tab: TabSpec
    today: date
    warn: Callable[[str], None]


def _parse_table(text: str, tab: TabSpec, today: date, build: Builder) -> TabParse:
    out = TabParse()
    rows = read_csv(text)
    found = _find_header(rows, tab.kind)
    if found is None:
        out.fatal = "header-not-found"
        return out
    hdr_idx, colmap, dups = found
    out.header_row = hdr_idx + 1
    if dups:
        out.warnings.append(RowIssue(hdr_idx + 1, "duplicate-header", f"{dups} repeated column header(s) ignored"))
    needed = REQUIRED_ANY[tab.kind]
    seen: set[tuple[str, str]] = set()
    for offset, raw in enumerate(rows[hdr_idx + 1 :]):
        rownum = hdr_idx + 2 + offset
        cells: dict[str, str] = {}
        for fld, idx in colmap.items():
            txt, truncated = _cell(raw, idx)
            cells[fld] = txt
            if truncated:
                out.warnings.append(RowIssue(rownum, "cell-truncated", fld))
        if not any(cells.values()) or not any(c.strip() for c in raw):
            out.blank_rows += 1
            continue
        out.data_rows += 1
        if not any(not v.is_null(cells.get(f)) for f in needed):
            filled = {f for f, c in cells.items() if not v.is_null(c)}
            if filled <= PLACEHOLDER_FIELDS:  # pre-filled template rows (only Status/Priority set)
                out.data_rows -= 1
                out.blank_rows += 1
            else:
                out.errors.append(RowIssue(rownum, "no-identifying-content", "row has none of the required columns"))
            continue
        row_hash = v.stable_hash(*(f"{f}={cells[f]}" for f in sorted(cells)))

        def warn(code: str, _r: int = rownum) -> None:
            out.warnings.append(RowIssue(_r, code))

        try:
            record = build(cells, rownum, row_hash, _Ctx(tab, today, warn))
        except Exception as exc:  # noqa: BLE001 — a bad row must never abort the tab
            out.errors.append(RowIssue(rownum, "row-parse-failed", type(exc).__name__))
            continue
        if record.record_id in seen:
            # Same natural key as an earlier row: keep both when the rows differ, keyed by row hash.
            record = replace(record, key="row:" + row_hash)
            if hasattr(record, "key_source"):
                record = replace(record, key_source="row_hash")  # type: ignore[type-var]
            out.warnings.append(RowIssue(rownum, "duplicate-key-rekeyed"))
            if record.record_id in seen:
                out.errors.append(RowIssue(rownum, "duplicate-row", "identical to an earlier row"))
                continue
        seen.add(record.record_id)
        out.rows.append(ParsedRow(record, rownum, row_hash))
    return out


# ----------------------------------------------------------------------------- builders
def _date(ctx: _Ctx, raw: str, name: str, *, not_after: date | None = None) -> date | None:
    if v.is_null(raw):
        return None
    d = v.parse_date(raw, ref=ctx.today, not_after=not_after)
    if d is None:
        ctx.warn(f"bad-date:{name}")
    return d


_MODE = re.compile(r"\b(online|offline|hybrid|on-?site|virtual|in-?person)\b", re.IGNORECASE)
_MODE_MAP = {"online": "online", "virtual": "online", "offline": "offline", "onsite": "offline",
             "on-site": "offline", "inperson": "offline", "in-person": "offline", "hybrid": "hybrid"}


def split_summary(summary: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """'Company - Topic - City | Mode' -> (company, topic, city, mode); best effort."""
    if not summary:
        return None, None, None, None
    head, _, tail = summary.rpartition("|")
    if not head:
        head, tail = summary, ""
    mode = None
    m = _MODE.search(tail or "")
    if m:
        mode = _MODE_MAP.get(m.group(1).lower().replace(" ", ""))
    parts = [p.strip() for p in re.split(r"\s[-–—]\s", head) if p.strip()]
    if len(parts) >= 3:
        return parts[0], " - ".join(parts[1:-1]), parts[-1], mode
    return None, None, None, mode


def _enquiry_fields(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> dict[str, object]:
    summary = v.nullable(v.strip_markdown_wrap(c.get("requirement_summary", "")))
    sc, st, sci, mode = split_summary(summary)
    added = _date(ctx, c.get("date_added", ""), "date_added")
    posted = _date(ctx, c.get("date_posted", ""), "date_posted", not_after=added)
    emails = v.parse_emails(c.get("email"))
    email_note = None if emails or v.is_null(c.get("email")) else "no-valid-address"
    link = v.nullable(c.get("link"))
    post_url = link if link and v.looks_like_url(link) else None
    prio = v.nullable(c.get("priority"))
    phones = v.parse_phones(c.get("phone"))
    phone_cell = v.nullable(c.get("phone"))
    contact_link = None
    if phone_cell and v.looks_like_url(phone_cell.split()[0]):  # a profile link typed into the phone column
        contact_link = v.normalize_url(phone_cell.split()[0])
    elif phone_cell and not phones:
        ctx.warn("bad-phone")

    pk = v.post_key(post_url)
    if pk:
        key, key_source = pk, "post"
    elif emails or summary:
        key = "eh:" + v.stable_hash("|".join(sorted(emails)), v.norm_key_text(summary))
        key_source = "email_summary"
    else:
        key, key_source = "row:" + row_hash, "row_hash"
    return {
        "key": key,
        "key_source": key_source,
        "date_posted": posted,
        "date_added": added,
        "requirement_summary": summary,
        "summary_company": sc,
        "summary_topic": st,
        "summary_city": sci,
        "engagement_mode": mode,
        "emails": emails,
        "email_note": email_note,
        "priority": prio.lower() if prio else None,
        "sheet_status": v.nullable(c.get("status")),
        "sheet_notes": v.nullable(c.get("notes")),
        "company": v.nullable(c.get("company")),
        "date_email_sent": _date(ctx, c.get("date_email_sent", ""), "date_email_sent", not_after=None),
        "date_proposal_sent": _date(ctx, c.get("date_proposal_sent", ""), "date_proposal_sent"),
        "phones": phones,
        "contact_link": contact_link,
        "post_url": v.normalize_url(post_url) if post_url else None,
        "link_text": None if post_url else link,
        "full_enquiry": v.nullable(c.get("full_enquiry")),
        "keyword_used": v.nullable(c.get("keyword_used")),
        "sheet_update": v.nullable(c.get("update")),
        "contact_person": v.nullable(c.get("contact_person")),
    }


def _build_enquiry(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> SheetRecord:
    return SheetEnquiry(**_enquiry_fields(c, rownum, row_hash, ctx))  # type: ignore[arg-type]


def _build_discarded(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> SheetRecord:
    f = _enquiry_fields(c, rownum, row_hash, ctx)
    return SheetDiscarded(**f, discard_reason=v.nullable(c.get("notes")))  # type: ignore[arg-type]


def _build_person(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> SheetRecord:
    li = v.nullable(c.get("linkedin"))
    slug = v.linkedin_slug(li)
    name, company = v.nullable(c.get("name")), v.nullable(c.get("company"))
    if li and not slug and not v.looks_like_url(li):
        ctx.warn("bad-linkedin-url")
    if slug:
        key, key_source = f"li:{slug}", "linkedin"
    else:
        key = "nc:" + v.stable_hash(v.norm_key_text(name), v.norm_key_text(company))
        key_source = "name_company"
    reason = v.nullable(c.get("reason"))
    extra = v.nullable(c.get("extra"))
    kwargs = {
        "key": key,
        "key_source": key_source,
        "name": name,
        "linkedin_url": v.normalize_url(li) if li and v.looks_like_url(li) else li,
        "linkedin_slug": slug,
        "company": company,
        "website": v.normalize_url(c.get("website")) or v.nullable(c.get("website")),
        "sheet_status": v.nullable(c.get("status")),
        "reason_url": v.normalize_url(reason) if reason and v.looks_like_url(reason) else None,
        "reason_text": None if (reason and v.looks_like_url(reason)) else reason,
        "extra_link": v.normalize_url(extra) if extra and v.looks_like_url(extra) else extra,
        "sheet_notes": v.nullable(c.get("notes")),
        "source_tab": ctx.tab.name,
    }
    cls = SheetOutreachPerson if ctx.tab.kind == "outreach" else SheetProspect
    return cls(**kwargs)  # type: ignore[arg-type]


def _build_competitor(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> SheetRecord:
    website = v.nullable(c.get("website"))
    wk = v.website_key(website)
    name = v.nullable(c.get("name"))
    if wk:
        key = f"web:{wk}"
    elif name:
        key = "name:" + v.norm_key_text(name)
    else:
        key = "row:" + row_hash
    n = v.nullable(c.get("approx_clients"))
    clients = int(m[0]) if n and (m := re.search(r"\d+", n)) else None
    if n and clients is None:
        ctx.warn("bad-number:approx_clients")
    email_cell = c.get("email", "")
    emails = v.parse_emails(email_cell)
    return SheetCompetitor(
        key=key,
        website=wk and v.normalize_url(website if v.looks_like_url(website) else f"https://{website}") or website,
        website_key=wk,
        name=name,
        approx_clients=clients,
        client_names=v.nullable(c.get("client_names")),
        contact=v.nullable(c.get("contact")) or (None if emails else v.nullable(email_cell)),
        emails=emails,
        sheet_notes=v.nullable(c.get("notes")),
    )


def _build_group(c: dict[str, str], rownum: int, row_hash: str, ctx: _Ctx) -> SheetRecord:
    link = v.nullable(c.get("link"))
    name = v.nullable(c.get("name"))
    nlink = v.normalize_url(link) if link and v.looks_like_url(link) else None
    if nlink:
        key = f"link:{nlink}"
    elif name:
        key = "name:" + v.norm_key_text(name)
    else:
        key = "row:" + row_hash
    details = v.nullable(c.get("details"))
    keep = None
    if details:
        d = details.strip().lower().replace(" ", "-").replace("_", "-")
        keep = d if d in ("keep", "keep-low") else None
        if keep is None:
            ctx.warn("unknown-group-details")
    return SheetGroup(
        key=key,
        name=name,
        platform=v.nullable(c.get("platform")),
        link=nlink or link,
        details=details,
        keep_level=keep,
    )


_BUILDERS: dict[str, Builder] = {
    "enquiries": _build_enquiry,
    "discarded": _build_discarded,
    "outreach": _build_person,
    "prospects": _build_person,
    "working": _build_person,
    "competitors": _build_competitor,
    "groups": _build_group,
}


# ----------------------------------------------------------------------------- metrics
_WEEK = re.compile(r"^week\s*[-_#]?\s*(\d{1,2})$", re.IGNORECASE)


def parse_number(text: str | None) -> float | None:
    t = v.nullable(text)
    if t is None:
        return None
    t = re.sub(r"[₹$€£,\s%]", "", t)
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    try:
        return float(t)
    except ValueError:
        return None


def _parse_metrics(text: str, tab: TabSpec, today: date) -> TabParse:
    out = TabParse()
    rows = read_csv(text)
    hdr_idx = next(
        (i for i, r in enumerate(rows[:HEADER_SCAN_ROWS]) if sum(1 for c in r if _WEEK.match(norm_header(c))) >= 1),
        None,
    )
    if hdr_idx is None:
        out.fatal = "header-not-found"
        return out
    header = rows[hdr_idx]
    out.header_row = hdr_idx + 1
    week_cols: list[tuple[int, str, int, int]] = []  # (col, label, week_no, occurrence)
    occ: dict[int, int] = {}
    target_col = None
    for i, h in enumerate(header):
        n = norm_header(h)
        if m := _WEEK.match(n):
            wn = int(m[1])
            occ[wn] = occ.get(wn, 0) + 1
            if occ[wn] > 1:
                out.warnings.append(RowIssue(hdr_idx + 1, "duplicate-week-header", f"week {wn}"))
            week_cols.append((i, f"Week {wn}", wn, occ[wn]))
        elif i > 0 and "target" in n and target_col is None:
            target_col = i
    # Secondary: col 0 is a group label (blank = same group as above), col 1 (blank header) the metric.
    grouped = len(header) > 1 and not norm_header(header[1]) and target_col is not None and target_col > 1
    name_col, group_col = (1, 0) if grouped else (0, None)
    group: str | None = None
    seen: set[str] = set()
    for offset, raw in enumerate(rows[hdr_idx + 1 :]):
        rownum = hdr_idx + 2 + offset
        if not any(c.strip() for c in raw):
            out.blank_rows += 1
            continue
        if group_col is not None:
            g, _ = _cell(raw, group_col)
            if not v.is_null(g):
                group = g
        metric, _ = _cell(raw, name_col)
        weeks_txt = [_cell(raw, c)[0] for c, *_ in week_cols]
        target_raw, _ = _cell(raw, target_col)
        if v.is_null(metric):
            if any(not v.is_null(x) for x in [*weeks_txt, target_raw]):
                out.data_rows += 1
                out.errors.append(RowIssue(rownum, "metric-name-missing"))
            else:
                out.blank_rows += 1
            continue
        out.data_rows += 1
        try:
            weeks = tuple(
                SheetMetricWeek(label, wn, o, txt, parse_number(txt))
                for (_c, label, wn, o), txt in zip(week_cols, weeks_txt, strict=True)
                if txt != ""
            )
            base = f"{tab.kind}:{v.norm_key_text(group)}/{v.norm_key_text(metric)}"
            key, n = base, 1
            while key in seen:  # repeated metric names stay distinct, in sheet order
                n += 1
                key = f"{base}#{n}"
            seen.add(key)
            payload = [group or "", metric, target_raw, *(f"{w.label}/{w.occurrence}={w.raw}" for w in weeks)]
            row_hash = v.stable_hash(*payload)
            rec = SheetMetricRow(
                key=key,
                metric_set=tab.kind,
                metric=metric,
                group=group,
                weekly_target=parse_number(target_raw),
                weekly_target_raw=v.nullable(target_raw),
                weeks=weeks,
            )
        except Exception as exc:  # noqa: BLE001
            out.errors.append(RowIssue(rownum, "row-parse-failed", type(exc).__name__))
            continue
        out.rows.append(ParsedRow(rec, rownum, row_hash))
    return out


def parse_tab(text: str, tab: TabSpec, *, today: date) -> TabParse:
    """Parse one tab's CSV text into records + issues."""
    if tab.kind in ("weekly", "secondary"):
        return _parse_metrics(text, tab, today)
    return _parse_table(text, tab, today, _BUILDERS[tab.kind])
