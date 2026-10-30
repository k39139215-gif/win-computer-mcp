"""
server.py - Advanced Windows Computer Use & Browser MCP Server for Antigravity.

Features:
1. Occlusion-Proof DWM Window Capture: Direct GPU composition buffer capture via PrintWindow
   (PW_RENDERFULLCONTENT), preventing other windows (like Antigravity) from spoiling screenshots.
2. 40ms Start Menu App Resolver: Instant app launching via Windows Start Menu shortcuts and ShellExecute,
   eliminating slow hard-drive recursive scans.
3. Unified Browser Suite: Playwright-powered browser automation supporting both isolated fast sandboxes
   and persistent/CDP Chrome sessions, with persistent Chrome Extension page support.
4. Comprehensive Desktop OS Control: Full mouse, keyboard, window focus, control tree, and audit guardrails.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import guard  # Must be imported first to ensure desktop attachment before any COM/UI handles

import psutil
import pyautogui
from PIL import Image as PILImage, ImageDraw
from pywinauto import Desktop
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("win-computer-mcp")

# ==============================================================================
# Helper Functions: Window Resolution & Desktop Helpers
# ==============================================================================

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


def _capture_window_dwm(hwnd: int) -> Optional[PILImage.Image]:
    """
    Capture a window directly from the Desktop Window Manager (DWM) composition buffer
    using user32.PrintWindow with PW_RENDERFULLCONTENT (0x00000002).
    This captures the window even if other windows (such as Antigravity) are covering it.
    """
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None

        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        save_bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mfc_dc, save_bitmap)

        # PW_RENDERFULLCONTENT = 2
        # Instructs DWM to render the full window content into our device context
        result = user32.PrintWindow(hwnd, mfc_dc, 2)

        bmpinfo = wintypes.BITMAPINFO()
        bmpinfo.bmiHeader.biSize = ctypes.sizeof(wintypes.BITMAPINFOHEADER)
        bmpinfo.bmiHeader.biWidth = w
        bmpinfo.bmiHeader.biHeight = -h  # Negative for top-down bitmap
        bmpinfo.bmiHeader.biPlanes = 1
        bmpinfo.bmiHeader.biBitCount = 32
        bmpinfo.bmiHeader.biCompression = 0

        buf = bytearray(w * h * 4)
        gdi32.GetDIBits(mfc_dc, save_bitmap, 0, h, (ctypes.c_char * len(buf)).from_buffer(buf), ctypes.byref(bmpinfo), 0)

        # Clean up GDI handles
        gdi32.DeleteObject(save_bitmap)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        if result:
            img = PILImage.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1)
            return img.convert('RGB')
    except Exception:
        pass
    return None


# ==============================================================================
# Helper Functions: Fast Start Menu Indexer (40ms App Resolution)
# ==============================================================================

_APP_INDEX_CACHE: Dict[str, str] = {}
_APP_INDEX_TIMESTAMP: float = 0.0

def _get_start_menu_apps() -> Dict[str, str]:
    """Index all Start Menu shortcuts across System and User profiles in < 50ms."""
    global _APP_INDEX_CACHE, _APP_INDEX_TIMESTAMP
    now = time.time()
    if _APP_INDEX_CACHE and (now - _APP_INDEX_TIMESTAMP < 300):
        return _APP_INDEX_CACHE

    apps = {}
    search_dirs = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".lnk"):
                    clean_name = f[:-4].lower()
                    apps[clean_name] = os.path.join(root, f)

    _APP_INDEX_CACHE = apps
    _APP_INDEX_TIMESTAMP = now
    return apps


def _resolve_and_launch_app(app_name: str) -> Tuple[bool, str, int]:
    """
    Instantly resolve an app name against running windows or Start Menu shortcuts.
    Avoids slow directory crawling. Returns (success, message, pid).
    """
    clean = (app_name or "").strip().lower()
    guard.ensure_default_desktop()

    # 1. Check if matching window is already running and bring to front
    d = Desktop(backend="uia")
    for w in d.windows():
        try:
            if not w.is_visible():
                continue
            title = w.window_text().lower()
            proc = _get_real_process_for_win(w).lower()
            if clean in title or clean in proc:
                _focus_hwnd(w.handle)
                return True, f"Window '{w.window_text()}' already running and brought to front", w.process_id()
        except Exception:
            continue

    # 2. Check in-memory Start Menu index (<1ms memory lookup)
    apps = _get_start_menu_apps()
    matched_target = None
    if clean in apps:
        matched_target = apps[clean]
    else:
        candidates = [k for k in apps if clean in k]
        if candidates:
            candidates.sort(key=len)
            matched_target = apps[candidates[0]]

    if matched_target:
        try:
            os.startfile(matched_target)
            return True, f"Launched '{app_name}' via shortcut: {matched_target}", 0
        except Exception:
            pass

    # 3. Fallback to ShellExecute (e.g. calc, notepad, mspaint, explorer)
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(None, "open", clean, None, None, 1)
        if ret > 32:
            return True, f"Launched '{app_name}' via ShellExecute", 0
    except Exception:
        pass

    # 4. Fallback to direct executable spawn
    try:
        exe_name = clean if clean.endswith(".exe") else f"{clean}.exe"
        proc = subprocess.Popen([exe_name], shell=False)
        return True, f"Launched '{exe_name}' directly", proc.pid
    except Exception as e:
        return False, f"Could not launch '{app_name}': {e}", 0


# ==============================================================================
# Helper Functions: Playwright Browser Manager (Dual Mode: Personal & Isolated)
# ==============================================================================

class BrowserManager:
    """Manages persistent browser sessions for Antigravity (Playwright / CDP)."""
    _instance: Optional[BrowserManager] = None

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.mode = "isolated"

    @classmethod
    def get(cls) -> BrowserManager:
        if cls._instance is None:
            cls._instance = BrowserManager()
        return cls._instance

    def ensure_page(self, mode: str = "isolated", headless: bool = False):
        from playwright.sync_api import sync_playwright

        if self.page and not self.page.is_closed():
            return self.page

        if not self.playwright:
            self.playwright = sync_playwright().start()

        self.mode = mode
        if mode == "personal":
            # Attempt 1: Connect to running Chrome with remote debugging on port 9222
            try:
                self.browser = self.playwright.chromium.connect_over_cdp("http://localhost:9222")
                contexts = self.browser.contexts
                self.context = contexts[0] if contexts else self.browser.new_context()
                pages = self.context.pages
                self.page = pages[0] if pages else self.context.new_page()
                return self.page
            except Exception:
                # Attempt 2: Launch Google Chrome with persistent profile data
                chrome_user_dir = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data Antigravity")
                self.context = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=chrome_user_dir,
                    channel="chrome",
                    headless=headless,
                    viewport={"width": 1280, "height": 800}
                )
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                return self.page
        else:
            # Isolated turbo mode
            self.browser = self.playwright.chromium.launch(headless=headless)
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
            self.page = self.context.new_page()
            return self.page

    def close(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None


# ==============================================================================
# MCP Tools: Screenshot & Visual Capture
# ==============================================================================

@mcp.tool()
def screenshot(window_title: Optional[str] = None) -> list:
    """
    Capture the screen of a target application window or the full desktop.
    Uses Direct DWM Capture to render the target window even if it is covered by
    other windows (e.g. Antigravity), avoiding occlusion and blackouts.
    
    Args:
        window_title: Title or process name of window to capture (e.g. 'Chrome', 'Paper', 'Notepad').
                      Pass None, 'desktop', or 'full' to capture the entire desktop display.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("screenshot", {"window_title": window_title}, "denied", kill_reason)
        return [f"Action denied: {kill_reason}"]

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("screenshot", {"window_title": window_title}, "denied", rate_reason)
        return [f"Action denied: {rate_reason}"]

    guard.ensure_default_desktop()

    try:
        import mss
        sct_factory = getattr(mss, "MSS", mss.mss)

        # Mode A: Full Desktop Capture (Explicitly requested or window_title is None/desktop)
        if not window_title or window_title.lower() in ("desktop", "full", "all", "screen"):
            with sct_factory() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(monitor)
                img = PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                proc_name = "desktop"
                target_rect = {"left": monitor["left"], "top": monitor["top"], "width": monitor["width"], "height": monitor["height"]}

        # Mode B: Specific Window Capture (DWM Direct Compositor First)
        else:
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

            # 1. Try DWM direct window capture (clean, occlusion-proof)
            img = _capture_window_dwm(target_win.handle)

            # 2. Fallback to mss.grab if DWM capture returned None
            if img is None:
                _focus_hwnd(target_win.handle)
                time.sleep(0.1)
                with sct_factory() as sct:
                    shot = sct.grab(target_rect)
                    img = PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # Downscale if longest dimension > 1280 (Fast inference, optimal token efficiency)
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
            new_w, new_h = orig_w, orig_h

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
            "window_title": window_title or "desktop",
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


