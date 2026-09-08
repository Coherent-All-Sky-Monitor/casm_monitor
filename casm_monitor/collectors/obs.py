"""Observation state: LMC daemon status plus the live process command lines.

Two read-only sources, both used, because they answer different questions:

* the LMC XML monitor port (medusa.cfg ``LMC_PORT``, 20300) answers
  ``daemon_status`` with ``daemons_state`` and one entry per area daemon. This
  is the control-plane view: verified live on 2026-09-08, a 5.2 kB reply. Only
  the monitoring command is ever sent — never ``startup``/``shutdown``/
  ``reload``.
* ``ps -eo args`` carries the facts the LMC reply does not contain: the vis
  observation UTC_START (``casm_corr_dump -o .../<UTC_START>.dat``), the
  ``--sub_incoh`` flag and ``--bf_scale_factor`` of ``casm_bfcorr``, and how
  many hella searchers are up.
"""

from __future__ import annotations

import re
import socket
from typing import Any
from xml.etree import ElementTree

from ..config import medusa_endpoints
from ..util import run, utc_start_to_unix
from .base import Collector, CollectorContext

LMC_REQUEST = (
    "<?xml version='1.0' encoding='ISO-8859-1'?>"
    "<lmc_cmd><requestor>casm_monitor</requestor>"
    "<command>daemon_status</command></lmc_cmd>"
)

_UTC_START_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2})")
_HELLA_CFG_RE = re.compile(r"/tmp/hella_(\d+)\.cfg")


def lmc_daemon_status(host: str, port: int, timeout: float = 5.0) -> str:
    """Send the read-only ``daemon_status`` query and return the XML reply.

    The LMC keeps the connection open after replying, so we read until the
    closing tag rather than until EOF.
    """
    with socket.create_connection((host, port), timeout) as sock:
        sock.sendall(LMC_REQUEST.encode("ascii"))
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        while True:
            buf = sock.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            if b"</lmc_reply>" in b"".join(chunks[-2:]):
                break
    return b"".join(chunks).decode("ascii", "replace")


def parse_daemon_status(xml: str) -> dict[str, Any]:
    """-> {'daemons_state': str, 'up': int, 'down': int, 'daemons': {...}}."""
    text = xml[xml.find("<lmc_reply>") :]
    root = ElementTree.fromstring(text)
    state_el = root.find("daemons_state")
    daemons: dict[str, bool] = {}
    for area in root.findall("area"):
        area_name = area.get("name", "?")
        area_id = area.get("id", "-1")
        for daemon in area.findall("daemon"):
            key = f"{area_name}[{area_id}].{daemon.get('name', '?')}"
            daemons[key] = (daemon.text or "").strip().lower() == "true"
    return {
        "daemons_state": (state_el.text or "").strip() if state_el is not None else "unknown",
        "up": sum(1 for v in daemons.values() if v),
        "down": sum(1 for v in daemons.values() if not v),
        "daemons": daemons,
    }


