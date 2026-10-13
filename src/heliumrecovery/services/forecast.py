"""Overnight forecast for the 10 PM report (docs/slack_alerts_and_guardian.md §2.2).

Pure functions over (time, value) points, so they can be tested without
instruments:

1. keep the last `window` (3 h) of 1-minute points,
2. start after the last known break (mode change, pump enabled/disabled) and
   after the biggest step if it clearly stands out (a transfer),
3. robust slope (Theil-Sen: median of pairwise slopes),
4. "trend unclear" when too little data is left or the scatter around the
   line is bigger than both the noise tolerance and the predicted change,
5. project to the horizon and work out when a threshold would be crossed.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

Point = tuple[datetime, float]


@dataclass(frozen=True)
class Forecast:
    value: float  # latest value
    slope_h: float | None  # per hour; None when unclear
    at_horizon: float | None
    unclear: str | None = None  # why there is no forecast
    since: datetime | None = None  # first point used

    def crossing(self, threshold: float, now: datetime, horizon: datetime) -> datetime | None:
        """When the value reaches `threshold` before `horizon` (None if not)."""
        if self.slope_h is None or self.slope_h == 0:
            return None
        hours = (threshold - self.value) / self.slope_h
        if hours < 0:
            return None
        t = now + timedelta(hours=hours)
        return t if t <= horizon else None


def theil_sen(points: Sequence[Point]) -> tuple[float, float]:
    """(slope per hour, intercept at the first point's time)."""
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    step = max(1, len(xs) // 120)  # ~7000 pairs at most
    sx, sy = xs[::step], ys[::step]
    slopes = [(sy[j] - sy[i]) / (sx[j] - sx[i])
              for i in range(len(sx)) for j in range(i + 1, len(sx)) if sx[j] != sx[i]]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(y - slope * x for x, y in zip(xs, ys))
    return slope, intercept


def _after_step(points: list[Point], min_step: float) -> list[Point]:
    """Drop everything up to the biggest jump when it clearly stands out."""
    if len(points) < 5:
        return points
    diffs = [abs(points[i + 1][1] - points[i][1]) for i in range(len(points) - 1)]
    k = max(range(len(diffs)), key=diffs.__getitem__)
    typical = statistics.median(diffs)
    if diffs[k] >= min_step and diffs[k] > 8 * typical:
        return points[k + 1:]
    return points


def forecast(
    points: Sequence[Point],
    now: datetime,
    horizon: datetime,
    *,
    window: timedelta = timedelta(hours=3),
    breaks: Sequence[datetime] = (),
    min_step: float,
    tolerance: float,
    min_span: timedelta = timedelta(minutes=30),
    latest: float | None = None,
) -> Forecast | None:
    """Forecast one series; None when there is no data at all.

    `min_step`: a jump at least this big (and far above the usual minute-to-
    minute change) counts as a step, e.g. a transfer. `tolerance`: scatter
    that is still fine (sensor noise).
    """
    pts = [p for p in points if p[0] >= now - window]
    value = latest if latest is not None else (points[-1][1] if points else None)
    if value is None:
        return None
    cut = max((b for b in breaks if pts and b > pts[0][0]), default=None)
    if cut is not None:
        pts = [p for p in pts if p[0] >= cut]
    pts = _after_step(pts, min_step)
    if len(pts) < 10 or pts[-1][0] - pts[0][0] < min_span:
        since = pts[0][0] if pts else cut
        minutes = int((now - since).total_seconds() // 60) if since else 0
        why = f"only {minutes} min of data" + (f" since {since.astimezone():%H:%M}" if since else "")
        return Forecast(value, None, None, why, since)
    slope, intercept = theil_sen(pts)
    t0 = pts[0][0]
    resid = [v - (intercept + slope * (t - t0).total_seconds() / 3600) for t, v in pts]
    noise = 1.4826 * statistics.median(abs(r) for r in resid)
    hours_left = max(0.0, (horizon - now).total_seconds() / 3600)
    change = abs(slope * hours_left)
    if noise > tolerance and noise > change:
        return Forecast(value, None, None, f"trend unclear (scatter ±{noise:.2g})", t0)
    return Forecast(value, slope, value + slope * hours_left, None, t0)
