"""GC add-on: reading GC sequences (GAML exports) and finding and measuring peaks.

    from toolbox.gc import read_gaml, integrate_peak, PeakSettings

- Reading: `read_gaml` (a GC sequence, every injection's trace), `vendor_peak` (the instrument's own peak).
- Peaks: `integrate_peak` (find and measure a compound's peak), `integrate_by_hand`, `PeakSettings`.
- Figures: `plot_time_courses` (concentrations over time per strain and substrate).

Importing it registers `read_gaml` as a reader of .gaml files (for the flow's "add a raw file") and a curve reader
for .gaml files (each injection's trace, for the review cards of an experiment's pages).
"""

from __future__ import annotations

from ..registry import register_curve_reader, register_reader
from .gaml import Injection, read_gaml, vendor_peak
from .peaks import PeakSettings, integrate_by_hand, integrate_peak
from .plots import plot_time_courses

__all__ = [
    "read_gaml", "vendor_peak", "Injection",
    "integrate_peak", "integrate_by_hand", "PeakSettings",
    "plot_time_courses",
]

# What the flow's palette shows for this add-on's ready-made steps (see toolbox/blocks.py's FORMS).
FORMS = {
    "read_gaml": {"title": "Read a GC sequence", "takes": ["file"], "gives": "injections", "fields": {
        "gaml_file": {"kind": "file", "label": "GAML export"}, "channel": {"label": "Channel"}},
        "example": "injections = read_gaml(gaml_file)"},
}


def gaml_curves(path, settings: dict) -> list[dict]:
    """Every injection's trace as a curve (item: the injection number, x: time in min, y: signal in pA), on the
    channel the experiment's `channel` setting names (empty: the first trace)."""
    channel = settings.get("channel")
    injections = read_gaml(path, channel if isinstance(channel, str) and channel else None)
    return [{"item": i.number, "name": i.name, "x": i.time_min, "y": i.signal_pa} for i in injections]


register_reader(read_gaml, "A GC sequence (GAML export from Chromeleon)", [".gaml"], "injections", "gaml_file")
register_curve_reader(".gaml", gaml_curves)
