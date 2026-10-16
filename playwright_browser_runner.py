"""
playwright_browser_runner.py - Real isolated headless Chromium browser automation (Codex-style).
Operates in an independent sandbox (does NOT touch user's Chrome/Edge/history).
Runs at turbo speed, immune to window minimizing, and captures screen snapshots into history.
"""

import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SNAPSHOT_DIR = Path(r"C:\Users\kartik\agent-workspace\browser_snapshots")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

def run_isolated_browser_task(url="https://news.ycombinator.com", search_query="AI Agents"):
    history = []
    print(f"[BROWSER] Launching isolated Chromium engine (Sandboxed)...")
    start_time = time.perf_counter()

    with sync_playwright() as p:
        # Launch dedicated isolated Chromium (independent from user's machine/browser)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Step 1: Navigate to target URL
        t0 = time.perf_counter()
        print(f"[STEP 1] Navigating to {url}...")
        page.goto(url, wait_until="domcontentloaded")
        s1_file = SNAPSHOT_DIR / "01_initial_page.png"
        page.screenshot(path=str(s1_file))
        d1 = round((time.perf_counter() - t0) * 1000, 2)
        history.append({
            "step": 1,
            "title": page.title(),
            "url": page.url,
            "duration_ms": d1,
            "snapshot": str(s1_file)
        })
        print(f"[STEP 1] Loaded '{page.title()}' in {d1} ms (Saved snapshot: {s1_file.name})")

        # Step 2: Query / Interact (Example: find top stories)
        t1 = time.perf_counter()
        top_items = []
        links = page.query_selector_all(".titleline > a")
        for i, link in enumerate(links[:5], 1):
            top_items.append({"rank": i, "title": link.inner_text(), "href": link.get_attribute("href")})
        d2 = round((time.perf_counter() - t1) * 1000, 2)
        history.append({
            "step": 2,
            "action": "Extracted top 5 items",
            "items": top_items,
            "duration_ms": d2
        })
        print(f"[STEP 2] Extracted {len(top_items)} items in {d2} ms at Turbo Speed")

        # Step 3: Highlight / Click first story
        t2 = time.perf_counter()
        if links:
            first_title = links[0].inner_text()
            print(f"[STEP 3] Opening first story: '{first_title[:40]}...'")
            links[0].click()
            page.wait_for_load_state("domcontentloaded")
            s3_file = SNAPSHOT_DIR / "02_story_page.png"
            page.screenshot(path=str(s3_file))
            d3 = round((time.perf_counter() - t2) * 1000, 2)
            history.append({
                "step": 3,
                "title": page.title(),
                "url": page.url,
                "duration_ms": d3,
                "snapshot": str(s3_file)
            })
            print(f"[STEP 3] Navigated to '{page.title()}' in {d3} ms (Saved snapshot: {s3_file.name})")

        total_duration = round((time.perf_counter() - start_time) * 1000, 2)
        browser.close()

    summary = {
        "success": True,
        "total_duration_ms": total_duration,
        "total_duration_s": round(total_duration / 1000, 3),
        "snapshots_saved": len([h for h in history if "snapshot" in h]),
        "history": history
    }

    result_json = SNAPSHOT_DIR / "run_history.json"
    result_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[DONE] Isolated Browser Run Completed in {summary['total_duration_s']}s!")
    print(f"[DONE] Snapshots & run history written to: {SNAPSHOT_DIR}")
    return summary

if __name__ == "__main__":
    run_isolated_browser_task()
