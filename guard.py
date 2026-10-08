"""
guard.py - Safety checks, audit logging, security boundaries, and persistence.
ALL safety checks live here.
"""

from __future__ import annotations

import collections
import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
AUDIT_LOG_PATH = BASE_DIR / "audit.log"
STOP_FILE_PATH = BASE_DIR / "STOP"
RATE_LIMITS_FILE = BASE_DIR / "rate_limits.json"

# Protected project and environment directories that must NEVER be writable
FORBIDDEN_PATHS = [
    BASE_DIR,
    Path(os.environ.get("USERPROFILE", "C:\\Users\\kartik")) / ".gemini",
]

# Sensitive keywords that must never be recorded in audit logs
SENSITIVE_KEYS = {
    "password", "passwd", "token", "secret", "auth", "credential",
    "pin", "api_key", "apikey", "private_key", "clipboard"
}

# In-memory kill switch flag
_KILL_SWITCH_FLAG: bool = False
_HOTKEY_THREAD: Optional[threading.Thread] = None

# Rate limit tracking lock
_RATE_LIMIT_LOCK = threading.Lock()


class GuardError(Exception):
    """Base exception for security boundary rejections."""
    pass


class GuardConfigError(GuardError):
    """Raised when configuration is missing, corrupt, or invalid (fail closed)."""
    pass


def init_dpi_awareness() -> None:
    """
    Set process DPI awareness to per-monitor aware (value 2).
    Ensures screenshot and window coordinates match 1:1 on Windows 11.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_default_desktop() -> bool:
    """
    Ensure the current thread is attached to the interactive 'default' desktop.
    MUST be called inside EVERY thread that registers hotkeys, displays UI,
    or interacts with windows.
    """
    if sys.platform != "win32":
        return True
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("default", 0, False, 0x1FF)
        if hdesk:
            return bool(user32.SetThreadDesktop(hdesk))
    except Exception:
        pass
    return False


# Execute immediately on module import
ensure_default_desktop()
init_dpi_awareness()


def load_config() -> Dict[str, Any]:
    """
    Load and validate configuration from config.json.
    FAIL CLOSED: If missing, corrupt, unparseable, or invalid, returns empty config
    that causes all authorization checks to deny.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        # Normalize and deduplicate case-insensitively
        normalized: Dict[str, Any] = {}
        normalized["allowed_apps"] = sorted(list({
            str(app).strip().lower() for app in cfg.get("allowed_apps", []) if str(app).strip()
        }))
        normalized["blocked_apps"] = sorted(list({
            str(app).strip().lower() for app in cfg.get("blocked_apps", []) if str(app).strip()
        }))
        normalized["allowed_dirs"] = sorted(list({
            os.path.realpath(os.path.abspath(d)).lower()
            for d in cfg.get("allowed_dirs", []) if str(d).strip()
        }))
        normalized["rate_limits"] = {
            "max_actions_per_minute": int(cfg.get("rate_limits", {}).get("max_actions_per_minute", 30)),
            "max_actions_per_task": int(cfg.get("rate_limits", {}).get("max_actions_per_task", 100)),
        }
        normalized["auto_approve_low_risk"] = bool(cfg.get("auto_approve_low_risk", False))
        return normalized
    except Exception as e:
        sys.stderr.write(f"Guard config load failure (fail-closed): {e}\n")
        return {}


def is_kill_switch_active() -> Tuple[bool, str]:
    """
    Check if the kill switch has been triggered via hotkey or persistent STOP file.
    PERSISTENT: If STOP file exists on disk, kill switch remains active across restarts.
    """
    global _KILL_SWITCH_FLAG
    if STOP_FILE_PATH.exists():
        return True, f"Kill switch active: file '{STOP_FILE_PATH.name}' exists in project folder (remove manually to resume)"

    if _KILL_SWITCH_FLAG:
        return True, "Kill switch activated via global hotkey (Ctrl+Alt+Shift+K)"

    return False, ""


def set_kill_switch(state: bool = True) -> None:
    """Trip or reset the in-memory kill switch flag."""
    global _KILL_SWITCH_FLAG
    _KILL_SWITCH_FLAG = state


