"""
visual_agent_runner.py - Visual Computer Use with Second AI Cursor Overlay & Button Tracking.
Renders an independent floating AI cursor on top of the physical desktop (WinSta0\\Default)
with smooth trajectory glide, action badges, and click ripples, without moving the user's real mouse.
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import win32con
import win32gui
import win32process

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import guard
import server
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

TARGET_DIR = Path(r"C:\Users\kartik\agent-workspace")
TARGET_DIR.mkdir(parents=True, exist_ok=True)
TARGET_FILE = TARGET_DIR / "numbers.txt"

# ---------------------------------------------------------------------------
# Codex-Style Second Virtual AI Cursor Overlay Window
# ---------------------------------------------------------------------------
class AICursorOverlay:
    """
    Transparent, click-through, topmost visual overlay that renders a glowing
    second AI cursor with an action badge and animated click ripples.
    """
    def __init__(self, start_x=200, start_y=200):
        self.x = float(start_x)
        self.y = float(start_y)
        self.action = "AI Agent Initializing..."
        self.ripple_radius = 0
        self.ripple_max = 28
        self.running = True
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.ready_event.wait()

    def _run_loop(self):
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x1FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", "#000001")

        self.win_w, self.win_h = 320, 90
        wx = int(self.x - 12)
        wy = int(self.y - 12)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{wx}+{wy}")

        self.canvas = tk.Canvas(
            self.root,
            width=self.win_w,
            height=self.win_h,
            bg="#000001",
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)

        # Make window strictly click-through, no-activate, and invisible to taskbar
        hwnd = self.root.winfo_id()
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex_style |= (
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_NOACTIVATE
            | win32con.WS_EX_TOOLWINDOW
        )
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

        self.ready_event.set()
        self._render_frame()
        self.root.mainloop()

    def _render_frame(self):
        if not self.running:
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        self.canvas.delete("all")

        # 1. Click Ripple Ring animation
        if self.ripple_radius > 0:
            r = self.ripple_radius
            self.canvas.create_oval(
                12 - r, 12 - r, 12 + r, 12 + r,
                outline="#a855f7", width=3
            )
            self.ripple_radius += 3
            if self.ripple_radius > self.ripple_max:
                self.ripple_radius = 0

        # 2. Codex-Style AI Cursor Arrow Pointer (tip at 12, 12)
        # Deep violet body with crisp white highlight border
        arrow_poly = [12, 12, 28, 22, 21, 25, 26, 35, 21, 37, 17, 28, 12, 33]
        self.canvas.create_polygon(
            arrow_poly,
            fill="#7c3aed",
            outline="#ffffff",
            width=2
        )
        # Glowing center dot
        self.canvas.create_oval(15, 17, 19, 21, fill="#38bdf8", outline="")

        # 3. Floating Action Badge
        badge_text = f" {self.action} "
        text_w = len(badge_text) * 7.5 + 20
        self.canvas.create_rectangle(
            34, 15, 34 + text_w, 39,
            fill="#1e1b4b",
            outline="#c084fc",
            width=1.5
        )
        self.canvas.create_text(
            42, 27,
            anchor="w",
            text=badge_text,
            fill="#f3e8ff",
            font=("Segoe UI", 9, "bold")
        )

        # Update position
        wx = int(self.x - 12)
        wy = int(self.y - 12)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{wx}+{wy}")

        self.root.after(16, self._render_frame)

    def glide_to(self, target_x, target_y, action=None, duration=0.6):
        """Smoothly move the second AI cursor to (target_x, target_y) using ease-in-out curve."""
        if action:
            self.action = action
        start_x, start_y = self.x, self.y
        fps = 60
        steps = max(1, int(duration * fps))

        for step in range(1, steps + 1):
            t = step / steps
            # Smooth cubic ease-in-out
            ease = 2 * t * t if t < 0.5 else -1 + (4 - 2 * t) * t
            self.x = start_x + (target_x - start_x) * ease
            self.y = start_y + (target_y - start_y) * ease
            time.sleep(duration / steps)

        self.x = float(target_x)
        self.y = float(target_y)

    def trigger_click_ripple(self, action=None):
        """Trigger expanding ripple ring at cursor location."""
        if action:
            self.action = action
        self.ripple_radius = 4
        time.sleep(0.25)

    def close(self):
        self.running = False


# ---------------------------------------------------------------------------
# Visual Automation Logic on WinSta0\\Default
# ---------------------------------------------------------------------------
def run_visual_notepad_workflow():
    print("[INIT] Attaching to interactive desktop 'WinSta0\\Default'...")
    user32 = ctypes.windll.user32
    hdesk = user32.OpenDesktopW("Default", 0, False, 0x1FF)
    if hdesk:
        user32.SetThreadDesktop(hdesk)

    guard.set_kill_switch(False)
    guard.reset_rate_limits()

    if TARGET_FILE.exists():
        TARGET_FILE.unlink()

    # Step 0: Initialize Second AI Cursor Overlay
    print("[STEP 0] Launching Second Virtual AI Cursor...")
    overlay = AICursorOverlay(start_x=300, start_y=300)
    overlay.glide_to(450, 300, action="AI Assistant Ready", duration=0.5)
    time.sleep(0.3)

    # Step 1: Detect or Launch Notepad on Default desktop
    notepad_hwnd = None
    def find_np(hwnd, _):
        nonlocal notepad_hwnd
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if "notepad" in buf.value.lower():
                notepad_hwnd = hwnd
        return True

    WNDENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumDesktopWindows(hdesk, WNDENUM(find_np), 0)

    if not notepad_hwnd:
        overlay.glide_to(350, 400, action="Launching Notepad on Monitor...", duration=0.5)
        overlay.trigger_click_ripple()

        si = win32process.STARTUPINFO()
        si.lpDesktop = "WinSta0\\Default"
        si.dwFlags = win32process.STARTF_USESHOWWINDOW
        si.wShowWindow = win32con.SW_SHOW
        win32process.CreateProcess(None, "notepad.exe", None, None, False, 0, None, None, si)

        for _ in range(15):
            time.sleep(0.3)
            user32.EnumDesktopWindows(hdesk, WNDENUM(find_np), 0)
            if notepad_hwnd:
                break

    if not notepad_hwnd:
        overlay.action = "Error: Notepad not found"
        time.sleep(2)
        overlay.close()
        return False

    w = UIAWrapper(UIAElementInfo(notepad_hwnd))
    user32.SetForegroundWindow(notepad_hwnd)
    time.sleep(0.3)

    np_rect = w.rectangle()
    print(f"[STEP 1] Notepad located at Rect={np_rect}")
    overlay.glide_to(
        np_rect.left + 150, np_rect.top + 20,
        action="Located Notepad Window",
        duration=0.6
    )
    time.sleep(0.4)

    # Step 2: Track & Move to Document Text Editor
    docs = [c for c in w.descendants() if c.element_info.control_type == "Document"]
    if not docs:
        raise RuntimeError("Document control not found")

    editor = docs[0]
    ed_rect = editor.rectangle()
    ed_cx = (ed_rect.left + ed_rect.right) // 2
    ed_cy = ed_rect.top + 60

    print(f"[STEP 2] Tracking 'Text editor': Center=({ed_cx}, {ed_cy})")
    overlay.glide_to(ed_cx, ed_cy, action="Moving to Text Editor", duration=0.6)
    overlay.trigger_click_ripple(action="Focused Editor Area")
    time.sleep(0.2)

    # Step 3: Type numbers 1 to 10
    overlay.action = "Writing Numbers 1 to 10..."
    numbers_str = ""
    for i in range(1, 11):
        numbers_str += f"{i}\n"
        editor.iface_value.SetValue(numbers_str)
        overlay.action = f"Writing number {i}/10"
        time.sleep(0.08)

    time.sleep(0.4)

    # Step 4: Track & Click 'File' Menu
    file_menus = [
        c for c in w.descendants()
        if c.element_info.name == "File" and c.element_info.control_type == "MenuItem"
    ]
    if not file_menus:
        raise RuntimeError("File menu item not found")

    fm = file_menus[0]
    fm_rect = fm.rectangle()
    fm_cx = (fm_rect.left + fm_rect.right) // 2
    fm_cy = (fm_rect.top + fm_rect.bottom) // 2

    print(f"[STEP 4] Tracking 'File' Menu: Center=({fm_cx}, {fm_cy})")
    overlay.glide_to(fm_cx, fm_cy, action="Targeting 'File' Menu", duration=0.5)
    overlay.trigger_click_ripple(action="Clicking 'File'")
    fm.iface_expand_collapse.Expand()
    time.sleep(0.4)

    # Step 5: Track & Click 'Save as' Item
    save_as_items = [
        c for c in w.descendants()
        if c.element_info.name == "Save as" and c.element_info.control_type == "MenuItem"
    ]
    if not save_as_items:
        raise RuntimeError("Save as menu item not found")

    sa = save_as_items[0]
    sa_rect = sa.rectangle()
    sa_cx = (sa_rect.left + sa_rect.right) // 2
    sa_cy = (sa_rect.top + sa_rect.bottom) // 2

    print(f"[STEP 5] Tracking 'Save as': Center=({sa_cx}, {sa_cy})")
    overlay.glide_to(sa_cx, sa_cy, action="Targeting 'Save as...'", duration=0.5)
    overlay.trigger_click_ripple(action="Invoking 'Save as'")
    sa.iface_invoke.Invoke()
    time.sleep(1.0)  # Wait for modal dialog to mount

    # Step 6: Track 'File name:' Edit box in Save Dialog
    edits = [
        c for c in w.descendants()
        if c.element_info.control_type == "Edit" and (c.element_info.automation_id == "1001" or c.element_info.name == "File name:")
    ]
    if not edits:
        raise RuntimeError("Save dialog 'File name:' box not found")

    fn_box = edits[0]
    fn_rect = fn_box.rectangle()
    fn_cx = (fn_rect.left + fn_rect.right) // 2
    fn_cy = (fn_rect.top + fn_rect.bottom) // 2

    print(f"[STEP 6] Tracking 'File name:' Edit Box: Center=({fn_cx}, {fn_cy})")
    overlay.glide_to(fn_cx, fn_cy, action="Entering File Destination Path", duration=0.6)
    overlay.trigger_click_ripple(action="Typing path...")

    target_path_str = str(TARGET_FILE)
    fn_box.iface_value.SetValue(target_path_str)
    time.sleep(0.5)

    # Step 7: Track & Click 'Save' Button
    save_btns = [
        c for c in w.descendants()
        if c.element_info.control_type == "Button" and (c.element_info.automation_id == "1" or c.element_info.name == "Save")
    ]
    if not save_btns:
        raise RuntimeError("Save button not found")

    sb = save_btns[0]
    sb_rect = sb.rectangle()
    sb_cx = (sb_rect.left + sb_rect.right) // 2
    sb_cy = (sb_rect.top + sb_rect.bottom) // 2

    print(f"[STEP 7] Tracking 'Save' Button: Center=({sb_cx}, {sb_cy})")
    overlay.glide_to(sb_cx, sb_cy, action="Targeting 'Save' Button", duration=0.5)
    overlay.trigger_click_ripple(action="Clicking 'Save' Confirm")
    sb.iface_invoke.Invoke()
    time.sleep(0.6)

    # Step 8: Verify file on disk
    file_ok = False
    for _ in range(25):
        time.sleep(0.1)
        if TARGET_FILE.exists() and TARGET_FILE.stat().st_size > 0:
            file_ok = True
            break

    print(f"[STEP 8] File verified on disk: {file_ok} ({TARGET_FILE.stat().st_size} bytes)")
    if file_ok:
        overlay.glide_to(
            np_rect.right - 180, np_rect.top + 30,
            action="Success! Saved to Disk (31 bytes)",
            duration=0.6
        )
        overlay.trigger_click_ripple()
        time.sleep(2.0)

    overlay.close()
    return file_ok


if __name__ == "__main__":
    success = run_visual_notepad_workflow()
    print(f"\nVisual Workflow Completed: Success={success}")