# ==============================================================================
# MCP Tools: Application Management & Window Control
# ==============================================================================

@mcp.tool()
def launch_app(app_name: str) -> Dict[str, Any]:
    """
    Launch or switch to any Windows application instantly (e.g. 'paper', 'chrome', 'spotify', 'notepad').
    Uses pre-indexed Start Menu shortcuts (<40ms) and window focus, eliminating slow hard-drive scans.
    
    Args:
        app_name: Name of the application to launch or bring forward (e.g. 'paper', 'chrome', 'notepad').
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        guard.log_audit("launch_app", {"app_name": app_name}, "denied", kill_reason)
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        guard.log_audit("launch_app", {"app_name": app_name}, "denied", rate_reason)
        return {"error": f"Action denied: {rate_reason}"}

    success, msg, pid = _resolve_and_launch_app(app_name)
    if success:
        guard.log_audit("launch_app", {"app_name": app_name, "pid": pid}, "success")
        return {"status": "success", "message": msg, "pid": pid}
    else:
        guard.log_audit("launch_app", {"app_name": app_name}, "error", msg)
        return {"error": msg}


@mcp.tool()
def list_windows() -> List[Dict[str, Any]]:
    """
    List visible top-level windows with title, process name, and bounding rect.
    Fast (<15ms) in-memory window query.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return [{"error": f"Action denied: {kill_reason}"}]

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return [{"error": f"Action denied: {rate_reason}"}]

    guard.ensure_default_desktop()
    d = Desktop(backend="uia")
    out = []

    for w in d.windows():
        try:
            if not w.is_visible():
                continue
            t = w.window_text()
            if not t.strip():
                continue
            proc = _get_real_process_for_win(w)
            r = w.rectangle()
            if r.width() <= 0 or r.height() <= 0:
                continue

            out.append({
                "title": t,
                "process": proc,
                "hwnd": w.handle,
                "rect": {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}
            })
        except Exception:
            continue

    return out[:30]


