"""
server.py - Windows Computer Use MCP Server (Phase 2.5: Guarded Read-Only + Safe File Writer).
Enforces window-scoped screenshot capture (fullscreen refused by default),
overlap blackout, blocked-window title redaction, and safe guarded file writing.
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import guard  # Must be imported first to ensure desktop attachment before any COM/UI handles

import psutil
from PIL import Image as PILImage, ImageDraw
from pywinauto import Desktop
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("win-computer-mcp")


def _get_real_process_for_win(win) -> str:
    """Resolve the real process name for a pywinauto window, resolving UWP hosts."""
    try:
        hwnd = win.handle
        if hwnd:
            real = guard.resolve_real_process(hwnd)
            if real != "unknown":
                return real
        pid = win.process_id()
        if pid:
            return psutil.Process(pid).name().lower()
    except Exception:
        pass
    return "unknown"


@mcp.tool()
def screenshot(window_title: Optional[str] = None) -> list:
    """
    Capture the screen of an allowed application window.
    SECURITY POLICY (Allowlist): Fullscreen capture is REFUSED by default.
    You MUST provide 'window_title' for a specific allowed application (e.g. 'Notepad', 'Calculator').
    If any other visible window (including notifications/browsers) overlaps the target window,
    the overlapping region is automatically blacked out.
    
    Args:
        window_title: Title or owning process name of an allowed window to capture.
    """
    # 1. Kill switch check
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("screenshot", {"window_title": window_title}, "denied", kill_reason)
        return [f"Action denied: {kill_reason}"]

    # 2. Rate limit check
    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("screenshot", {"window_title": window_title}, "denied", rate_reason)
        return [f"Action denied: {rate_reason}"]

    # 3. Allowlist policy: Refuse fullscreen capture
    if not window_title:
        msg = "Fullscreen capture is refused by security policy. You must specify 'window_title' for an allowed window."
        guard.log_audit("screenshot", {}, "denied", msg)
        return [f"Security rejection: {msg}"]

    guard.ensure_default_desktop()

    try:
        import mss
        sct_factory = getattr(mss, "MSS", mss.mss)

        # 4. Find the target window among visible top-level windows
        d = Desktop(backend="uia")
        target_win = None
        target_lower = window_title.lower()

        for w in d.windows():
            try:
                if not w.is_visible():
                    continue
                t = w.window_text()
                proc = _get_real_process_for_win(w)
                if target_lower in t.lower() or target_lower in proc.lower():
                    if ctypes.windll.user32.IsIconic(w.handle):
                        ctypes.windll.user32.ShowWindow(w.handle, 9)
                        time.sleep(0.1)
                    r = w.rectangle()
                    if r.width() <= 0 or r.height() <= 0:
                        continue
                    target_win = w
                    break
            except Exception:
                continue

        if not target_win:
            msg = f"Target window '{window_title}' not found or not visible"
            guard.log_audit("screenshot", {"window_title": window_title}, "error", msg)
            return [f"Screenshot error: {msg}"]

        # 5. Check if target window's process is allowed
        proc_name = _get_real_process_for_win(target_win)
        allowed, reason = guard.is_app_allowed(proc_name)
        if not allowed:
            guard.log_audit("screenshot", {"window_title": window_title, "process": proc_name}, "denied", reason)
            return [f"Security rejection: Cannot capture window '{window_title}' ({reason})"]

        r_target = target_win.rectangle()
        target_rect = {
            "left": max(0, r_target.left),
            "top": max(0, r_target.top),
            "width": r_target.width(),
            "height": r_target.height(),
        }

        with sct_factory() as sct:
            shot = sct.grab(target_rect)
            img = PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

            # 6. Overlap blackout: Black out any overlapping foreign/other window
            draw = ImageDraw.Draw(img)
            t_left, t_top = target_rect["left"], target_rect["top"]
            t_right = t_left + target_rect["width"]
            t_bottom = t_top + target_rect["height"]

            for w in d.windows():
                try:
                    if w.handle == target_win.handle:
                        continue
                    if not w.is_visible():
                        continue
                    wr = w.rectangle()
                    if wr.width() <= 0 or wr.height() <= 0:
                        continue

                    # Calculate intersection with target window
                    i_left = max(t_left, wr.left)
                    i_top = max(t_top, wr.top)
                    i_right = min(t_right, wr.right)
                    i_bottom = min(t_bottom, wr.bottom)

                    if i_right > i_left and i_bottom > i_top:
                        # Map to image coordinates
                        box_x1 = i_left - t_left
                        box_y1 = i_top - t_top
                        box_x2 = i_right - t_left
                        box_y2 = i_bottom - t_top

                        draw.rectangle([box_x1, box_y1, box_x2, box_y2], fill=(0, 0, 0))
                        guard.log_audit(
                            "screenshot",
                            {"redacted_overlap": w.window_text()[:30], "overlap_rect": [box_x1, box_y1, box_x2, box_y2]},
                            "redacted",
                            f"Blacked out overlapping window '{w.window_text()[:30]}' from target screenshot",
                        )
                except Exception:
                    continue

            # 7. Downscale if longest dimension > 1280
            orig_w, orig_h = img.size
            longest = max(orig_w, orig_h)
            max_dimension = 1280

            if longest > max_dimension:
                scale_factor = round(max_dimension / float(longest), 6)
                new_w = int(round(orig_w * scale_factor))
                new_h = int(round(orig_h * scale_factor))
                resample_filter = getattr(PILImage.Resampling, "LANCZOS", PILImage.LANCZOS)
                img = img.resize((new_w, new_h), resample=resample_filter)
            else:
                scale_factor = 1.0
                new_w = orig_w
                new_h = orig_h

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()

            metadata = {
                "scale_factor": scale_factor,
                "original_width": orig_w,
                "original_height": orig_h,
                "scaled_width": new_w,
                "scaled_height": new_h,
                "region": target_rect,
                "window_title": window_title,
                "process_name": proc_name,
            }

            guard.log_audit(
                "screenshot",
                {"window_title": window_title, "scale_factor": scale_factor, "process": proc_name},
                "success",
            )

            return [
                Image(data=png_bytes, format="png"),
                json.dumps(metadata, indent=2),
            ]

    except Exception as e:
        guard.log_audit("screenshot", {"window_title": window_title}, "error", str(e))
        return [f"Screenshot error: {e}"]


@mcp.tool()
def list_windows() -> List[Dict[str, Any]]:
    """
    List visible top-level windows with title, process name, and bounding rect.
    Guarded: Refuses if kill switch is active. Hides titles of blocked apps
    (reports title as '[BLOCKED]' to protect sensitive tabs/documents).
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("list_windows", {}, "denied", kill_reason)
        return [{"error": f"Action denied: {kill_reason}"}]

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("list_windows", {}, "denied", rate_reason)
        return [{"error": f"Action denied: {rate_reason}"}]

    guard.ensure_default_desktop()
    results: List[Dict[str, Any]] = []

    try:
        config = guard.load_config()
        blocked_apps = set(config.get("blocked_apps", []))

        d = Desktop(backend="uia")
        for w in d.windows():
            try:
                if not w.is_visible():
                    continue
                rect = w.rectangle()
                if rect.width() <= 0 or rect.height() <= 0 or rect.left <= -10000 or rect.top <= -10000:
                    continue
                raw_title = w.window_text().strip()
                if not raw_title:
                    continue

                proc_name = _get_real_process_for_win(w)

                # Redact title if process is blocked
                display_title = "[BLOCKED]" if proc_name.lower() in blocked_apps else raw_title

                results.append({
                    "title": display_title,
                    "process_name": proc_name,
                    "rect": {
                        "left": rect.left,
                        "top": rect.top,
                        "right": rect.right,
                        "bottom": rect.bottom,
                        "width": rect.width(),
                        "height": rect.height(),
                    },
                })
            except Exception:
                continue

        guard.log_audit(
            "list_windows",
            {},
            "success",
            f"Returned {len(results)} visible windows",
        )
        return results

    except Exception as e:
        guard.log_audit("list_windows", {}, "error", str(e))
        return [{"error": str(e)}]


