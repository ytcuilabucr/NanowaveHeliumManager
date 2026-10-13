"""Trend plots of the ATL log as a PNG (for Notion / Drive)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"ATL1": "#2a6fdb", "ATL2": "#9a9a9a", "ATL3": "#8a3ffc"}
PANELS = [
    ("liquid_volume", "Liquid volume (L)", False),
    ("diode_temp", "Diode temperature (K)", True),  # True: cold units only (ATL2 at 294 K)
    ("dewar_pressure", "Dewar pressure (psi)", False),
    ("input_gas_flow", "Input gas flow (L/min)", True),
]
WARM_K = 50.0


def render_trends(rows: list[dict], path: Path | str, *, title: str = "ATL liquefiers") -> Path:
    """Draw a 2x2 panel of the key ATL parameters from `read_log` rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    units = sorted({r["unit"] for r in rows})
    series = {u: [r for r in rows if r["unit"] == u] for u in units}
    warm = {u for u, rs in series.items() if rs and (rs[-1]["diode_temp"] or 0) > WARM_K}

    fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex=True, constrained_layout=True)
    for ax, (field, label, cold_only) in zip(axes.flat, PANELS):
        for u in units:
            if cold_only and u in warm:
                continue
            pts = [(r["time"].astimezone(), r[field]) for r in series[u] if r[field] is not None]
            if not pts:
                continue
            t, v = zip(*pts)
            ax.plot(t, v, color=COLORS.get(u), lw=1.6, ls="--" if u in warm else "-",
                    label=f"{u}{' (warm)' if u in warm else ''}")
        ax.set_title(label, loc="left", fontsize=11)
        ax.grid(alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
        tz = datetime.now().astimezone().tzinfo
        locator = mdates.AutoDateLocator(tz=tz)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
    axes.flat[0].legend(frameon=False, fontsize=9)
    last = max((r["time"] for r in rows), default=None)
    stamp = f" — last reading {last.astimezone():%Y-%m-%d %H:%M:%S}" if last else ""
    fig.suptitle(f"{title}{stamp}", fontsize=13, x=0.01, ha="left")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
