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

Two invariants make the state machine safe against a second worker, a cancel and
an orphan reconciliation racing each other:

* **ownership.** Renew and finish are conditional UPDATEs on ``state='running'
  AND worker=<me> AND lease_until > now``. Zero rows updated means this worker no
  longer owns the job (its lease expired and reconciliation failed it, or another
  worker claimed it): the child is killed and *no* result is written, so a
  cancellation or a reconciliation verdict can never be overwritten. The one
  exception is the cancelled path, which stamps ``finished``/``result`` while
  requiring ``state='cancelled'`` — it records the reason without changing state.
* **containment.** ``jobs_root`` and every per-job directory are resolved and
  refused unless they are inside the store root.
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
from ..store.shards import ensure_contained, safe_name
from .kinds import KINDS, get_kind

log = logging.getLogger("casm_monitor.jobs")

LEASE_S = 60.0
LEASE_RENEW_S = 20.0
POLL_S = 1.0
IDLE_SLEEP_S = 2.0
TERM_GRACE_S = 10.0

# _supervise's verdicts; "lost" is not a job state, it means "someone else owns
# this row now" and nothing is written for it.
STATE_LOST = "lost"


class JobWorker:
    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        self._own_store = store is None
        self.store = store or Store(settings.db_path, store_root=settings.store_root)
        self.store_root = Path(settings.store_root).resolve()
        # Refused at construction, not at the first write: a jobs_root outside
        # the store root is a configuration error, not a runtime surprise.
        self.jobs_root = ensure_contained(settings.jobs_root, self.store_root)
        self.worker_id = f"{os.uname().nodename}:{os.getpid()}"
        self._stop = False
        self._active: tuple[int, subprocess.Popen] | None = None

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        if self._own_store:
            self.store.close()

    def stop(self, *_args: object) -> None:
        """SIGTERM/SIGINT handler: stop looping and let the active job die."""
        self._stop = True

    def reconcile(self) -> list[int]:
        """Fail jobs left ``running`` by a dead worker."""
        ids = self.store.reconcile_orphaned_jobs()
        if ids:
            log.warning("marked orphaned jobs failed: %s", ids)
        return ids

    # -- lease / ownership ----------------------------------------------
    def _renew_lease(self, job_id: int) -> bool:
        """Extend the lease. False = this worker no longer owns the job."""
        now = time.time()
        cur = self.store.execute(
            "UPDATE jobs SET lease_until = ? WHERE id = ? AND state = 'running' "
            "AND worker = ? AND lease_until > ?",
            (now + LEASE_S, job_id, self.worker_id, now),
        )
        return cur.rowcount == 1

    def _finish_owned(self, job_id: int, state: str, result: dict[str, Any]) -> bool:
        """Terminal write, conditional on still owning the job.

        ``done``/``failed`` require the row to still be ours and running.
        ``cancelled`` only fills in ``finished``/``result`` on a row that is
        already cancelled, so the cancellation itself is never rewritten.
        """
        if state not in ("done", "failed", "cancelled"):
            raise ValueError(f"bad terminal state {state!r}")
        now = time.time()
        if state == "cancelled":
            cur = self.store.execute(
                "UPDATE jobs SET finished = ?, lease_until = NULL, result = ? "
                "WHERE id = ? AND state = 'cancelled' AND finished IS NULL AND worker = ?",
                (now, json.dumps(result, default=str), job_id, self.worker_id),
            )
        else:
            cur = self.store.execute(
                "UPDATE jobs SET state = ?, finished = ?, lease_until = NULL, result = ? "
                "WHERE id = ? AND state = 'running' AND worker = ? AND lease_until > ?",
                (state, now, json.dumps(result, default=str), job_id, self.worker_id, now),
            )
        if cur.rowcount == 1:
            return True
        self._ownership_lost(job_id, state)
        return False

    def _ownership_lost(self, job_id: int, intended_state: str) -> None:
        current = self.store.get_job(job_id)
        log.warning(
            "job %s no longer owned by %s (state=%s); not writing %s",
            job_id,
            self.worker_id,
            None if current is None else current["state"],
            intended_state,
        )
        self.store.add_event(
            "job_ownership_lost",
            severity="warn",
            subject=f"job {job_id}",
            detail={
                "job_id": job_id,
                "worker": self.worker_id,
                "intended_state": intended_state,
                "state": None if current is None else current["state"],
            },
        )

    # -- one job --------------------------------------------------------
    def job_dir(self, job_id: int) -> Path:
        """``store_root/jobs/<id>``, resolved and contained."""
        return ensure_contained(self.jobs_root / safe_name(str(int(job_id)), "job id"), self.jobs_root)

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
            self._finish_owned(job_id, "failed", {"reason": "unknown_kind", "error": str(exc)})
            return "failed"

        (jdir / "job.json").write_text(
            json.dumps({"id": job_id, "kind": kind_name, "params": job.get("params") or {}}, indent=1)
        )
        result_path = jdir / "result.json"
        if result_path.exists():
            result_path.unlink()

        started = time.time()
        proc: subprocess.Popen | None = None
        with log_path.open("a") as log_fh:
            log_fh.write(f"=== job {job_id} kind={kind_name} start {time.ctime(started)}\n")
            log_fh.flush()
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "casm_monitor.jobs.run_job", str(jdir)],
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._active = (job_id, proc)
                state, reason = self._supervise(job_id, proc, kind.timeout_s, started)
            finally:
                # Whatever happened (including SIGTERM landing in this thread),
                # the child's process group never outlives the worker.
                if proc is not None and proc.poll() is None:
                    self._kill(proc)
                self._active = None
            log_fh.write(
                f"=== job {job_id} {state} ({reason or 'ok'}) after {time.time() - started:.1f}s\n"
            )

        if state == STATE_LOST:
            # Somebody else owns this row now (cancelled+reclaimed, orphan
            # reconciliation, a second worker): the child is dead and we write
            # nothing but the event.
            self._ownership_lost(job_id, f"lost: {reason}")
            return STATE_LOST

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
        if not self._finish_owned(job_id, state, result):
            return STATE_LOST
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

            if self._stop:
                # systemd is stopping us (or SIGINT): the job dies with the
                # worker and is failed with an explicit reason, never left
                # "running" for the next start to reconcile.
                self._kill(proc)
                return "failed", "worker_stopped"

            now = time.time()
            if now - last_renew >= LEASE_RENEW_S:
                if not self._renew_lease(job_id):
                    self._kill(proc)
                    return STATE_LOST, "lease_lost"
                last_renew = now

            current = self.store.get_job(job_id)
            if current is None:
                self._kill(proc)
                return STATE_LOST, "job row vanished"
            if current["state"] == "cancelled":
                self._kill(proc)
                return "cancelled", "cancelled by request"
            if current["state"] != "running" or current["worker"] != self.worker_id:
                self._kill(proc)
                return STATE_LOST, f"state {current['state']} worker {current['worker']}"

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
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            log.error("job process group %s survived SIGKILL", proc.pid)

    def terminate_active(self) -> None:
        """Kill the job this worker is supervising, if any (shutdown path)."""
        active = self._active
        if active is None:
            return
        job_id, proc = active
        if proc.poll() is None:
            log.warning("worker stopping: killing job %s process group", job_id)
            self._kill(proc)

    # -- main loop ------------------------------------------------------
    def run_pending(self, max_jobs: int | None = None) -> int:
        """Drain the queue (used by tests and by ``--drain``)."""
        done = 0
        while max_jobs is None or done < max_jobs:
            if self._stop:
                break
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
        try:
            while not self._stop:
                job = self.store.claim_next_job(self.worker_id, LEASE_S)
                if job is None:
                    time.sleep(IDLE_SLEEP_S)
                    continue
                log.info("running job %s kind=%s", job["id"], job["kind"])
                state = self.run_job(job)
                log.info("job %s -> %s", job["id"], state)
        finally:
            self.terminate_active()


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
        worker.terminate_active()
        worker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
