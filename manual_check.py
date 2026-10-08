"""
manual_check.py - Interactive manual verification script for Windows 11.
To be run directly by the user in their own terminal:
    python manual_check.py

Steps:
1. Registers hotkey Ctrl+Alt+Shift+K, waits 15s, prints FIRED or NOT FIRED.
2. Prompts native confirm dialog twice (Click NO, then Click YES), prints results.
3. Opens real Tkinter password Entry (show="*") and reports what UIA says about IsPassword.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import time
import tkinter as tk
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import guard
from pywinauto import Desktop

# Ensure thread is attached to interactive desktop
guard.ensure_default_desktop()
guard.init_dpi_awareness()


def run_hotkey_check() -> bool:
    print("\n" + "=" * 60)
    print("STEP 1: Global Hotkey Check (Ctrl+Alt+Shift+K)")
    print("=" * 60)
    print("Press Ctrl+Alt+Shift+K within 15 seconds...")

    # Clear STOP file before test
    if guard.STOP_FILE_PATH.exists():
        guard.STOP_FILE_PATH.unlink()
    guard.set_kill_switch(False)

    user32 = ctypes.windll.user32
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_K = ord("K")
    hotkey_id = 8888

    res = user32.RegisterHotKey(0, hotkey_id, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_K)
    if not res:
        print("Note: Hotkey already registered by background daemon or another process.")

    fired = False
    start_time = time.time()
    msg = wintypes.MSG()

    while time.time() - start_time < 15.0:
        # Check STOP file or message
        if guard.STOP_FILE_PATH.exists() or guard.is_kill_switch_active()[0]:
            fired = True
            break
        if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
            if msg.message == 0x0312:  # WM_HOTKEY
                fired = True
                guard.set_kill_switch(True)
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)

    if res:
        user32.UnregisterHotKey(0, hotkey_id)

    if fired:
        print("RESULT: FIRED")
        print(f"STOP file created: {guard.STOP_FILE_PATH.exists()}")
        # Clean up STOP file so subsequent steps can proceed
        if guard.STOP_FILE_PATH.exists():
            guard.STOP_FILE_PATH.unlink()
        guard.set_kill_switch(False)
        return True
    else:
        print("RESULT: NOT FIRED (timeout reached)")
        return False


def run_confirm_dialog_check():
    print("\n" + "=" * 60)
    print("STEP 2: Native Confirmation Dialog Check")
    print("=" * 60)

    print("\nDialog 1/2: Please click 'NO' (or wait for timeout)...")
    res1 = guard.confirm("Manual Check Test 1: Please click 'NO' on this dialog.", timeout_ms=10000)
    print(f"Dialog 1 result: Approved = {res1} (Expected: False)")

    print("\nDialog 2/2: Please click 'YES'...")
    res2 = guard.confirm("Manual Check Test 2: Please click 'YES' on this dialog.", timeout_ms=15000)
    print(f"Dialog 2 result: Approved = {res2} (Expected: True)")


def run_password_uia_check():
    print("\n" + "=" * 60)
    print("STEP 3: Real Tkinter Password Entry & UIA IsPassword Check")
    print("=" * 60)

    root = tk.Tk()
    root.title("Manual_Check_Password_Window")
    root.geometry("350x150")

    lbl = tk.Label(root, text="Real Tkinter Password Entry (show='*'):")
    lbl.pack(pady=5)

    entry = tk.Entry(root, show="*", width=30)
    entry.insert(0, "SuperSecret123!")
    entry.pack(pady=10)

    root.update()

    print("Tkinter window opened with show='*'. Querying Windows UIA tree...")

    d = Desktop(backend="uia")
    target_win = None
    for w in d.windows():
        if "Manual_Check_Password_Window" in w.window_text():
            target_win = w
            break

    if target_win:
        print(f"Found window: '{target_win.window_text()}' (HWND: {hex(target_win.handle)})")
        controls = target_win.descendants()
        print(f"Total controls detected by UIA: {len(controls)}")
        for i, c in enumerate(controls):
            elem = getattr(c.element_info, "_element", None)
            uia_is_pwd = getattr(elem, "CurrentIsPassword", None) if elem else None
            is_guard_blocked = guard.is_password_control(c)
            print(f" Control [{i}]: Type={c.element_info.control_type}, Name='{c.element_info.name}', UIA CurrentIsPassword={uia_is_pwd}, guard.is_password_control={is_guard_blocked}")
    else:
        print("Error: Could not locate 'Manual_Check_Password_Window' in UIA.")

    root.destroy()


def main():
    print("Starting Manual Verification for Phase 2.5...")
    run_hotkey_check()
    run_confirm_dialog_check()
    run_password_uia_check()
    print("\n" + "=" * 60)
    print("MANUAL VERIFICATION SCRIPT FINISHED")
    print("=" * 60)


if __name__ == "__main__":
    main()
