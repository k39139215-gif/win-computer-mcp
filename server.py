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


if __name__ == "__main__":
    mcp.run()
