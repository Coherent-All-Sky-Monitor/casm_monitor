"""Live hella (T1) search thresholds from /tmp/hella_<job>.cfg.

Format is one ``KEY value`` per line, e.g.::

    OUTPUTPATH /mnt/nvme4/data/casm/hella_cands/cands_2026-09-04-16:42:39.dat.0
    DM_MIN 20
    SNR 15

corr1 runs jobs 0-3 and corr2 jobs 4-7, so the corr1 files are read locally at
the normal cadence and corr2's are pulled once every 10 minutes over ssh
(failures are tolerated: the node may be unreachable and the rest of the strip
must stay live).
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import Any

from ..util import run
from .base import Collector, CollectorContext

NUMERIC_KEYS = ("SNR", "DM_MIN", "DM_MAX", "WIDTH_MIN", "WIDTH_MAX", "NBEAM", "GULP")
_JOB_RE = re.compile(r"hella_(\d+)\.cfg$")


def parse_hella_cfg(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
    return out


def parse_multi(text: str, filenames: list[str] | None = None) -> dict[int, dict[str, str]]:
    """Parse a concatenation of cfg files (``cat /tmp/hella_*.cfg``).

    Jobs are identified by the trailing ``.dat.<job>`` of OUTPUTPATH, which is
    the authoritative job index on both nodes; ``filenames`` is only used as a
    fallback when OUTPUTPATH is absent.
    """
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().startswith("OUTPUT ") and current:
            blocks.append(current)
            current = {}
        parsed = parse_hella_cfg(line)
        current.update(parsed)
    if current:
        blocks.append(current)

    out: dict[int, dict[str, str]] = {}
    for i, block in enumerate(blocks):
        job = None
        path = block.get("OUTPUTPATH", "")
        if "." in path:
            tail = path.rsplit(".", 1)[-1]
            if tail.isdigit():
                job = int(tail)
        if job is None and filenames and i < len(filenames):
            match = _JOB_RE.search(filenames[i])
            if match:
                job = int(match.group(1))
        if job is None:
            job = i
        out[job] = block
    return out


def _summarise(values: list[Any]) -> Any:
    """One value if all jobs agree, else a comma-joined string of the set."""
    uniq = sorted({str(v) for v in values})
    if len(uniq) == 1:
        try:
            return float(uniq[0])
        except ValueError:
            return uniq[0]
    return ",".join(uniq)


class HellaCollector(Collector):
    """corr1 (local files) or corr2 (over ssh) hella thresholds."""

    default_cadence_s = 60.0
    timeout_s = 60.0

    def __init__(self, settings, node: str = "corr1", cadence_s: float | None = None) -> None:
        self.node = node
        self.name = "hella" if node == "corr1" else f"hella_{node}"
        super().__init__(settings, cadence_s)

    def _read(self) -> dict[int, dict[str, str]]:
        if self.node == "corr1":
            paths = sorted(glob.glob("/tmp/hella_*.cfg"))
            text = "".join(Path(p).read_text(errors="replace") for p in paths)
            return parse_multi(text, paths)
        host = self.settings.corr2_ssh
        rc, out, err = run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                host,
                "cat /tmp/hella_*.cfg",
            ],
            timeout=30.0,
        )
        if rc != 0:
            raise RuntimeError(f"ssh {host} hella cfg read failed (rc={rc}): {err.strip()[:200]}")
        return parse_multi(out)

    def collect(self, ctx: CollectorContext) -> None:
        try:
            blocks = self._read()
        except (OSError, RuntimeError) as exc:
            # A missing/unreachable node is reported, not fatal.
            ctx.scalar(f"hella.{self.node}.n_jobs", 0)
            ctx.event(
                "hella_cfg_unavailable",
                severity="warn",
                subject=self.node,
                detail={"error": str(exc)},
            )
            return

        snrs: list[Any] = []
        dm_mins: list[Any] = []
        for job, block in sorted(blocks.items()):
            tags = {"node": self.node, "job": job}
            for key in NUMERIC_KEYS:
                if key in block:
                    try:
                        value: float | str = float(block[key])
                    except ValueError:
                        value = block[key]
                    ctx.scalar(f"hella.{key.lower()}", value, tags=tags)
            if "SNR" in block:
                snrs.append(block["SNR"])
            if "DM_MIN" in block:
                dm_mins.append(block["DM_MIN"])

        ctx.scalar(f"hella.{self.node}.n_jobs", len(blocks))
        if snrs:
            ctx.on_change(
                f"hella.{self.node}.snr",
                _summarise(snrs),
                kind="hella_threshold_change",
                severity="info",
                subject=f"{self.node} SNR",
            )
        if dm_mins:
            ctx.on_change(
                f"hella.{self.node}.dm_min",
                _summarise(dm_mins),
                kind="hella_threshold_change",
                severity="info",
                subject=f"{self.node} DM_MIN",
            )
