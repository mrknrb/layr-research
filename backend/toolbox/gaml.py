"""Reading GC sequences from GAML files (the open XML export of Chromeleon).

One GAML file holds a whole sequence: for every injection the raw detector trace (x = seconds, y = pA, numbers
stored as base64) plus Chromeleon's own peak table. The instrument's peak table is kept only to compare with: the
peaks are found and measured again by `integrate_peak`.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import cache


@dataclass(repr=False)
class Injection:
    """One injection of a sequence: its name, when it was measured, and its trace (time in min, signal in pA)."""

    number: int  # 1-based position in the sequence (= Chromeleon's injection number)
    name: str
    collected: pd.Timestamp
    sample_type: str  # SAMPLE / BLANK / ... as set in Chromeleon
    time_min: np.ndarray
    signal_pa: np.ndarray
    vendor_peaks: pd.DataFrame = field(repr=False)  # the instrument's peaks: name, rt_min, area_pa_min, height_pa
    key: str = ""  # which file and injection this is (for remembering its peaks)

    def __repr__(self) -> str:
        return f"Injection #{self.number} {self.name!r}"


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
        rows.append({
            "name": pk.get("name", ""),
            "rt_min": float(pk.findtext("peakXvalue")) / 60.0,
            "area_pa_min": float(p.get("Peak_Area", "nan")) / 60.0,
            "height_pa": float(p.get("Peak_Height", "nan")),
        })
    return pd.DataFrame(rows, columns=["name", "rt_min", "area_pa_min", "height_pa"])


def parse_gaml(path: str | Path, channel: str | None = None) -> list[Injection]:
    """Read every injection of a GAML file, one after the other (they are about 100 MB, so piece by piece)."""
    injections: list[Injection] = []
    for _, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != "experiment":
            continue
        traces = el.findall("trace")
        if channel is not None:
            traces = [t for t in traces if t.get("name") == channel]
        if not traces:
            raise ValueError(f"No trace (channel={channel!r}) in injection {el.get('name')!r} of the GAML file.")
        trace = traces[0]
        x = _decode(trace.find("Xdata/values"))
        y = _decode(trace.find("Xdata/Ydata/values"))
        if x.size == 0:
            raise ValueError("Empty time axis in a GAML trace.")
        x_units = trace.find("Xdata").get("units", "SECONDS").upper()
        time_min = x / 60.0 if x_units.startswith("SEC") else x
        injections.append(Injection(
            number=len(injections) + 1,
            name=el.get("name", "").strip(),
            collected=pd.Timestamp(el.findtext("collectdate")),
            sample_type=_params(el).get("type", ""),
            time_min=time_min,
            signal_pa=y,
            vendor_peaks=_vendor_peaks(trace),
        ))
        el.clear()
    return injections


def read_gaml(gaml_file: str | Path, channel: str | None = None) -> list[Injection]:
    """Read the injections of a GC sequence from its GAML export: names, times and detector traces.

    `channel` is the trace's name in the file (e.g. BackDetectorFID); empty means the first trace. The file is read
    once; later runs get the remembered injections back (see toolbox/cache.py).
    """
    channel = channel or None
    fp = cache.file_fingerprint(gaml_file)

    def parse() -> list[Injection]:
        injections = parse_gaml(gaml_file, channel)
        for injection in injections:
            injection.key = f"{fp}:{channel}:{injection.number}"
        return injections

    return cache.remember_big(f"gaml-{fp}-{channel or 'first'}", parse)


def vendor_peak(injection: Injection, compound: str) -> tuple[float, float]:
    """The instrument's own peak for a compound in an injection: (retention time in min, area in pA·min).

    Both are NaN when Chromeleon's peak table has no peak of that name.
    """
    v = injection.vendor_peaks[injection.vendor_peaks.name == compound]
    if len(v) == 0:
        return np.nan, np.nan
    return float(v.rt_min.iloc[0]), float(v.area_pa_min.iloc[0])
