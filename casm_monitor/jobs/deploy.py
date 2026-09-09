"""The ``deploy_stage`` and ``deploy_upload`` jobs.

Both are thin wrappers around ``bf_weights_generator/deploy_bf_weights.py``:
this module builds an argv, runs it and records what happened. It never writes
a DADA byte, an FIFO or a header itself.

``deploy_stage`` runs the tool WITHOUT ``--upload`` (the wiki's step 1
dry-run) into ``store_root/cal_builds/<tag>/stage/``, which is the only
directory outside the store's own trees this service points a foreign tool at,
and it is inside the store root. It records:

* the md5 of every ``direct*.dada`` file it produced (whole file) and the
  header-skipped payload md5 the weights registry keys on;
* the CB/IB SCALE pairing, PARSED from the last row of ``deployed_weights.csv``
  (currently ``--scale 8064 --ib-scale 32``, the Route Z pairing). It is never
  hardcoded and never defaulted: a row whose pairing cannot be read fails the
  job with that message;
* the exact upload command line, so what a human later approves is the string
  they can read, not something rebuilt at click time;
* the checks: ``format_type`` on both HDF5 files, and the populated CB and IB
  slot counts against the build's active antenna set (plan.md M3
  layout-provenance check; a cal/array mismatch produces a near-empty file that
  still passes the driver's own verification, casm-wiki incidents.md
  2026-08-31). Any failing check fails the job, after ``stage.json`` is written
  so the operator can see which one.

``deploy_upload`` is the only code path in this service that can move bytes to
the correlator nodes, and it refuses unless ALL of:

1. ``settings.allow_upload`` (config ``cal.allow_upload`` AND
   ``CASM_MONITOR_ALLOW_UPLOAD=1`` in the unit environment);
2. ``stage.json`` exists, its checks passed, and every staged file still
   hashes to the md5 recorded in it;
3. ``confirm_tag == build_tag`` (the operator typed the tag);
4. no ``casm-track`` process is running;
5. the SCALE pairing in ``stage.json`` still equals the ledger's current one;
6. the build's layout snapshot sha256 still equals the current layout's.

It then runs the recorded command, writes an ``uploads`` audit row (whatever
the exit code), emits ``weights_uploaded``, and makes sure the product is in
the weights registry: the deploy tool's own registry call is not on every
branch (casm-wiki weights-and-deploy.md, incidents.md 2026-09-04), and an
unregistered upload leaves T2/T3 without beam coordinates, so this job checks
for ``registry/products/<id>.json`` and registers it directly when it is
missing.

Decision record for the button itself:
casm-wiki ``decisions/2026-09-09-monitor-upload-button.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..cal_defaults import layout_info, parse_scale_pairing
from ..collectors.weights import read_last_ledger_row
from ..config import Settings, load_settings
from ..store import Store
from ..store.shards import ensure_contained
from ..util import iso, run as run_cmd
from .cal_build import build_dir, load_summary

#: Streams the deploy tool writes: 0-2 on corr1, 3-5 on corr2.
STREAMS = (0, 1, 2, 3, 4, 5)
#: DADA header size; the registry's payload md5 skips exactly this many bytes.
HDR_SIZE = 4096
#: How much of the tool's output is kept in the audit row / stage.json.
OUTPUT_TAIL_CHARS = 4000
DEPLOY_TIMEOUT_S = 600.0


class DeployError(RuntimeError):
    """A stage/upload that must not proceed."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def deploy_script() -> str:
    """Absolute path of ``deploy_bf_weights.py`` in the installed package."""
    from bf_weights_generator import deploy_bf_weights

    return str(Path(deploy_bf_weights.__file__).resolve())


def stage_dir(settings: Settings, tag: str) -> Path:
    return ensure_contained(build_dir(settings, tag) / "stage", build_dir(settings, tag))


def stage_json_path(settings: Settings, tag: str) -> Path:
    return stage_dir(settings, tag) / "stage.json"


