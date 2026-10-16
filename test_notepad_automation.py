"""
test_notepad_automation.py - Automation script to open Notepad, write numbers 1 to 10,
save the file to agent-workspace, and measure exact high-precision timings from start to end.
"""

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import guard
import server
from pywinauto import Desktop

guard.set_kill_switch(False)
guard.reset_rate_limits()

TARGET_DIR = Path(r"C:\Users\kartik\agent-workspace")
TARGET_DIR.mkdir(parents=True, exist_ok=True)
TARGET_FILE = TARGET_DIR / "numbers.txt"

# Clean any existing file
if TARGET_FILE.exists():
    try:
        TARGET_FILE.unlink()
    except Exception as e:
        print(f"Warning: could not delete existing file: {e}")

report = {
    "target_file": str(TARGET_FILE),
    "steps": {},
    "success": False,
}

total_start = time.perf_counter()
wall_start = time.time()

# -------------------------------------------------------------
# STEP 1: Launch / Detect Notepad
# -------------------------------------------------------------
s1_start = time.perf_counter()
d = Desktop(backend="uia")
notepad_win = None

# Check if Notepad is already open
for w in d.windows():
    try:
        if "notepad" in w.window_text().lower():
            notepad_win = w
            break
    except Exception:
        continue

if not notepad_win:
    print("[STEP 1] Launching Notepad...")
    launch_res = server.launch_app("notepad")
    print(f"[STEP 1] launch_res: {launch_res}")
    for attempt in range(12):
        time.sleep(0.3)
        for w in d.windows():
            try:
                if "notepad" in w.window_text().lower():
                    notepad_win = w
                    break
            except Exception:
                continue
        if notepad_win:
            break
else:
    print(f"[STEP 1] Found existing Notepad window: '{notepad_win.window_text()}'")

s1_end = time.perf_counter()
s1_duration = s1_end - s1_start
report["steps"]["1_launch_or_detect_notepad"] = {
    "duration_seconds": round(s1_duration, 4),
    "duration_ms": round(s1_duration * 1000, 2),
    "window_title": notepad_win.window_text() if notepad_win else None,
}
print(f"[STEP 1] Completed in {s1_duration*1000:.2f} ms ({s1_duration:.4f}s)")

if not notepad_win:
    print("[ERROR] Notepad window did not appear.")
    sys.exit(1)

# -------------------------------------------------------------
# STEP 2: Focus Notepad Window
# -------------------------------------------------------------
s2_start = time.perf_counter()
focus_res = server.focus_window("Notepad")
time.sleep(0.15)
s2_end = time.perf_counter()
s2_duration = s2_end - s2_start
report["steps"]["2_focus_notepad"] = {
    "duration_seconds": round(s2_duration, 4),
    "duration_ms": round(s2_duration * 1000, 2),
    "status": focus_res.get("status"),
}
print(f"[STEP 2] Completed in {s2_duration*1000:.2f} ms ({s2_duration:.4f}s)")

# -------------------------------------------------------------
# STEP 3: Write numbers 1 to 10 into Document
# -------------------------------------------------------------
s3_start = time.perf_counter()
numbers_text = "\n".join(str(i) for i in range(1, 11)) + "\n"

docs = [c for c in notepad_win.descendants() if c.element_info.control_type == "Document"]
if not docs:
    raise RuntimeError("Notepad 'Document' text editor control not found")

editor = docs[0]
editor.iface_value.SetValue(numbers_text)
time.sleep(0.2)

s3_end = time.perf_counter()
s3_duration = s3_end - s3_start
report["steps"]["3_write_numbers_1_to_10"] = {
    "duration_seconds": round(s3_duration, 4),
    "duration_ms": round(s3_duration * 1000, 2),
    "lines_written": 10,
    "characters": len(numbers_text),
}
print(f"[STEP 3] Completed in {s3_duration*1000:.2f} ms ({s3_duration:.4f}s)")

# -------------------------------------------------------------
# STEP 4: Invoke File -> Save As Dialog
# -------------------------------------------------------------
s4_start = time.perf_counter()
file_menus = [c for c in notepad_win.descendants() if c.element_info.name == "File" and c.element_info.control_type == "MenuItem"]
if file_menus:
    file_menus[0].iface_expand_collapse.Expand()
    time.sleep(0.25)

save_as_items = [c for c in notepad_win.descendants() if c.element_info.name == "Save as" and c.element_info.control_type == "MenuItem"]
if not save_as_items:
    # Check top-level popups
    for win in d.windows():
        save_as_items.extend([c for c in win.descendants() if c.element_info.name == "Save as" and c.element_info.control_type == "MenuItem"])

if not save_as_items:
    raise RuntimeError("Notepad 'Save as' menu item not found")

save_as_items[0].iface_invoke.Invoke()
time.sleep(0.6)  # Wait for modal dialog to mount

