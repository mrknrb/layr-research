"""A stand-in for layr's hub in tests: an MCP server over HTTP with the one tool the plugin calls,
layr__events_emit, writing each event as a JSON line to a file.  python fake_hub.py <port> <events file>"""

import json
import sys
import time

from mcp.server.fastmcp import FastMCP

port, path = int(sys.argv[1]), sys.argv[2]
hub = FastMCP("hub", port=port, log_level="WARNING")


@hub.tool(name="layr__events_emit")
def events_emit(topic: str, data: dict) -> dict:
    """Records an event."""
    time.sleep(0.02)  # the real hub hands it to the panels
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"topic": topic, "data": data}) + "\n")
    return {"ok": True}


hub.run("streamable-http")