@mcp.tool()
def list_controls(window_title: str, max_items: int = 150) -> Dict[str, Any]:
    """
    Inspect UIA UI controls for a given window by title or substring.
    Guarded: Refuses if kill switch is active, or if target window belongs to a blocked/non-allowed app.
    Flags/masks password controls (IsPassword=True).
    
    Args:
        window_title: Exact title or case-insensitive substring of target window.
        max_items: Maximum number of controls to return (default 150).
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("list_controls", {"window_title": window_title}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}", "controls": []}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("list_controls", {"window_title": window_title}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}", "controls": []}

    guard.ensure_default_desktop()
    try:
        d = Desktop(backend="uia")
        target_win = None
        target_title_lower = window_title.lower()

        # Search visible windows with non-zero dimensions
        for w in d.windows():
            try:
                if not w.is_visible():
                    continue
                t = w.window_text()
                proc = _get_real_process_for_win(w)
                if target_title_lower in t.lower() or target_title_lower in proc.lower():
                    if ctypes.windll.user32.IsIconic(w.handle):
                        ctypes.windll.user32.ShowWindow(w.handle, 9)
                        time.sleep(0.1)
                    r = w.rectangle()
                    if r.width() <= 0 or r.height() <= 0:
                        continue
                    target_win = w
                    break
            except Exception:
                continue

        if not target_win:
            msg = f"Window matching '{window_title}' not found among visible windows"
            guard.log_audit(
                "list_controls",
                {"window_title": window_title, "max_items": max_items},
                "error",
                msg,
            )
            return {"error": msg, "controls": []}

        # Check process security authorization
        real_proc = _get_real_process_for_win(target_win)
        allowed, reason = guard.is_app_allowed(real_proc)
        if not allowed:
            guard.log_audit("list_controls", {"window_title": window_title, "process": real_proc}, "denied", reason)
            return {"error": f"Security rejection: Cannot inspect window '{window_title}' ({reason})", "controls": []}

        full_title = target_win.window_text()
        descendants = target_win.descendants()
        total_count = len(descendants)

        controls: List[Dict[str, Any]] = []
        for c in descendants[:max_items]:
            try:
                elem = c.element_info
                r = c.rectangle()
                is_pwd = guard.is_password_control(c)
                ctrl_name = "[PASSWORD_FIELD]" if is_pwd else (elem.name or "")

                controls.append({
                    "control_type": elem.control_type or "",
                    "name": ctrl_name,
                    "automation_id": elem.automation_id or "",
                    "rect": {
                        "left": r.left,
                        "top": r.top,
                        "right": r.right,
                        "bottom": r.bottom,
                        "width": r.width(),
                        "height": r.height(),
                    },
                    "enabled": c.is_enabled(),
                    "is_password": is_pwd,
                })
            except Exception:
                continue

        result = {
            "window": full_title,
            "process": real_proc,
            "total_controls": total_count,
            "returned_controls": len(controls),
            "truncated": total_count > max_items,
            "controls": controls,
        }

        guard.log_audit(
            "list_controls",
            {"window_title": window_title, "max_items": max_items},
            "success",
            f"Returned {len(controls)}/{total_count} controls for '{full_title}'",
        )
        return result

    except Exception as e:
        guard.log_audit(
            "list_controls",
            {"window_title": window_title, "max_items": max_items},
            "error",
            str(e),
        )
        return {"error": str(e), "controls": []}


@mcp.tool()
def write_text_file(path: str, content: str) -> str:
    """
    Safely write text content to a file inside allowed_dirs (e.g. C:\\Users\\kartik\\agent-workspace).
    Guarded: Refuses if kill switch is active, or if path is outside allowed_dirs.
    Refuses if path targets project files, guard.py, config.json, audit.log, or .gemini.
    If the target file already exists, prompts for native human confirmation before overwriting.
    
    Args:
        path: Destination file path inside allowed_dirs.
        content: Text content to write.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("write_text_file", {"path": path}, "denied", kill_reason)
        return f"Action denied: {kill_reason}"

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("write_text_file", {"path": path}, "denied", rate_reason)
        return f"Action denied: {rate_reason}"

    # Path check: must be inside allowed_dirs
    path_allowed, path_reason = guard.check_path_allowed(path)
    if not path_allowed:
        guard.log_audit("write_text_file", {"path": path}, "denied", path_reason)
        return f"Security rejection: {path_reason}"

    real_dest = Path(os.path.realpath(os.path.abspath(path)))

    # If file exists, require human confirmation before overwriting
    if real_dest.exists():
        confirmed = guard.confirm(f"File '{real_dest}' already exists. Authorize overwrite?")
        if not confirmed:
            guard.log_audit("write_text_file", {"path": str(real_dest)}, "denied", "User denied overwrite confirmation")
            return f"Action denied: Overwrite of '{real_dest.name}' was not confirmed by user."

    try:
        real_dest.parent.mkdir(parents=True, exist_ok=True)
        real_dest.write_text(content, encoding="utf-8")
        msg = f"Successfully wrote {len(content)} characters to {real_dest}"
        guard.log_audit("write_text_file", {"path": str(real_dest), "bytes": len(content.encode('utf-8'))}, "success", msg)
        return msg
    except Exception as e:
        guard.log_audit("write_text_file", {"path": str(real_dest)}, "error", str(e))
        return f"Error writing file: {e}"


