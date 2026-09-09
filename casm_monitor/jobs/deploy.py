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
2. a single-use ``upload_authorizations`` row, minted by the browser upload
   route in the same transaction as the job and CONSUMED atomically here,
   exists and still matches this stage (``stage_digest``). The job params
   carry only its id: nothing that decides whether bytes move is taken from
   the params (2026-09-09 security review, finding 1);
3. ``stage.json`` exists, its checks passed, it holds EXACTLY the twelve
   ``direct.dada.0-5`` / ``direct_ib.dada.0-5`` files, and every one of them
   plus both source HDF5 files still hashes to what was recorded — re-hashed
   immediately before exec, under the ``deploy.lock`` store lock, so there is
   no check/use gap (finding 3);
4. the URL/summary/stage/authorization/confirm tags are all the same string,
   and no build, stage or staged file is a symlink or resolves outside
   ``cal_builds_root`` (finding 4);
5. no ``casm-track`` process is running — and a ``ps`` that fails or does not
   parse is a REFUSAL, not a "nothing running" (finding 6);
6. the SCALE pairing in ``stage.json`` still equals the ledger's current one;
7. the build's layout snapshot sha256 still equals the current layout's AND
   the snapshot file itself still hashes to what the build recorded
   (finding 5).

The argv is never taken from ``stage.json``: it is REBUILT from canonical,
contained paths (the build dir's own weights/IB HDF5 files, ``-o`` the stage
dir, the scales re-read from the ledger, ``--upload``) and then compared to
the recorded one; a difference is a ``stage_drift`` refusal, and
``--no-registry`` may never appear (finding 2).

It then writes the ``uploads`` audit row with state ``started`` BEFORE exec
(argv and every hash included), runs the tool, re-hashes the payloads it
regenerated, updates the row to ``done``/``failed``, emits
``weights_uploaded``, and makes sure the product is in the weights registry:
the deploy tool's own registry call is not on every branch (casm-wiki
weights-and-deploy.md, incidents.md 2026-09-04), and an unregistered upload
leaves T2/T3 without beam coordinates, so this job checks for
``registry/products/<id>.json`` and registers it directly when it is missing.
A registry failure is recorded (``registry='failed'``), evented at severity
``error`` and reported in the job result, never swallowed (finding 7).

Decision record for the button itself:
casm-wiki ``decisions/2026-09-09-monitor-upload-button.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..cal_defaults import layout_info, parse_scale_pairing, sha256_file
from ..collectors.weights import read_last_ledger_row
from ..config import Settings, load_settings
from ..store import Store
from ..store.shards import ensure_contained, safe_name
from ..util import iso, run as run_cmd
from .cal_build import build_dir, load_summary

#: Streams the deploy tool writes: 0-2 on corr1, 3-5 on corr2.
STREAMS = (0, 1, 2, 3, 4, 5)
#: The EXACT staged file set. Not "at least these": an upload whose stage
#: recorded four files would otherwise bind four files and push twelve.
CB_FILES = tuple(f"direct.dada.{s}" for s in STREAMS)
IB_FILES = tuple(f"direct_ib.dada.{s}" for s in STREAMS)
EXPECTED_FILES = frozenset(CB_FILES + IB_FILES)
#: DADA header size; the registry's payload md5 skips exactly this many bytes.
HDR_SIZE = 4096
#: How much of the tool's output is kept in the audit row / stage.json.
OUTPUT_TAIL_CHARS = 4000
DEPLOY_TIMEOUT_S = 600.0

#: The interprocess upload lock, in the store's ``watermarks`` table, taken
#: with the same compare-and-set pattern as ``snap_read``'s board lock: the
#: UPDATE branch only fires when the stored expiry is in the past, so two
#: uploads (or an upload and anything else that wants exclusivity over the
#: deployed product) can never overlap. Held across the casm-track check AND
#: the upload itself, so nothing can start between them.
LOCK_STREAM = "deploy"
LOCK_KEY = "lock"
LOCK_TTL_S = 1200.0  # longer than DEPLOY_TIMEOUT_S plus hashing, self-healing
#: Marker file written for the duration of an upload. ``casm-track`` lives in
#: another repo we may not change, so this is the flag its operators can test
#: (documented in docs/api-cal.md) before starting an observation.
INHIBIT_DIR = "inhibit"
INHIBIT_FILE = "deploy.active"


