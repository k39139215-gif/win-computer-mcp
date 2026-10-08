"""
test_mcp_client.py - End-to-end integration test through the official MCP Client.
Spawns server.py over stdio and executes tool calls using ClientSession.
Tests all Phase 2 guardrails end-to-end through the protocol.
"""

import json
import sys
from pathlib import Path
import pytest
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

BASE_DIR = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = BASE_DIR / "server.py"
STOP_FILE = BASE_DIR / "STOP"


@pytest.fixture(autouse=True)
def cleanup_stop_file():
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    yield
    if STOP_FILE.exists():
        STOP_FILE.unlink()


@pytest.mark.anyio
async def test_mcp_client_read_and_security_e2e():
    """Verify tool discovery, delivery of Image+dict, redaction, and security rejections over MCP."""
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        cwd=str(BASE_DIR),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1. Initialize session
            init_res = await session.initialize()
            assert init_res.serverInfo.name == "win-computer-mcp"

            # 2. Tool discovery
            tools_res = await session.list_tools()
            tool_names = [t.name for t in tools_res.tools]
            assert "screenshot" in tool_names
            assert "list_windows" in tool_names
            assert "list_controls" in tool_names
            assert "write_text_file" in tool_names

            # 3. Fullscreen screenshot without window_title must be refused by allowlist policy
            shot_res = await session.call_tool("screenshot", arguments={})
            assert len(shot_res.content) == 1
            assert "Security rejection" in shot_res.content[0].text

            # 4. Call screenshot for allowed window (Calculator) -> must deliver Image and metadata
            shot_calc = await session.call_tool("screenshot", arguments={"window_title": "Calculator"})
            assert len(shot_calc.content) == 2
            assert shot_calc.content[0].type == "image"
            assert shot_calc.content[0].mimeType == "image/png"
            meta = json.loads(shot_calc.content[1].text)
            assert meta["window_title"] == "Calculator"


            # 5. Call screenshot for blocked window (Settings) -> must be rejected
            shot_blocked = await session.call_tool("screenshot", arguments={"window_title": "Settings"})
            assert len(shot_blocked.content) >= 1
            blocked_text = shot_blocked.content[0].text
            assert "Security rejection" in blocked_text or "not found" in blocked_text

            # 6. Call list_windows tool and verify blocked app title redaction
            win_res = await session.call_tool("list_windows", arguments={})
            assert len(win_res.content) >= 1
            for block in win_res.content:
                w_info = json.loads(block.text)
                if w_info["process_name"].lower() in ["chrome.exe", "msedge.exe", "1password.exe"]:
                    assert w_info["title"] == "[BLOCKED]"

            # 7. Call list_controls for Calculator (allowed)
            calc_ctrls_res = await session.call_tool("list_controls", arguments={"window_title": "Calculator", "max_items": 10})
            calc_data = json.loads(calc_ctrls_res.content[0].text)
            assert "controls" in calc_data
            assert calc_data["total_controls"] > 0

            # 8. Call list_controls for Settings (blocked) -> must be rejected
            settings_ctrls_res = await session.call_tool("list_controls", arguments={"window_title": "Settings"})
            settings_data = json.loads(settings_ctrls_res.content[0].text)
            assert "Security rejection" in settings_data["error"] or "not found" in settings_data["error"]


@pytest.mark.anyio
async def test_mcp_client_kill_switch_e2e():
    """Verify STOP file immediately blocks all tools over MCP client protocol."""
    # Create STOP file
    STOP_FILE.write_text("STOP", encoding="utf-8")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        cwd=str(BASE_DIR),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Screenshot must be denied
            res1 = await session.call_tool("screenshot", arguments={})
            assert "Action denied" in res1.content[0].text

            # list_windows must be denied
            res2 = await session.call_tool("list_windows", arguments={})
            res2_data = json.loads(res2.content[0].text)
            # When list_windows returns a list with an error dict, FastMCP serializes the item
            assert "Action denied" in res2_data["error"]

            # list_controls must be denied
            res3 = await session.call_tool("list_controls", arguments={"window_title": "Calculator"})
            res3_data = json.loads(res3.content[0].text)
            assert "Action denied" in res3_data["error"]
