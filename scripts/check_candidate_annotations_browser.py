"""Exercise real annotation APIs/UI with entirely temporary candidate data.

Run after npm run build. No requests go to the live monitor or pipeline.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright, expect

from casm_monitor.config import Settings
from casm_monitor.web.cands import build_router as candidates
from casm_monitor.web.candidate_annotations import build_router as annotations
from casm_monitor.web.app import STATIC_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"tests"))
from test_cands import _connect, _insert_cluster, _insert_trigger


def main():
    directory = Path(tempfile.mkdtemp(prefix="casm-candidate-review-test-"))
    settings = Settings(store_root=directory/"store", observation_cache_root=directory/"preview",
                        candidates_dir=directory/"candidates", t2_db=directory/"t2.sqlite",
                        candidate_annotations_root=directory/"annotations", candidate_archive_root=directory/"archive")
    os.environ["CASM_MONITOR_WORKSPACE"] = "1"
    with _connect(settings.t2_db) as conn:
        for i in range(16):
            name = f"261006{i:06d}"
            _insert_cluster(conn, name, snr=20+i, dm=100+i)
            _insert_trigger(conn, name, "triggered")
            candidate = settings.candidates_dir/name
            candidate.mkdir(parents=True)
            image = Image.new("RGB", (900,700), "white")
            draw = ImageDraw.Draw(image)
            draw.text((30,20), "SYNTHETIC UI TEST ONLY: " + name, fill="black")
            draw.rectangle((60,70,850,640), outline="#435b75", width=2)
            for x in range(80,850,30):
                draw.line((x,600,x+20,100),fill="#58779d",width=2)
            image.save(candidate/f"{name}.png")
            (candidate/f"{name}.json").write_text('{"test_fixture":true}')
    before = settings.t2_db.read_bytes()
    app = FastAPI()
    app.include_router(candidates(settings))
    app.include_router(annotations(settings))
    app.mount("/assets", StaticFiles(directory=STATIC_DIR/"assets"), name="assets")

    @app.get("/{path:path}")
    def index(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(STATIC_DIR/"index.html")

    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda:server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    url = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width":1500,"height":1100})
            page = context.new_page()
            errors, writes = [], []
            page.on("pageerror", lambda error:errors.append(str(error)))
            page.on("request", lambda request:writes.append(request.url) if request.method=="POST" else None)
            page.goto(url+"/cands")
            expect(page.locator(".candidate-card")).to_have_count(12)
            page.get_by_label("Reviewer name", exact=True).fill("Browser reviewer")
            page.get_by_role("button", name="Use name", exact=True).click()
            expect(page.locator(".candidate-identity")).to_contain_text("Reviewing as Browser reviewer")
            page.locator(".candidate-card").first.get_by_role("button",name="Classify",exact=True).click()
            expect(page.locator(".candidate-review-card")).to_be_visible()
            frb = page.get_by_role("button",name="FRB 1",exact=True)
            expect(frb).to_be_enabled()
            first = page.locator(".candidate-review-card h2").inner_text()
            frb.click()
            expect(page.locator(".candidate-review-save")).to_contain_text("Saved")
            expect(frb).to_have_attribute("aria-pressed","true")
            page.get_by_role("button",name="RFI 4",exact=True).click()
            expect(page.get_by_role("button",name="RFI 4",exact=True)).to_have_attribute("aria-pressed","true")
            page.get_by_label("Narrowband",exact=True).check()
            page.get_by_label("Annotation note",exact=True).fill("Synthetic browser check")
            page.get_by_role("button",name="Save details",exact=True).click()
            expect(page.locator(".candidate-review-save")).to_contain_text("Saved")
            page.get_by_role("button",name="Undo",exact=True).click()
            expect(page.get_by_label("Annotation note",exact=True)).to_have_value("")
            page.get_by_role("button",name="Undo",exact=True).click()
            expect(frb).to_have_attribute("aria-pressed","true")
            page.locator(".candidate-image").click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.get_by_role("button",name="Zoom in",exact=True).click()
            expect(page.get_by_label("Display zoom")).to_have_text("150%")
            page.keyboard.press("Escape")
            # Auto advance preserves a route back to undo the previous review.
            page.get_by_label("Advance after saving",exact=True).check()
            page.get_by_role("button",name="Noise 5",exact=True).click()
            expect(page.locator(".candidate-review-card h2")).not_to_have_text(first)
            page.get_by_role("button",name="Return / undo",exact=True).click()
            expect(page.locator(".candidate-review-card h2")).to_have_text(first)
            page.get_by_label("Advance after saving",exact=True).uncheck()
            # A second browser takes an independent label; gallery shows both.
            other = browser.new_context(viewport={"width":1500,"height":1100})
            second = other.new_page()
            second.goto(url+"/cands?mode=classify&review="+first)
            second.get_by_label("Reviewer name",exact=True).fill("Second reviewer")
            second.get_by_role("button",name="Use name",exact=True).click()
            expect(second.get_by_role("button",name="Pulsar 2",exact=True)).to_be_enabled()
            second.get_by_role("button",name="Pulsar 2",exact=True).click()
            expect(second.locator(".candidate-review-save")).to_contain_text("Saved")
            page.reload()
            expect(page.locator(".candidate-identity")).to_contain_text("Reviewing as Browser reviewer")
            page.get_by_role("button",name="Browse",exact=True).click()
            card = page.locator(f'.candidate-card[data-name="{first}"]')
            expect(card).to_contain_text("Browser reviewer · Noise")
            expect(card).to_contain_text("Second reviewer · Pulsar")
            expect(card).to_contain_text("Different labels")
            page.screenshot(path=str(directory/"browse.png"),full_page=False)
            # Save failures keep the active event and do not fabricate a saved vote.
            card.get_by_role("button",name="Classify",exact=True).click()
            expect(page.get_by_role("button",name="Solar 3",exact=True)).to_be_enabled()
            def fail(route):
                if route.request.method=="POST":
                    route.fulfill(status=503,content_type="application/json",body='{"detail":"Test save failure"}')
                else:
                    route.continue_()
            page.route("**/api/candidate-annotations/events/*",fail)
            page.get_by_role("button",name="Solar 3",exact=True).click()
            expect(page.locator(".candidate-review-save")).to_contain_text("Save failed")
            expect(page.locator(".candidate-review-card h2")).to_have_text(first)
            page.unroute("**/api/candidate-annotations/events/*",fail)
            page.get_by_role("button",name="Solar 3",exact=True).click()
            expect(page.locator(".candidate-review-save")).to_contain_text("Saved")
            # Two tabs with the same person cannot silently overwrite each other.
            tab = context.new_page()
            tab.goto(url+"/cands?mode=classify&review="+first)
            expect(tab.get_by_role("button",name="FRB 1",exact=True)).to_be_enabled()
            page.get_by_role("button",name="Noise 5",exact=True).click()
            expect(page.locator(".candidate-review-save")).to_contain_text("Saved")
            tab.get_by_role("button",name="FRB 1",exact=True).click()
            expect(tab.locator(".candidate-review-error")).to_contain_text("another window")
            tab.close()
            # Keyboard actions ignore text entry; Unsure is shortcut six.
            page.locator("body").click(position={"x":2,"y":2})
            page.keyboard.press("6")
            expect(page.get_by_role("button",name="Unsure 6",exact=True)).to_have_attribute("aria-pressed","true")
            page.screenshot(path=str(directory/"classify.png"),full_page=True)
            for width in (1500,1024,700,390,320):
                page.set_viewport_size({"width":width,"height":1100})
                page.wait_for_timeout(150)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"), width
            assert all("/api/candidate-annotations/" in path for path in writes), writes
            assert not errors, errors
            browser.close()
        assert settings.t2_db.read_bytes()==before
        with sqlite3.connect(settings.candidate_annotations_root/"annotations.sqlite3") as conn:
            count=conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
        print(json.dumps({"result":"passed","annotation_revisions":count,"screenshots":str(directory),"production_requests":0}))
    finally:
        server.should_exit=True
        thread.join(timeout=5)


if __name__=="__main__":
    main()
