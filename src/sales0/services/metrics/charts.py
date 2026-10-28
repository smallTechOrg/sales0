"""Server-rendered inline SVG charts (no JavaScript, works offline). Every label is HTML-escaped;
each chart carries a <title>/<desc> and is followed by an accessible table in the page."""

from __future__ import annotations

import zlib
from html import escape

from markupsafe import Markup

COLORS = ("#2563eb", "#d97706", "#059669", "#7c3aed")
W, H = 640, 220
PAD_L, PAD_R, PAD_T, PAD_B = 36, 12, 14, 34


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000):
        if v <= step:
            return step
    return v


def _frame(title: str, desc: str, body: str, ymax: float, legend: list[tuple[str, str]]) -> Markup:
    plot_h = H - PAD_T - PAD_B
    grid = []
    for i in range(5):
        y = PAD_T + plot_h * i / 4
        val = ymax * (4 - i) / 4
        grid.append(
            f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e3e7ec"/>'
            f'<text x="{PAD_L - 4}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#5b6672">{val:g}</text>'
        )
    leg = "".join(
        f'<span class="chart__key"><i style="background:{c}"></i>{escape(n)}</span>' for n, c in legend
    )
    svg = (
        f'<svg class="chart__svg" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t-{zlib.crc32(title.encode())}">'
        f'<title id="t-{zlib.crc32(title.encode())}">{escape(title)}</title><desc>{escape(desc)}</desc>'
        f'{"".join(grid)}{body}</svg>'
    )
    return Markup(f'<figure class="chart"><figcaption>{escape(title)}</figcaption>{svg}<div class="chart__legend">{leg}</div></figure>')


def _xlabels(labels: list[str], n_slots: int) -> str:
    plot_w = W - PAD_L - PAD_R
    every = 1 if n_slots <= 14 else 2 if n_slots <= 28 else 4
    out = []
    for i, lbl in enumerate(labels):
        if i % every:
            continue
        x = PAD_L + plot_w * (i + 0.5) / n_slots
        out.append(f'<text x="{x:.1f}" y="{H - PAD_B + 14}" text-anchor="middle" font-size="10" fill="#5b6672">{escape(lbl)}</text>')
    return "".join(out)


def bar_chart(title: str, labels: list[str], series: list[tuple[str, list[float | None]]]) -> Markup:
    n, k = len(labels), max(len(series), 1)
    ymax = _nice_max(max((v or 0 for _, vals in series for v in vals), default=0))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / max(n, 1)
    bw = max(slot * 0.8 / k, 1.5)
    bars = []
    for j, (name, vals) in enumerate(series):
        for i, v in enumerate(vals):
            if not v:
                continue
            h = plot_h * v / ymax
            x = PAD_L + slot * i + slot * 0.1 + bw * j
            bars.append(
                f'<rect x="{x:.1f}" y="{PAD_T + plot_h - h:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{COLORS[j % 4]}">'
                f"<title>{escape(name)} {escape(labels[i])}: {v:g}</title></rect>"
            )
    desc = "; ".join(f"{name}: " + ", ".join("-" if v is None else f"{v:g}" for v in vals) for name, vals in series)
    return _frame(title, f"Weeks {labels[0] if labels else ''} to {labels[-1] if labels else ''}. {desc}",
                  "".join(bars) + _xlabels(labels, n), ymax, [(nm, COLORS[j % 4]) for j, (nm, _) in enumerate(series)])


def line_chart(title: str, labels: list[str], series: list[tuple[str, list[float | None]]]) -> Markup:
    n = len(labels)
    ymax = _nice_max(max((v or 0 for _, vals in series for v in vals), default=0))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    parts = []
    for j, (name, vals) in enumerate(series):
        pts = [(PAD_L + plot_w * (i + 0.5) / max(n, 1), PAD_T + plot_h * (1 - v / ymax), i, v) for i, v in enumerate(vals) if v is not None]
        if len(pts) > 1:
            parts.append(f'<polyline fill="none" stroke="{COLORS[j % 4]}" stroke-width="2" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in pts)}"/>')
        for x, y, i, v in pts:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{COLORS[j % 4]}"><title>{escape(name)} {escape(labels[i])}: {v:g}</title></circle>')
    desc = "; ".join(f"{name}: " + ", ".join("-" if v is None else f"{v:g}" for v in vals) for name, vals in series)
    return _frame(title, desc, "".join(parts) + _xlabels(labels, n), ymax, [(nm, COLORS[j % 4]) for j, (nm, _) in enumerate(series)])


def funnel_chart(title: str, stages: list[tuple[str, int, float | None]]) -> Markup:
    top = max((c for _, c, _ in stages), default=0) or 1
    row_h, left = 28, 150
    height = row_h * len(stages) + 8
    out = []
    for i, (label, count, pct) in enumerate(stages):
        w = max((W - left - 90) * count / top, 1 if count else 0)
        y = 4 + i * row_h
        extra = f" ({pct:g}% of previous)" if pct is not None else ""
        out.append(
            f'<text x="{left - 6}" y="{y + 16}" text-anchor="end" font-size="12" fill="#1f2933">{escape(label)}</text>'
            f'<rect x="{left}" y="{y + 3}" width="{w:.1f}" height="18" fill="{COLORS[0]}"><title>{escape(label)}: {count}{escape(extra)}</title></rect>'
            f'<text x="{left + w + 6:.1f}" y="{y + 16}" font-size="12" fill="#1f2933">{count}</text>'
        )
    desc = "; ".join(f"{lbl}: {c}" for lbl, c, _ in stages)
    svg = (f'<svg class="chart__svg" viewBox="0 0 {W} {height}" role="img" aria-label="{escape(title)}">'
           f'<title>{escape(title)}</title><desc>{escape(desc)}</desc>{"".join(out)}</svg>')
    return Markup(f'<figure class="chart"><figcaption>{escape(title)}</figcaption>{svg}</figure>')


__all__ = ["bar_chart", "funnel_chart", "line_chart"]
