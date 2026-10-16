"""
test_phase1.py - Automated tests for Phase 1 read-only tools and audit logging.
Tests screenshot, list_windows, list_controls with Notepad and Calculator.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image as PILImage

# Ensure win-computer-mcp is in python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import guard
import server


@pytest.fixture(autouse=True)
def ensure_desktop_env():
    """Ensure attached to default desktop and clean guard state before each test."""
    guard.ensure_default_desktop()
    guard.set_kill_switch(False)
    guard.reset_rate_limits()
    yield
    guard.set_kill_switch(False)
    guard.reset_rate_limits()


def test_audit_log_format_and_sanitization():
    """Verify audit.log records valid JSON lines and redacts sensitive keys."""
    initial_log_size = 0
    if guard.AUDIT_LOG_PATH.exists():
        initial_log_size = guard.AUDIT_LOG_PATH.stat().st_size

    guard.log_audit(
        "test_tool",
        {"user": "alice", "password": "supersecret123", "token": "abc-xyz-token"},
        "success",
        "Test entry"
    )

    assert guard.AUDIT_LOG_PATH.exists()
    assert guard.AUDIT_LOG_PATH.stat().st_size > initial_log_size

    with open(guard.AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
        last_record = json.loads(lines[-1].strip())

    assert last_record["tool"] == "test_tool"
    assert last_record["status"] == "success"
    assert last_record["parameters"]["user"] == "alice"
    assert last_record["parameters"]["password"] == "[REDACTED]"
    assert last_record["parameters"]["token"] == "[REDACTED]"
    assert "supersecret123" not in json.dumps(last_record)


def test_screenshot_fullscreen_refused_by_policy():
    """Verify fullscreen screenshot capture without window_title is refused by default allowlist policy."""
    res = server.screenshot()
    assert isinstance(res, list)
    assert len(res) == 1
    assert "Security rejection" in res[0]
    assert "Fullscreen capture is refused" in res[0]


def test_screenshot_window_scoped_allowed():
    """Verify window-scoped capture of an allowed application (Calculator)."""
    _ensure_app_open("calc.exe", "Calculator")
    res = server.screenshot(window_title="Calculator")
    assert isinstance(res, list)
    assert len(res) == 2

    img_obj, meta_json = res[0], res[1]
    meta = json.loads(meta_json)

    assert "scale_factor" in meta
    assert "original_width" in meta
    assert "original_height" in meta
    assert "scaled_width" in meta
    assert "scaled_height" in meta
    assert "region" in meta
    assert meta["window_title"] == "Calculator"

    scale_factor = meta["scale_factor"]
    orig_w = meta["original_width"]
    orig_h = meta["original_height"]
    scaled_w = meta["scaled_width"]
    scaled_h = meta["scaled_height"]

    assert max(scaled_w, scaled_h) <= 1280
    assert scaled_w == int(round(orig_w * scale_factor))
    assert scaled_h == int(round(orig_h * scale_factor))

    img_bytes = img_obj.data
    assert isinstance(img_bytes, bytes)
    img = PILImage.open(io.BytesIO(img_bytes))
    assert img.format == "PNG"
    assert img.size == (scaled_w, scaled_h)



def test_list_windows():
    """Verify list_windows returns top-level visible windows with title, process, and valid rect."""
    windows = server.list_windows()
    assert isinstance(windows, list)
    assert len(windows) > 0

    for win in windows:
        assert "title" in win and len(win["title"]) > 0
        assert "process_name" in win and len(win["process_name"]) > 0
        assert "rect" in win
        r = win["rect"]
        assert r["width"] > 0
        assert r["height"] > 0
        assert r["right"] > r["left"]
        assert r["bottom"] > r["top"]


def _ensure_app_open(cmd_name: str, window_keyword: str):
    """Ensure an application is open and visible on desktop before testing."""
    for w in server.list_windows():
        if window_keyword.lower() in w["title"].lower():
            return
    server.launch_app(cmd_name)
    for _ in range(15):
        time.sleep(0.3)
        for w in server.list_windows():
            if window_keyword.lower() in w["title"].lower():
                return


def test_list_controls_notepad():
    """Verify list_controls inspects controls in Notepad."""
    _ensure_app_open("notepad.exe", "Notepad")
    result = server.list_controls("Notepad", max_items=50)

    if "error" in result and "not found" in result["error"].lower():
        pytest.skip("Notepad window is not currently open on this desktop")

    assert "error" not in result, f"list_controls failed on Notepad: {result.get('error')}"
    assert result["total_controls"] > 0
    assert len(result["controls"]) > 0
    first_ctrl = result["controls"][0]
    assert "control_type" in first_ctrl
    assert "name" in first_ctrl
    assert "automation_id" in first_ctrl
    assert "rect" in first_ctrl
    assert "enabled" in first_ctrl




def test_list_controls_calculator():
    """Verify list_controls inspects controls in Calculator."""
    _ensure_app_open("calc.exe", "Calculator")
    result = server.list_controls("Calculator", max_items=30)

    assert "error" not in result, f"Failed to find Calculator window: {result.get('error')}"
    assert result["total_controls"] > 0
    assert len(result["controls"]) > 0
    # Check truncation behavior
    if result["total_controls"] > 30:
        assert result["truncated"] is True
        assert result["returned_controls"] == 30


def test_list_controls_nonexistent_window():
    """Verify graceful handling when window is not found."""
    result = server.list_controls("NonExistentWindow_XYZ_98765")
    assert "error" in result
    assert result["controls"] == []

