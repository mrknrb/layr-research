"""Starts the research backend over stdio, as layr does, and calls its tools with the MCP client.

Run from plugins/research:  uv run pytest tests -q
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN = Path(__file__).resolve().parent.parent
SAMPLE = PLUGIN / "sample" / "plant_growth.csv"


@contextlib.asynccontextmanager
async def server(data_dir: Path, hub: str | None = None):
    """The backend as layr starts it: its own data folder, MCP on stdin/stdout, no hub (or the hub at `hub`)."""
    env = {**os.environ, "LAYR_DATA_DIR": str(data_dir), "LAYR_PLUGIN_ID": "research"}
    env.pop("LAYR_HUB_URL", None)
    if hub:
        env.update(LAYR_HUB_URL=hub, LAYR_HUB_TOKEN="test")
    params = StdioServerParameters(command=sys.executable, args=[str(PLUGIN / "backend" / "server.py")], env=env,
                                   cwd=str(PLUGIN))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        yield s


def value(result: Any) -> Any:
    """The tool's value, the way layr.tools.run reads it."""
    assert not result.isError, result.content
    sc = result.structuredContent
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and list(sc) == ["result"] else sc
    return json.loads(result.content[0].text)


def error_text(result: Any) -> str:
    assert result.isError
    return " ".join(c.text for c in result.content if c.type == "text")