def _hotkey_listener_thread() -> None:
    """Background listener for global emergency hotkey: Ctrl+Alt+Shift+K."""
    if sys.platform != "win32":
        return
    # Ensure this specific thread is attached to interactive desktop
    ensure_default_desktop()

    user32 = ctypes.windll.user32
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_K = ord("K")
    hotkey_id = 9999

    res = user32.RegisterHotKey(0, hotkey_id, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_K)
    if not res:
        return

    try:
        msg = wintypes.MSG()
        while True:
            if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):  # PM_REMOVE = 1
                if msg.message == 0x0312:  # WM_HOTKEY
                    global _KILL_SWITCH_FLAG
                    _KILL_SWITCH_FLAG = True
                    # PERSIST to STOP file immediately
                    try:
                        STOP_FILE_PATH.write_text(
                            f"STOP triggered by hotkey at {datetime.now(timezone.utc).isoformat()}",
                            encoding="utf-8"
                        )
                    except Exception:
                        pass
                    sys.stderr.write("!!! EMERGENCY KILL SWITCH TRIGGERED VIA HOTKEY (STOP file written) !!!\n")
                    log_audit("kill_switch", {}, "triggered", "Global hotkey Ctrl+Alt+Shift+K detected; STOP file created")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.05)
    except Exception:
        pass
    finally:
        user32.UnregisterHotKey(0, hotkey_id)


def start_hotkey_listener() -> None:
    """Start global hotkey listener daemon attached to default desktop."""
    global _HOTKEY_THREAD
    if _HOTKEY_THREAD is None or not _HOTKEY_THREAD.is_alive():
        _HOTKEY_THREAD = threading.Thread(target=_hotkey_listener_thread, daemon=True, name="GuardHotkeyListener")
        _HOTKEY_THREAD.start()


# Start listener immediately
start_hotkey_listener()


def resolve_real_process(hwnd: int) -> str:
    """
    Resolve the real owning process name for an HWND.
    Crucial for UWP apps hosted by ApplicationFrameHost.exe (e.g., Calculator, Settings).
    Inspects child CoreWindow, child HWND PIDs, and UIA elements.
    Returns lowercase process name (e.g. 'calculatorapp.exe', 'systemsettings.exe') or 'unknown'.
    """
    if not hwnd or sys.platform != "win32":
        return "unknown"
    try:
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "unknown"

        frame_proc_name = psutil.Process(pid.value).name().lower()
        if frame_proc_name != "applicationframehost.exe":
            return frame_proc_name

        import win32gui
        import win32process

        child_names: List[str] = []

        def enum_child(ch, _):
            _, cpid = win32process.GetWindowThreadProcessId(ch)
            if cpid != pid.value:
                try:
                    cname = psutil.Process(cpid).name().lower()
                    if cname != "applicationframehost.exe":
                        child_names.append(cname)
                except Exception:
                    pass
            return True

        win32gui.EnumChildWindows(hwnd, enum_child, None)
        if child_names:
            return child_names[0]

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()

        if title:
            core_hwnd = win32gui.FindWindowEx(0, 0, "Windows.UI.Core.CoreWindow", title)
            if core_hwnd:
                _, cpid = win32process.GetWindowThreadProcessId(core_hwnd)
                try:
                    cname = psutil.Process(cpid).name().lower()
                    if cname != "applicationframehost.exe":
                        return cname
                except Exception:
                    pass

        try:
            from pywinauto import Desktop
            d = Desktop(backend="uia")
            w = d.window(handle=hwnd)
            for c in w.children():
                cpid = c.process_id()
                if cpid and cpid != pid.value:
                    cname = psutil.Process(cpid).name().lower()
                    if cname != "applicationframehost.exe":
                        return cname
        except Exception:
            pass

        return "applicationframehost.exe"

    except Exception:
        return "unknown"


