"""Bounded read-only browser verification against the isolated preview."""
from pathlib import Path
import json
import re
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8061"
OUTPUT = Path("/home/casm/scratch/casm-observation-preview/screenshots")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector(".observation-page")
        assert page.url.endswith("/observation")
        assert page.locator(".header__tabs a").all_text_contents() == ["Observation", "Readiness", "Antennas"]
        assert page.locator(".science-image").evaluate_all("images => images.every(i => i.complete && i.naturalWidth > 0)")
        assert page.locator(".array-geometry circle").count() > 0
        assert " UTC" in page.locator(".observatory-line").inner_text()
        assert not re.search(r"\d{4}-\d{2}-\d{2}T", page.locator(".observatory-line").inner_text())
        assert page.get_by_text("Processing and provenance", exact=True).count() == 1
        page.get_by_label("Coordinates", exact=False).select_option("product")
        assert page.locator(".array-geometry circle").count() > 0
        page.get_by_label("Coordinates", exact=False).select_option("current")
        page.screenshot(path=str(OUTPUT / "observation-desktop.png"), full_page=True)
        for route in ("readiness", "antennas"):
            page.locator(f'.header__tabs a[href="/{route}"]').click()
            page.wait_for_url(f"**/{route}")
            assert page.locator("h2").count() > 0
        page.goto(URL + "/observation", wait_until="networkidle")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(OUTPUT / "observation-mobile.png"), full_page=True)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"Mobile overflow: {overflow}px"
        page.route("**/api/observation", lambda route: route.fulfill(status=503, body="Unavailable"))
        page.reload(wait_until="networkidle")
        assert "Observation evidence is unavailable" in page.locator("main").inner_text()
        assert "0 / 0 recovered" not in page.locator("main").inner_text()
        page.unroute("**/api/observation")
        payload = page.request.get(URL + "/api/observation").json()
        payload["injections"]["status"] = "unavailable"
        page.route("**/api/observation", lambda route: route.fulfill(json=payload))
        page.reload(wait_until="networkidle")
        assert "Injection evidence unavailable" in page.locator("main").inner_text()
        assert page.locator(".recovery-headline").count() == 0
        assert page.request.post(URL + "/api/read-only-check").status == 403
        browser.close()
    assert not errors, errors
    print(json.dumps({"result": "passed", "mobile_overflow_px": overflow, "screenshots": str(OUTPUT)}))


if __name__ == "__main__":
    main()
