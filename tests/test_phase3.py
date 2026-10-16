"""
test_phase3.py - Comprehensive automated tests for Phase 3 active input and window management tools.
Tests launch_app, focus_window, mouse_click, mouse_move, mouse_drag, mouse_scroll,
type_text, press_key, and hotkey, including all security rejection policies.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import guard
import server

STOP_FILE = BASE_DIR / "STOP"
SERVER_SCRIPT = BASE_DIR / "server.py"


@pytest.fixture(autouse=True)
def setup_teardown():
    """Ensure clean desktop environment, reset kill switch, rate limits, and cleanup STOP file."""
    guard.ensure_default_desktop()
    guard.set_kill_switch(False)
    guard.reset_rate_limits()
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    yield
    guard.set_kill_switch(False)
    guard.reset_rate_limits()
    if STOP_FILE.exists():
        STOP_FILE.unlink()


# -------------------------------------------------------------------------
# 1. launch_app tests
# -------------------------------------------------------------------------

def test_launch_app_security_rejection_forbidden():
    """Verify launch_app rejects unauthorized executables and shell injections."""
    for bad_app in ["cmd.exe", "powershell.exe", "explorer.exe", "whoami", "calc.exe & dir"]:
        res = server.launch_app(bad_app)
        assert "error" in res
        assert "Security rejection" in res["error"] or "Access denied" in res["error"]


def test_launch_app_path_traversal():
    """Verify launch_app rejects path traversal attacks."""
    res = server.launch_app("../../Windows/System32/cmd.exe")
    assert "error" in res
    assert "Security rejection" in res["error"] or "Access denied" in res["error"]


def test_launch_app_allowed_mocked():
    """Verify launch_app calls _launch_executable for allowed application."""
    with patch("server._launch_executable", return_value=99999) as mock_launch:
        res = server.launch_app("notepad")
        assert res["status"] == "success"
        assert res["app_name"] == "notepad.exe"
        assert res["pid"] == 99999
        mock_launch.assert_called_once_with("notepad.exe")


# -------------------------------------------------------------------------
# 2. focus_window tests
# -------------------------------------------------------------------------

def test_focus_window_nonexistent():
    """Verify focus_window returns error for non-existent window."""
    res = server.focus_window("DefinitelyNonExistentWindow_12345")
    assert "error" in res
    assert "not found" in res["error"]


def test_focus_window_blocked_app():
    """Verify focus_window refuses to focus windows belonging to blocked apps."""
    mock_win = MagicMock()
    mock_win.window_text.return_value = "Chrome - Google"
    rect = {"left": 0, "top": 0, "width": 800, "height": 600}

    with patch("server._find_target_window", return_value=(mock_win, "chrome.exe", rect, 12345)):
        res = server.focus_window("Chrome")
        assert "error" in res
        assert "Security rejection" in res["error"]
        assert "chrome.exe" in res["error"] or "blocked" in res["error"].lower()


def test_focus_window_allowed_success():
    """Verify focus_window brings allowed window to foreground."""
    mock_win = MagicMock()
    mock_win.window_text.return_value = "Untitled - Notepad"
    rect = {"left": 100, "top": 100, "width": 800, "height": 600}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True) as mock_focus:
        res = server.focus_window("Notepad")
        assert res["status"] == "success"
        assert res["process"] == "notepad.exe"
        mock_focus.assert_called_once_with(12345)


# -------------------------------------------------------------------------
# 3. mouse_click tests
# -------------------------------------------------------------------------

def test_mouse_click_invalid_button():
    """Verify mouse_click rejects invalid button identifiers."""
    res = server.mouse_click("Notepad", 100, 100, button="invalid_btn")
    assert "error" in res
    assert "Invalid mouse button" in res["error"]


def test_mouse_click_out_of_bounds():
    """Verify mouse_click rejects coordinates outside target window boundaries."""
    mock_win = MagicMock()
    rect = {"left": 100, "top": 100, "width": 500, "height": 400}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)):
        # x = 600 is > width 500
        res = server.mouse_click("Notepad", 600, 200, scaled=False)
        assert "error" in res
        assert "Security rejection" in res["error"]
        assert "outside target window boundaries" in res["error"]

        # negative coordinate
        res_neg = server.mouse_click("Notepad", -10, 200, scaled=False)
        assert "error" in res_neg
        assert "Security rejection" in res_neg["error"]


def test_mouse_click_blocked_app():
    """Verify mouse_click refuses to click inside windows belonging to blocked apps."""
    mock_win = MagicMock()
    rect = {"left": 100, "top": 100, "width": 500, "height": 400}

    with patch("server._find_target_window", return_value=(mock_win, "msedge.exe", rect, 12345)):
        res = server.mouse_click("Edge", 200, 200)
        assert "error" in res
        assert "Security rejection" in res["error"]


def test_mouse_click_obscured_point():
    """Verify mouse_click refuses if target screen coordinate is obscured by another window."""
    mock_win = MagicMock()
    rect = {"left": 100, "top": 100, "width": 500, "height": 400}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("guard.verify_point_in_target_window", return_value=(False, "Point obscured by notification")):
        res = server.mouse_click("Notepad", 200, 200, scaled=False)
        assert "error" in res
        assert "Security rejection" in res["error"]
        assert "obscured" in res["error"]


def test_mouse_click_allowed_success():
    """Verify mouse_click executes pyautogui.click when coordinates are valid and unobstructed."""
    mock_win = MagicMock()
    rect = {"left": 100, "top": 100, "width": 500, "height": 400}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("guard.verify_point_in_target_window", return_value=(True, "")), \
         patch("pyautogui.click") as mock_click:
        res = server.mouse_click("Notepad", 200, 150, button="left", clicks=1, scaled=False)
        assert res["status"] == "success"
        assert res["abs_x"] == 300  # 100 + 200
        assert res["abs_y"] == 250  # 100 + 150
        mock_click.assert_called_once_with(x=300, y=250, button="left", clicks=1, interval=0.1)


# -------------------------------------------------------------------------
# 4. mouse_move & mouse_drag & mouse_scroll tests
# -------------------------------------------------------------------------

def test_mouse_move_bounds_and_success():
    """Verify mouse_move enforces bounds and moves cursor."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("pyautogui.moveTo") as mock_move:
        # Out of bounds
        bad_res = server.mouse_move("Notepad", 500, 100, scaled=False)
        assert "error" in bad_res
        assert "Security rejection" in bad_res["error"]

        # Valid in bounds
        ok_res = server.mouse_move("Notepad", 100, 100, scaled=False)
        assert ok_res["status"] == "success"
        assert ok_res["abs_x"] == 150
        assert ok_res["abs_y"] == 150
        mock_move.assert_called_once_with(150, 150, duration=0.1)


