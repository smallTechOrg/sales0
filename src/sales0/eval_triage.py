"""Ground-truth triage eval against the team's REAL Google Sheets (Phase 1.5).

The sheets are fetched as public CSV at RUNTIME and never stored in the repo. Output holds
counts, row numbers and the model's one-line reasons (e-mails/phones scrubbed) - never post
text or contact data.

Labelling:
  * Active Enquiries rows with ``Full Enquiry``  -> genuine (the team pursued them).
  * Discarded Enquiries rows -> discard, EXCEPT status ``No Response`` / ``Rejected`` or a note
    saying the lead did not reply (genuine-but-lost) -> genuine.
  * Rows without ``Full Enquiry`` text: Active ones are skipped; discard-labelled Discarded ones fall
    back to the ``Requirement Summary`` (source "summary", low fidelity, reported separately);
    genuine-but-lost ones without full text are skipped.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass
from typing import Literal

SHEET_ID = "1xUdfMXUKFRNd3RXTq-PAynlbFqyI5sfWUtEHDtlw3ag"
ACTIVE_GID = 0
DISCARDED_GID = 1948969217
CSV_URL = "https://docs.google.com/spreadsheets/d/{sheet}/export?format=csv&gid={gid}"

Label = Literal["genuine", "discard"]
_LOST = re.compile(r"no response|not replying|did not reply|didn'?t reply|no reply", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d ()\-]{7,}\d")

TARGET_RECALL = 0.85
TARGET_DISCARD_PRECISION = 0.75


@dataclass(frozen=True)
class LabelledRow:
    tab: Literal["active", "discarded"]
    row: int  # 1-based sheet row number (header = 1)
    label: Label
    text: str
    source: Literal["full", "summary"]
    status: str


def fetch_csv(gid: int, sheet_id: str = SHEET_ID, timeout: float = 30.0) -> list[dict[str, str]]:
    url = CSV_URL.format(sheet=sheet_id, gid=gid)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        data = resp.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(data)))


def _cell(row: dict[str, str], name: str) -> str:
    for key, value in row.items():
        if key and key.strip().lower() == name.lower():
            return (value or "").strip()
    return ""


def build_examples(active: list[dict[str, str]], discarded: list[dict[str, str]]) -> list[LabelledRow]:
    out: list[LabelledRow] = []
    for i, r in enumerate(active, start=2):
        text = _cell(r, "Full Enquiry")
        if text:
            out.append(LabelledRow("active", i, "genuine", text, "full", _cell(r, "Status")))
    for i, r in enumerate(discarded, start=2):
        status = _cell(r, "Status")
        lost = status.lower() in {"no response", "rejected"} or bool(_LOST.search(_cell(r, "Notes")))
        label: Label = "genuine" if lost else "discard"
        text, source = _cell(r, "Full Enquiry"), "full"
        if not text:
            text, source = _cell(r, "Requirement Summary"), "summary"
        if source == "summary" and label == "genuine":
            continue  # a bare summary is too thin to be a fair genuine-but-lost example
        if text:
            out.append(LabelledRow("discarded", i, label, text, source, status))  # type: ignore[arg-type]
    return out


def scrub(text: str, limit: int = 160) -> str:
    one = " ".join(text.split())
    return _PHONE.sub("<phone>", _EMAIL.sub("<email>", one))[:limit]


@dataclass(frozen=True)
class Prediction:
    row: LabelledRow
    verdict: Label | None
    priority: str | None
    opportunity_type: str | None
    reason: str


def run_triage_over(rows: list[LabelledRow], concurrency: int = 4) -> list[Prediction]:
    from sales0.agent.batch import BatchItem, triage_concurrently
    from sales0.agent.schemas import TriageDeps, TriageInput
    from sales0.config import get_settings
    from sales0.llm import get_llm_client

    settings = get_settings()
    deps = TriageDeps(llm=get_llm_client(), offerings=[], examples=[], settings=settings)
    items = [BatchItem(key=i, inp=TriageInput(enquiry_id=9000 + i, text=r.text)) for i, r in enumerate(rows)]
    preds: dict[int, Prediction] = {}
    for res in triage_concurrently(items, deps, concurrency=concurrency):
        if res.fatal is not None:
            raise res.fatal
        o = res.outcome
        row = rows[res.key]
        if o is None or o.result is None:
            preds[res.key] = Prediction(row, None, None, None, f"triage failed: {scrub(o.error or '', 80) if o else ''}")
        else:
            r = o.result
            preds[res.key] = Prediction(row, r.verdict, r.priority, r.opportunity_type, scrub(r.reason))
    return [preds[i] for i in sorted(preds)]


@dataclass(frozen=True)
class Metrics:
    n_active: int
    active_recall: float
    n_discard_full: int
    discard_precision_full: float | None
    n_discard_all: int
    discard_precision_all: float | None
    genuine_precision: float | None
    tp: int
    fp: int
    fn: int
    tn: int
    failed: int


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def compute_metrics(preds: list[Prediction]) -> Metrics:
    ok = [p for p in preds if p.verdict is not None]
    tp = sum(p.row.label == "genuine" and p.verdict == "genuine" for p in ok)
    fn = sum(p.row.label == "genuine" and p.verdict == "discard" for p in ok)
    fp = sum(p.row.label == "discard" and p.verdict == "genuine" for p in ok)
    tn = sum(p.row.label == "discard" and p.verdict == "discard" for p in ok)
    act = [p for p in ok if p.row.tab == "active"]
    act_hit = sum(p.verdict == "genuine" for p in act)
    d_all = [p for p in ok if p.row.label == "discard"]
    d_full = [p for p in d_all if p.row.source == "full"]
    return Metrics(
        n_active=len(act),
        active_recall=act_hit / len(act) if act else 0.0,
        n_discard_full=len(d_full),
        discard_precision_full=_ratio(sum(p.verdict == "discard" for p in d_full), len(d_full)),
        n_discard_all=len(d_all),
        discard_precision_all=_ratio(tn, len(d_all)),
        genuine_precision=_ratio(tp, tp + fp),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        failed=len(preds) - len(ok),
    )


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def format_report(preds: list[Prediction], m: Metrics) -> str:
    lines = [
        f"EVAL rows={len(preds)} failed={m.failed}",
        f"confusion (label x predicted): genuine->genuine={m.tp} genuine->discard={m.fn} "
        f"discard->genuine={m.fp} discard->discard={m.tn}",
        f"recall on Active-labelled rows: {_fmt(m.active_recall)} (n={m.n_active}, target >= {TARGET_RECALL})",
        f"discard-correct on Discarded rows with full text: {_fmt(m.discard_precision_full)} (n={m.n_discard_full})",
        f"discard-correct on all discard-labelled rows (summary-only included): {_fmt(m.discard_precision_all)} "
        f"(n={m.n_discard_all}, target >= {TARGET_DISCARD_PRECISION})",
        f"precision of genuine predictions: {_fmt(m.genuine_precision)}",
        "MISSES (tab row label->predicted source priority opportunity | reason):",
    ]
    for p in preds:
        if p.verdict != p.row.label:
            lines.append(
                f"  {p.row.tab}#{p.row.row} {p.row.label}->{p.verdict} {p.row.source} "
                f"{p.priority}/{p.opportunity_type} | {p.reason}"
            )
    return "\n".join(lines)


def run_eval(concurrency: int = 4) -> tuple[list[Prediction], Metrics]:
    rows = build_examples(fetch_csv(ACTIVE_GID), fetch_csv(DISCARDED_GID))
    preds = run_triage_over(rows, concurrency)
    return preds, compute_metrics(preds)


def meets_targets(m: Metrics) -> bool:
    d = m.discard_precision_all
    return m.active_recall >= TARGET_RECALL and (d is None or d >= TARGET_DISCARD_PRECISION)