def test_import_starts_no_worker(tmp_path: Path):
    """`import server` (tests, tools) starts no calculation process; only running the server warms one."""
    import subprocess

    code = ("import time\nfrom lab import runner\nstarted = []\n"
            "runner.Worker.__init__ = lambda self: started.append(1)\n"
            "import server\ntime.sleep(0.5)\nprint(len(started), server.POOL._warm)")
    env = {**os.environ, "LAYR_DATA_DIR": str(tmp_path)}
    env.pop("LAYR_HUB_URL", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=PLUGIN / "backend", env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["0", "None"], out.stdout


async def test_tools_have_descriptions(tmp_path: Path):
    async with server(tmp_path) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in ("load_csv", "load_sample", "describe", "plot", "ttest", "anova", "list_datasets", "history"):
            assert name in tools, name
            assert len(tools[name].description or "") > 40, name
        assert set(tools["plot"].inputSchema["properties"]) >= {"dataset", "x", "y", "kind"}
        assert tools["plot"].inputSchema["properties"]["kind"]["enum"] == ["scatter", "line", "hist", "box", "bar"]


async def test_load_describe_plot_test(tmp_path: Path):
    async with server(tmp_path) as session:
        loaded = value(await session.call_tool("load_csv", {"content": SAMPLE.read_text("utf-8"), "name": "plants"}))
        ds = loaded["dataset"]
        assert loaded["rows"] == 48
        types = {c["name"]: c["type"] for c in loaded["columns"]}
        assert types == {"plant": "text", "treatment": "text", "light": "text", "height_cm": "number",
                         "leaves": "integer", "dry_mass_g": "number", "germination_days": "integer"}
        assert len(loaded["preview"]) == 10 and loaded["preview"][0]["plant"] == "P01"

        listed = value(await session.call_tool("list_datasets", {}))
        assert [d["dataset"] for d in listed] == [ds]

        desc = value(await session.call_tool("describe", {"dataset": ds, "by": "treatment"}))
        columns = {c["name"]: c for c in desc["columns"]}
        assert [c["name"] for c in desc["columns"]][:3] == ["plant", "treatment", "light"]  # table order
        h = columns["height_cm"]
        assert h["count"] == 48 and h["min"] <= h["median"] <= h["max"]
        assert columns["treatment"]["unique"] == 3
        assert [g["group"] for g in desc["groups"]] == ["control", "compost", "nitrogen"]  # control first
        assert desc["groups"][0]["n"] == 16 and desc["groups"][0]["stats"]["height_cm"]["count"] == 16

        for args in (
            {"kind": "scatter", "x": "height_cm", "y": "dry_mass_g", "group": "treatment"},
            {"kind": "box", "x": "Treatment", "y": "height cm"},  # names matched ignoring case and spaces
            {"kind": "bar", "x": "light", "y": "leaves", "theme": "dark"},
            {"kind": "hist", "x": "height_cm", "group": "light"},
            {"kind": "line", "x": "germination_days", "y": "height_cm"},
        ):
            res = await session.call_tool("plot", {"dataset": ds, **args})
            assert not res.isError, res.content
            image = next(c for c in res.content if c.type == "image")
            assert image.mimeType == "image/png"
            assert base64.b64decode(image.data)[:8] == b"\x89PNG\r\n\x1a\n"
            text = next(c for c in res.content if c.type == "text").text
            assert "saved to" in text
        assert len(list((tmp_path / "plots").glob("*.png"))) == 5

        t = value(await session.call_tool("ttest", {"dataset": ds, "column": "height_cm", "group": "treatment",
                                                     "groups": ["control", "nitrogen"]}))
        assert t["test"] == "Welch's t-test"
        assert [g["group"] for g in t["groups"]] == ["control", "nitrogen"] and t["groups"][0]["n"] == 16
        assert t["p"] < 0.05 and t["significant"] and t["mean_difference"] < 0
        assert t["ci95"][0] < t["mean_difference"] < t["ci95"][1]

        two = value(await session.call_tool("ttest", {"dataset": ds, "column": "height_cm", "group": "light"}))
        assert {g["group"] for g in two["groups"]} == {"shade", "sun"}

        a = value(await session.call_tool("anova", {"dataset": ds, "column": "height_cm", "group": "treatment"}))
        assert a["significant"] and len(a["pairs"]) == 3 and 0 < a["eta_squared"] < 1

        hist = value(await session.call_tool("history", {"limit": 50}))
        assert [h["tool"] for h in hist][:3] == ["anova", "ttest", "ttest"]
        plot_id = next(h["plot"] for h in hist if h["tool"] == "plot")
        img = await session.call_tool("get_plot", {"plot": plot_id})
        assert img.content[0].type == "image"


async def test_european_csv_and_errors(tmp_path: Path):
    async with server(tmp_path) as session:
        csv = "név;csoport;magasság\nA;kontroll;12,5\nB;kontroll;13,1\nC;kezelt;15,0\nD;kezelt;16,2\n"
        loaded = value(await session.call_tool("load_csv", {"content": csv}))
        types = {c["name"]: c["type"] for c in loaded["columns"]}
        assert types["magasság"] == "number" and loaded["preview"][0]["magasság"] == 12.5
        ds = loaded["dataset"]

        args = {"dataset": ds, "kind": "scatter", "x": "nope", "y": "magasság"}
        err = error_text(await session.call_tool("plot", args))
        assert "No column 'nope'" in err and "magasság" in err
        args = {"dataset": ds, "kind": "scatter", "x": "csoport", "y": "magasság"}
        err = error_text(await session.call_tool("plot", args))
        assert "must hold numbers" in err
        err = error_text(await session.call_tool("describe", {"dataset": "d00000000"}))
        assert "No dataset" in err
        err = error_text(await session.call_tool("load_csv", {}))
        assert "Give either content" in err
        sample = value(await session.call_tool("load_sample", {}))
        err = error_text(await session.call_tool("ttest", {"dataset": sample["dataset"], "column": "height_cm",
                                                           "group": "treatment"}))
        assert "3 groups" in err and "anova" in err


async def test_datasets_survive_a_restart(tmp_path: Path):
    async with server(tmp_path) as s:
        ds = value(await s.call_tool("load_sample", {}))["dataset"]
    async with server(tmp_path) as s:
        desc = value(await s.call_tool("describe", {"dataset": ds}))
        assert desc["rows"] == 48