def load_stage(settings: Settings, tag: str) -> dict[str, Any] | None:
    path = stage_json_path(settings, tag)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def md5_file(path: str | Path, skip: int = 0) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        if skip:
            fh.seek(skip)
        for chunk in iter(lambda: fh.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def dada_files(directory: Path) -> list[Path]:
    """Staged ``direct.dada.N`` / ``direct_ib.dada.N`` files, in a stable order."""
    return sorted(p for p in directory.glob("direct*.dada.*") if p.is_file())


#: The console script name (``casm_beam_scheduler/pyproject.toml``
#: ``[project.scripts]``): ``casm-track = "casm_beam_scheduler.cli:main"``.
CASM_TRACK_ENTRY = "casm-track"
#: The module a ``python -m ...`` invocation of the same entry point names.
CASM_TRACK_MODULE = "casm_beam_scheduler"
_PYTHON_BASENAME_RE = re.compile(r"python3?(\.\d+)?")


def _is_casm_track_argv(argv: list[str]) -> bool:
    """Exact match, not substring: argv[0]'s basename is the console script,
    or a python interpreter running it by path or by ``-m``.

    A shell (``bash -c 'source .../shell-snapshots/... casm-track ...'``),
    ``grep casm-track`` or an editor with the string in its command line must
    NOT match (2026-09-09: the old ``pgrep -af`` substring test flagged a
    bash snapshot process and refused every upload).
    """
    if not argv:
        return False
    base0 = Path(argv[0]).name
    if base0 == CASM_TRACK_ENTRY:
        return True
    if not _PYTHON_BASENAME_RE.fullmatch(base0):
        return False
    rest = list(argv[1:])
    if "-m" in rest:
        i = rest.index("-m")
        if i + 1 < len(rest) and (
            rest[i + 1] == CASM_TRACK_MODULE or rest[i + 1].startswith(f"{CASM_TRACK_MODULE}.")
        ):
            return True
    for tok in rest:
        if tok.startswith("-"):
            continue
        return Path(tok).name == CASM_TRACK_ENTRY
    return False


def casm_track_processes() -> list[str]:
    """Exact ``casm-track`` process lines from ``ps -eo pid,args``.

    Every line is tokenised with :mod:`shlex` and matched with
    :func:`_is_casm_track_argv`; nothing is matched as a bare substring.
    """
    code, out, _err = run_cmd(["ps", "-eo", "pid=,args="], timeout=10.0)
    if code != 0:
        return []
    mine = str(os.getpid())
    lines: list[str] = []
    for raw in out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        pid, _sep, args = raw.partition(" ")
        if pid == mine or not args.strip():
            continue
        try:
            argv = shlex.split(args)
        except ValueError:
            continue
        if _is_casm_track_argv(argv):
            lines.append(raw)
    return lines


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


def _weights_slots(weights_h5: str, antennas: list[int]) -> dict[str, Any]:
    """Populated CB slots at channel 1500 vs the requested antenna set."""
    import h5py
    import numpy as np

    with h5py.File(weights_h5, "r") as f:
        fmt = str(f.attrs.get("format_type", "")).strip()
        ids = np.asarray(f["array_config/antenna_ids"][:], dtype=int)
        sl = np.abs(np.asarray(f["weights_int8"][:, 1500, :, :, :], dtype=np.int32))
    n_slots = sl.shape[-1]
    populated = sorted(int(i) for i in np.where(sl.max(axis=(0, 1, 2)) > 0)[0])
    expected = sorted(int(i) for i in range(n_slots) if int(ids[i]) in set(antennas))
    return {
        "format_type": fmt,
        "populated_slots": populated,
        "expected_slots": expected,
        "populated_antennas": [int(ids[i]) for i in populated],
        "n_populated": len(populated),
        "n_expected": len(expected),
    }


def _ib_slots(ib_h5: str, antennas: list[int]) -> dict[str, Any]:
    """Populated IB mask slots (``snap*12+adc`` = antenna-1) vs the same set."""
    import h5py
    import numpy as np

    with h5py.File(ib_h5, "r") as f:
        fmt = str(f.attrs.get("format_type", "")).strip()
        w = np.asarray(f["weights_int8"][:], dtype=np.int32)
    populated = sorted(int(i) for i in np.where((w > 0).any(axis=0))[0])
    expected = sorted(int(a) - 1 for a in antennas)
    return {
        "format_type": fmt,
        "populated_slots": populated,
        "expected_slots": expected,
        "n_populated": len(populated),
        "n_expected": len(expected),
        "shape": list(w.shape),
    }


def _checks(weights_h5: str, ib_h5: str | None, antennas: list[int]) -> list[dict[str, Any]]:
    """format_type + populated-slot checks, the ones that can run off the files.

    (a) of casm-wiki ``weights-verification.md`` (the cal-division pointing fit)
    ran inside the build; (b) the as-deployed subband decode can only run after
    an upload; (c) the byte validation is the driver's own test suite. What is
    left for the stage is what the plan asks for here.
    """
    from bf_weights_generator.deploy_bf_weights import FORMAT_CB, FORMAT_IB

    rows: list[dict[str, Any]] = []
    cb = _weights_slots(weights_h5, antennas)
    rows.append(
        {
            "name": "cb_format_type",
            "ok": cb["format_type"] == FORMAT_CB,
            "detail": f"{weights_h5} format_type={cb['format_type']!r} (want {FORMAT_CB!r})",
        }
    )
    rows.append(
        {
            "name": "cb_populated_slots",
            "ok": cb["populated_slots"] == cb["expected_slots"],
            "detail": (
                f"{cb['n_populated']} populated CB slots "
                f"{cb['populated_antennas']} vs {cb['n_expected']} antennas {antennas}"
            ),
            "data": cb,
        }
    )
    if ib_h5:
        ib = _ib_slots(ib_h5, antennas)
        rows.append(
            {
                "name": "ib_format_type",
                "ok": ib["format_type"] == FORMAT_IB,
                "detail": f"{ib_h5} format_type={ib['format_type']!r} (want {FORMAT_IB!r})",
            }
        )
        rows.append(
            {
                "name": "ib_populated_slots",
                "ok": ib["populated_slots"] == ib["expected_slots"],
                "detail": (
                    f"{ib['n_populated']} populated IB slots vs {ib['n_expected']} "
                    f"antennas; the IB mask must cover exactly the CB antenna set or "
                    f"the CB-IB |w| condition breaks (casm-wiki ib-subtraction.md)"
                ),
                "data": ib,
            }
        )
        # CB and IB compared to EACH OTHER, not just each to the requested
        # set: cal_build generates the IB from this build's own CB file, so a
        # disagreement here means the generator ran against the wrong file,
        # not just a stale antenna list (slot = ant_id-1 = packet_idx, the
        # vis-beamforming-conventions.md convention this array uses).
        ib_as_antennas = sorted(s + 1 for s in ib["populated_slots"])
        rows.append(
            {
                "name": "cb_ib_slot_agreement",
                "ok": ib_as_antennas == cb["populated_antennas"],
                "detail": (
                    f"IB slots as antennas {ib_as_antennas} vs CB populated "
                    f"antennas {cb['populated_antennas']}"
                ),
            }
        )
    else:
        rows.append(
            {
                "name": "ib_present",
                "ok": False,
                "detail": (
                    "no IB mask recorded for this build (summary.json "
                    "paths.ib_h5 is empty): cal_build should have generated one "
                    "with gen_ib_from_cb.py (bf_weights_generator/docs/ib_weights.md, "
                    "casm-wiki weights-and-deploy.md step 6) — rebuild before staging."
                ),
            }
        )
    return rows


def _run(argv: list[str], timeout: float = DEPLOY_TIMEOUT_S) -> tuple[int, str]:
    """Run the deploy tool, echoing its output into the job log as it ends."""
    print(f"$ {' '.join(argv)}", flush=True)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout:g}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output, flush=True)
    return int(proc.returncode), output


