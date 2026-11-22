"""Figures of a GC activity assay."""

from __future__ import annotations

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from ..plots import COLORS


def plot_time_courses(time_course: pd.DataFrame) -> Figure:
    """Draw mean concentration ± SD over time: one panel per strain and fed substrate, one fixed colour per compound.

    `time_course` needs the columns strain, substrate, compound, time_min, mean and sd. Error bars are ± one
    standard deviation of the replicates.
    """
    if time_course.empty:
        raise ValueError("The time course is empty: nothing to plot.")
    panels = list(time_course.groupby(["strain", "substrate"]))
    compounds = list(dict.fromkeys(time_course.compound))
    color = {c: COLORS[i % len(COLORS)] for i, c in enumerate(compounds)}  # a fixed colour per compound
    cols = min(len(panels), 2)
    rows = (len(panels) + cols - 1) // cols
    fig = Figure(figsize=(5.2 * cols, 3.6 * rows), dpi=130)
    FigureCanvasAgg(fig)
    axes = fig.subplots(rows, cols, squeeze=False)
    for ax, ((strain, substrate), group) in zip(axes.flat, panels):
        for compound, points in group.groupby("compound", sort=False):
            points = points.sort_values("time_min")
            ax.errorbar(points.time_min, points["mean"], yerr=points.sd.fillna(0), marker="o", ms=4, capsize=3,
                        lw=1.6, color=color[compound], label=compound)
        ax.set_title(f"{substrate}-fed {strain}", fontsize=10)
        ax.set_xlabel("Time (min)")
        ax.set_ylabel("Concentration (mM)")
        ax.grid(axis="y", color="#e6e8ec", lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    for ax in list(axes.flat)[len(panels):]:
        ax.set_visible(False)
    handles = [Line2D([], [], color=color[c], marker="o", lw=1.6) for c in compounds]
    fig.legend(handles, compounds, loc="upper center", ncol=min(len(compounds), 6), frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig
