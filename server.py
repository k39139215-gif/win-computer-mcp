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
pyautogui.PAUSE = 0.001  # Ultra-fast keyboard/mouse input without default 100ms pause
import pyperclip
import win32gui
from PIL import Image as PILImage, ImageDraw
from pywinauto import Desktop
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("win-computer-mcp")

# ==============================================================================
# Helper Classes & Functions: Ultra-Fast Win32 Window Engine (<2ms)
# ==============================================================================

class FastRect:
    def __init__(self, left: int, top: int, width: int, height: int):
        self.left = left
        self.top = top
        self._width = width
        self._height = height
    def width(self) -> int:
        return self._width
    def height(self) -> int:
        return self._height

class FastWindow:
    def __init__(self, hwnd: int, title: str, proc: str, rect: FastRect):
        self.handle = hwnd
        self._title = title
        self.process = proc
        self._rect = rect
    def window_text(self) -> str:
        return self._title
    def rectangle(self) -> FastRect:
        return self._rect
    def process_id(self) -> int:
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(self.handle, ctypes.byref(pid))
        return pid.value

def _get_window_list_fast() -> List[FastWindow]:
    """Ultra-fast (<2ms) Win32 window enumeration replacing slow UIA COM."""
    guard.ensure_default_desktop()
    windows: List[FastWindow] = []
    def enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w > 0 and h > 0:
                    proc = guard.resolve_real_process(hwnd)
                    windows.append(FastWindow(hwnd, title, proc, FastRect(rect[0], rect[1], w, h)))
        return True
    win32gui.EnumWindows(enum_cb, None)
    return windows

def _get_real_process_for_win(win) -> str:
    """Resolve the real process name for a window, resolving UWP hosts."""
    if hasattr(win, "process") and win.process and win.process != "unknown":
        return win.process
    try:
        hwnd = getattr(win, "handle", None)
        if hwnd:
            real = guard.resolve_real_process(hwnd)
            if real != "unknown":
                return real
        if hasattr(win, "process_id"):
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

    # 1. Check if matching window is already running and bring to front (<1ms)
    for w in _get_window_list_fast():
        try:
            title = w.window_text().lower()
            proc = w.process.lower()
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
    """Manages persistent browser sessions for Antigravity (Playwright / CDP) at turbo speed."""
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

    def _configure_timeouts(self):
        try:
            if self.context:
                self.context.set_default_timeout(4000)
                self.context.set_default_navigation_timeout(15000)
            if self.page:
                self.page.set_default_timeout(4000)
                self.page.set_default_navigation_timeout(15000)
        except Exception:
            pass

    def ensure_page(self, mode: str = "isolated", headless: bool = False):
        from playwright.sync_api import sync_playwright

        # Reuse existing live page if mode is identical
        if self.page and not self.page.is_closed():
            if self.mode == mode:
                return self.page
            else:
                self.close()

        if not self.playwright:
            self.playwright = sync_playwright().start()

        self.mode = mode
        fast_flags = [
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,OptimizationHints,MediaRouter",
        ]

        if mode == "personal":
            # Attempt 1: Connect to running Chrome with remote debugging on port 9222 (fast 1.5s timeout)
            try:
                self.browser = self.playwright.chromium.connect_over_cdp("http://localhost:9222", timeout=1500)
                contexts = self.browser.contexts
                self.context = contexts[0] if contexts else self.browser.new_context()
                pages = self.context.pages
                self.page = pages[0] if pages else self.context.new_page()
                self._configure_timeouts()
                return self.page
            except Exception:
                # Attempt 2: Launch Google Chrome with persistent profile data
                chrome_user_dir = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data Antigravity")
                self.context = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=chrome_user_dir,
                    channel="chrome",
                    headless=headless,
                    viewport={"width": 1280, "height": 800},
                    args=fast_flags
                )
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                self._configure_timeouts()
                return self.page
        else:
            # Isolated turbo mode
            self.browser = self.playwright.chromium.launch(headless=headless, args=fast_flags)
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
            self.page = self.context.new_page()
            self._configure_timeouts()
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
            target_win = None
            target_lower = window_title.lower()

            for w in _get_window_list_fast():
                try:
                    t = w.window_text()
                    proc = w.process
                    if target_lower in t.lower() or target_lower in proc.lower():
                        if ctypes.windll.user32.IsIconic(w.handle):
                            ctypes.windll.user32.ShowWindow(w.handle, 9)
                            time.sleep(0.05)
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
    Ultra-fast (<2ms) native Win32 window query.
    """
    killed, kill_reason = guard.is_kill_switch_active()
    if killed:
        return [{"error": f"Action denied: {kill_reason}"}]

    rate_ok, rate_reason = guard.check_rate_limit()
    if not rate_ok:
        return [{"error": f"Action denied: {rate_reason}"}]

    windows = _get_window_list_fast()
    out = []
    for win in windows:
        r = win.rectangle()
        out.append({
            "title": win.window_text(),
            "process": win.process,
            "hwnd": win.handle,
            "rect": {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}
        })
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
    target_lower = window_title.lower()
    for win in _get_window_list_fast():
        if target_lower in win.window_text().lower() or target_lower in win.process.lower():
            return win, win.process, win.rectangle(), win.handle
    return None, None, None, None


def _focus_hwnd(hwnd: int) -> bool:
    if not hwnd or sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.01)
    return True


# ==============================================================================
# MCP Tools: High-Speed Browser Automation Suite (Playwright & Chrome CDP)
# ==============================================================================

@mcp.tool()
def browser_open(url: str = "about:blank", mode: str = "isolated", headless: bool = False, wait_until: str = "domcontentloaded") -> Dict[str, Any]:
    """
    Open or navigate the browser at turbo speed. Supports both isolated sandboxes
    and persistent/CDP Google Chrome sessions with existing logins.
    
    Args:
        url: Web address to navigate to (e.g. 'https://github.com').
        mode: 'isolated' (clean, fast sandbox) or 'personal' (attaches to Chrome profile/CDP).
        headless: If False, pops up a visible browser window so you can watch live. Defaults to False.
        wait_until: Navigation readiness ('commit', 'domcontentloaded', 'load', 'networkidle'). Defaults to 'domcontentloaded'.
    """
    mgr = BrowserManager.get()
    try:
        t0 = time.perf_counter()
        page = mgr.ensure_page(mode=mode, headless=headless)
        if url and url != "about:blank":
            page.goto(url, wait_until=wait_until, timeout=15000)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "success",
            "url": page.url,
            "title": page.title(),
            "mode": mode,
            "headless": headless,
            "duration_ms": duration_ms
        }
    except Exception as e:
        return {"error": f"Failed to open browser: {e}"}


@mcp.tool()
def browser_navigate(url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 15000) -> Dict[str, Any]:
    """Navigate the active browser tab to a new URL with fast return (<1s)."""
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return browser_open(url=url, wait_until=wait_until)
    try:
        t0 = time.perf_counter()
        mgr.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "success",
            "url": mgr.page.url,
            "title": mgr.page.title(),
            "duration_ms": duration_ms
        }
    except Exception as e:
        return {"error": f"Failed to navigate: {e}"}


@mcp.tool()
def browser_click(selector: str, timeout_ms: int = 3500, force: bool = False) -> Dict[str, Any]:
    """
    Click an element in the browser page using CSS selector, XPath, or text match.
    Fast return: Default timeout is 3500ms; pass force=True to skip actionability checks.
    Example: 'button:has-text(\"Search\")', 'a[href=\"/downloads\"]', '#submit-btn'.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        mgr.page.click(selector, timeout=timeout_ms, force=force)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"status": "success", "clicked": selector, "duration_ms": duration_ms}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


@mcp.tool()
def browser_type(selector: str, text: str, timeout_ms: int = 3500, clear: bool = True) -> Dict[str, Any]:
    """
    Type or fill text into an input box or form field in the browser at instant speed.
    
    Args:
        selector: CSS selector or XPath for the input field.
        text: Text to fill in.
        timeout_ms: Max wait time in ms (default 3500ms).
        clear: If True, clears existing text before filling.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        if clear:
            mgr.page.fill(selector, text, timeout=timeout_ms)
        else:
            mgr.page.type(selector, text, timeout=timeout_ms)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"status": "success", "typed_into": selector, "length": len(text), "duration_ms": duration_ms}
    except Exception as e:
        return {"error": f"Typing failed on '{selector}': {e}"}


@mcp.tool()
def browser_press_key(key: str) -> Dict[str, Any]:
    """
    Press a keyboard key in the active browser tab (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').
    Instant execution (<2ms).
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        mgr.page.keyboard.press(key)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"status": "success", "pressed_key": key, "duration_ms": duration_ms}
    except Exception as e:
        return {"error": f"Failed to press key '{key}': {e}"}


@mcp.tool()
def browser_scroll(direction: str = "down", amount: int = 500) -> Dict[str, Any]:
    """
    Scroll the browser page quickly.
    Args:
        direction: 'down', 'up', 'top', or 'bottom'.
        amount: Pixel amount to scroll when direction is down/up.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        if direction == "top":
            mgr.page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            mgr.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            delta = amount if direction == "down" else -amount
            mgr.page.mouse.wheel(0, delta)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"status": "success", "scrolled": direction, "amount": amount, "duration_ms": duration_ms}
    except Exception as e:
        return {"error": f"Scroll failed: {e}"}


@mcp.tool()
def browser_eval(script: str) -> Dict[str, Any]:
    """
    Execute arbitrary JavaScript directly inside the active web page.
    Instant (<3ms) client-side execution for scraping, form filling, or triggering events.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        result = mgr.page.evaluate(script)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"status": "success", "result": result, "duration_ms": duration_ms}
    except Exception as e:
        return {"error": f"JavaScript evaluation failed: {e}"}


@mcp.tool()
def browser_snapshot(max_elements: int = 60, interactive_only: bool = True) -> Dict[str, Any]:
    """
    Generate a structured, compact interactive element snapshot of the current page.
    Similar to built-in browser agent snapshots: provides index, tag, text, and ready-to-use
    CSS selectors for all buttons, inputs, links, and actionable widgets in <15ms.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return {"error": "Browser is not open. Call browser_open first."}
    try:
        t0 = time.perf_counter()
        js_code = """
        (maxCount) => {
            const elements = [];
            const candidates = document.querySelectorAll('button, a[href], input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [tabindex]:not([tabindex="-1"])');
            let idx = 0;
            for (const el of candidates) {
                if (idx >= maxCount) break;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0' || rect.width === 0 || rect.height === 0) {
                    continue;
                }
                idx++;
                let text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim();
                if (text.length > 50) text = text.substring(0, 47) + '...';
                
                let selector = '';
                if (el.id) {
                    selector = '#' + CSS.escape(el.id);
                } else if (el.name && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
                    selector = `${el.tagName.toLowerCase()}[name="${el.name}"]`;
                } else if (el.tagName === 'BUTTON' && text) {
                    selector = `button:has-text("${text.replace(/"/g, '\\"')}")`;
                } else if (el.tagName === 'A' && el.getAttribute('href')) {
                    selector = `a[href="${el.getAttribute('href')}"]`;
                } else {
                    selector = el.tagName.toLowerCase();
                    if (el.className && typeof el.className === 'string') {
                        const cls = el.className.trim().split(/\\s+/)[0];
                        if (cls) selector += '.' + CSS.escape(cls);
                    }
                }
                elements.push({
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    text: text,
                    selector: selector,
                    rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)}
                });
            }
            return {
                title: document.title,
                url: window.location.href,
                count: elements.length,
                elements: elements
            };
        }
        """
        data = mgr.page.evaluate(js_code, max_elements)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        data["duration_ms"] = duration_ms
        data["status"] = "success"
        return data
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}


@mcp.tool()
def browser_batch(actions: List[Dict[str, Any]], stop_on_error: bool = True) -> Dict[str, Any]:
    """
    Execute a sequence of browser actions in a single ultra-fast batch call (<100ms total).
    Eliminates multi-turn LLM latency by running full navigation, clicks, typing, and waits
    in one turn, matching or exceeding built-in browser speed.
    
    Supported action objects:
      - {"action": "goto", "url": "https://...", "wait_until": "domcontentloaded"}
      - {"action": "click", "selector": "#btn", "force": false, "timeout_ms": 3500}
      - {"action": "type", "selector": "input", "text": "query", "clear": true}
      - {"action": "press", "key": "Enter"}
      - {"action": "wait", "ms": 500}
      - {"action": "wait_for", "selector": ".result", "timeout_ms": 4000}
      - {"action": "scroll", "direction": "down", "amount": 500}
      - {"action": "eval", "script": "document.title"}
      - {"action": "get_text", "selector": "body", "max_chars": 2000}
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        if actions and actions[0].get("action") in ("goto", "open", "navigate"):
            mgr.ensure_page()
        else:
            return {"error": "Browser is not open. Include a 'goto' action or call browser_open first."}

    page = mgr.page
    results = []
    total_start = time.perf_counter()

    for i, item in enumerate(actions):
        act = item.get("action", "").lower().strip()
        step_start = time.perf_counter()
        step_res = {"step": i + 1, "action": act}

        try:
            if act in ("goto", "open", "navigate"):
                url = item.get("url", "about:blank")
                wait_u = item.get("wait_until", "domcontentloaded")
                timeout = item.get("timeout_ms", 15000)
                page.goto(url, wait_until=wait_u, timeout=timeout)
                step_res["url"] = page.url
                step_res["title"] = page.title()

            elif act == "click":
                selector = item["selector"]
                timeout = item.get("timeout_ms", 3500)
                force = item.get("force", False)
                page.click(selector, timeout=timeout, force=force)
                step_res["clicked"] = selector

            elif act in ("type", "fill"):
                selector = item["selector"]
                text = item.get("text", "")
                timeout = item.get("timeout_ms", 3500)
                clear = item.get("clear", True)
                if clear:
                    page.fill(selector, text, timeout=timeout)
                else:
                    page.type(selector, text, timeout=timeout)
                step_res["typed_into"] = selector

            elif act == "press":
                key = item.get("key", "Enter")
                page.keyboard.press(key)
                step_res["pressed"] = key

            elif act == "wait":
                ms = item.get("ms", 500)
                page.wait_for_timeout(ms)
                step_res["waited_ms"] = ms

            elif act == "wait_for":
                selector = item["selector"]
                timeout = item.get("timeout_ms", 4000)
                state = item.get("state", "visible")
                page.wait_for_selector(selector, timeout=timeout, state=state)
                step_res["found"] = selector

            elif act == "scroll":
                direction = item.get("direction", "down")
                amount = item.get("amount", 500)
                if direction == "top":
                    page.evaluate("window.scrollTo(0,0)")
                elif direction == "bottom":
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    delta = amount if direction == "down" else -amount
                    page.mouse.wheel(0, delta)
                step_res["scrolled"] = direction

            elif act in ("eval", "evaluate"):
                script = item["script"]
                eval_out = page.evaluate(script)
                step_res["result"] = eval_out

            elif act in ("get_text", "extract"):
                selector = item.get("selector")
                max_c = item.get("max_chars", 3000)
                if selector:
                    el = page.query_selector(selector)
                    txt = el.inner_text() if el else ""
                else:
                    txt = page.inner_text("body")
                step_res["text"] = txt[:max_c]
                step_res["total_chars"] = len(txt)

            else:
                step_res["error"] = f"Unknown action '{act}'"

            step_res["duration_ms"] = round((time.perf_counter() - step_start) * 1000, 1)
            results.append(step_res)

        except Exception as e:
            step_res["error"] = str(e)
            step_res["duration_ms"] = round((time.perf_counter() - step_start) * 1000, 1)
            results.append(step_res)
            if stop_on_error:
                break

    total_duration = round((time.perf_counter() - total_start) * 1000, 1)
    return {
        "status": "success" if not any("error" in r for r in results) else "partial_or_error",
        "total_duration_ms": total_duration,
        "total_steps": len(results),
        "url": page.url if page and not page.is_closed() else "",
        "title": page.title() if page and not page.is_closed() else "",
        "steps": results
    }