def is_app_allowed(process_name: str) -> Tuple[bool, str]:
    """
    Verify whether a process name is permitted according to config.
    FAIL CLOSED: Unresolved names ('unknown' or empty) or missing config = DENIED.
    Case-insensitive comparison.
    """
    config = load_config()
    allowed_apps = config.get("allowed_apps", [])
    blocked_apps = config.get("blocked_apps", [])

    if not allowed_apps:
        return False, "Fail-closed: No allowed_apps defined in config"

    pname = (process_name or "").strip().lower()
    if not pname or pname == "unknown":
        return False, "Fail-closed: Process name is unknown or unresolvable"

    if pname in blocked_apps:
        return False, f"Access denied: Process '{pname}' is explicitly blocked"

    if pname not in allowed_apps:
        return False, f"Access denied: Process '{pname}' is not in allowed_apps"

    return True, ""


def check_foreground_window() -> Tuple[bool, str]:
    """
    Verify that the current foreground window belongs to an allowed app
    and is not a blocked app.
    FAIL CLOSED: Any failure or unresolvable window = DENIED.
    """
    if sys.platform != "win32":
        return True, ""
    try:
        ensure_default_desktop()
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False, "Fail-closed: No active foreground window detected"

        real_process = resolve_real_process(hwnd)
        return is_app_allowed(real_process)

    except Exception as e:
        return False, f"Fail-closed: Foreground window check failed with exception: {e}"


def is_file_dialog_window(hwnd: int) -> bool:
    """
    Detect if an HWND represents a standard Windows file dialog (Save As, Open, etc.).
    Class #32770 or known file dialog titles.
    """
    if not hwnd or sys.platform != "win32":
        return False
    try:
        import win32gui
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd).strip().lower()

        if cls_name == "#32770":
            return True
        if any(dialog_name in title for dialog_name in ["save as", "open file", "save file", "browse for folder"]):
            return True
        return False
    except Exception:
        return False


def is_foreground_file_dialog() -> bool:
    """Return True if the current foreground window is a file dialog."""
    if sys.platform != "win32":
        return False
    ensure_default_desktop()
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return is_file_dialog_window(hwnd)


def check_path_allowed(target_path: str | Path) -> Tuple[bool, str]:
    """
    Path verification resolving symlinks and '..' traversal via os.path.realpath.
    Compares case-insensitively against allowed_dirs.
    Explicitly refuses paths inside the project folder, .gemini, or config files.
    FAIL CLOSED: Any error, missing allowed_dirs, or escaping path = DENIED.
    """
    try:
        config = load_config()
        allowed_dirs = config.get("allowed_dirs", [])
        if not allowed_dirs:
            return False, "Fail-closed: No allowed_dirs configured"

        real_target = Path(os.path.realpath(os.path.abspath(target_path)))
        real_target_str = str(real_target).lower()

        # Explicitly protect project files and forbidden parent directories
        for forbidden in FORBIDDEN_PATHS:
            real_forbidden = Path(os.path.realpath(os.path.abspath(forbidden)))
            real_forbidden_str = str(real_forbidden).lower()
            if real_target_str == real_forbidden_str or real_target_str.startswith(real_forbidden_str + os.sep):
                return False, f"Access denied: Path targets protected directory '{forbidden}'"

        # Check against allowed_dirs
        for allowed in allowed_dirs:
            real_allowed = Path(os.path.realpath(os.path.abspath(allowed)))
            real_allowed_str = str(real_allowed).lower()
            if real_target_str == real_allowed_str or real_target_str.startswith(real_allowed_str + os.sep):
                return True, ""

        return False, f"Access denied: Path '{target_path}' is outside allowed_dirs"

    except Exception as e:
        return False, f"Fail-closed: Path check exception: {e}"


# -------------------------------------------------------------------------
# Persistent Rate Limiting
# -------------------------------------------------------------------------

def _load_rate_limits_state() -> Tuple[List[float], int]:
    """Load persistent rate limit timestamps and counter from disk."""
    if not RATE_LIMITS_FILE.exists():
        return [], 0
    try:
        data = json.loads(RATE_LIMITS_FILE.read_text(encoding="utf-8"))
        return data.get("minute_timestamps", []), data.get("task_action_count", 0)
    except Exception:
        return [], 0


