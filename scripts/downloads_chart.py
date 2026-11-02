# ruff: noqa: E501  (inline SVG markup reads better unwrapped)
"""Render assets/downloads.svg: cumulative PyPI downloads of argus-agents.

Mirror downloads are excluded (they are bandwidth mirrors, not installs).
Rerun on release:  python scripts/downloads_chart.py
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from itertools import accumulate
from pathlib import Path

URL = "https://pypistats.org/api/packages/argus-agents/overall?mirrors=false"
OUT = Path(__file__).resolve().parent.parent / "assets" / "downloads.svg"
W, H = 800, 240
LEFT, RIGHT, TOP, BOTTOM = 48, 72, 36, 28


def fetch_daily() -> list[tuple[date, int]]:
    with urllib.request.urlopen(URL, timeout=30) as resp:
        rows = json.load(resp)["data"]
    daily = sorted(
        (date.fromisoformat(r["date"]), r["downloads"])
        for r in rows
        if r["category"] == "without_mirrors"
    )
    if len(daily) < 2:
        raise SystemExit(f"pypistats returned {len(daily)} days; nothing to plot")
    return daily


def fmt(n: float) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(int(n))


def nice_step(top: int) -> int:
    raw = top / 4
    mag = 10 ** (len(str(int(raw))) - 1)
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def render(daily: list[tuple[date, int]]) -> str:
    days = [d for d, _ in daily]
    totals = list(accumulate(n for _, n in daily))
    step = nice_step(totals[-1])
    y_max = step * -(-totals[-1] // step)
    span = (days[-1] - days[0]).days
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM

    def x(d: date) -> float:
        return LEFT + pw * (d - days[0]).days / span

    def y(v: float) -> float:
        return TOP + ph * (1 - v / y_max)

    line = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in zip(days, totals))
    area = f"{x(days[0]):.1f},{y(0):.1f} {line} {x(days[-1]):.1f},{y(0):.1f}"

    grid = []
    for v in range(0, int(y_max) + 1, int(step)):
        grid.append(
            f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text class="muted" x="{LEFT - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>'
        )
    months = sorted({d.replace(day=1) for d in days if d.replace(day=1) >= days[0]})
    for m in months:
        grid.append(
            f'<text class="muted" x="{x(m):.1f}" y="{H - 8}" text-anchor="middle">{m:%b}</text>'
        )

    ex, ey = x(days[-1]), y(totals[-1])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" font-size="12">
<style>
  .bg {{ fill: #ffffff; }} .title {{ fill: #1f2328; }} .muted {{ fill: #59636e; }}
  .grid {{ stroke: #d1d9e0; stroke-width: 1; }} .line {{ stroke: #6366f1; }} .area {{ fill: #6366f1; fill-opacity: .12; }}
  @media (prefers-color-scheme: dark) {{
    .bg {{ fill: #0d1117; }} .title {{ fill: #f0f6fc; }} .muted {{ fill: #9198a1; }}
    .grid {{ stroke: #3d444d; }} .line {{ stroke: #818cf8; }} .area {{ fill: #818cf8; fill-opacity: .18; }}
  }}
</style>
<title>argus-agents cumulative PyPI downloads: {totals[-1]} as of {days[-1]}</title>
<rect class="bg" width="{W}" height="{H}" rx="6"/>
<text class="title" x="{LEFT}" y="20" font-weight="600" font-size="13">PyPI downloads (cumulative, excluding mirrors)</text>
{"".join(grid)}
<polygon class="area" points="{area}"/>
<polyline class="line" points="{line}" fill="none" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
<circle class="line bg" cx="{ex:.1f}" cy="{ey:.1f}" r="4" stroke-width="2"/>
<text class="title" x="{ex + 8:.1f}" y="{ey + 4:.1f}" font-weight="600">{fmt(totals[-1])}</text>
</svg>
"""


if __name__ == "__main__":
    daily = fetch_daily()
    OUT.write_text(render(daily))
    print(f"wrote {OUT} — total {sum(n for _, n in daily)} through {daily[-1][0]}")
