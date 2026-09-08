# casm_monitor

One long-running monitoring service for the CASM array on corr1: collectors
that continuously record array state into their own history store, and a web UI
that shows it. Additive by construction — it never touches the medusa /
Fourier-Space flow (read-only Redis PING, group-less Kafka reads, no medusa
restarts, no SNAP programming path).

M0 (this milestone) ships the package, the store, the status strip, the events
table, the job queue and the systemd units. Plan: `docs/plan.md`.

## Layout

    casm_monitor/config.py       settings from config/monitor.yaml (+ env overrides)
    casm_monitor/store/          SQLite (scalars, events, shards, jobs) + zarr shards
    casm_monitor/collectors/     asyncio runner + obs, hella, services, disks,
                                 gpus, weights, sky, store collectors
    casm_monitor/web/            FastAPI app, status strip table, SPA hosting
    casm_monitor/jobs/           durable job queue worker + job kinds
    deploy/systemd/              the three user units + install.sh

## Install

    /home/casm/software/dev/casm_venvs/casm_offline_env/bin/pip install -e . --no-deps

## Run

    casm-monitor-collect          # collectors -> /mnt/nvme3/casm_monitor
    casm-monitor-web              # FastAPI/uvicorn on 127.0.0.1:8060
    casm-monitor-jobs             # single job worker

Config file: `config/monitor.yaml`, overridable with `CASM_MONITOR_CONFIG`;
`CASM_MONITOR_STORE_ROOT`, `CASM_MONITOR_WEB_HOST`, `CASM_MONITOR_WEB_PORT` and
`CASM_MONITOR_ALLOW_UPLOAD` override individual keys (handy for a scratch-store
smoke run).

As services (units are linked but never enabled/started by the script):

    bash deploy/install.sh
    systemctl --user enable --now casm-monitor-collect casm-monitor-web casm-monitor-jobs

## Look at it

The web service binds 127.0.0.1 only, so from your laptop:

    ssh -L 8060:127.0.0.1:8060 casm-corr1

then open <http://127.0.0.1:8060/>. On corr1 itself remember the proxy bypass:

    curl --noproxy 127.0.0.1 http://127.0.0.1:8060/api/status

API: `/api/health`, `/api/status`, `/api/events`, `/api/scalars`, `/api/jobs`
(+ `POST /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel`) and
`WS /ws/status` (status payload every 10 s).

## Tests

    /home/casm/software/dev/casm_venvs/casm_offline_env/bin/python -m pytest -q

Tests use temporary store roots only; nothing under `/mnt` is written.