class DeployError(RuntimeError):
    """A stage/upload that must not proceed."""

    def __init__(self, message: str, *, code: str = "refused") -> None:
        super().__init__(message)
        #: Short machine-readable reason (``stage_drift``, ``symlink``,
        #: ``tag_mismatch``, ``no_authorization``, ``casm_track`` ...). The
        #: message stays the human-readable one.
        self.code = code


class CasmTrackCheckError(DeployError):
    """The ``casm-track`` check could not be completed, so it fails CLOSED.

    A ``ps`` that exits non-zero or a line that does not tokenise used to be
    read as "nothing is running", which is the one answer the check must never
    invent (2026-09-09 security review, finding 6).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="casm_track_check")


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


# -- containment --------------------------------------------------------

def unresolved_build_dir(settings: Settings, tag: str) -> Path:
    """``cal_builds_root/<tag>`` AS WRITTEN.

    :func:`casm_monitor.jobs.cal_build.build_dir` returns the RESOLVED path
    (``ensure_contained`` resolves), which is precisely what hides a symlinked
    build directory from a symlink check, so the checks below start from this
    one instead.
    """
    return Path(settings.cal_builds_root) / safe_name(str(tag), "build tag")


def no_symlinks_inside(path: str | Path, root: str | Path) -> Path:
    """``path``, guaranteed to be inside ``root`` with no symlink on the way.

    ``ensure_contained`` compares RESOLVED paths, which is exactly what a
    symlink defeats: ``cal_builds/<tag>`` pointing at another build inside the
    same root resolves to a contained path and passes, while the tag the
    operator approved and the bytes that get uploaded belong to two different
    products (2026-09-09 security review, finding 4). So: every component from
    ``root`` down is checked for being a symlink, and the result is still
    required to resolve inside ``root``.
    """
    root_path = Path(root)
    target = Path(path)
    ensure_contained(target, root_path)
    # The walk goes over the path AS WRITTEN, not as resolved: resolving first
    # is exactly what hides the symlink (``cal_builds/alias`` resolves to
    # ``cal_builds/real_tag`` and looks innocent).
    try:
        relative = target.relative_to(root_path)
    except ValueError:
        try:
            relative = target.resolve().relative_to(root_path.resolve())
        except ValueError as exc:  # pragma: no cover - ensure_contained caught it
            raise DeployError(f"{target} is outside {root_path}", code="containment") from exc
    walk = root_path
    for part in relative.parts:
        walk = walk / part
        if walk.is_symlink():
            raise DeployError(
                f"{walk} is a symlink; build, stage and staged files must be real "
                f"paths inside {root_path} (a symlinked build dir would let the "
                f"approved tag and the uploaded bytes come from different products)",
                code="symlink",
            )
    if target.is_symlink():
        raise DeployError(f"{target} is a symlink", code="symlink")
    return target


# -- the deploy lock (same CAS pattern as jobs/snap_read.py) -------------

def new_lock_token() -> str:
    """A lease token no other upload can repeat (a restart reuses pids)."""
    return uuid.uuid4().hex


def acquire_deploy_lock(
    store: Store,
    holder: str,
    *,
    ttl_s: float = LOCK_TTL_S,
    now: float | None = None,
    token: str | None = None,
) -> str | None:
    """Take the deploy lock; the lease token, or None if somebody holds it.

    One statement, one transaction: the UPDATE branch fires only when the
    stored expiry is in the past, so two uploads racing cannot both win.
    """
    t = time.time() if now is None else now
    tok = token or new_lock_token()
    value = json.dumps(
        {"holder": holder, "token": tok, "acquired": t, "expires": t + float(ttl_s)}
    )
    cur = store.execute(
        "INSERT INTO watermarks (stream, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(stream, key) DO UPDATE SET value = excluded.value "
        "WHERE CAST(json_extract(watermarks.value, '$.expires') AS REAL) <= ?",
        (LOCK_STREAM, LOCK_KEY, value, t),
    )
    return tok if cur.rowcount == 1 else None


def release_deploy_lock(store: Store, token: str) -> bool:
    """Release the lock, but only if the stored token is still ours."""
    cur = store.execute(
        "DELETE FROM watermarks WHERE stream = ? AND key = ? "
        "AND json_extract(value, '$.token') = ?",
        (LOCK_STREAM, LOCK_KEY, token),
    )
    return cur.rowcount == 1


def deploy_lock_holder(store: Store, *, now: float | None = None) -> dict[str, Any] | None:
    """The live lock record, or None if free/expired (never raises)."""
    t = time.time() if now is None else now
    value = store.get_watermark(LOCK_STREAM, LOCK_KEY)
    if not isinstance(value, dict):
        return None
    try:
        expires = float(value.get("expires", 0.0))
    except (TypeError, ValueError):
        return None
    return value if expires > t else None


def inhibit_path(settings: Settings) -> Path:
    """``store_root/inhibit/deploy.active`` — the marker an upload holds.

    We cannot change ``casm-track``/``casm_beam_scheduler``, so the inhibit is
    advisory in that direction: this service writes the file for the whole
    duration of an upload and documents it (docs/api-cal.md) so a scheduler
    operator has one path to test before starting an observation.
    """
    return ensure_contained(
        Path(settings.store_root) / INHIBIT_DIR / INHIBIT_FILE, Path(settings.store_root)
    )


@contextmanager
def deploy_inhibit(settings: Settings, detail: dict[str, Any]) -> Iterator[Path]:
    """Write the inhibit marker for the duration of the block."""
    path = inhibit_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**detail, "pid": os.getpid()}, default=str, indent=1))
    try:
        yield path
    finally:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - the marker is advisory
            print(f"[upload] WARNING: could not remove the inhibit marker {path}", flush=True)


def stage_digest(stage: dict[str, Any]) -> str:
    """sha256 over the staged file md5s and the recorded upload command.

    This is what the browser route binds an authorization to and what the
    worker re-derives from the stage.json it reads: an edited stage — a file
    md5 changed, a file added or removed, a flag appended to the command —
    produces a different digest and the upload is refused.
    """
    payload = json.dumps(
        {
            "md5s": sorted((str(k), str(v)) for k, v in (stage.get("md5s") or {}).items()),
            "command": [str(x) for x in (stage.get("upload_command") or [])],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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

    Fails CLOSED: a ``ps`` that exits non-zero, times out or produces a line
    :mod:`shlex` cannot tokenise raises :class:`CasmTrackCheckError` instead of
    returning an empty list. "I could not look" is not "nothing is running"
    (2026-09-09 security review, finding 6).
    """
    code, out, err = run_cmd(["ps", "-eo", "pid=,args="], timeout=10.0)
    if code != 0:
        raise CasmTrackCheckError(
            f"could not check for a running casm-track: ps exited {code} "
            f"({(err or out or '').strip()[:200]}); refusing rather than assuming none"
        )
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
        except ValueError as exc:
            raise CasmTrackCheckError(
                f"could not parse the process line {raw[:120]!r} ({exc}); refusing "
                f"rather than skipping a process that might be casm-track"
            ) from exc
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