# -------------------------------------------------------------------------
# Phase 3: Active Input & Window Management Tools
# -------------------------------------------------------------------------

import subprocess
import pyautogui

# Safe defaults for input automation
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


def _find_target_window(window_title: str):
    """
    Find target window by title or process name, ensure desktop attachment,
    and return (pywinauto_window, real_process_name, target_rect_dict, hwnd).
    """
    guard.ensure_default_desktop()
    d = Desktop(backend="uia")
    target_lower = window_title.lower()

    for w in d.windows():
        try:
            if not w.is_visible():
                continue
            t = w.window_text()
            proc = _get_real_process_for_win(w)
            if target_lower in t.lower() or target_lower in proc.lower():
                hwnd = w.handle
                if hwnd and ctypes.windll.user32.IsIconic(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    time.sleep(0.15)
                r = w.rectangle()
                if r.width() <= 0 or r.height() <= 0:
                    continue
                rect_dict = {
                    "left": max(0, r.left),
                    "top": max(0, r.top),
                    "width": r.width(),
                    "height": r.height(),
                }
                return w, proc, rect_dict, hwnd
        except Exception:
            continue
    return None, "unknown", {}, 0


def _focus_hwnd(hwnd: int) -> bool:
    """Bring window to foreground safely."""
    if not hwnd or sys.platform != "win32":
        return False
    guard.ensure_default_desktop()
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.1)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    return True


