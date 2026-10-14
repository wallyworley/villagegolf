#!/usr/bin/env python3
"""Rasterize the app icon SVGs into the PNG sizes the manifest and head tags use.

Usage: ``.venv/bin/python scripts/make_app_icons.py``

Uses Playwright with the installed Chrome channel and falls back to the bundled
Chromium. Each PNG is exactly the stated pixel size at device scale factor 1.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

ICON_DIR = Path(__file__).resolve().parents[1] / "static" / "icons"

# (output file, source SVG, size in pixels)
TARGETS = (
    ("icon-192.png", "icon.svg", 192),
    ("icon-512.png", "icon.svg", 512),
    ("apple-touch-icon.png", "icon.svg", 180),
    ("icon-maskable-512.png", "icon-maskable.svg", 512),
    ("favicon-32.png", "icon.svg", 32),
)


def launch(playwright):
    try:
        return playwright.chromium.launch(channel="chrome")
    except Exception:
        return playwright.chromium.launch()


def main():
    with sync_playwright() as playwright:
        browser = launch(playwright)
        try:
            for output, source, size in TARGETS:
                svg = (ICON_DIR / source).read_text(encoding="utf-8")
                context = browser.new_context(viewport={"width": size, "height": size}, device_scale_factor=1)
                page = context.new_page()
                page.set_content(
                    "<!doctype html><html><body style='margin:0;padding:0;overflow:hidden'>"
                    f"<div style='width:{size}px;height:{size}px'>"
                    + svg.replace('width="512" height="512"', f'width="{size}" height="{size}"', 1)
                    + "</div></body></html>"
                )
                page.screenshot(path=str(ICON_DIR / output), clip={"x": 0, "y": 0, "width": size, "height": size})
                context.close()
                print(f"wrote {output} ({size}x{size})")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