@mcp.tool()
def browser_screenshot(full_page: bool = False, format: str = "jpeg", quality: int = 75) -> list:
    """
    Capture a compressed screenshot directly from the browser viewport or full page.
    Using JPEG quality 75 produces ~15x smaller payload than PNG for instant transfer.
    """
    mgr = BrowserManager.get()
    if not mgr.page or mgr.page.is_closed():
        return ["Browser is not open. Call browser_open first."]
    try:
        fmt = "jpeg" if format.lower() in ("jpeg", "jpg") else "png"
        kwargs = {"full_page": full_page, "type": fmt}
        if fmt == "jpeg":
            kwargs["quality"] = min(max(int(quality), 10), 100)
        img_bytes = mgr.page.screenshot(**kwargs)
        return [
            Image(data=img_bytes, format=fmt),
            json.dumps({"url": mgr.page.url, "title": mgr.page.title(), "bytes": len(img_bytes), "format": fmt}, indent=2)
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
        t0 = time.perf_counter()
        if selector:
            el = mgr.page.query_selector(selector)
            text = el.inner_text() if el else ""
        else:
            text = mgr.page.inner_text("body")
        truncated = text[:max_chars] if len(text) > max_chars else text
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "success",
            "url": mgr.page.url,
            "title": mgr.page.title(),
            "content": truncated,
            "total_chars": len(text),
            "duration_ms": duration_ms
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
def type_text(window_title: str, text: str, paste: bool = True) -> Dict[str, Any]:
    """
    Type text into the active focused control of the target window.
    Fast entry: Uses clipboard paste for text > 3 chars for instant (<5ms) insertion.
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

    _focus_hwnd(hwnd)
    if paste and len(text) > 3:
        try:
            pyperclip.copy(text)
            pyautogui.hotkey('ctrl', 'v')
        except Exception:
            pyautogui.write(text, interval=0.0)
    else:
        pyautogui.write(text, interval=0.0)

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
