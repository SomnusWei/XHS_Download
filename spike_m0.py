# -*- coding: utf-8 -*-
"""M0 tech probe: PySide6 import + DrissionPage/Edge open xiaohongshu + profile persistence."""
import os
import time

def probe_pyside():
    print("[1/3] PySide6 import test ...")
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication([])
    lbl = QLabel("PySide6 OK")
    lbl.resize(320, 80)
    print("[1/3] PySide6 usable")

def probe_edge():
    print("[2/3] DrissionPage + Edge launch test ...")
    from DrissionPage import ChromiumPage, ChromiumOptions
    profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data")
    os.makedirs(profile_dir, exist_ok=True)
    co = ChromiumOptions()
    co.set_browser_path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    co.set_user_data_path(profile_dir)
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    page = ChromiumPage(co)
    page.get("https://www.xiaohongshu.com")
    time.sleep(8)
    print("[2/3] title =", repr(page.title))
    print("[2/3] url   =", repr(page.url))
    shot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m0_screen.png")
    try:
        page.get_screenshot(path=shot)
        print("[2/3] screenshot saved:", shot)
    except Exception as e:
        print("[2/3] screenshot fail:", e)
    files = [f for f in os.listdir(profile_dir) if not f.startswith(".")]
    print("[3/3] user_data entries:", len(files), files[:5])
    page.quit()
    print("[3/3] browser closed, profile kept at", profile_dir)

if __name__ == "__main__":
    probe_pyside()
    probe_edge()
    print("\n=== M0 DONE ===")