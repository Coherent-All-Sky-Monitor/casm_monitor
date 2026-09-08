"""Job queue lifecycle: submit -> worker runs -> done, cancel, orphans."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.jobs.worker import JobWorker
from casm_monitor.store import Store, UnsafePathError
from casm_monitor.web.app import create_app


def test_submit_run_done(settings, store: Store):
    job_id = store.submit_job("noop", {"seconds": 0.2, "message": "hello"})
    assert store.get_job(job_id)["state"] == "queued"

    worker = JobWorker(settings, store=store)
    assert worker.run_pending() == 1

    job = store.get_job(job_id)
    assert job["state"] == "done"
    assert job["started"] and job["finished"] and job["lease_until"] is None
    assert job["result"]["slept_s"] == 0.2
    assert job["result"]["message"] == "hello"
    log_path = Path(job["log_path"])
    assert log_path.is_file() and "noop: done" in log_path.read_text()
    assert log_path.parent == Path(settings.jobs_root) / str(job_id)


def test_unknown_kind_fails_cleanly(settings, store: Store):
    job_id = store.submit_job("definitely-not-a-kind", {})
    JobWorker(settings, store=store).run_pending()
    job = store.get_job(job_id)
    assert job["state"] == "failed" and job["result"]["reason"] == "unknown_kind"


def test_orphan_reconciliation(settings, store: Store):
    job_id = store.submit_job("noop", {"seconds": 0})
    store.execute(
        "UPDATE jobs SET state = 'running', started = ?, lease_until = ?, worker = 'dead:1' WHERE id = ?",
        (time.time() - 600, time.time() - 300, job_id),
    )
    worker = JobWorker(settings, store=store)
    assert worker.reconcile() == [job_id]
    job = store.get_job(job_id)
    assert job["state"] == "failed" and job["result"]["reason"] == "orphaned"
    assert store.events(kind="job_orphaned")[0]["detail"]["job_id"] == job_id
    # a job whose lease is still valid is left alone
    other = store.submit_job("noop", {})
    store.execute(
        "UPDATE jobs SET state = 'running', lease_until = ? WHERE id = ?",
        (time.time() + 60, other),
    )
    assert worker.reconcile() == []
    assert store.get_job(other)["state"] == "running"


def test_claim_is_exclusive_and_fifo(settings, store: Store):
    first = store.submit_job("noop", {"seconds": 0})
    second = store.submit_job("noop", {"seconds": 0})
    claimed = store.claim_next_job("w1")
    assert claimed["id"] == first and claimed["state"] == "running"
    assert claimed["lease_until"] > time.time()
    assert store.claim_next_job("w2")["id"] == second
    assert store.claim_next_job("w3") is None


def test_cancel_queued_and_running(settings, store: Store):
    queued = store.submit_job("noop", {"seconds": 0})
    assert store.cancel_job(queued) == "cancelled"
    assert store.get_job(queued)["state"] == "cancelled"
    assert store.cancel_job(999999) == ""

    running = store.submit_job("noop", {"seconds": 30})
    worker = JobWorker(settings, store=store)
    done = threading.Event()

    def drain():
        worker.run_pending(max_jobs=1)
        done.set()

    thread = threading.Thread(target=drain)
    thread.start()
    deadline = time.time() + 20
    while store.get_job(running)["state"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    assert store.get_job(running)["state"] == "running"
    store.cancel_job(running)
    assert done.wait(timeout=30)
    thread.join()
    job = store.get_job(running)
    assert job["state"] == "cancelled"
    assert "cancelled" in job["result"]["reason"]


def test_job_timeout(settings, store: Store, monkeypatch):
    import casm_monitor.jobs.worker as worker_mod
    from casm_monitor.jobs.kinds import KINDS, JobKind

    monkeypatch.setitem(KINDS, "noop", JobKind(name="noop", run=KINDS["noop"].run, timeout_s=1.0))
    monkeypatch.setattr(worker_mod, "POLL_S", 0.1)
    job_id = store.submit_job("noop", {"seconds": 30})
    JobWorker(settings, store=store).run_pending(max_jobs=1)
    job = store.get_job(job_id)
    assert job["state"] == "failed" and "timeout" in job["result"]["reason"]


def test_lost_lease_does_not_overwrite_the_row(settings, store: Store, monkeypatch):
    """A worker whose lease lapsed writes nothing: reconciliation wins."""
    import casm_monitor.jobs.worker as worker_mod

    monkeypatch.setattr(worker_mod, "POLL_S", 0.05)
    monkeypatch.setattr(worker_mod, "LEASE_RENEW_S", 0.1)
    job_id = store.submit_job("noop", {"seconds": 30})
    worker = JobWorker(settings, store=store)
    done = threading.Event()
    state: list[str] = []

    def drain():
        state.append(worker.run_pending(max_jobs=1) and "ran" or "none")
        done.set()

    thread = threading.Thread(target=drain)
    thread.start()
    deadline = time.time() + 20
    while store.get_job(job_id)["state"] != "running" and time.time() < deadline:
        time.sleep(0.05)

    # another worker (or the reconciler) takes the row away from us
    store.execute(
        "UPDATE jobs SET state = 'failed', finished = ?, lease_until = NULL, result = ? WHERE id = ?",
        (time.time(), json.dumps({"reason": "orphaned"}), job_id),
    )
    assert done.wait(timeout=30)
    thread.join()

    job = store.get_job(job_id)
    assert job["state"] == "failed"
    assert job["result"] == {"reason": "orphaned"}  # untouched by the worker
    assert store.events(kind="job_ownership_lost")[0]["detail"]["job_id"] == job_id
    assert not store.events(kind="job_finished")


def test_cancellation_result_keeps_the_cancelled_state(settings, store: Store):
    """The worker fills in the reason without rewriting the cancelled state."""
    job_id = store.submit_job("noop", {"seconds": 30})
    worker = JobWorker(settings, store=store)
    done = threading.Event()
    threading.Thread(target=lambda: (worker.run_pending(max_jobs=1), done.set())).start()
    deadline = time.time() + 20
    while store.get_job(job_id)["state"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    store.cancel_job(job_id)
    assert done.wait(timeout=30)
    job = store.get_job(job_id)
    assert job["state"] == "cancelled" and job["finished"] is not None
    assert "cancelled" in job["result"]["reason"]


def test_worker_stop_kills_the_job_and_fails_it(settings, store: Store, monkeypatch):
    """SIGTERM/stop: the child's process group dies, the job says why."""
    import casm_monitor.jobs.worker as worker_mod

    monkeypatch.setattr(worker_mod, "POLL_S", 0.05)
    job_id = store.submit_job("noop", {"seconds": 30})
    worker = JobWorker(settings, store=store)
    done = threading.Event()
    threading.Thread(target=lambda: (worker.run_pending(max_jobs=1), done.set())).start()

    deadline = time.time() + 20
    while worker._active is None and time.time() < deadline:
        time.sleep(0.02)
    assert worker._active is not None
    _job, proc = worker._active
    pid = proc.pid

    worker.stop()  # what the SIGTERM handler does
    assert done.wait(timeout=40)

    job = store.get_job(job_id)
    assert job["state"] == "failed"
    assert job["result"]["reason"] == "worker_stopped"
    assert proc.poll() is not None  # reaped, not left behind
    with pytest.raises(OSError):
        os.killpg(pid, 0)  # the whole process group is gone