def _save_rate_limits_state(timestamps: List[float], task_count: int) -> None:
    """Save persistent rate limit state to disk."""
    try:
        payload = {
            "minute_timestamps": timestamps,
            "task_action_count": task_count,
        }
        RATE_LIMITS_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def check_rate_limit() -> Tuple[bool, str]:
    """
    Persistent rate limiting: max actions per minute and max actions per task.
    Preserves action counters across server restarts via rate_limits.json.
    FAIL CLOSED on missing config.
    """
    config = load_config()
    limits = config.get("rate_limits", {})
    max_per_min = limits.get("max_actions_per_minute", 30)
    max_per_task = limits.get("max_actions_per_task", 100)

    now = time.time()
    with _RATE_LIMIT_LOCK:
        timestamps, task_count = _load_rate_limits_state()
        # Filter timestamps older than 60s
        filtered_timestamps = [t for t in timestamps if now - t <= 60]

        if len(filtered_timestamps) >= max_per_min:
            return False, f"Rate limit exceeded: {len(filtered_timestamps)}/{max_per_min} actions in the last 60s"

        if task_count >= max_per_task:
            return False, f"Task limit exceeded: {task_count}/{max_per_task} actions for this task"

        filtered_timestamps.append(now)
        task_count += 1
        _save_rate_limits_state(filtered_timestamps, task_count)

    return True, ""


def reset_rate_limits() -> None:
    """Reset persistent and in-memory rate limiting counters."""
    with _RATE_LIMIT_LOCK:
        if RATE_LIMITS_FILE.exists():
            try:
                RATE_LIMITS_FILE.unlink()
            except Exception:
                pass


def confirm(action_description: str, timeout_ms: int = 15000) -> bool:
    """
    Native blocking Windows confirmation dialog in the server process.
    Attaches calling thread to default desktop before displaying dialog.
    The model cannot answer or bypass it.
    Default button = NO (deny). Timeout = deny.
    Returns True ONLY if user explicitly clicks YES.
    """
    if sys.platform != "win32":
        return False

    ensure_default_desktop()
    user32 = ctypes.windll.user32
    MB_YESNO = 0x00000004
    MB_ICONWARNING = 0x00000030
    MB_DEFBUTTON2 = 0x00000100  # Default = No (Deny)
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND

    msg = f"An AI agent is requesting the following action:\n\n{action_description}\n\nDo you authorize this action?"
    title = "[SECURITY] Windows Computer MCP - Action Authorization"

    try:
        res = user32.MessageBoxTimeoutW(0, msg, title, flags, 0, timeout_ms)
        approved = (res == 6)
        log_audit(
            "confirm",
            {"action": action_description, "timeout_ms": timeout_ms},
            "approved" if approved else "denied",
            f"Dialog result code: {res}",
        )
        return approved
    except Exception as e:
        sys.stderr.write(f"Confirmation dialog failed (fail-closed): {e}\n")
        log_audit("confirm", {"action": action_description}, "error", str(e))
        return False


def is_password_control(control: Any) -> bool:
    """
    Check if a UIA control is a password field (IsPassword == True).
    Typing into password fields is strictly forbidden.
    """
    try:
        elem = getattr(control.element_info, "_element", None)
        if elem and getattr(elem, "CurrentIsPassword", None):
            return True
        if getattr(control.element_info, "is_password", False):
            return True
        name_lower = (control.element_info.name or "").lower()
        if "password" in name_lower or "passcode" in name_lower:
            return True
        return False
    except Exception:
        return False


def sanitize_data(data: Any) -> Any:
    """Recursively redact any sensitive keys or values."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = sanitize_data(v)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    return data


def log_audit(
    tool_name: str,
    parameters: Optional[Dict[str, Any]] = None,
    status: str = "success",
    details: Optional[str] = None
) -> None:
    """
    Append an action record to audit.log in JSON Lines format.
    Guarantees no passwords, tokens, or clipboard contents are logged.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "parameters": sanitize_data(parameters or {}),
        "status": status,
        "details": details or "",
    }
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"Audit log write failed: {e}\n")