def parse_ps(ps_output: str) -> dict[str, Any]:
    """Pull obs facts out of ``ps -eo args`` text.

    Only the GPU binaries count, never their ``/bin/sh -c numactl ...``
    wrappers nor medusa's python daemons of the same name: a ``casm_bfcorr``
    line must carry ``--bf_out`` and a hella line must be the search binary
    given a ``/tmp/hella_<job>.cfg``. Getting this wrong is what makes
    ``--sub_incoh`` read as off while it is on.
    """
    utc_start: str | None = None
    scale: float | None = None
    n_bfcorr = 0
    n_sub_incoh = 0
    hella_jobs: set[int] = set()
    n_corr_dump = 0
    for line in ps_output.splitlines():
        wrapper = line.startswith("/bin/sh") or " -c numactl" in line
        if "casm_corr_dump" in line and " -o " in line and not wrapper:
            n_corr_dump += 1
            match = _UTC_START_RE.search(line)
            if match:
                utc_start = match.group(1)
        if "casm_bfcorr" in line and "--bf_out" in line and not wrapper:
            n_bfcorr += 1
            if "--sub_incoh" in line:
                n_sub_incoh += 1
            fields = line.split()
            if "--bf_scale_factor" in fields:
                try:
                    scale = float(fields[fields.index("--bf_scale_factor") + 1])
                except (IndexError, ValueError):
                    pass
        hella = _HELLA_CFG_RE.search(line)
        if hella and "casm_hella" in line and not wrapper and ".py" not in line:
            hella_jobs.add(int(hella.group(1)))
    return {
        "utc_start": utc_start,
        # 1 only when every searching bfcorr has the flag, so a partial state
        # is visible as 0 next to n_bfcorr / n_sub_incoh.
        "sub_incoh": None if n_bfcorr == 0 else int(n_sub_incoh == n_bfcorr),
        "n_sub_incoh": n_sub_incoh,
        "bf_scale_factor": scale,
        "n_bfcorr": n_bfcorr,
        "n_hella": len(hella_jobs),
        "n_corr_dump": n_corr_dump,
    }


class ObsCollector(Collector):
    """Observation / control-plane state."""

    name = "obs"
    default_cadence_s = 30.0
    timeout_s = 20.0

    def collect(self, ctx: CollectorContext) -> None:
        endpoints = medusa_endpoints(ctx.settings)

        # -- LMC control plane (read-only monitoring command only) -------
        lmc_ok = 0
        try:
            status = parse_daemon_status(
                lmc_daemon_status(endpoints.lmc_host, endpoints.lmc_port)
            )
            lmc_ok = 1
            ctx.scalar("obs.daemons_up", status["up"])
            ctx.scalar("obs.daemons_down", status["down"])
            ctx.on_change(
                "obs.daemons_state",
                status["daemons_state"],
                kind="obs_daemons_state",
                severity="warn",
                subject="medusa daemons",
            )
        except OSError as exc:
            ctx.scalar("obs.daemons_state", "lmc_unreachable")
            ctx.event(
                "lmc_unreachable",
                severity="warn",
                subject=f"{endpoints.lmc_host}:{endpoints.lmc_port}",
                detail={"error": str(exc)},
            )
        except ElementTree.ParseError as exc:
            ctx.event("lmc_parse_error", severity="warn", detail={"error": str(exc)})
        ctx.scalar("obs.lmc_ok", lmc_ok)

        # -- process view -------------------------------------------------
        _rc, out, _err = run(["ps", "-eo", "args"], timeout=10.0)
        info = parse_ps(out)
        if info["utc_start"]:
            ctx.on_change(
                "obs.utc_start",
                info["utc_start"],
                kind="obs_restart",
                severity="info",
                subject="observation",
                detail={"unix": utc_start_to_unix(info["utc_start"])},
            )
        else:
            ctx.on_change(
                "obs.utc_start",
                "none",
                kind="obs_restart",
                severity="warn",
                subject="observation",
                detail={"note": "no casm_corr_dump process"},
            )
        if info["sub_incoh"] is not None:
            ctx.on_change(
                "obs.sub_incoh",
                info["sub_incoh"],
                kind="sub_incoh_change",
                severity="info",
                subject="bfcorr --sub_incoh",
            )
        if info["bf_scale_factor"] is not None:
            ctx.on_change(
                "obs.bf_scale_factor",
                info["bf_scale_factor"],
                kind="bf_scale_factor_change",
                severity="warn",
                subject="bfcorr --bf_scale_factor",
            )
        ctx.scalar("obs.n_bfcorr", info["n_bfcorr"])
        ctx.scalar("obs.n_sub_incoh", info["n_sub_incoh"])
        ctx.scalar("obs.n_hella", info["n_hella"])
        ctx.scalar("obs.n_corr_dump", info["n_corr_dump"])