@mcp.tool()
def focus_window(window_title: str) -> Dict[str, Any]:
    """
    Bring a specific window to the foreground and direct keyboard focus to it.
    
    Args:
        window_title: Substring of the title or process name to focus.
    """
    guard.ensure_default_desktop()
    win, proc, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Window '{window_title}' not found"}

    _focus_hwnd(hwnd)
    return {"status": "success", "focused": win.window_text(), "hwnd": hwnd}


def _find_target_window(window_title: str):
    d = Desktop(backend="uia")
    target_lower = window_title.lower()
    for w in d.windows():
        try:
            if not w.is_visible():
                continue
            t = w.window_text()
            proc = _get_real_process_for_win(w)
            if target_lower in t.lower() or target_lower in proc.lower():
                r = w.rectangle()
                return w, proc, r, w.handle
        except Exception:
            continue
    return None, None, None, None


def _focus_hwnd(hwnd: int) -> bool:
    if not hwnd or sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return True


# ==============================================================================
# MCP Tools: Browser Automation Suite (Playwright & Chrome Extensions)
# ==============================================================================

@mcp.tool()
def browser_open(url: str = "about:blank", mode: str = "isolated", headless: bool = False) -> Dict[str, Any]:
    """
    Open or navigate the browser. Supports both isolated turbo sandboxes and
    persistent/CDP Google Chrome sessions with existing logins.
    
    Args:
        url: Web address to navigate to (e.g. 'https://paper.design', 'https://github.com').
        mode: 'isolated' (clean, disposable sandbox) or 'personal' (attaches to Chrome profile/CDP).
        headless: If False, pops up a visible browser window so you can watch live. Defaults to False.
    """
    mgr = BrowserManager.get()
    try:
        page = mgr.ensure_page(mode=mode, headless=headless)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {
            "status": "success",
            "url": page.url,
            "title": page.title(),
            "mode": mode,
            "headless": headless
        }
    except Exception as e:
        return {"error": f"Failed to open browser: {e}"}


@mcp.tool()
def browser_navigate(url: str) -> Dict[str, Any]:
    """Navigate the active browser tab to a new URL."""
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return browser_open(url=url)
    try:
        mgr.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"status": "success", "url": mgr.page.url, "title": mgr.page.title()}
    except Exception as e:
        return {"error": f"Failed to navigate: {e}"}


@mcp.tool()
def browser_click(selector: str, timeout_ms: int = 8000) -> Dict[str, Any]:
    """
    Click an element in the browser page using CSS selector, XPath, or text match.
    Example: 'button:has-text(\"Scan\")', 'a[href=\"/downloads\"]', '#submit-btn'.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        mgr.page.click(selector, timeout=timeout_ms)
        return {"status": "success", "clicked": selector}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


@mcp.tool()
def browser_type(selector: str, text: str, timeout_ms: int = 8000) -> Dict[str, Any]:
    """
    Type text into an input box or form field in the browser.
    
    Args:
        selector: CSS selector or XPath for the input field.
        text: Text to fill in.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        mgr.page.fill(selector, text, timeout=timeout_ms)
        return {"status": "success", "typed_into": selector, "text": text}
    except Exception as e:
        return {"error": f"Typing failed on '{selector}': {e}"}