def test_job_dir_stays_inside_the_store_root(settings, store: Store):
    """jobs_root and every job directory are contained, or refused."""
    worker = JobWorker(settings, store=store)
    assert worker.job_dir(7) == (Path(settings.store_root).resolve() / "jobs" / "7")
    with pytest.raises(ValueError):
        worker.job_dir("../../etc")  # not an int, not a path component

    @dataclasses.dataclass(frozen=True)
    class EscapingSettings(Settings):
        @property
        def jobs_root(self) -> Path:  # a config pointing out of the store
            return Path("/tmp/casm_monitor_escape/jobs")

    escaping = EscapingSettings(**dataclasses.asdict(settings))
    with pytest.raises(UnsafePathError):
        JobWorker(escaping, store=store)


def test_api_job_lifecycle(settings):
    store = Store(settings.db_path, store_root=settings.store_root)
    client = TestClient(create_app(settings))

    assert client.post("/api/jobs", json={"kind": "nope"}).status_code == 400
    body = client.post("/api/jobs", json={"kind": "noop", "params": {"seconds": 0.1}}).json()
    job_id = body["id"]
    assert body["state"] == "queued"
    assert client.get("/api/jobs").json()["jobs"][0]["id"] == job_id
    assert client.get(f"/api/jobs/{job_id}").json()["kind"] == "noop"
    assert client.get("/api/jobs/424242").status_code == 404
    assert store.events(kind="job_submitted")

    JobWorker(settings, store=store).run_pending()
    fetched = client.get(f"/api/jobs/{job_id}").json()
    assert fetched["state"] == "done"
    assert json.loads(json.dumps(fetched["result"]))["slept_s"] == 0.1

    queued = client.post("/api/jobs", json={"kind": "noop", "params": {}}).json()["id"]
    assert client.post(f"/api/jobs/{queued}/cancel").json()["state"] == "cancelled"
    assert client.get(f"/api/jobs/{queued}").json()["state"] == "cancelled"
    assert client.post("/api/jobs/424242/cancel").status_code == 404
    store.close()
