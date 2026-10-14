#!/usr/bin/env python3
"""Capture the user guide screenshots from the synthetic preview server.

Usage: ``.venv/bin/python scripts/capture_guide_screenshots.py``

Starts ``tests/preview_server.py`` on port 8095 with the request watcher off and
synthetic backend maps, drives it with Playwright (installed Chrome), and writes
JPEG files to ``static/guide/``. Nothing here contacts The Villages. The server
is always stopped at the end, even on error.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static" / "guide"
PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
DISMISS = "try{localStorage.setItem('vf_install_card_dismissed','1')}catch(e){}"
CLEAR = "try{localStorage.removeItem('vf_install_card_dismissed')}catch(e){}"


def ensure_port_free():
    """Refuse to run if something already listens on the port, so the script never drives another server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"Port {PORT} is already in use. Stop that process, then run this script again.")


def start_server():
    ensure_port_free()
    env = dict(os.environ, PREVIEW_PORT=str(PORT), DISABLE_REQUEST_WATCHER="1", PREVIEW_OFFLINE_MAPS="1")
    server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "preview_server.py")], cwd=str(ROOT), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if server.poll() is not None:
            raise RuntimeError("preview server exited early")
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=1)
            return server
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("preview server did not start")


def new_page(browser, user_agent=None, dismissed=True):
    options = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                   color_scheme="light", service_workers="block")
    if user_agent:
        options["user_agent"] = user_agent
    context = browser.new_context(**options)
    context.add_init_script(DISMISS if dismissed else CLEAR)
    return context, context.new_page()


def sign_in(page):
    page.goto(BASE)
    page.fill("#login-user-input", "preview")
    page.fill("#login-pass-input", "preview")
    page.fill("#login-pin-input", "1234")
    page.evaluate("loginUser()")
    page.wait_for_selector("#app .d-card", timeout=15000)
    page.wait_for_timeout(900)


def shot(page, name):
    page.screenshot(path=str(OUT / name), type="jpeg", quality=80)
    print("wrote", name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome")
            except Exception:
                browser = playwright.chromium.launch()
            try:
                context, page = new_page(browser, user_agent=IPHONE_UA)
                sign_in(page)
                shot(page, "book.jpg")

                page.locator(".d-card").nth(1).click()
                page.locator("#golferList .g-card").first.click()
                page.locator("#t-exec").click()
                page.locator(".t-tile").first.click()
                page.wait_for_timeout(300)
                page.locator("#fetchBtn").click()
                page.wait_for_selector("#timeList .time-row-main", timeout=15000)
                page.wait_for_timeout(700)
                page.locator("#res-header").scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                shot(page, "times.jpg")

                page.locator("#timeList .time-row-main").first.click()
                page.wait_for_selector("#overlay.show", timeout=5000)
                page.wait_for_timeout(500)
                shot(page, "confirm.jpg")
                page.locator("#overlay .cs-cancel").click()  # never book from the guide capture
                page.wait_for_timeout(300)

                page.locator("#tab-mytimes").click()
                page.wait_for_selector("#myTimesList .mytimes-card", timeout=15000)
                page.wait_for_timeout(600)
                page.evaluate("window.scrollTo(0,0)")
                shot(page, "mytimes.jpg")

                page.locator("#tab-request").click()
                page.wait_for_timeout(400)
                page.evaluate("window.scrollTo(0,0)")
                shot(page, "requests.jpg")

                page.locator("#tab-courses").click()
                page.wait_for_timeout(2500)
                page.evaluate("window.scrollTo(0,0)")
                shot(page, "courses.jpg")

                page.locator("#hdr-badge").click()
                page.wait_for_timeout(500)
                page.locator("#account-help-heading").scroll_into_view_if_needed()
                page.evaluate("document.querySelector('.account-dialog').scrollTop+=140")
                page.wait_for_timeout(300)
                shot(page, "account.jpg")
                context.close()

                context, page = new_page(browser, user_agent=IPHONE_UA, dismissed=False)
                sign_in(page)
                page.wait_for_selector("#install-card:not([hidden])", timeout=5000)
                page.evaluate("window.scrollTo(0,0)")
                shot(page, "install-card.jpg")
                context.close()
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