# ---------------------------------------------------------------------------
# deploy_stage
# ---------------------------------------------------------------------------

def run_stage(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point for ``deploy_stage`` (see module docstring)."""
    settings = load_settings(params.get("config"))
    tag = str(params.get("build_tag") or "").strip()
    if not tag:
        raise DeployError("build_tag is required")
    summary = load_summary(settings, tag)
    if summary is None:
        raise DeployError(f"no completed cal_build with tag {tag!r} (no summary.json)")
    weights_h5 = (summary.get("paths") or {}).get("weights_h5")
    if not weights_h5 or not Path(weights_h5).is_file():
        raise DeployError(f"build {tag} has no weights file to stage")

    ledger = read_last_ledger_row(settings.deployed_weights_csv)
    # Raises with its own message when the pairing cannot be read; the job
    # fails and says so rather than falling back to the documented 32/32.
    pairing = parse_scale_pairing(ledger)
    # cal_build generates the IB companion itself, paired to THIS build's own
    # CB file and antenna set (jobs/cal_build.py generate_ib_mask). The
    # deployed IB is never substituted here: staging it against a different
    # build's CB antenna set is exactly the near-empty-file mismatch this
    # check exists to catch (casm-wiki incidents.md 2026-08-31).
    ib_h5 = (summary.get("paths") or {}).get("ib_h5")
    if ib_h5 and not Path(ib_h5).is_file():
        print(
            f"[stage] IB mask {ib_h5} recorded in the build summary but missing "
            f"on disk; staging CB only",
            flush=True,
        )
        ib_h5 = None

    sdir = stage_dir(settings, tag)
    sdir.mkdir(parents=True, exist_ok=True)
    for old in dada_files(sdir):
        old.unlink()

    base = [
        sys.executable,
        deploy_script(),
        str(weights_h5),
        "-o",
        str(sdir),
        "--scale",
        str(pairing["scale"]),
        "--ib-scale",
        str(pairing["ib_scale"]),
    ]
    if ib_h5:
        base += ["--ib-weights", str(ib_h5)]
    # The dry run must not touch the registry: registering a product that was
    # never uploaded would put a row in the ledger of things that have been
    # live. The upload command below deliberately omits --no-registry
    # ("Never --no-registry live", casm-wiki weights-and-deploy.md).
    stage_cmd = base + ["--no-registry"]
    upload_cmd = base + ["--upload"]

    started = time.time()
    code, output = _run(stage_cmd)

    files = dada_files(sdir)
    md5s = {p.name: md5_file(p) for p in files}
    payload_md5s = {p.name: md5_file(p, skip=HDR_SIZE) for p in files}
    sizes = {p.name: p.stat().st_size for p in files}

    checks = [
        {
            "name": "dry_run_exit_code",
            "ok": code == 0,
            "detail": f"deploy_bf_weights.py (no --upload) exited {code}",
        }
    ]
    expected_cb = {f"direct.dada.{s}" for s in STREAMS}
    checks.append(
        {
            "name": "cb_dada_files",
            "ok": expected_cb <= set(md5s),
            "detail": f"staged {sorted(md5s)}",
        }
    )
    if code == 0:
        checks += _checks(str(weights_h5), str(ib_h5) if ib_h5 else None, list(summary["antennas"]))

    layout_now = layout_info(settings.layout_csv)
    stage = {
        "build_tag": tag,
        "staged_utc": iso(started),
        "stage_dir": str(sdir),
        "weights_h5": str(weights_h5),
        "ib_h5": str(ib_h5) if ib_h5 else None,
        "scale": pairing["scale"],
        "ib_scale": pairing["ib_scale"],
        "scale_source": pairing["source"],
        "ledger_date_deployed": pairing.get("date_deployed"),
        "antennas": summary["antennas"],
        "layout_sha256": (summary.get("layout") or {}).get("sha256"),
        "layout_sha256_now": layout_now.get("sha256"),
        "md5s": md5s,
        "payload_md5s": payload_md5s,
        "sizes": sizes,
        "stage_command": stage_cmd,
        "upload_command": upload_cmd,
        "upload_command_str": " ".join(upload_cmd),
        # --save-defaults is an option of the upload, chosen at click time; it
        # is appended to upload_command verbatim when requested.
        "save_defaults_flag": ["--save-defaults"],
        "save_defaults_default": bool(params.get("save_defaults", False)),
        "exit_code": code,
        "output_tail": _tail(output),
        "checks": checks,
        "checks_ok": all(c["ok"] for c in checks),
        "wall_s": round(time.time() - started, 1),
    }
    stage_json_path(settings, tag).write_text(json.dumps(stage, default=str, indent=1))

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        store.add_event(
            "weights_staged",
            severity="info" if stage["checks_ok"] else "warn",
            subject=tag,
            detail={
                "tag": tag,
                "checks_ok": stage["checks_ok"],
                "failed": [c["name"] for c in checks if not c["ok"]],
                "scale": stage["scale"],
                "ib_scale": stage["ib_scale"],
            },
        )
    finally:
        store.close()

    failed = [c for c in checks if not c["ok"]]
    if failed:
        raise DeployError(
            "stage checks failed: "
            + "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
            + f" (details in {stage_json_path(settings, tag)})"
        )
    return {
        "tag": tag,
        "stage_dir": str(sdir),
        "stage_json": str(stage_json_path(settings, tag)),
        "md5s": md5s,
        "scale": stage["scale"],
        "ib_scale": stage["ib_scale"],
        "upload_command": stage["upload_command_str"],
        "checks_ok": True,
    }


# ---------------------------------------------------------------------------
# deploy_upload
# ---------------------------------------------------------------------------

def upload_refusal(
    settings: Settings, tag: str, confirm_tag: str, *, stage: dict[str, Any] | None = None
) -> str | None:
    """The reason this upload must not run, or None. Pure checks, no side effects."""
    if not settings.allow_upload:
        return (
            "uploads are disabled: set CASM_MONITOR_ALLOW_UPLOAD=1 in the "
            "casm-monitor-web/jobs unit environment and cal.allow_upload: true in "
            "the config, then restart the services"
        )
    if str(confirm_tag) != str(tag):
        return f"confirm_tag {confirm_tag!r} does not match build_tag {tag!r}"
    stage = load_stage(settings, tag) if stage is None else stage
    if stage is None:
        return f"build {tag!r} has not been staged (no stage.json); run the dry run first"
    if not stage.get("checks_ok"):
        failed = [c["name"] for c in stage.get("checks", []) if not c.get("ok")]
        return f"the staged product failed its checks ({', '.join(failed) or 'unknown'})"
    sdir = Path(stage["stage_dir"])
    for name, md5 in (stage.get("md5s") or {}).items():
        path = sdir / name
        if not path.is_file():
            return f"staged file {path} is missing; re-stage before uploading"
        if md5_file(path) != md5:
            return f"staged file {path} has changed since the dry run; re-stage"
    tracks = casm_track_processes()
    if tracks:
        return f"casm-track is running ({tracks[0]}); kill it before deploying weights"
    ledger = read_last_ledger_row(settings.deployed_weights_csv)
    try:
        pairing = parse_scale_pairing(ledger)
    except ValueError as exc:
        return str(exc)
    if (stage.get("scale"), stage.get("ib_scale")) != (pairing["scale"], pairing["ib_scale"]):
        return (
            f"SCALE pairing changed since staging: staged "
            f"{stage.get('scale')}/{stage.get('ib_scale')}, ledger now "
            f"{pairing['scale']}/{pairing['ib_scale']}; re-stage"
        )
    summary = load_summary(settings, tag)
    build_sha = (summary or {}).get("layout", {}).get("sha256")
    now_sha = layout_info(settings.layout_csv).get("sha256")
    if build_sha != now_sha:
        return (
            f"the antenna layout changed since this product was built "
            f"(build sha256 {build_sha}, current {now_sha}); rebuild before deploying"
        )
    return None


def _registry_product_id(stage: dict[str, Any]) -> str | None:
    """Deterministic registry product id of the staged CB payloads."""
    from casm_t2.weights_registry import product_id_from_payloads

    payloads = {
        int(name.rsplit(".", 1)[1]): md5
        for name, md5 in (stage.get("payload_md5s") or {}).items()
        if name.startswith("direct.dada.")
    }
    if len(payloads) != len(STREAMS):
        return None
    return product_id_from_payloads(payloads)


def ensure_registered(settings: Settings, stage: dict[str, Any]) -> dict[str, Any]:
    """Make sure ``registry/products/<id>.json`` exists for what was uploaded.

    The deploy tool is meant to register every upload, but its own call is not
    on every branch (casm-wiki weights-and-deploy.md: "the deploy tool only
    registers uploads once branch b0329-fixed-cells is merged"), and an
    unregistered upload leaves T2/T3 without beam coordinates
    (incidents.md 2026-09-04). So: compute the product id the payloads imply,
    and if the registry does not hold it, record the product and one live event
    per uploaded stream directly.
    """
    from datetime import datetime, timezone

    import h5py
    from casm_t2.weights_registry import Registry

    result: dict[str, Any] = {"product_id": None, "registered_here": False}
    pid = _registry_product_id(stage)
    if pid is None:
        result["error"] = "could not derive a product id from the staged payloads"
        return result
    result["product_id"] = pid
    path = Path(settings.registry_dir) / "products" / f"{pid}.json"
    if path.is_file():
        result["already_registered"] = True
        return result

    payloads = {
        int(name.rsplit(".", 1)[1]): md5
        for name, md5 in (stage.get("payload_md5s") or {}).items()
        if name.startswith("direct.dada.")
    }
    with h5py.File(stage["weights_h5"], "r") as f:
        alt = [float(x) for x in f["pointings/alt_deg"][:]]
        az = [float(x) for x in f["pointings/az_deg"][:]]
    registry = Registry(settings.registry_dir)
    registered = registry.record_product(
        h5_path=str(stage["weights_h5"]),
        stream_md5=payloads,
        alt_deg=alt,
        az_deg=az,
        meta={
            "scale": int(stage["scale"]),
            "ib_scale": int(stage["ib_scale"]),
            "recorded_by": "casm_monitor.deploy_upload",
            "build_tag": stage["build_tag"],
            "streams": sorted(payloads),
        },
    )
    now = datetime.now(timezone.utc)
    for stream in sorted(payloads):
        registry.record_live_event(
            utc=now,
            stream=stream,
            payload_md5=payloads[stream],
            source="upload",
            evidence=f"casm_monitor deploy_upload {stage['build_tag']}",
        )
    result.update(product_id=registered, registered_here=True)
    return result


def run_upload(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point for ``deploy_upload`` (see module docstring)."""
    settings = load_settings(params.get("config"))
    tag = str(params.get("build_tag") or "").strip()
    confirm = str(params.get("confirm_tag") or "").strip()
    note = params.get("note")
    save_defaults = bool(params.get("save_defaults", False))
    if not tag:
        raise DeployError("build_tag is required")

    stage = load_stage(settings, tag)
    refusal = upload_refusal(settings, tag, confirm, stage=stage)
    if refusal is not None:
        raise DeployError(f"upload refused: {refusal}")
    assert stage is not None  # upload_refusal returns a reason when it is None

    argv = list(stage["upload_command"])
    if save_defaults:
        argv += list(stage.get("save_defaults_flag") or ["--save-defaults"])
    print(f"[upload] {tag}: {' '.join(argv)}", flush=True)

    started = time.time()
    code, output = _run(argv)

    registry: dict[str, Any] = {}
    if code == 0:
        try:
            registry = ensure_registered(settings, stage)
        except Exception as exc:  # noqa: BLE001 - never lose the audit row
            registry = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"[upload] registry: {registry}", flush=True)

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        upload_id = store.add_upload(
            tag,
            job_id=params.get("job_id"),
            note=None if note is None else str(note),
            md5s=stage.get("md5s") or {},
            command=argv,
            exit_code=code,
            output_tail=_tail(output),
            product_id=registry.get("product_id"),
            save_defaults=save_defaults,
            scale=stage.get("scale"),
            ib_scale=stage.get("ib_scale"),
            ts=started,
        )
        store.add_event(
            "weights_uploaded",
            severity="info" if code == 0 else "error",
            subject=tag,
            detail={
                "tag": tag,
                "upload_id": upload_id,
                "exit_code": code,
                "save_defaults": save_defaults,
                "product_id": registry.get("product_id"),
                "registered_here": registry.get("registered_here"),
                "scale": stage.get("scale"),
                "ib_scale": stage.get("ib_scale"),
                "note": note,
            },
        )
    finally:
        store.close()

    if code != 0:
        raise DeployError(
            f"deploy_bf_weights.py exited {code}; audit row {upload_id} written. "
            f"Tail: {_tail(output)[-500:]}"
        )
    return {
        "tag": tag,
        "upload_id": upload_id,
        "command": argv,
        "exit_code": code,
        "save_defaults": save_defaults,
        "product_id": registry.get("product_id"),
        "registered_here": registry.get("registered_here"),
        "output_tail": _tail(output)[-1000:],
    }


__all__ = [
    "DeployError",
    "casm_track_processes",
    "dada_files",
    "ensure_registered",
    "load_stage",
    "md5_file",
    "run_stage",
    "run_upload",
    "stage_dir",
    "stage_json_path",
    "upload_refusal",
]
