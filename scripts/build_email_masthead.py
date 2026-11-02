"""Render static/email/masthead.png (3x) from scripts/email-masthead.html with headless Chrome via Playwright."""
import os
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = "file://" + os.path.join(ROOT, "scripts", "email-masthead.html")
OUT = os.path.join(ROOT, "static", "email", "masthead.png")

with sync_playwright() as p:
    br = p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    pg = br.new_context(viewport={"width": 800, "height": 200}, device_scale_factor=3).new_page()
    pg.goto(SRC)
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(300)
    box = pg.locator("#lockup").bounding_box()
    pg.locator("#lockup").screenshot(path=OUT)
    print(f"{OUT}: CSS size {box['width']:.1f} x {box['height']:.1f}")
    br.close()