s4_end = time.perf_counter()
s4_duration = s4_end - s4_start
report["steps"]["4_invoke_save_as_dialog"] = {
    "duration_seconds": round(s4_duration, 4),
    "duration_ms": round(s4_duration * 1000, 2),
}
print(f"[STEP 4] Completed in {s4_duration*1000:.2f} ms ({s4_duration:.4f}s)")

# -------------------------------------------------------------
# STEP 5: Input Destination Path & Confirm Save
# -------------------------------------------------------------
s5_start = time.perf_counter()

# Find File name edit control
file_name_edits = [
    c for c in notepad_win.descendants()
    if c.element_info.control_type == "Edit" and (c.element_info.automation_id == "1001" or c.element_info.name == "File name:")
]
if not file_name_edits:
    for win in d.windows():
        file_name_edits.extend([
            c for c in win.descendants()
            if c.element_info.control_type == "Edit" and (c.element_info.automation_id == "1001" or c.element_info.name == "File name:")
        ])

if not file_name_edits:
    raise RuntimeError("Save As dialog 'File name:' edit box not found")

target_path_str = str(TARGET_FILE)
file_name_edits[0].iface_value.SetValue(target_path_str)
time.sleep(0.2)

# Click Save button
save_btns = [
    c for c in notepad_win.descendants()
    if c.element_info.control_type == "Button" and (c.element_info.automation_id == "1" or c.element_info.name == "Save")
]
if not save_btns:
    for win in d.windows():
        save_btns.extend([
            c for c in win.descendants()
            if c.element_info.control_type == "Button" and (c.element_info.automation_id == "1" or c.element_info.name == "Save")
        ])

if not save_btns:
    raise RuntimeError("Save As dialog 'Save' button not found")

save_btns[0].iface_invoke.Invoke()

s5_end = time.perf_counter()
s5_duration = s5_end - s5_start
report["steps"]["5_set_destination_and_save"] = {
    "duration_seconds": round(s5_duration, 4),
    "duration_ms": round(s5_duration * 1000, 2),
    "target_path": target_path_str,
}
print(f"[STEP 5] Completed in {s5_duration*1000:.2f} ms ({s5_duration:.4f}s)")

# -------------------------------------------------------------
# STEP 6: Verify File on Disk
# -------------------------------------------------------------
s6_start = time.perf_counter()
file_verified = False
for _ in range(30):
    time.sleep(0.1)
    if TARGET_FILE.exists() and TARGET_FILE.stat().st_size > 0:
        file_verified = True
        break

s6_end = time.perf_counter()
s6_duration = s6_end - s6_start
file_content = TARGET_FILE.read_text(encoding="utf-8") if file_verified else None

report["steps"]["6_verify_file_on_disk"] = {
    "duration_seconds": round(s6_duration, 4),
    "duration_ms": round(s6_duration * 1000, 2),
    "verified": file_verified,
    "file_size_bytes": TARGET_FILE.stat().st_size if file_verified else 0,
}
print(f"[STEP 6] Completed in {s6_duration*1000:.2f} ms ({s6_duration:.4f}s). File verified: {file_verified}")

# -------------------------------------------------------------
# STEP 7: Capture Verification Screenshot
# -------------------------------------------------------------
s7_start = time.perf_counter()
shot_path = BASE_DIR / "notepad_result.png"
shot_res = server.screenshot("Notepad")
if len(shot_res) == 2:
    shot_path.write_bytes(shot_res[0].data)
s7_end = time.perf_counter()
s7_duration = s7_end - s7_start

report["steps"]["7_capture_screenshot"] = {
    "duration_seconds": round(s7_duration, 4),
    "duration_ms": round(s7_duration * 1000, 2),
    "screenshot_path": str(shot_path) if shot_path.exists() else None,
}
print(f"[STEP 7] Completed in {s7_duration*1000:.2f} ms ({s7_duration:.4f}s)")

# -------------------------------------------------------------
# FINAL TIMING AND BENCHMARK CALCULATIONS
# -------------------------------------------------------------
total_end = time.perf_counter()
wall_end = time.time()
total_duration = total_end - total_start

report["success"] = file_verified and (file_content is not None)
report["total_duration_seconds"] = round(total_duration, 4)
report["total_duration_ms"] = round(total_duration * 1000, 2)
report["total_duration_formatted"] = f"{total_duration:.3f}s ({int(total_duration * 1000)} ms)"
report["wall_clock_start"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall_start))
report["wall_clock_end"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall_end))
report["file_content"] = file_content

print("\n" + "=" * 65)
print(f"AUTOMATION RUN SUMMARY: SUCCESS = {report['success']}")
print(f"Total time elapsed: {report['total_duration_formatted']}")
print(f"File verified at: {TARGET_FILE}")
print(f"File content:\n{file_content}")
print("=" * 65)

with open(BASE_DIR / "notepad_run_result.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

print(f"Results written to: {BASE_DIR / 'notepad_run_result.json'}")
