"""Reader for GAML (Generalized Analytical Markup Language) exports from Chromeleon.

One GAML file holds a whole sequence: for every injection the raw detector trace
(x = seconds, y = pA, base64 FLOAT64) plus Chromeleon's own peak table. We keep the
vendor peak table only for cross-checking; integration is redone in `integrate.py`.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class Injection:
    number: int  # 1-based position in the sequence (= Chromeleon injection number)
    name: str
    collected: pd.Timestamp
    sample_type: str  # SAMPLE / BLANK / ... as set in Chromeleon
    time_min: np.ndarray
    signal_pa: np.ndarray
    vendor_peaks: pd.DataFrame = field(repr=False)  # columns: name, rt_min, area_pa_min, height_pa


def _decode(values_el: ET.Element) -> np.ndarray:
    fmt = values_el.get("format", "FLOAT64")
    dtype = {"FLOAT64": "<f8", "FLOAT32": "<f4"}[fmt]
    if values_el.get("byteorder", "INTEL") != "INTEL":
        dtype = dtype.replace("<", ">")
    return np.frombuffer(base64.b64decode(values_el.text or ""), dtype).astype(float)


def _params(el: ET.Element) -> dict[str, str]:
    return {p.get("name"): (p.text or "") for p in el.findall("parameter")}


def _vendor_peaks(trace: ET.Element) -> pd.DataFrame:
    rows = []
    for pk in trace.iter("peak"):
        p = _params(pk)
        rows.append(
            {
                "name": pk.get("name", ""),
                "rt_min": float(pk.findtext("peakXvalue")) / 60.0,
                "area_pa_min": float(p.get("Peak_Area", "nan")) / 60.0,
                "height_pa": float(p.get("Peak_Height", "nan")),
            }
        )
    return pd.DataFrame(rows, columns=["name", "rt_min", "area_pa_min", "height_pa"])


def read_gaml(path: str | Path, channel: str | None = None) -> list[Injection]:
    """Stream-parse a GAML file (they are ~100 MB) into a list of injections."""
    injections: list[Injection] = []
    for _, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != "experiment":
            continue
        traces = el.findall("trace")
        if channel is not None:
            traces = [t for t in traces if t.get("name") == channel]
        if not traces:
            raise ValueError(f"No trace (channel={channel!r}) in experiment {el.get('name')!r}")
        trace = traces[0]
        x = _decode(trace.find("Xdata/values"))
        y = _decode(trace.find("Xdata/Ydata/values"))
        if x.size == 0:  # some exports put Ydata next to Xdata instead of inside it
            raise ValueError("Empty X axis in GAML trace")
        x_units = trace.find("Xdata").get("units", "SECONDS").upper()
        time_min = x / 60.0 if x_units.startswith("SEC") else x
        injections.append(
            Injection(
                number=len(injections) + 1,
                name=el.get("name", "").strip(),
                collected=pd.Timestamp(el.findtext("collectdate")),
                sample_type=_params(el).get("type", ""),
                time_min=time_min,
                signal_pa=y,
                vendor_peaks=_vendor_peaks(trace),
            )
        )
        el.clear()
    return injections