def _launch_executable(exe_name: str) -> int:
    """Launch an allowed executable on the interactive desktop, activating packaged UWP aliases."""
    if sys.platform == "win32":
        try:
            import pythoncom
            pythoncom.CoInitialize()
            import win32com.client
            shell = win32com.client.Dispatch("Shell.Application")
            shell.ShellExecute(exe_name, "", "", "open", 1)
            return 0
        except Exception:
            pass
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "open", exe_name, None, None, 1)
            if ret > 32:
                return 0
        except Exception:
            pass
    proc = subprocess.Popen([exe_name], shell=False)
    return proc.pid


@mcp.tool()
def launch_app(app_name: str) -> Dict[str, Any]:
    """
    Launch an allowed Windows application (e.g., 'notepad', 'calc').
    SECURITY POLICY (Allowlist): Only applications explicitly in config.allowed_apps
    can be launched. Rejects arbitrary commands, scripts, or blocked applications.
    
    Args:
        app_name: Name of the application to launch (e.g. 'notepad', 'calc').
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("launch_app", {"app_name": app_name}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("launch_app", {"app_name": app_name}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    allowed, reason, exe_name = guard.validate_app_launch(app_name)
    if not allowed:
        guard.log_audit("launch_app", {"app_name": app_name}, "denied", reason)
        return {"error": f"Security rejection: {reason}"}

    guard.ensure_default_desktop()
    try:
        pid = _launch_executable(exe_name)
        time.sleep(1.0)  # Allow UI window to register

        guard.log_audit("launch_app", {"app_name": app_name, "exe": exe_name, "pid": pid}, "success")
        return {"status": "success", "app_name": exe_name, "pid": pid}
    except Exception as e:
        guard.log_audit("launch_app", {"app_name": app_name}, "error", str(e))
        return {"error": f"Failed to launch '{app_name}': {e}"}


@mcp.tool()
def focus_window(window_title: str) -> Dict[str, Any]:
    """
    Bring an allowed target application window to the foreground.
    
    Args:
        window_title: Title or owning process name of target window.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("focus_window", {"window_title": window_title}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("focus_window", {"window_title": window_title}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        msg = f"Target window '{window_title}' not found or not visible"
        guard.log_audit("focus_window", {"window_title": window_title}, "error", msg)
        return {"error": msg}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        guard.log_audit("focus_window", {"window_title": window_title, "process": proc_name}, "denied", reason)
        return {"error": f"Security rejection: Cannot focus window ({reason})"}

    _focus_hwnd(hwnd)
    guard.log_audit("focus_window", {"window_title": window_title, "process": proc_name, "hwnd": hwnd}, "success")
    return {"status": "success", "window_title": win.window_text(), "process": proc_name, "hwnd": hwnd}


@mcp.tool()
def mouse_click(
    window_title: str,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    scaled: bool = True
) -> Dict[str, Any]:
    """
    Click at (x, y) inside an allowed application window.
    SECURITY POLICY (Boundary & Scope Enforcement):
    - Rejects clicks if target window belongs to a blocked/non-allowed app.
    - Clamps coordinates strictly within window boundaries (cannot click outside or on other apps).
    - Verifies via WindowFromPoint that the target point is not obscured by another window.
    
    Args:
        window_title: Title or process name of target allowed window.
        x: X-coordinate (within window screenshot).
        y: Y-coordinate (within window screenshot).
        button: Mouse button ('left', 'right', 'middle'). Default 'left'.
        clicks: Number of clicks (1 for single, 2 for double click).
        scaled: True if (x, y) comes from the 1280px downscaled screenshot (default True).
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("mouse_click", {"window_title": window_title, "x": x, "y": y}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("mouse_click", {"window_title": window_title, "x": x, "y": y}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    btn = button.lower()
    if btn not in ("left", "right", "middle"):
        return {"error": f"Invalid mouse button: '{button}'"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        msg = f"Target window '{window_title}' not found or not visible"
        guard.log_audit("mouse_click", {"window_title": window_title}, "error", msg)
        return {"error": msg}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        guard.log_audit("mouse_click", {"window_title": window_title, "process": proc_name}, "denied", reason)
        return {"error": f"Security rejection: Cannot click window ({reason})"}

    longest = max(rect["width"], rect["height"])
    scale_factor = round(1280.0 / float(longest), 6) if longest > 1280 else 1.0

    valid_coords, coord_err, abs_x, abs_y = guard.validate_click_coordinates(
        rect, x, y, scaled=scaled, scale_factor=scale_factor
    )
    if not valid_coords:
        guard.log_audit("mouse_click", {"window_title": window_title, "x": x, "y": y}, "denied", coord_err)
        return {"error": f"Security rejection: {coord_err}"}

    _focus_hwnd(hwnd)

    point_ok, point_err = guard.verify_point_in_target_window(hwnd, abs_x, abs_y)
    if not point_ok:
        guard.log_audit("mouse_click", {"window_title": window_title, "abs_x": abs_x, "abs_y": abs_y}, "denied", point_err)
        return {"error": f"Security rejection: {point_err}"}

    try:
        pyautogui.click(x=abs_x, y=abs_y, button=btn, clicks=clicks, interval=0.1)
        guard.log_audit(
            "mouse_click",
            {"window_title": window_title, "x": x, "y": y, "abs_x": abs_x, "abs_y": abs_y, "button": btn, "clicks": clicks},
            "success"
        )
        return {"status": "success", "abs_x": abs_x, "abs_y": abs_y, "button": btn, "clicks": clicks}
    except Exception as e:
        guard.log_audit("mouse_click", {"window_title": window_title, "x": x, "y": y}, "error", str(e))
        return {"error": f"Mouse click failed: {e}"}


@mcp.tool()
def mouse_move(
    window_title: str,
    x: int,
    y: int,
    scaled: bool = True
) -> Dict[str, Any]:
    """
    Move the mouse cursor to (x, y) inside an allowed application window.
    
    Args:
        window_title: Title or process name of target allowed window.
        x: X-coordinate.
        y: Y-coordinate.
        scaled: True if coordinates match downscaled screenshot (default True).
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        return {"error": f"Security rejection: {reason}"}

    longest = max(rect["width"], rect["height"])
    scale_factor = round(1280.0 / float(longest), 6) if longest > 1280 else 1.0

    valid_coords, coord_err, abs_x, abs_y = guard.validate_click_coordinates(
        rect, x, y, scaled=scaled, scale_factor=scale_factor
    )
    if not valid_coords:
        return {"error": f"Security rejection: {coord_err}"}

    _focus_hwnd(hwnd)
    pyautogui.moveTo(abs_x, abs_y, duration=0.1)
    guard.log_audit("mouse_move", {"window_title": window_title, "abs_x": abs_x, "abs_y": abs_y}, "success")
    return {"status": "success", "abs_x": abs_x, "abs_y": abs_y}


@mcp.tool()
def mouse_drag(
    window_title: str,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    scaled: bool = True
) -> Dict[str, Any]:
    """
    Click and drag from (start_x, start_y) to (end_x, end_y) inside an allowed window.
    Both start and end points must remain within target window boundaries.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        return {"error": f"Security rejection: {reason}"}

    longest = max(rect["width"], rect["height"])
    scale_factor = round(1280.0 / float(longest), 6) if longest > 1280 else 1.0

    v1, e1, s_abs_x, s_abs_y = guard.validate_click_coordinates(rect, start_x, start_y, scaled=scaled, scale_factor=scale_factor)
    if not v1:
        return {"error": f"Security rejection (start point): {e1}"}

    v2, e2, e_abs_x, e_abs_y = guard.validate_click_coordinates(rect, end_x, end_y, scaled=scaled, scale_factor=scale_factor)
    if not v2:
        return {"error": f"Security rejection (end point): {e2}"}

    _focus_hwnd(hwnd)
    pyautogui.moveTo(s_abs_x, s_abs_y)
    pyautogui.dragTo(e_abs_x, e_abs_y, button=button.lower(), duration=0.3)
    guard.log_audit("mouse_drag", {"window_title": window_title, "start": [s_abs_x, s_abs_y], "end": [e_abs_x, e_abs_y]}, "success")
    return {"status": "success", "start": [s_abs_x, s_abs_y], "end": [e_abs_x, e_abs_y]}


@mcp.tool()
def mouse_scroll(
    window_title: str,
    x: int,
    y: int,
    clicks: int,
    scaled: bool = True
) -> Dict[str, Any]:
    """
    Scroll vertical mouse wheel at (x, y) inside an allowed window.
    Positive clicks scroll UP, negative clicks scroll DOWN.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        return {"error": f"Security rejection: {reason}"}

    longest = max(rect["width"], rect["height"])
    scale_factor = round(1280.0 / float(longest), 6) if longest > 1280 else 1.0

    valid_coords, coord_err, abs_x, abs_y = guard.validate_click_coordinates(
        rect, x, y, scaled=scaled, scale_factor=scale_factor
    )
    if not valid_coords:
        return {"error": f"Security rejection: {coord_err}"}

    _focus_hwnd(hwnd)
    pyautogui.moveTo(abs_x, abs_y)
    pyautogui.scroll(clicks)
    guard.log_audit("mouse_scroll", {"window_title": window_title, "abs_x": abs_x, "abs_y": abs_y, "clicks": clicks}, "success")
    return {"status": "success", "abs_x": abs_x, "abs_y": abs_y, "clicks": clicks}


@mcp.tool()
def type_text(window_title: str, text: str) -> Dict[str, Any]:
    """
    Type text into the currently focused control of an allowed application window.
    SECURITY POLICY (Password Shielding):
    - Refuses to type if target window is not an allowed application.
    - Inspects active focused control; if it is a password field (is_password_control),
      typing is strictly blocked to prevent credential exposure.
    - Audit logs are automatically sanitized to prevent secret leaks.
    
    Args:
        window_title: Title or process name of target window.
        text: Text string to type.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("type_text", {"window_title": window_title}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("type_text", {"window_title": window_title}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        msg = f"Target window '{window_title}' not found or not visible"
        guard.log_audit("type_text", {"window_title": window_title}, "error", msg)
        return {"error": msg}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        guard.log_audit("type_text", {"window_title": window_title, "process": proc_name}, "denied", reason)
        return {"error": f"Security rejection: Cannot type into window ({reason})"}

    _focus_hwnd(hwnd)

    try:
        focused = win.get_focus()
        if focused and guard.is_password_control(focused):
            msg = "Security rejection: Active focused element is a password field. Typing is strictly blocked."
            guard.log_audit("type_text", {"window_title": window_title}, "denied", msg)
            return {"error": msg}
    except Exception:
        pass

    try:
        pyautogui.write(text, interval=0.01)
        guard.log_audit(
            "type_text",
            {"window_title": window_title, "chars_count": len(text)},
            "success"
        )
        return {"status": "success", "typed_characters": len(text)}
    except Exception as e:
        guard.log_audit("type_text", {"window_title": window_title}, "error", str(e))
        return {"error": f"Type text error: {e}"}


@mcp.tool()
def press_key(window_title: str, key: str) -> Dict[str, Any]:
    """
    Press a single key (e.g. 'enter', 'tab', 'esc', 'backspace', 'delete', 'space', 'up', 'down').
    SECURITY POLICY: Rejects forbidden system keys (e.g. Windows key).
    
    Args:
        window_title: Title or process name of target allowed window.
        key: Key name (e.g. 'enter', 'tab', 'escape').
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    clean_key = (key or "").strip().lower()
    if clean_key in guard.FORBIDDEN_KEY_NAMES:
        return {"error": f"Security rejection: Key '{key}' is forbidden"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        return {"error": f"Security rejection: {reason}"}

    _focus_hwnd(hwnd)
    pyautogui.press(clean_key)
    guard.log_audit("press_key", {"window_title": window_title, "key": clean_key}, "success")
    return {"status": "success", "key": clean_key}


@mcp.tool()
def hotkey(window_title: str, keys: str) -> Dict[str, Any]:
    """
    Trigger a safe key combination (e.g. 'ctrl+c', 'ctrl+v', 'ctrl+a', 'ctrl+s', 'ctrl+z').
    SECURITY POLICY: Strictly forbids Windows key combinations (Win+R, etc.),
    system interrupt shortcuts (Ctrl+Alt+Del, Alt+F4, Ctrl+Shift+Esc),
    and simulating the emergency kill switch.
    
    Args:
        window_title: Title or process name of target allowed window.
        keys: Key combination joined by '+' (e.g. 'ctrl+a', 'ctrl+v').
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("hotkey", {"window_title": window_title, "keys": keys}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("hotkey", {"window_title": window_title, "keys": keys}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    ok, reason, key_list = guard.validate_hotkey(keys)
    if not ok:
        guard.log_audit("hotkey", {"window_title": window_title, "keys": keys}, "denied", reason)
        return {"error": f"Security rejection: {reason}"}

    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        msg = f"Target window '{window_title}' not found or not visible"
        guard.log_audit("hotkey", {"window_title": window_title, "keys": keys}, "error", msg)
        return {"error": msg}

    allowed, reason = guard.is_app_allowed(proc_name)
    if not allowed:
        guard.log_audit("hotkey", {"window_title": window_title, "process": proc_name}, "denied", reason)
        return {"error": f"Security rejection: Cannot send hotkey ({reason})"}

    _focus_hwnd(hwnd)
    pyautogui.hotkey(*key_list)
    guard.log_audit("hotkey", {"window_title": window_title, "keys": key_list}, "success")
    return {"status": "success", "hotkey": "+".join(key_list)}


if __name__ == "__main__":
    mcp.run()