def test_mouse_drag_bounds():
    """Verify mouse_drag validates both start and end coordinates."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("pyautogui.moveTo"), \
         patch("pyautogui.dragTo") as mock_drag:
        # Start point out of bounds
        bad_start = server.mouse_drag("Notepad", -10, 50, 200, 200, scaled=False)
        assert "error" in bad_start
        assert "start point" in bad_start["error"]

        # End point out of bounds
        bad_end = server.mouse_drag("Notepad", 50, 50, 600, 200, scaled=False)
        assert "error" in bad_end
        assert "end point" in bad_end["error"]

        # Valid drag
        ok_res = server.mouse_drag("Notepad", 50, 50, 200, 200, scaled=False)
        assert ok_res["status"] == "success"
        assert ok_res["start"] == [100, 100]
        assert ok_res["end"] == [250, 250]
        mock_drag.assert_called_once_with(250, 250, button="left", duration=0.3)


def test_mouse_scroll_bounds_and_success():
    """Verify mouse_scroll enforces boundary check and executes scroll."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("pyautogui.moveTo"), \
         patch("pyautogui.scroll") as mock_scroll:
        # Out of bounds
        bad = server.mouse_scroll("Notepad", 100, 500, clicks=3, scaled=False)
        assert "error" in bad
        assert "Security rejection" in bad["error"]

        # In bounds
        ok_res = server.mouse_scroll("Notepad", 100, 100, clicks=-5, scaled=False)
        assert ok_res["status"] == "success"
        assert ok_res["clicks"] == -5
        mock_scroll.assert_called_once_with(-5)


# -------------------------------------------------------------------------
# 5. type_text tests
# -------------------------------------------------------------------------

def test_type_text_password_control_shielding():
    """Verify type_text strictly refuses to type if the focused element is a password field."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}
    mock_ctrl = MagicMock()

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch.object(mock_win, "get_focus", return_value=mock_ctrl), \
         patch("guard.is_password_control", return_value=True):
        res = server.type_text("Notepad", "secretpassword123")
        assert "error" in res
        assert "Security rejection" in res["error"]
        assert "password field" in res["error"]


def test_type_text_allowed_success():
    """Verify type_text types into regular non-password controls."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}
    mock_ctrl = MagicMock()

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch.object(mock_win, "get_focus", return_value=mock_ctrl), \
         patch("guard.is_password_control", return_value=False), \
         patch("pyautogui.write") as mock_write:
        res = server.type_text("Notepad", "Hello Safe Automation")
        assert res["status"] == "success"
        assert res["typed_characters"] == len("Hello Safe Automation")
        mock_write.assert_called_once_with("Hello Safe Automation", interval=0.01)