@mcp.tool()
def browser_screenshot(full_page: bool = False) -> list:
    """Capture a clean screenshot directly from the browser viewport or full page."""
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return ["Browser is not open. Call browser_open first."]
    try:
        img_bytes = mgr.page.screenshot(full_page=full_page)
        return [
            Image(data=img_bytes, format="png"),
            json.dumps({"url": mgr.page.url, "title": mgr.page.title(), "full_page": full_page}, indent=2)
        ]
    except Exception as e:
        return [f"Browser screenshot error: {e}"]


@mcp.tool()
def browser_get_content(selector: Optional[str] = None, max_chars: int = 5000) -> Dict[str, Any]:
    """
    Extract readable text content from the browser page or a specific container.
    Much faster than taking screenshots when reading articles, documentation, or tables.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        if selector:
            el = mgr.page.query_selector(selector)
            text = el.inner_text() if el else ""
        else:
            text = mgr.page.inner_text("body")
        truncated = text[:max_chars] if len(text) > max_chars else text
        return {
            "status": "success",
            "url": mgr.page.url,
            "title": mgr.page.title(),
            "content": truncated,
            "total_chars": len(text)
        }
    except Exception as e:
        return {"error": f"Failed to extract content: {e}"}


@mcp.tool()
def browser_open_extension(extension_id: str, page_name: str = "popup.html") -> Dict[str, Any]:
    """
    Open a Chrome extension popup in a dedicated, persistent browser tab.
    Prevents ephemeral extension bubbles from closing or disappearing when focus shifts.
    
    Args:
        extension_id: The Chrome extension ID (from chrome://extensions).
        page_name: Extension entry point, typically 'popup.html' or 'index.html'.
    """
    url = f"chrome-extension://{extension_id}/{page_name}"
    return browser_open(url=url, mode="personal", headless=False)


@mcp.tool()
def browser_close() -> Dict[str, Any]:
    """Close the active browser session and release memory."""
    BrowserManager.get().close()
    return {"status": "success", "message": "Browser session closed"}


# ==============================================================================
# MCP Tools: Native Mouse & Keyboard Interaction
# ==============================================================================

@mcp.tool()
def mouse_click(
    window_title: str,
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
) -> Dict[str, Any]:
    """Click at coordinates (x, y) relative to the specified window's top-left corner."""
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

    _focus_hwnd(hwnd)
    abs_x = rect.left + (x if x is not None else rect.width() // 2)
    abs_y = rect.top + (y if y is not None else rect.height() // 2)

    pyautogui.click(x=abs_x, y=abs_y, button=button, clicks=clicks)
    guard.log_audit("mouse_click", {"window_title": window_title, "x": abs_x, "y": abs_y, "button": button}, "success")
    return {"status": "success", "clicked_at": {"x": abs_x, "y": abs_y}, "window": win.window_text()}


@mcp.tool()
def type_text(window_title: str, text: str) -> Dict[str, Any]:
    """Type text into the active focused control of the target window."""
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

    _focus_hwnd(hwnd)
    pyautogui.write(text, interval=0.01)
    guard.log_audit("type_text", {"window_title": window_title, "length": len(text)}, "success")
    return {"status": "success", "typed_length": len(text)}


@mcp.tool()
def press_key(window_title: str, key: str) -> Dict[str, Any]:
    """Press a single key (e.g. 'enter', 'tab', 'esc', 'backspace', 'down')."""
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    clean_key = (key or "").strip().lower()
    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    _focus_hwnd(hwnd)
    pyautogui.press(clean_key)
    guard.log_audit("press_key", {"window_title": window_title, "key": clean_key}, "success")
    return {"status": "success", "key": clean_key}


@mcp.tool()
def hotkey(window_title: str, keys: str) -> Dict[str, Any]:
    """Trigger a key combination (e.g. 'ctrl+c', 'ctrl+v', 'ctrl+a', 'ctrl+s')."""
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return {"error": f"Action denied: {kill_reason}"}

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return {"error": f"Action denied: {rate_reason}"}

    key_list = [k.strip().lower() for k in keys.split("+")]
    win, proc_name, rect, hwnd = _find_target_window(window_title)
    if not win:
        return {"error": f"Target window '{window_title}' not found"}

    _focus_hwnd(hwnd)
    pyautogui.hotkey(*key_list)
    guard.log_audit("hotkey", {"window_title": window_title, "keys": key_list}, "success")
    return {"status": "success", "hotkey": "+".join(key_list)}


if __name__ == "__main__":
    mcp.run()