def canonical_sources(
    settings: Settings, tag: str, summary: dict[str, Any]
) -> tuple[Path, Path | None]:
    """The build's OWN weights/IB HDF5 files, contained and symlink-free.

    ``summary.json`` names them, but only as names: the path must live inside
    THIS build's directory (which itself must live inside ``cal_builds_root``
    with no symlink component), or the summary could point the deploy tool at
    any file on the node.
    """
    root = Path(settings.cal_builds_root)
    bdir = no_symlinks_inside(unresolved_build_dir(settings, tag), root)
    paths = summary.get("paths") or {}
    raw_weights = paths.get("weights_h5")
    if not raw_weights:
        raise DeployError(f"build {tag} records no weights file", code="no_weights")
    weights = no_symlinks_inside(Path(str(raw_weights)), bdir)
    if not weights.is_file():
        raise DeployError(f"build {tag}'s weights file {weights} is missing", code="no_weights")
    raw_ib = paths.get("ib_h5")
    ib: Path | None = None
    if raw_ib:
        candidate = no_symlinks_inside(Path(str(raw_ib)), bdir)
        ib = candidate if candidate.is_file() else None
    return weights, ib


def canonical_argv(
    settings: Settings,
    tag: str,
    *,
    weights_h5: Path,
    ib_h5: Path | None,
    scale: Any,
    ib_scale: Any,
    upload: bool,
    save_defaults: bool = False,
) -> list[str]:
    """The ONE argv builder for both the dry run and the upload.

    Built from canonical, contained paths and the ledger's scales — never from
    anything read out of ``stage.json`` (2026-09-09 security review, finding
    2). ``--no-registry`` belongs to the dry run only and can never reach an
    upload: an unregistered live product leaves T2/T3 without beam coordinates
    (casm-wiki incidents.md 2026-09-04).
    """
    argv = [
        sys.executable,
        deploy_script(),
        str(weights_h5),
        "-o",
        str(stage_dir(settings, tag)),
        "--scale",
        str(scale),
        "--ib-scale",
        str(ib_scale),
    ]
    if ib_h5 is not None:
        argv += ["--ib-weights", str(ib_h5)]
    argv += ["--upload"] if upload else ["--no-registry"]
    if upload and save_defaults:
        argv += ["--save-defaults"]
    return argv


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
    if str(summary.get("tag") or "") != tag:
        raise DeployError(
            f"build {tag!r}'s summary.json says tag {summary.get('tag')!r}", code="tag_mismatch"
        )
    # cal_build generates the IB companion itself, paired to THIS build's own
    # CB file and antenna set (jobs/cal_build.py generate_ib_mask). The
    # deployed IB is never substituted here: staging it against a different
    # build's CB antenna set is exactly the near-empty-file mismatch this
    # check exists to catch (casm-wiki incidents.md 2026-08-31).
    weights_path, ib_path = canonical_sources(settings, tag, summary)
    weights_h5, ib_h5 = str(weights_path), (str(ib_path) if ib_path else None)
    if (summary.get("paths") or {}).get("ib_h5") and ib_path is None:
        print(
            f"[stage] IB mask {(summary.get('paths') or {}).get('ib_h5')} recorded in "
            f"the build summary but missing on disk; staging CB only",
            flush=True,
        )

    ledger = read_last_ledger_row(settings.deployed_weights_csv)
    # Raises with its own message when the pairing cannot be read; the job
    # fails and says so rather than falling back to the documented 32/32.
    pairing = parse_scale_pairing(ledger)

    sdir = stage_dir(settings, tag)
    sdir.mkdir(parents=True, exist_ok=True)
    no_symlinks_inside(sdir, Path(settings.cal_builds_root))
    for old in dada_files(sdir):
        old.unlink()

    # The dry run must not touch the registry: registering a product that was
    # never uploaded would put a row in the ledger of things that have been
    # live. The upload command deliberately omits --no-registry ("Never
    # --no-registry live", casm-wiki weights-and-deploy.md). Both come from
    # the same builder the upload rebuilds its argv with, so the recorded
    # command and the reconstructed one can be compared byte for byte.
    argv_kw = dict(
        weights_h5=weights_path,
        ib_h5=ib_path,
        scale=pairing["scale"],
        ib_scale=pairing["ib_scale"],
    )
    stage_cmd = canonical_argv(settings, tag, upload=False, **argv_kw)  # type: ignore[arg-type]
    upload_cmd = canonical_argv(settings, tag, upload=True, **argv_kw)  # type: ignore[arg-type]

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
    # EXACTLY the twelve files, not "at least the CB six": the upload binds
    # the recorded set and pushes what the tool regenerates, so a stage that
    # recorded fewer files than it produced would bind fewer than it pushes
    # (2026-09-09 security review, finding 3).
    expected = set(EXPECTED_FILES) if ib_h5 else set(CB_FILES)
    checks.append(
        {
            "name": "cb_dada_files",
            "ok": set(md5s) == expected,
            "detail": f"staged {sorted(md5s)}, expected exactly {sorted(expected)}",
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
        # The SOURCES the tool reads, bound too: the DADA files are derived
        # from these HDF5 files and the tool re-reads them at upload time, so
        # binding only the derived bytes leaves the real inputs mutable
        # (2026-09-09 security review, finding 3).
        "source_md5s": {
            k: v
            for k, v in (
                ("weights_h5", md5_file(weights_h5)),
                ("ib_h5", md5_file(ib_h5) if ib_h5 else None),
            )
            if v is not None
        },
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

def upload_plan(
    settings: Settings,
    tag: str,
    confirm_tag: str,
    *,
    stage: dict[str, Any] | None = None,
    save_defaults: bool = False,
) -> dict[str, Any]:
    """Every upload gate, in one place, raising :class:`DeployError` on any of
    them; returns the argv to run and the hashes it was bound to.

    Pure checks plus hashing — no side effects, so the browser route can call
    it to give the operator a status code and the worker can call it again
    (under the deploy lock, immediately before exec) as the real gate.
    """
    if not settings.allow_upload:
        raise DeployError(
            "uploads are disabled: set CASM_MONITOR_ALLOW_UPLOAD=1 in the "
            "casm-monitor-web/jobs unit environment and cal.allow_upload: true in "
            "the config, then restart the services",
            code="disabled",
        )
    if str(confirm_tag) != str(tag):
        raise DeployError(
            f"confirm_tag {confirm_tag!r} does not match build_tag {tag!r}",
            code="tag_mismatch",
        )
    # Containment FIRST: the build dir, the stage dir and (below) every staged
    # file are real paths under cal_builds_root. A symlinked build dir would
    # otherwise be read straight through into a perfectly consistent-looking
    # summary/stage pair belonging to another product.
    root = Path(settings.cal_builds_root)
    bdir = no_symlinks_inside(unresolved_build_dir(settings, tag), root)
    summary = load_summary(settings, tag)
    if summary is None:
        raise DeployError(f"no completed build {tag!r} (no summary.json)", code="no_build")
    if str(summary.get("tag") or "") != str(tag):
        raise DeployError(
            f"build {tag!r}'s summary.json says tag {summary.get('tag')!r}",
            code="tag_mismatch",
        )
    stage = load_stage(settings, tag) if stage is None else stage
    if stage is None:
        raise DeployError(
            f"build {tag!r} has not been staged (no stage.json); run the dry run first",
            code="not_staged",
        )
    if str(stage.get("build_tag") or "") != str(tag):
        raise DeployError(
            f"stage.json under {tag!r} says build_tag {stage.get('build_tag')!r}",
            code="tag_mismatch",
        )
    if not stage.get("checks_ok"):
        failed = [c["name"] for c in stage.get("checks", []) if not c.get("ok")]
        raise DeployError(
            f"the staged product failed its checks ({', '.join(failed) or 'unknown'})",
            code="checks_failed",
        )

    sdir = no_symlinks_inside(bdir / "stage", root)
    if str(Path(str(stage.get("stage_dir") or ""))) != str(sdir):
        raise DeployError(
            f"stage.json's stage_dir {stage.get('stage_dir')!r} is not this build's "
            f"stage dir {sdir}",
            code="stage_drift",
        )

    # The staged set is EXACTLY the twelve files, all present, all hashing to
    # what the dry run recorded, re-hashed here and now.
    recorded = {str(k): str(v) for k, v in (stage.get("md5s") or {}).items()}
    if set(recorded) != set(EXPECTED_FILES):
        raise DeployError(
            f"the stage records {sorted(recorded)}; an upload requires exactly "
            f"{sorted(EXPECTED_FILES)} — re-stage",
            code="file_set",
        )
    on_disk = {p.name for p in dada_files(sdir)}
    if on_disk != set(EXPECTED_FILES):
        raise DeployError(
            f"the stage directory holds {sorted(on_disk)}, not exactly "
            f"{sorted(EXPECTED_FILES)}; re-stage",
            code="file_set",
        )
    file_md5s: dict[str, str] = {}
    for name in sorted(EXPECTED_FILES):
        path = no_symlinks_inside(sdir / name, root)
        if not path.is_file():
            raise DeployError(
                f"staged file {path} is missing; re-stage before uploading", code="missing_file"
            )
        digest = md5_file(path)
        if digest != recorded[name]:
            raise DeployError(
                f"staged file {path} has changed since the dry run; re-stage",
                code="stale_md5",
            )
        file_md5s[name] = digest

    # The SOURCES the tool re-reads, bound the same way.
    weights_path, ib_path = canonical_sources(settings, tag, summary)
    if ib_path is None:
        raise DeployError(
            f"build {tag!r} has no IB mask on disk; an upload must carry the IB "
            f"companion of its own CB product (casm-wiki ib-subtraction.md)",
            code="no_ib",
        )
    recorded_sources = {str(k): str(v) for k, v in (stage.get("source_md5s") or {}).items()}
    if set(recorded_sources) != {"weights_h5", "ib_h5"}:
        raise DeployError(
            "the stage does not record the md5 of both source HDF5 files "
            "(it predates the byte-binding fix); re-stage before uploading",
            code="no_source_md5s",
        )
    source_md5s = {"weights_h5": md5_file(weights_path), "ib_h5": md5_file(ib_path)}
    for key, path in (("weights_h5", weights_path), ("ib_h5", ib_path)):
        if source_md5s[key] != recorded_sources[key]:
            raise DeployError(
                f"the source file {path} has changed since the dry run "
                f"(md5 {source_md5s[key]}, staged {recorded_sources[key]}); re-stage",
                code="stale_source",
            )
    if str(stage.get("weights_h5") or "") != str(weights_path) or str(
        stage.get("ib_h5") or ""
    ) != str(ib_path):
        raise DeployError(
            f"stage.json names {stage.get('weights_h5')!r}/{stage.get('ib_h5')!r}, "
            f"but this build's own files are {weights_path}/{ib_path}",
            code="stage_drift",
        )

    # casm-track, failing CLOSED (CasmTrackCheckError is a DeployError).
    tracks = casm_track_processes()
    if tracks:
        raise DeployError(
            f"casm-track is running ({tracks[0]}); kill it before deploying weights",
            code="casm_track",
        )

    ledger = read_last_ledger_row(settings.deployed_weights_csv)
    try:
        pairing = parse_scale_pairing(ledger)
    except ValueError as exc:
        raise DeployError(str(exc), code="scale_unreadable") from exc
    if (stage.get("scale"), stage.get("ib_scale")) != (pairing["scale"], pairing["ib_scale"]):
        raise DeployError(
            f"SCALE pairing changed since staging: staged "
            f"{stage.get('scale')}/{stage.get('ib_scale')}, ledger now "
            f"{pairing['scale']}/{pairing['ib_scale']}; re-stage",
            code="scale_changed",
        )

    # Layout: the live file against the hash the build recorded (which is the
    # hash OF THE SNAPSHOT it solved with), and the snapshot itself against
    # the same hash, so neither the live layout nor the snapshot can drift.
    build_layout = (summary.get("layout") or {})
    build_sha = build_layout.get("sha256")
    now_sha = layout_info(settings.layout_csv).get("sha256")
    if build_sha != now_sha:
        raise DeployError(
            f"the antenna layout changed since this product was built "
            f"(build sha256 {build_sha}, current {now_sha}); rebuild before deploying",
            code="layout_changed",
        )
    snapshot = build_layout.get("snapshot")
    if not snapshot:
        raise DeployError(
            f"build {tag!r} records no layout snapshot; rebuild before deploying",
            code="no_layout_snapshot",
        )
    snapshot_path = no_symlinks_inside(Path(str(snapshot)), bdir)
    snapshot_sha = sha256_file(snapshot_path)
    if snapshot_sha != build_sha:
        raise DeployError(
            f"the build's layout snapshot {snapshot_path} now hashes to "
            f"{snapshot_sha}, not the recorded {build_sha}; rebuild before deploying",
            code="layout_snapshot_changed",
        )

    # The argv is REBUILT, then compared to the recorded one.
    argv = canonical_argv(
        settings,
        tag,
        weights_h5=weights_path,
        ib_h5=ib_path,
        scale=pairing["scale"],
        ib_scale=pairing["ib_scale"],
        upload=True,
    )
    recorded_argv = [str(x) for x in (stage.get("upload_command") or [])]
    if argv != recorded_argv:
        raise DeployError(
            f"the upload command reconstructed from this build "
            f"({' '.join(argv)}) is not the one stage.json recorded "
            f"({' '.join(recorded_argv)}); re-stage",
            code="stage_drift",
        )
    if "--no-registry" in argv:
        raise DeployError(
            "--no-registry must never appear in a live upload (an unregistered "
            "product leaves T2/T3 without beam coordinates)",
            code="no_registry_flag",
        )
    if save_defaults:
        argv = argv + ["--save-defaults"]

    return {
        "tag": tag,
        "argv": argv,
        "stage": stage,
        "summary": summary,
        "stage_dir": str(sdir),
        "weights_h5": str(weights_path),
        "ib_h5": str(ib_path),
        "scale": pairing["scale"],
        "ib_scale": pairing["ib_scale"],
        "stage_digest": stage_digest(stage),
        "hashes": {
            "files_before": file_md5s,
            "payload_md5s_staged": dict(stage.get("payload_md5s") or {}),
            "sources": source_md5s,
            "layout_sha256": build_sha,
            "layout_snapshot_sha256": snapshot_sha,
        },
    }


def upload_refusal(
    settings: Settings,
    tag: str,
    confirm_tag: str,
    *,
    stage: dict[str, Any] | None = None,
) -> str | None:
    """The reason this upload must not run, or None (see :func:`upload_plan`)."""
    try:
        upload_plan(settings, tag, confirm_tag, stage=stage)
    except DeployError as exc:
        return str(exc)
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
    """Job entry point for ``deploy_upload`` (see module docstring).

    The params are NOT the gate: the only one that matters is
    ``authorization_id``, and the row it names is consumed atomically here
    (once, ever) and supplies the tag, the confirm tag, the note and the
    ``--save-defaults`` choice. Everything else is re-derived from disk under
    the ``deploy.lock`` store lock, which is held from before the casm-track
    check until after the deploy tool has exited.
    """
    settings = load_settings(params.get("config"))
    raw_auth = params.get("authorization_id")
    try:
        auth_id = int(raw_auth)
    except (TypeError, ValueError) as exc:
        raise DeployError(
            "deploy_upload requires an authorization_id minted by "
            "POST /api/cal/builds/{tag}/upload; a hand-submitted job has none",
            code="no_authorization",
        ) from exc

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        auth = store.consume_upload_authorization(auth_id)
        if auth is None:
            raise DeployError(
                f"upload authorization {auth_id} does not exist or has already been "
                f"consumed; every authorization is single-use",
                code="no_authorization",
            )
        tag = str(auth["build_tag"]).strip()
        confirm = str(auth["confirm_tag"]).strip()
        note = auth.get("note")
        save_defaults = bool(auth.get("save_defaults"))
        params_tag = str(params.get("build_tag") or tag).strip()
        if params_tag != tag:
            raise DeployError(
                f"job params name build_tag {params_tag!r} but authorization "
                f"{auth_id} is for {tag!r}",
                code="tag_mismatch",
            )

        token = acquire_deploy_lock(store, f"deploy_upload {tag} pid {os.getpid()}")
        if token is None:
            holder = deploy_lock_holder(store)
            raise DeployError(
                f"another deploy holds the {LOCK_STREAM}.{LOCK_KEY} lock ({holder}); "
                f"not uploading",
                code="locked",
            )
        try:
            with deploy_inhibit(
                settings, {"tag": tag, "auth_id": auth_id, "started_utc": iso(time.time())}
            ):
                return _upload_locked(
                    settings,
                    store,
                    tag=tag,
                    confirm=confirm,
                    note=None if note is None else str(note),
                    save_defaults=save_defaults,
                    auth_id=auth_id,
                    job_id=params.get("job_id"),
                    stage_digest_authorized=str(auth["stage_digest"]),
                )
        finally:
            release_deploy_lock(store, token)
    finally:
        store.close()


def _upload_locked(
    settings: Settings,
    store: Store,
    *,
    tag: str,
    confirm: str,
    note: str | None,
    save_defaults: bool,
    auth_id: int,
    job_id: Any,
    stage_digest_authorized: str,
) -> dict[str, Any]:
    """The upload proper, with the deploy lock held and the inhibit marker up."""
    stage = load_stage(settings, tag)
    plan = upload_plan(settings, tag, confirm, stage=stage, save_defaults=save_defaults)
    if not hmac.compare_digest(str(plan["stage_digest"]), stage_digest_authorized):
        raise DeployError(
            f"the stage changed after this upload was authorized (digest "
            f"{plan['stage_digest']}, authorized {stage_digest_authorized}); "
            f"re-stage and click Upload again",
            code="stage_drift",
        )
    stage = plan["stage"]
    argv = list(plan["argv"])
    print(f"[upload] {tag}: {' '.join(argv)}", flush=True)

    started = time.time()
    hashes = dict(plan["hashes"])
    hashes["stage_digest"] = plan["stage_digest"]
    # The audit row exists BEFORE the first byte moves: a worker killed mid
    # upload still leaves the record that it might have.
    upload_id = store.add_upload(
        tag,
        job_id=job_id,
        note=note,
        md5s=stage.get("md5s") or {},
        command=argv,
        exit_code=None,
        output_tail=None,
        save_defaults=save_defaults,
        scale=stage.get("scale"),
        ib_scale=stage.get("ib_scale"),
        ts=started,
        state="started",
        auth_id=auth_id,
        registry="pending",
        hashes=hashes,
    )

    code, output = _run(argv)

    # The tool REGENERATES the staged files under -o before uploading, with a
    # fresh UTC_START in each DADA header, so the whole-file md5 legitimately
    # changes while the PAYLOAD (everything past HDR_SIZE, which is what the
    # beamformer consumes and the registry keys on) must not. A payload that
    # moved means the tool uploaded different bytes than the ones the operator
    # approved: it is recorded and evented at severity error.
    after_files: dict[str, str] = {}
    after_payloads: dict[str, str] = {}
    sdir = Path(plan["stage_dir"])
    for name in sorted(EXPECTED_FILES):
        path = sdir / name
        if path.is_file():
            after_files[name] = md5_file(path)
            after_payloads[name] = md5_file(path, skip=HDR_SIZE)
    payload_drift = {
        name: {"staged": md5, "after": after_payloads.get(name)}
        for name, md5 in (stage.get("payload_md5s") or {}).items()
        if after_payloads.get(name) != md5
    }
    hashes = {
        **hashes,
        "files_after": after_files,
        "payload_md5s_after": after_payloads,
        "payload_drift": payload_drift,
    }
    if payload_drift:
        store.add_event(
            "deploy_payload_mismatch",
            severity="error",
            subject=tag,
            detail={
                "tag": tag,
                "upload_id": upload_id,
                "drift": payload_drift,
                "note": (
                    "deploy_bf_weights.py regenerated the staged payloads and the "
                    "regenerated bytes differ from the ones that were approved"
                ),
            },
        )
        print(f"[upload] PAYLOAD MISMATCH after the run: {payload_drift}", flush=True)

    registry: dict[str, Any] = {}
    registry_state = "skipped"
    if code == 0:
        try:
            registry = ensure_registered(
                settings, {**stage, "payload_md5s": after_payloads or stage.get("payload_md5s")}
            )
            registry_state = "failed" if registry.get("error") else "ok"
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            registry = {"error": f"{type(exc).__name__}: {exc}"}
            registry_state = "failed"
        print(f"[upload] registry: {registry}", flush=True)
        if registry_state == "failed":
            # An unregistered live product leaves T2/T3 without beam
            # coordinates (casm-wiki incidents.md 2026-09-04). The upload
            # already happened, so this cannot be undone here: it is an error
            # event, an audit field and a job-result field.
            store.add_event(
                "weights_registry_failed",
                severity="error",
                subject=tag,
                detail={
                    "tag": tag,
                    "upload_id": upload_id,
                    "error": registry.get("error"),
                    "note": (
                        "the weights ARE live but UNREGISTERED: T2/T3 will store no "
                        "beam coordinates until this product is registered"
                    ),
                },
            )

    state = "done" if code == 0 and registry_state != "failed" and not payload_drift else "failed"
    store.update_upload(
        upload_id,
        state=state,
        exit_code=code,
        output_tail=_tail(output),
        product_id=registry.get("product_id"),
        registry=registry_state,
        hashes=hashes,
    )
    store.add_event(
        "weights_uploaded",
        severity="info" if state == "done" else "error",
        subject=tag,
        detail={
            "tag": tag,
            "upload_id": upload_id,
            "auth_id": auth_id,
            "exit_code": code,
            "state": state,
            "save_defaults": save_defaults,
            "product_id": registry.get("product_id"),
            "registered_here": registry.get("registered_here"),
            "registry": registry_state,
            "payload_mismatch": bool(payload_drift),
            "scale": stage.get("scale"),
            "ib_scale": stage.get("ib_scale"),
            "note": note,
        },
    )

    if code != 0:
        raise DeployError(
            f"deploy_bf_weights.py exited {code}; audit row {upload_id} written. "
            f"Tail: {_tail(output)[-500:]}",
            code="tool_failed",
        )
    if registry_state == "failed":
        raise DeployError(
            f"the upload succeeded but the weights registry did not record it "
            f"({registry.get('error')}); audit row {upload_id} says registry=failed. "
            f"The product is LIVE and UNREGISTERED: register it before T2/T3 needs "
            f"its beam coordinates",
            code="registry_failed",
        )
    if payload_drift:
        raise DeployError(
            f"the deploy tool regenerated payload bytes that differ from the "
            f"approved ones ({sorted(payload_drift)}); audit row {upload_id}",
            code="payload_mismatch",
        )
    return {
        "tag": tag,
        "upload_id": upload_id,
        "auth_id": auth_id,
        "command": argv,
        "exit_code": code,
        "save_defaults": save_defaults,
        "product_id": registry.get("product_id"),
        "registered_here": registry.get("registered_here"),
        "registry": registry_state,
        "payload_mismatch": False,
        "output_tail": _tail(output)[-1000:],
    }


__all__ = [
    "CasmTrackCheckError",
    "DeployError",
    "acquire_deploy_lock",
    "canonical_argv",
    "canonical_sources",
    "casm_track_processes",
    "dada_files",
    "deploy_inhibit",
    "deploy_lock_holder",
    "ensure_registered",
    "inhibit_path",
    "load_stage",
    "md5_file",
    "no_symlinks_inside",
    "release_deploy_lock",
    "run_stage",
    "run_upload",
    "stage_dir",
    "stage_digest",
    "stage_json_path",
    "upload_plan",
    "upload_refusal",
]