def test_type_text_blocked_app():
    """Verify type_text refuses to type into blocked apps."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "chrome.exe", rect, 12345)):
        res = server.type_text("Chrome", "search query")
        assert "error" in res
        assert "Security rejection" in res["error"]


# -------------------------------------------------------------------------
# 6. press_key and hotkey tests
# -------------------------------------------------------------------------

def test_press_key_forbidden_keys():
    """Verify press_key rejects Windows key and forbidden system keys."""
    for key in ["win", "windows", "winleft", "winright", "pause"]:
        res = server.press_key("Notepad", key)
        assert "error" in res
        assert "Security rejection" in res["error"]
        assert "forbidden" in res["error"]


def test_press_key_allowed_success():
    """Verify press_key presses allowed key."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("pyautogui.press") as mock_press:
        res = server.press_key("Notepad", "enter")
        assert res["status"] == "success"
        assert res["key"] == "enter"
        mock_press.assert_called_once_with("enter")


def test_hotkey_forbidden_combinations():
    """Verify hotkey rejects dangerous and system combinations."""
    forbidden = [
        "win+r",
        "win+x",
        "alt+f4",
        "ctrl+alt+del",
        "ctrl+alt+delete",
        "ctrl+shift+esc",
        "ctrl+alt+shift+k",
    ]
    for combo in forbidden:
        res = server.hotkey("Notepad", combo)
        assert "error" in res
        assert "Security rejection" in res["error"]


def test_hotkey_safe_allowed_success():
    """Verify hotkey allows safe document shortcuts."""
    mock_win = MagicMock()
    rect = {"left": 50, "top": 50, "width": 400, "height": 300}

    with patch("server._find_target_window", return_value=(mock_win, "notepad.exe", rect, 12345)), \
         patch("server._focus_hwnd", return_value=True), \
         patch("pyautogui.hotkey") as mock_hotkey:
        res = server.hotkey("Notepad", "ctrl+a")
        assert res["status"] == "success"
        assert res["hotkey"] == "ctrl+a"
        mock_hotkey.assert_called_once_with("ctrl", "a")


# -------------------------------------------------------------------------
# 7. Kill switch and rate limit enforcement on Phase 3 tools
# -------------------------------------------------------------------------

def test_phase3_tools_kill_switch_active():
    """Verify Phase 3 tools immediately deny execution when kill switch is tripped."""
    with patch("guard.is_kill_switch_active", return_value=(True, "Kill switch STOP file present")):
        assert "denied" in server.launch_app("notepad")["error"].lower()
        assert "denied" in server.focus_window("Notepad")["error"].lower()
        assert "denied" in server.mouse_click("Notepad", 100, 100)["error"].lower()
        assert "denied" in server.mouse_move("Notepad", 100, 100)["error"].lower()
        assert "denied" in server.mouse_drag("Notepad", 10, 10, 50, 50)["error"].lower()
        assert "denied" in server.mouse_scroll("Notepad", 100, 100, 5)["error"].lower()
        assert "denied" in server.type_text("Notepad", "test")["error"].lower()
        assert "denied" in server.press_key("Notepad", "enter")["error"].lower()
        assert "denied" in server.hotkey("Notepad", "ctrl+c")["error"].lower()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_client_phase3_tools_discovery_and_security():
    """Verify all Phase 3 tools are exposed via MCP and enforce guardrails over stdio."""
    guard.reset_rate_limits()
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        cwd=str(BASE_DIR),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_res = await session.initialize()
            assert init_res.serverInfo.name == "win-computer-mcp"

            # 1. Tool discovery
            tools_res = await session.list_tools()
            tool_names = [t.name for t in tools_res.tools]
            phase3_tools = [
                "launch_app",
                "focus_window",
                "mouse_click",
                "mouse_move",
                "mouse_drag",
                "mouse_scroll",
                "type_text",
                "press_key",
                "hotkey",
            ]
            for pt in phase3_tools:
                assert pt in tool_names, f"Tool {pt} not found in MCP tool list"

            # 2. Test launch_app security rejection over MCP
            launch_bad = await session.call_tool("launch_app", arguments={"app_name": "cmd.exe"})
            launch_data = json.loads(launch_bad.content[0].text)
            assert "error" in launch_data
            assert "Security rejection" in launch_data["error"] or "Access denied" in launch_data["error"]

            # 3. Test hotkey security rejection over MCP (Win+R)
            hotkey_bad = await session.call_tool("hotkey", arguments={"window_title": "Notepad", "keys": "win+r"})
            hotkey_data = json.loads(hotkey_bad.content[0].text)
            assert "error" in hotkey_data
            assert "Security rejection" in hotkey_data["error"]

            # 4. Test press_key security rejection over MCP (Windows key)
            press_bad = await session.call_tool("press_key", arguments={"window_title": "Notepad", "key": "win"})
            press_data = json.loads(press_bad.content[0].text)
            assert "error" in press_data
            assert "Security rejection" in press_data["error"]
