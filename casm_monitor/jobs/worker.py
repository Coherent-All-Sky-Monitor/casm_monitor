"""The single job worker.

One process, one job at a time, over the durable SQLite queue:

* on startup every ``running`` job whose lease has expired is failed with
  ``{"reason": "orphaned"}`` (the worker crashed or was restarted);
* a queued job is claimed in one transaction with a 60 s lease, renewed while it
  runs;
* the job body runs in a subprocess (``casm_monitor.jobs.run_job``) with its own
  log file under ``store_root/jobs/<id>/``, so a job cannot take the worker down
  with it;
* the per-kind timeout kills the subprocess and fails the job;
* cancelling flips the job state, which the worker notices and turns into
  SIGTERM (then SIGKILL) for the subprocess.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from ..config import Settings, load_settings
from ..store import Store
from .kinds import KINDS, get_kind

log = logging.getLogger("casm_monitor.jobs")

LEASE_S = 60.0
LEASE_RENEW_S = 20.0
POLL_S = 1.0
IDLE_SLEEP_S = 2.0
TERM_GRACE_S = 10.0


class JobWorker:
    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        self._own_store = store is None
        self.store = store or Store(settings.db_path, store_root=settings.store_root)
        self.worker_id = f"{os.uname().nodename}:{os.getpid()}"
        self._stop = False

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        if self._own_store:
            self.store.close()

    def stop(self, *_args: object) -> None:
        self._stop = True

    def reconcile(self) -> list[int]:
        """Fail jobs left ``running`` by a dead worker."""
        ids = self.store.reconcile_orphaned_jobs()
        if ids:
            log.warning("marked orphaned jobs failed: %s", ids)
        return ids

    # -- one job --------------------------------------------------------
    def job_dir(self, job_id: int) -> Path:
        return Path(self.settings.jobs_root) / str(job_id)

    def run_job(self, job: dict[str, Any]) -> str:
        """Run a claimed job to completion; returns its terminal state."""
        job_id = int(job["id"])
        kind_name = str(job["kind"])
        jdir = self.job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        log_path = jdir / "log.txt"
        self.store.set_job_log(job_id, str(log_path))

        try:
            kind = get_kind(kind_name)
        except KeyError as exc:
            self.store.finish_job(job_id, "failed", {"reason": "unknown_kind", "error": str(exc)})
            return "failed"

        (jdir / "job.json").write_text(
            json.dumps({"id": job_id, "kind": kind_name, "params": job.get("params") or {}}, indent=1)
        )
        result_path = jdir / "result.json"
        if result_path.exists():
            result_path.unlink()

        started = time.time()
        with log_path.open("a") as log_fh:
            log_fh.write(f"=== job {job_id} kind={kind_name} start {time.ctime(started)}\n")
            log_fh.flush()
            proc = subprocess.Popen(
                [sys.executable, "-m", "casm_monitor.jobs.run_job", str(jdir)],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            state, reason = self._supervise(job_id, proc, kind.timeout_s, started)
            log_fh.write(f"=== job {job_id} {state} ({reason or 'ok'}) after {time.time() - started:.1f}s\n")

        result: dict[str, Any] = {}
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
            except ValueError:
                result = {"error": "unparseable result.json"}
        if reason:
            result["reason"] = reason
        result["log_path"] = str(log_path)
        result["elapsed_s"] = round(time.time() - started, 3)
        self.store.finish_job(job_id, state, result)
        self.store.add_event(
            "job_finished",
            severity="info" if state == "done" else "warn",
            subject=f"job {job_id} ({kind_name})",
            detail={"job_id": job_id, "state": state, "reason": reason},
        )
        return state

    def _supervise(
        self, job_id: int, proc: subprocess.Popen, timeout_s: float, started: float
    ) -> tuple[str, str | None]:
        last_renew = time.time()
        while True:
            rc = proc.poll()
            if rc is not None:
                return ("done", None) if rc == 0 else ("failed", f"exit code {rc}")

            now = time.time()
            if now - last_renew >= LEASE_RENEW_S:
                self.store.renew_lease(job_id, LEASE_S)
                last_renew = now

            current = self.store.get_job(job_id)
            if current is not None and current["state"] == "cancelled":
                self._kill(proc)
                return "cancelled", "cancelled by request"

            if now - started > timeout_s:
                self._kill(proc)
                return "failed", f"timeout after {timeout_s:g}s"

            time.sleep(POLL_S)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """SIGTERM the job's process group, then SIGKILL if it lingers."""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                pass

    # -- main loop ------------------------------------------------------
    def run_pending(self, max_jobs: int | None = None) -> int:
        """Drain the queue (used by tests and by ``--drain``)."""
        done = 0
        while max_jobs is None or done < max_jobs:
            job = self.store.claim_next_job(self.worker_id, LEASE_S)
            if job is None:
                break
            self.run_job(job)
            done += 1
        return done

    def loop(self) -> None:
        self.reconcile()
        self.store.add_event(
            "job_worker_started", severity="info", subject=self.worker_id, detail={"kinds": sorted(KINDS)}
        )
        while not self._stop:
            job = self.store.claim_next_job(self.worker_id, LEASE_S)
            if job is None:
                time.sleep(IDLE_SLEEP_S)
                continue
            log.info("running job %s kind=%s", job["id"], job["kind"])
            state = self.run_job(job)
            log.info("job %s -> %s", job["id"], state)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CASM monitor job worker")
    parser.add_argument("--config", default=None)
    parser.add_argument("--drain", action="store_true", help="run queued jobs then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings(args.config)
    worker = JobWorker(settings)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        if args.drain:
            worker.reconcile()
            n = worker.run_pending()
            log.info("drained %d job(s)", n)
        else:
            worker.loop()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
