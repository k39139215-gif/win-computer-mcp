"""
test_guard.py - Comprehensive unit tests for Phase 2 Guard Layer.
Tests fail-closed architecture, UWP resolution, path escape defense,
kill switch, rate limits, native confirmation, and password field blocking.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import guard
import server


@pytest.fixture(autouse=True)
def reset_guard_state():
    """Ensure clean state before each guard test."""
    guard.set_kill_switch(False)
    guard.reset_rate_limits()
    if guard.STOP_FILE_PATH.exists():
        guard.STOP_FILE_PATH.unlink()
    yield
    guard.set_kill_switch(False)
    guard.reset_rate_limits()
    if guard.STOP_FILE_PATH.exists():
        guard.STOP_FILE_PATH.unlink()


# -------------------------------------------------------------------------
# A. Config & Fail-Closed Tests
# -------------------------------------------------------------------------

def test_config_case_insensitive_and_deduplication():
    """Verify process names and paths are deduplicated and case-insensitive."""
    cfg = guard.load_config()
    assert "calculatorapp.exe" in cfg["allowed_apps"]
    assert "notepad.exe" in cfg["allowed_apps"]
    assert "applicationframehost.exe" not in cfg["allowed_apps"]
    assert "chrome.exe" in cfg["blocked_apps"]
    assert "systemsettings.exe" in cfg["blocked_apps"]

    # Verify case-insensitive check
    allowed, _ = guard.is_app_allowed("Chrome.exe")
    assert allowed is False  # Must match chrome.exe in blocked_apps

    allowed, _ = guard.is_app_allowed("CHROME.EXE")
    assert allowed is False

    allowed, _ = guard.is_app_allowed("Notepad.exe")
    assert allowed is True

    allowed, _ = guard.is_app_allowed("NOTEPAD.EXE")
    assert allowed is True


def test_fail_closed_on_missing_or_corrupt_config(tmp_path):
    """Verify that if config is missing, corrupt, or invalid, guard fails closed (all deny)."""
    with patch.object(guard, "CONFIG_PATH", tmp_path / "nonexistent_config.json"):
        cfg = guard.load_config()
        assert cfg == {}
        # is_app_allowed must deny
        allowed, reason = guard.is_app_allowed("notepad.exe")
        assert allowed is False
        assert "Fail-closed" in reason

        # check_path_allowed must deny
        path_allowed, path_reason = guard.check_path_allowed("C:\\Users\\kartik\\agent-workspace\\file.txt")
        assert path_allowed is False
        assert "Fail-closed" in path_reason

    # Corrupt JSON
    corrupt_file = tmp_path / "corrupt_config.json"
    corrupt_file.write_text("{invalid_json: 123", encoding="utf-8")
    with patch.object(guard, "CONFIG_PATH", corrupt_file):
        cfg = guard.load_config()
        assert cfg == {}
        allowed, reason = guard.is_app_allowed("notepad.exe")
        assert allowed is False


def test_uwp_process_resolution_allow_deny():
    """Verify Calculator is ALLOWED and Windows Settings is DENIED."""
    allowed_calc, _ = guard.is_app_allowed("calculatorapp.exe")
    assert allowed_calc is True

    allowed_settings, reason = guard.is_app_allowed("systemsettings.exe")
    assert allowed_settings is False
    assert "blocked" in reason

    # ApplicationFrameHost by itself is NOT allowed
    allowed_frame, reason = guard.is_app_allowed("applicationframehost.exe")
    assert allowed_frame is False
    assert "not in allowed_apps" in reason

    # Unknown or unresolvable process is DENIED
    allowed_unk, reason = guard.is_app_allowed("unknown")
    assert allowed_unk is False
    assert "Fail-closed" in reason


# -------------------------------------------------------------------------
# B. Path Security & Traversal Escape Tests
# -------------------------------------------------------------------------

def test_path_checks_allowed_directory():
    """Verify valid paths in agent-workspace are approved."""
    allowed, _ = guard.check_path_allowed("C:\\Users\\kartik\\agent-workspace\\subfolder\\doc.txt")
    assert allowed is True

    allowed, _ = guard.check_path_allowed(Path("C:/Users/kartik/agent-workspace/file.json"))
    assert allowed is True


def test_path_checks_refuse_traversal_escapes():
    """Verify '..' path traversal attempts are caught and denied."""
    # Attempting to escape agent-workspace to parent directory
    escape_path = "C:\\Users\\kartik\\agent-workspace\\..\\desktop\\secret.txt"
    allowed, reason = guard.check_path_allowed(escape_path)
    assert allowed is False
    assert "Access denied" in reason

    escape_root = "C:\\Users\\kartik\\agent-workspace\\..\\..\\Windows\\System32\\cmd.exe"
    allowed, reason = guard.check_path_allowed(escape_root)
    assert allowed is False


def test_path_checks_refuse_project_and_gemini_folders():
    """Verify writes/access to win-computer-mcp, guard.py, config.json, audit.log, and .gemini are refused."""
    # Project directory
    allowed, reason = guard.check_path_allowed(BASE_DIR)
    assert allowed is False
    assert "protected directory" in reason

    # Individual critical project files
    for sensitive_file in [guard.CONFIG_PATH, guard.AUDIT_LOG_PATH, BASE_DIR / "guard.py", BASE_DIR / "server.py"]:
        allowed, reason = guard.check_path_allowed(sensitive_file)
        assert allowed is False
        assert "protected directory" in reason

    # .gemini folder
    gemini_path = Path("C:\\Users\\kartik\\.gemini\\config\\settings.json")
    allowed, reason = guard.check_path_allowed(gemini_path)
    assert allowed is False
    assert "protected directory" in reason


def test_path_checks_symlink_escape(tmp_path):
    """Verify symlinks pointing outside allowed_dirs are resolved to real target and denied."""
    target_outside = tmp_path / "outside_target.txt"
    target_outside.write_text("outside", encoding="utf-8")

    link_path = tmp_path / "symlink_test.txt"
    try:
        os.symlink(target_outside, link_path)
        allowed, reason = guard.check_path_allowed(link_path)
        assert allowed is False
    except OSError:
        # On Windows without SeCreateSymbolicLinkPrivilege, symlink creation might fail
        pass


# -------------------------------------------------------------------------
# C. Kill Switch Tests
# -------------------------------------------------------------------------

def test_kill_switch_flag():
    """Verify in-memory kill switch immediately blocks operations."""
    try:
        assert guard.is_kill_switch_active()[0] is False

        guard.set_kill_switch(True)
        active, reason = guard.is_kill_switch_active()
        assert active is True
        assert "Ctrl+Alt+Shift+K" in reason

        # Verify server tools respect kill switch
        win_res = server.list_windows()
        assert len(win_res) == 1 and "error" in win_res[0]
        assert "Action denied" in win_res[0]["error"]

        ctrl_res = server.list_controls("Calculator")
        assert "error" in ctrl_res and "Action denied" in ctrl_res["error"]

        shot_res = server.screenshot()
        assert len(shot_res) == 1 and "Action denied" in shot_res[0]
    finally:
        guard.set_kill_switch(False)


def test_kill_switch_stop_file():
    """Verify STOP file in project directory triggers immediate refusal."""
    try:
        guard.STOP_FILE_PATH.write_text("STOP", encoding="utf-8")
        active, reason = guard.is_kill_switch_active()
        assert active is True
        assert "STOP" in reason

        win_res = server.list_windows()
        assert "Action denied" in win_res[0]["error"]
    finally:
        if guard.STOP_FILE_PATH.exists():
            guard.STOP_FILE_PATH.unlink()


# -------------------------------------------------------------------------
# D. Rate Limiting Tests
# -------------------------------------------------------------------------

def test_rate_limit_per_minute():
    """Verify actions per minute limit is strictly enforced."""
    guard.reset_rate_limits()
    # Mock max_actions_per_minute = 5
    with patch.dict(guard.load_config()["rate_limits"], {"max_actions_per_minute": 5}):
        with patch.object(guard, "load_config") as mock_cfg:
            mock_cfg.return_value = {
                "allowed_apps": ["calc.exe"],
                "blocked_apps": [],
                "allowed_dirs": ["C:\\Users\\kartik\\agent-workspace"],
                "rate_limits": {"max_actions_per_minute": 5, "max_actions_per_task": 100},
            }
            # First 5 actions must succeed
            for _ in range(5):
                allowed, _ = guard.check_rate_limit()
                assert allowed is True

            # 6th action must be rejected
            allowed, reason = guard.check_rate_limit()
            assert allowed is False
            assert "Rate limit exceeded" in reason


def test_rate_limit_per_task():
    """Verify actions per task limit is strictly enforced."""
    guard.reset_rate_limits()
    with patch.object(guard, "load_config") as mock_cfg:
        mock_cfg.return_value = {
            "allowed_apps": ["calc.exe"],
            "blocked_apps": [],
            "allowed_dirs": ["C:\\Users\\kartik\\agent-workspace"],
            "rate_limits": {"max_actions_per_minute": 100, "max_actions_per_task": 3},
        }
        for _ in range(3):
            allowed, _ = guard.check_rate_limit()
            assert allowed is True

        allowed, reason = guard.check_rate_limit()
        assert allowed is False
        assert "Task limit exceeded" in reason


# -------------------------------------------------------------------------
# E. Native Confirmation Dialog Tests
# -------------------------------------------------------------------------

def test_confirm_dialog_timeout_denies():
    """Verify confirmation dialog times out and returns False (deny)."""
    # 300ms timeout
    approved = guard.confirm("Automated test prompt - timeout check", timeout_ms=300)
    assert approved is False


# -------------------------------------------------------------------------
# F. Password Field Blocking
# -------------------------------------------------------------------------

def test_is_password_control_detection():
    """Verify UIA password elements are recognized."""
    mock_ctrl = MagicMock()
    mock_ctrl.element_info._element.CurrentIsPassword = 1
    assert guard.is_password_control(mock_ctrl) is True

    mock_ctrl2 = MagicMock()
    mock_ctrl2.element_info._element.CurrentIsPassword = 0
    mock_ctrl2.element_info.name = "Normal text field"
    mock_ctrl2.element_info.is_password = False
    assert guard.is_password_control(mock_ctrl2) is False

    mock_ctrl3 = MagicMock()
    mock_ctrl3.element_info._element = None
    mock_ctrl3.element_info.name = "Enter your Password"
    assert guard.is_password_control(mock_ctrl3) is True


# -------------------------------------------------------------------------
# G. Redaction of Blocked Apps in Windows Listing
# -------------------------------------------------------------------------

def test_list_windows_redacts_blocked_app_titles():
    """Verify visible blocked apps have their titles masked as [BLOCKED]."""
    windows = server.list_windows()
    for w in windows:
        if w["process_name"].lower() in ["chrome.exe", "msedge.exe", "1password.exe"]:
            assert w["title"] == "[BLOCKED]", f"Title was not redacted for {w['process_name']}: {w['title']}"


# -------------------------------------------------------------------------
# H. Phase 2.5: Overlap Blackout, File Writing & Persistence Tests
# -------------------------------------------------------------------------

def test_screenshot_overlap_blackout_on_notepad():
    """Verify that an overlapping window on Notepad has its intersection blacked out in the captured image."""
    # Find Notepad window rect
    d_real = server.Desktop(backend="uia")
    notepad_win = None
    for w in d_real.windows():
        if "notepad" in w.window_text().lower() or "notepad" in server._get_real_process_for_win(w).lower():
            notepad_win = w
            break

    if not notepad_win:
        pytest.skip("Notepad must be open for overlap blackout test")

    np_rect = notepad_win.rectangle()

    # Create mock overlapping window that covers top-left quadrant of Notepad
    overlap_left = np_rect.left + 20
    overlap_top = np_rect.top + 20
    overlap_width = 100
    overlap_height = 80

    mock_overlap_win = MagicMock()
    mock_overlap_win.handle = 0x999999
    mock_overlap_win.is_visible.return_value = True
    mock_overlap_rect = MagicMock()
    mock_overlap_rect.left = overlap_left
    mock_overlap_rect.top = overlap_top
    mock_overlap_rect.right = overlap_left + overlap_width
    mock_overlap_rect.bottom = overlap_top + overlap_height
    mock_overlap_rect.width.return_value = overlap_width
    mock_overlap_rect.height.return_value = overlap_height
    mock_overlap_win.rectangle.return_value = mock_overlap_rect
    mock_overlap_win.window_text.return_value = "Chrome Overlap Popup"
    mock_overlap_win.process_id.return_value = 12345

    original_windows = d_real.windows()
    mock_windows_list = [notepad_win, mock_overlap_win] + [w for w in original_windows if w.handle != notepad_win.handle]

    with patch.object(server.Desktop, "windows", return_value=mock_windows_list):
        with patch("server._get_real_process_for_win", side_effect=lambda w: "chrome.exe" if w == mock_overlap_win else "notepad.exe"):
            res = server.screenshot(window_title="Notepad")

    assert len(res) == 2
    img_obj = res[0]
    img = server.PILImage.open(server.io.BytesIO(img_obj.data))

    # Calculate expected box coordinates in scaled image space
    meta = json.loads(res[1])
    scale = meta["scale_factor"]
    check_x = int(round((20 + overlap_width // 2) * scale))
    check_y = int(round((20 + overlap_height // 2) * scale))

    # Sample pixel inside the blacked-out intersection
    pixel = img.getpixel((check_x, check_y))
    assert pixel == (0, 0, 0), f"Overlapping pixel at ({check_x}, {check_y}) was not blacked out: {pixel}"


def test_write_text_file_security_and_overwrite(tmp_path):
    """Verify write_text_file enforces path restrictions and requests confirm() on overwrite."""
    dest_path = "C:\\Users\\kartik\\agent-workspace\\phase2_test.txt"

    # 1. Clean write
    if os.path.exists(dest_path):
        os.remove(dest_path)

    msg = server.write_text_file(dest_path, "Security Test Content")
    assert "Successfully wrote" in msg
    assert os.path.exists(dest_path)
    assert Path(dest_path).read_text(encoding="utf-8") == "Security Test Content"

    # 2. Overwrite with denial -> must refuse
    with patch("guard.confirm", return_value=False) as mock_confirm:
        deny_msg = server.write_text_file(dest_path, "Overwritten Content")
        assert "Overwrite of 'phase2_test.txt' was not confirmed" in deny_msg
        assert mock_confirm.called
        # File content unchanged
        assert Path(dest_path).read_text(encoding="utf-8") == "Security Test Content"

    # 3. Overwrite with approval -> must succeed
    with patch("guard.confirm", return_value=True) as mock_confirm:
        ok_msg = server.write_text_file(dest_path, "Updated Content")
        assert "Successfully wrote" in ok_msg
        assert Path(dest_path).read_text(encoding="utf-8") == "Updated Content"

    # 4. Attempt to write to protected project folder -> must be refused
    protected_dest = str(BASE_DIR / "evil.txt")
    reject_msg = server.write_text_file(protected_dest, "Should Fail")
    assert "Security rejection" in reject_msg
    assert not os.path.exists(protected_dest)

    # Clean up test file
    if os.path.exists(dest_path):
        os.remove(dest_path)


def test_persistent_rate_limits():
    """Verify rate limits are persisted to disk and reloadable."""
    guard.reset_rate_limits()
    assert not guard.RATE_LIMITS_FILE.exists()

    allowed, _ = guard.check_rate_limit()
    assert allowed is True
    assert guard.RATE_LIMITS_FILE.exists()

    # Read state from disk
    timestamps, task_count = guard._load_rate_limits_state()
    assert len(timestamps) == 1
    assert task_count == 1

    # Second check preserves counter
    allowed2, _ = guard.check_rate_limit()
    assert allowed2 is True
    _, task_count2 = guard._load_rate_limits_state()
    assert task_count2 == 2

    guard.reset_rate_limits()
    assert not guard.RATE_LIMITS_FILE.exists()


def test_file_dialog_detection():
    """Verify detection of file dialog windows."""
    mock_hwnd = 0x1234
    with patch("win32gui.GetClassName", return_value="#32770"):
        with patch("win32gui.GetWindowText", return_value="Save As"):
            assert guard.is_file_dialog_window(mock_hwnd) is True

    with patch("win32gui.GetClassName", return_value="Notepad"):
        with patch("win32gui.GetWindowText", return_value="Untitled - Notepad"):
            assert guard.is_file_dialog_window(mock_hwnd) is False

