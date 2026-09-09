"""Calibration tab (M3): defaults, parameter validation, staging and the
upload gate.

Nothing here builds a real product or touches a correlator node: the cal
driver is never called (the one end-to-end build was run against the live
system by hand), the deploy tool is replaced by a fake runner that writes
plausible DADA files, and every store lives under ``tmp_path``. The synthetic
weights/IB HDF5 files are small but carry the exact attributes and slot layout
the checks read, so the check code under test is the real one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

from casm_monitor import cal_defaults as cd
from casm_monitor.config import Settings
from casm_monitor.jobs import deploy as dep
from casm_monitor.jobs import cal_build as cb
from casm_monitor.jobs.deploy import casm_track_processes as dep_casm_track_processes
from casm_monitor.jobs.cal_build import ParamError, build_dir, validate
from casm_monitor.store import Store
from casm_monitor.web.app import create_app

ANTENNAS = [9, 10, 15, 19]
LEDGER_NOTES = (
    "Uploaded by Vishnu --upload --save-defaults --scale 8064 --ib-scale 32 "
    "(Route Z pairing); defaults md5-verified on both nodes."
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def write_layout(path: Path, antennas=ANTENNAS, wired_extra=(3, 7)) -> Path:
    rows = ["antenna,x,y,z,functional,include_in_beamforming"]
    for ant in sorted(set(antennas) | set(wired_extra)):
        bf = 1 if ant in antennas else 0
        rows.append(f"{ant},0.0,{ant}.0,0.0,1,{bf}")
    path.write_text("\n".join(rows) + "\n")
    return path


def write_ledger(
    path: Path,
    notes: str = LEDGER_NOTES,
    cal_file: str = "/tmp/cal_prev.h5",
    weights_file: str = "/tmp/w.h5",
    ib_file: str | None = "ib_w.h5",
) -> Path:
    cell = weights_file if ib_file is None else f"{weights_file} (+ {ib_file})"
    path.write_text(
        "date_deployed,weights_file,cal_file,n_ant_set,notes\n"
        f'2026-09-04 08:42:33 UTC,{cell},{cal_file},4,"{notes}"\n'
    )
    return path


def write_cb_h5(path: Path, antennas=ANTENNAS, n_chan: int = 1501, fmt="int8_snap_weights") -> Path:
    """A CB weights file with the real attrs/datasets the checks (and the real
    gen_ib_from_cb.py IB generator) read."""
    w = np.zeros((2, n_chan, 1, 4, 64), dtype=np.int8)
    for ant in antennas:
        w[:, :, :, :, ant - 1] = 100
    with h5py.File(path, "w") as f:
        f.attrs["format_type"] = fmt
        f.attrs["n_beams"] = 4
        f.attrs["n_channels"] = n_chan
        f.create_dataset("weights_int8", data=w)
        f.create_dataset("frequencies_hz", data=np.linspace(484.375e6, 400e6, n_chan))
        f.create_dataset("array_config/antenna_ids", data=np.arange(1, 73))
        f.create_dataset(
            "array_config/active_mask",
            data=np.array([a in antennas for a in range(1, 73)]),
        )
        f.create_dataset("pointings/alt_deg", data=np.linspace(20, 80, 4))
        f.create_dataset("pointings/az_deg", data=np.linspace(0, 300, 4))
        freq_grp = f.create_group("freq_config")
        freq_grp.attrs["n_chan"] = n_chan
        freq_grp.attrs["total_bw_mhz"] = 125.0
        freq_grp.attrs["total_n_chan"] = 4096
        freq_grp.attrs["freq_end_voltage_mhz"] = 400.0
    return path


def write_ib_h5(path: Path, antennas=ANTENNAS, fmt="int8_incoh_bf_weights") -> Path:
    mask = np.zeros((16, 132), dtype=np.uint8)
    for ant in antennas:
        mask[:, ant - 1] = 1
    with h5py.File(path, "w") as f:
        f.attrs["format_type"] = fmt
        f.create_dataset("weights_int8", data=mask)
    return path


#: A stand-in for gen_ib_from_cb.py, written into tmp_path and PINNED by the
#: fixture's ``cal_ib_generator_sha256``. Nothing in the suite executes its
#: ``main`` (no test runs a whole cal_build); what is exercised is the
#: path+hash gate that decides whether it may be executed at all.
STUB_IB_GENERATOR = '''"""Stub IB mask generator (tests only)."""


def main(cb_h5, out_h5, bf_scale_factor):
    raise NotImplementedError("the stub generator is never executed")
'''


def write_ib_generator(path: Path, body: str = STUB_IB_GENERATOR) -> Path:
    path.write_text(body)
    return path


@pytest.fixture
def cal_settings(settings: Settings, tmp_path: Path, monkeypatch) -> Settings:
    layout = write_layout(tmp_path / "layout.csv")
    ib_generator = write_ib_generator(tmp_path / "gen_ib_from_cb.py")
    # The "deployed" product cal_defaults reads antennas from: a real CB file
    # (so deployed_cb_antennas() has something to load) paired with a real IB,
    # both populated for the same ANTENNAS as the layout's bf set by default.
    deployed_cb = write_cb_h5(tmp_path / "deployed_weights.h5")
    deployed_ib = write_ib_h5(tmp_path / "deployed_ib.h5")
    ledger = write_ledger(
        tmp_path / "deployed_weights.csv",
        cal_file=str(tmp_path / "prev_cal.h5"),
        weights_file=str(deployed_cb),
        ib_file=deployed_ib.name,
    )
    (tmp_path / "prev_cal.h5").write_bytes(b"not really hdf5, only its existence is checked")
    cal_settings = dataclasses.replace(
        settings,
        snap_layout_csv=layout,
        deployed_weights_csv=ledger,
        registry_dir=tmp_path / "registry",
        cal_ib_generator_script=ib_generator,
        cal_ib_generator_sha256=cd.sha256_file(ib_generator),
    )
    # The job bodies resolve their own Settings from CASM_MONITOR_CONFIG, which
    # in this checkout points at the PRODUCTION store. Pin both of them to the
    # tmp store so no test can reach /mnt/nvme3 or the real ledger.
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: cal_settings)
    monkeypatch.setattr(cb, "load_settings", lambda _p=None: cal_settings)
    return cal_settings


def make_build(cal_settings: Settings, tag: str = "cal_20260908_1951", **over: Any) -> dict[str, Any]:
    """A finished build on disk: summary.json, weights, IB mask, log, figs."""
    out = build_dir(cal_settings, tag)
    (out / "figs").mkdir(parents=True, exist_ok=True)
    weights = write_cb_h5(out / f"weights_{tag}_4ant_4_int8.h5")
    ib = write_ib_h5(out / f"ib_{tag}.h5")
    (out / "figs" / f"rank1_vs_freq_{tag}.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (out / "build.log").write_text("[cal_build] fake log\n")
    # The layout snapshot a real cal_build copies, hashes and solves against:
    # its sha256 IS summary["layout"]["sha256"] (the copy, never the live
    # symlink), and deploy_upload re-hashes it.
    snapshot = out / "layout_snapshot.csv"
    layout = cd.layout_info(cal_settings.layout_csv)
    shutil.copyfile(layout["resolved"], snapshot)
    layout["snapshot"] = str(snapshot)
    layout["live_sha256_at_build"] = layout["sha256"]
    layout["sha256"] = cd.sha256_file(snapshot)
    summary = {
        "tag": tag,
        "kind": "cal_build",
        "created_utc": "2026-09-08T19:00:00Z",
        "finished_utc": "2026-09-08T19:10:00Z",
        "wall_s": 600.0,
        "peak_rss_mb": 4096.0,
        "source": "sun",
        "source_window": ["2026-09-08 19:21:00", "2026-09-08 20:21:00"],
        "static_window": ["2026-09-08 03:00:00", "2026-09-08 03:30:00"],
        "antennas": list(ANTENNAS),
        "n_ant": len(ANTENNAS),
        "ref_ant": 9,
        "layout": layout,
        "paths": {
            "out_dir": str(out),
            "weights_h5": str(weights),
            "ib_h5": str(ib),
            "notebook": None,
            "build_log": str(out / "build.log"),
        },
        "figs": [f"rank1_vs_freq_{tag}.png"],
        "numbers": {"cal": {"rank1_median": 7.5}},
        "rank1_median": 7.5,
        "has_weights": True,
    }
    summary.update(over)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def fake_deploy_runner(stage: Path, payload: bytes = b"\x01\x02\x03\x04", ib=True):
    """Stand-in for ``deploy_bf_weights.py``: writes header+payload DADA files."""
    calls: list[list[str]] = []

    def _run(argv: list[str], timeout: float = 0.0) -> tuple[int, str]:
        calls.append(list(argv))
        if "--upload" not in argv:
            for stream in range(6):
                for name in ("direct.dada",) + (("direct_ib.dada",) if ib else ()):
                    (stage / f"{name}.{stream}").write_bytes(
                        b"UTC_START 2026-09-09-00:00:00\n".ljust(4096, b"\x00")
                        + payload
                        + bytes([stream])
                    )
        return 0, "fake deploy output\nDone.\n"

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ---------------------------------------------------------------------------
# 1. defaults
# ---------------------------------------------------------------------------

def test_sun_altitude_maximum_is_measured(cal_settings: Settings):
    peak = cd.altitude_maximum("sun", "2026-09-08")
    # Independent check on a 10 s grid: the 1-minute grid must land within
    # half a grid step of it, and the altitude must agree to 0.01 deg.
    fine = cd.altitude_maximum("sun", "2026-09-08", step_s=10.0)
    assert abs(peak["t_unix"] - fine["t_unix"]) <= 30.0
    assert abs(peak["alt_deg"] - fine["alt_deg"]) < 0.01
    # The Sun transits OVRO in the late UTC morning; the 2026-09-04 deployed
    # product used 19:22-20:22 UTC for the 2026-09-03 transit.
    assert peak["utc"].startswith("2026-09-08T19:")
    assert 55.0 < peak["alt_deg"] < 62.0


def test_defaults_window_static_and_tag(cal_settings: Settings):
    d = cd.cal_defaults("2026-09-08", cal_settings)
    assert d["source"] == "sun"
    assert [s["name"] for s in d["sources"] if s["enabled"]] == ["sun"]
    assert any(s["name"] == "cyg-a" and not s["enabled"] for s in d["sources"])
    t0, t1 = d["source_window"]
    midnight = cd.parse_date("2026-09-08")
    assert d["alt_max_utc"][:10] == "2026-09-08"
    assert d["sun_max_utc"] == d["alt_max_utc"]
    # window = max +/- 30 min, centred, and the offset is stated
    from datetime import datetime, timezone

    def _u(text):
        return (
            datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )

    assert _u(t1) - _u(t0) == pytest.approx(3600.0)
    assert (_u(t0) + _u(t1)) / 2 == pytest.approx(_u(d["alt_max_utc"]))
    assert d["window_center_offset_min"] == 0.0 and d["window_offset_min"] == 30.0
    assert midnight.timestamp() < _u(t0)
    # static: 03:00-03:30 on the same UTC date (the local night before)
    assert d["static_window"] == ["2026-09-08T03:00:00Z", "2026-09-08T03:30:00Z"]
    assert d["antennas"] == ANTENNAS and d["ref_ant"] == 9
    assert d["grid_mode"] == "exact" and d["n_beams"] == 512
    assert d["tag"] == "cal_20260908_" + d["alt_max_utc"][11:16].replace(":", "")
    assert d["layout"]["n_bf"] == 4 and d["layout"]["n_wired"] == 6
    assert d["deployed"]["scale"] == 8064 and d["deployed"]["ib_scale"] == 32
    assert d["prev_cal_path"] == d["deployed"]["cal_file"]
    # the default set comes from the DEPLOYED CB weights file, not the layout
    # column directly (they agree here because the fixture built them that way)
    assert d["antennas_source"] == "deployed"
    assert d["layout_bf_antennas"] == ANTENNAS
    assert d["deployed_antennas"] == ANTENNAS
    assert d["antennas_mismatch_note"] is None


def test_defaults_antennas_source_deployed_wins_over_layout_mismatch(cal_settings: Settings):
    """2026-09-09 finding: the layout gate and the deployed CB set can differ
    (layout adds/drops antennas without a rebuild). The default set must
    still be what is actually live (deployed), with the mismatch surfaced."""
    write_layout(Path(cal_settings.layout_csv), antennas=ANTENNAS + [22], wired_extra=(3, 7))
    d = cd.cal_defaults("2026-09-08", cal_settings)
    assert d["antennas_source"] == "deployed"
    assert d["antennas"] == ANTENNAS  # the deployed set, unchanged by the layout edit
    assert d["layout_bf_antennas"] == sorted(ANTENNAS + [22])
    assert d["deployed_antennas"] == ANTENNAS
    assert d["antennas_mismatch_note"] and "22" in d["antennas_mismatch_note"]


def test_defaults_falls_back_to_layout_when_deployed_weights_unreadable(cal_settings: Settings):
    write_ledger(
        Path(cal_settings.deployed_weights_csv),
        cal_file=str(Path(cal_settings.deployed_weights_csv).parent / "prev_cal.h5"),
        weights_file="/nonexistent/deployed.h5",
        ib_file=None,
    )
    d = cd.cal_defaults("2026-09-08", cal_settings)
    assert d["antennas_source"] == "layout_fallback"
    assert d["antennas"] == ANTENNAS == d["layout_bf_antennas"]
    assert d["deployed_antennas"] == []
    assert "does not exist" in d["antennas_note"]


def test_deployed_cb_antennas_reads_populated_slots(cal_settings: Settings):
    deployed = cd.deployed_product(cal_settings)
    out = cd.deployed_cb_antennas(deployed["weights_file"])
    assert out["antennas"] == ANTENNAS and out["error"] is None
    assert cd.deployed_cb_antennas(None)["error"] == "no deployed weights file on record"
    missing = cd.deployed_cb_antennas("/nonexistent/x.h5")
    assert missing["antennas"] == [] and "does not exist" in missing["error"]


def test_ref_ant_falls_back_to_lowest_when_9_absent(tmp_path: Path):
    assert cd.default_ref_ant([12, 15, 30]) == 12
    assert cd.default_ref_ant([9, 15]) == 9
    assert cd.default_ref_ant([]) is None


def test_scale_pairing_must_be_parsed_not_guessed(tmp_path: Path):
    row = {"notes": "uploaded with the usual scales"}
    with pytest.raises(ValueError, match="SCALE pairing"):
        cd.parse_scale_pairing(row)
    good = cd.parse_scale_pairing({"notes": LEDGER_NOTES})
    assert (good["scale"], good["ib_scale"]) == (8064, 32)


# ---------------------------------------------------------------------------
# 1b. IB mask generation (gen_ib_from_cb.py, never hand-rolled)
# ---------------------------------------------------------------------------

#: The real production script this service is configured to call by default
#: (Settings.cal_ib_generator_script). Used directly rather than a fake
#: stand-in so the "never hand-roll the mask format" rule is exercised for
#: real: it also requires the real file present on this host.
REAL_IB_GENERATOR = Path("/home/casm/scratch/bf_experiment_v1/scripts/gen_ib_from_cb.py")


def test_generate_ib_mask_pairs_own_build(tmp_path: Path):
    if not REAL_IB_GENERATOR.is_file():
        pytest.skip(f"{REAL_IB_GENERATOR} not present on this host")
    cb_h5 = write_cb_h5(tmp_path / "weights_x_4ant_512_int8.h5", antennas=ANTENNAS)
    out = cb.generate_ib_mask(
        cb_h5, tmp_path, "cal_x", len(ANTENNAS), REAL_IB_GENERATOR,
        cd.sha256_file(REAL_IB_GENERATOR),
    )
    assert out.name == f"ib_cal_x_{len(ANTENNAS)}ant.h5"
    assert out.is_file()
    with h5py.File(out) as f:
        assert f.attrs["format_type"] == "int8_incoh_bf_weights"
        w = f["weights_int8"][:]
    populated = sorted(int(s) for s in np.where((w > 0).any(axis=0))[0])
    assert populated == sorted(a - 1 for a in ANTENNAS)


def test_generate_ib_mask_missing_script_raises(tmp_path: Path):
    cb_h5 = write_cb_h5(tmp_path / "weights.h5")
    with pytest.raises(cb.IBGenerationError, match="does not exist"):
        cb.generate_ib_mask(cb_h5, tmp_path, "cal_x", 4, tmp_path / "no_such_script.py")


def test_generated_ib_mask_matches_deployed_regression(tmp_path: Path):
    """Regenerating the IB mask from the REAL deployed 17-ant CB file with the
    REAL generator is byte-identical, in its ``weights_int8`` dataset, to the
    REAL deployed IB file (md5). Read-only against the production files;
    writes only to tmp_path. This is the check that matters: it is the exact
    pairing casm_monitor now reproduces for every new build."""
    cb_h5 = Path(
        "/mnt/nvme3/vishnu/b0329_build_20260904/weights_b0329_20260904_17ant_512_int8.h5"
    )
    deployed_ib = Path("/mnt/nvme3/vishnu/b0329_build_20260904/ib_b0329_20260904_17ant.h5")
    if not (REAL_IB_GENERATOR.is_file() and cb_h5.is_file() and deployed_ib.is_file()):
        pytest.skip("real b0329 20260904 build products are not present on this host")
    out = cb.generate_ib_mask(
        cb_h5, tmp_path, "regress", 17, REAL_IB_GENERATOR, cd.sha256_file(REAL_IB_GENERATOR)
    )
    with h5py.File(out) as f:
        got = np.asarray(f["weights_int8"][:])
    with h5py.File(deployed_ib) as f:
        want = np.asarray(f["weights_int8"][:])
    assert got.shape == want.shape
    assert hashlib.md5(got.tobytes()).hexdigest() == hashlib.md5(want.tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# 2. parameter validation
# ---------------------------------------------------------------------------

def base_params(**over: Any) -> dict[str, Any]:
    params = {
        "source": "sun",
        "tag": "cal_20260908_1951",
        "source_window": ["2026-09-08 19:21:00", "2026-09-08 20:21:00"],
        "static_window": ["2026-09-08 03:00:00", "2026-09-08 03:30:00"],
        "antennas": list(ANTENNAS),
        "ref_ant": 9,
    }
    params.update(over)
    return params


def test_validate_normalises_iso_windows(cal_settings: Settings):
    """The API speaks ISO-8601 Z; RecipeParams gets the driver's spelling."""
    p = validate(
        base_params(
            source_window=["2026-09-08T19:20:00Z", "2026-09-08T20:20:00Z"],
            static_window=["2026-09-08T03:00:00Z", "2026-09-08T03:30:00Z"],
        ),
        cal_settings,
    )
    assert p["source_window"] == ["2026-09-08 19:20:00", "2026-09-08 20:20:00"]
    assert p["static_window"] == ["2026-09-08 03:00:00", "2026-09-08 03:30:00"]


def test_validate_accepts_and_fixes_policy(cal_settings: Settings):
    p = validate(base_params(), cal_settings)
    assert p["grid_mode"] == "exact" and p["n_beams"] == 512
    assert p["diagnostics"] and p["notebook"] and p["execute_notebook"]
    assert p["prev_cal_path"] == str(Path(cal_settings.deployed_weights_csv).parent / "prev_cal.h5")
    assert p["layout_csv"] == str(cal_settings.layout_csv)


@pytest.mark.parametrize(
    "over, match",
    [
        ({"source": "cyg-a"}, "not enabled"),
        ({"source": ""}, "not enabled"),
        ({"tag": "../escape"}, "tag must match"),
        ({"grid_mode": "bounds"}, "grid_mode is fixed"),
        ({"n_beams": 256}, "n_beams is fixed"),
        ({"diagnostics": False}, "cannot be turned off"),
        ({"antennas": [9, 99]}, "not functional=1"),
        # wired, but neither include_in_beamforming nor in the deployed set
        ({"antennas": [9, 3], "ref_ant": 9}, "neither include_in_beamforming"),
        ({"ref_ant": 44}, "not in the antenna set"),
        ({"source_window": ["2026-09-08 20:00:00", "2026-09-08 19:00:00"]}, "before it starts"),
        ({"source_window": ["not a time", "2026-09-08 19:00:00"]}, "not a UTC timestamp"),
        ({"prev_cal_path": "/nonexistent/cal.h5"}, "prev_cal_path .* does not exist"),
        # tag charset/length: [A-Za-z0-9_.-]{3,64}
        ({"tag": "ab"}, "tag must match"),
        ({"tag": "x" * 65}, "tag must match"),
        ({"tag": "cal 20260908"}, "tag must match"),
        ({"tag": "cal/../escape"}, "tag must match"),
    ],
)
def test_validate_refusals(cal_settings: Settings, over, match):
    with pytest.raises(ParamError, match=match):
        validate(base_params(**over), cal_settings)


def test_validate_refuses_overlapping_windows(cal_settings: Settings):
    """The static template must come from an off-source window: one that
    overlaps the solve carries the source itself."""
    with pytest.raises(ParamError, match="overlap"):
        validate(
            base_params(static_window=["2026-09-08 20:00:00", "2026-09-08 20:40:00"]),
            cal_settings,
        )


def test_validate_refuses_future_and_ancient_windows(cal_settings: Settings):
    from datetime import datetime, timedelta, timezone

    def spell(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    now = datetime.now(timezone.utc)
    future = [spell(now + timedelta(hours=2)), spell(now + timedelta(hours=3))]
    with pytest.raises(ParamError, match="ends in the future"):
        validate(base_params(source_window=future, static_window=None), cal_settings)
    ancient = [spell(now - timedelta(days=45)), spell(now - timedelta(days=45) + timedelta(hours=1))]
    with pytest.raises(ParamError, match="beyond the 30-day limit"):
        validate(base_params(source_window=ancient, static_window=None), cal_settings)


def test_validate_source_is_sun_even_if_config_lists_more(cal_settings: Settings):
    """``cal.sources_enabled`` can narrow the list, never widen it: a config
    that enables cyg-a still does not make it buildable."""
    widened = dataclasses.replace(cal_settings, cal_sources_enabled=("sun", "cyg-a"))
    with pytest.raises(ParamError, match="not enabled"):
        validate(base_params(source="cyg-a"), widened)
    assert validate(base_params(), widened)["source"] == "sun"


def test_validate_allows_a_deployed_antenna_the_layout_no_longer_flags(cal_settings: Settings):
    """The deployed CB set is the other truth: an antenna that is wired and
    populated in the LIVE product stays buildable after the layout column is
    edited (2026-09-09 finding), while a wired antenna in neither set does
    not."""
    # antenna 15 is deployed (the fixture's deployed CB carries ANTENNAS) but
    # dropped from include_in_beamforming here; 3 is wired only.
    write_layout(Path(cal_settings.layout_csv), antennas=[9, 10, 19], wired_extra=(3, 7, 15))
    p = validate(base_params(antennas=[9, 10, 15, 19]), cal_settings)
    assert p["antennas"] == [9, 10, 15, 19]
    with pytest.raises(ParamError, match="neither include_in_beamforming"):
        validate(base_params(antennas=[9, 10, 3], ref_ant=9), cal_settings)


def test_validate_refuses_unpinned_or_moved_ib_generator(cal_settings: Settings, tmp_path: Path):
    """The generator is exec'd in-process, so its path AND its bytes are
    pinned; an edited script, a moved script or a missing pin all refuse."""
    unpinned = dataclasses.replace(cal_settings, cal_ib_generator_sha256="")
    with pytest.raises(ParamError, match="no cal.ib_generator_sha256 pinned"):
        validate(base_params(), unpinned)
    wrong = dataclasses.replace(cal_settings, cal_ib_generator_sha256="0" * 64)
    with pytest.raises(ParamError, match="does not match the pinned"):
        validate(base_params(), wrong)
    # the script itself edited after the pin
    Path(cal_settings.cal_ib_generator_script).write_text("def main(*a):\n    pass\n")
    with pytest.raises(ParamError, match="does not match the pinned"):
        validate(base_params(), cal_settings)
    # and a generator somewhere else entirely
    other = write_ib_generator(tmp_path / "elsewhere.py")
    moved = dataclasses.replace(
        cal_settings, cal_ib_generator_sha256=cd.sha256_file(other)
    )
    assert cb.ib_generator_refusal(other, moved.cal_ib_generator_script, moved.cal_ib_generator_sha256)


def test_build_post_refuses_disabled_source(cal_settings: Settings):
    with TestClient(create_app(cal_settings)) as client:
        r = client.post("/api/cal/build", json=base_params(source="cyg-a"))
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]
        assert client.get("/api/jobs").json()["jobs"] == []


def test_build_post_queues_a_job_then_refuses_a_second(cal_settings: Settings):
    with TestClient(create_app(cal_settings)) as client:
        r = client.post("/api/cal/build", json=base_params())
        assert r.status_code == 200 and r.json()["tag"] == "cal_20260908_1951"
        again = client.post("/api/cal/build", json=base_params(tag="cal_20260908_other"))
        assert again.status_code == 409


def test_defaults_endpoint(cal_settings: Settings):
    with TestClient(create_app(cal_settings)) as client:
        d = client.get("/api/cal/defaults", params={"date": "2026-09-08"}).json()
        assert d["tag"].startswith("cal_20260908_")
        assert d["deployed"]["scale"] == 8064
        assert d["layout"]["sha256"] and d["layout"]["n_bf"] == 4


# ---------------------------------------------------------------------------
# 3. staging
# ---------------------------------------------------------------------------

def test_stage_records_md5s_scale_and_upload_command(cal_settings: Settings, monkeypatch):
    summary = make_build(cal_settings)
    tag = summary["tag"]
    runner = fake_deploy_runner(dep.stage_dir(cal_settings, tag))
    dep.stage_dir(cal_settings, tag).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dep, "_run", runner)

    out = dep.run_stage({"build_tag": tag})
    stage = dep.load_stage(cal_settings, tag)

    assert out["checks_ok"] and stage["checks_ok"]
    # md5s cover all 12 staged files and match the bytes on disk
    assert len(stage["md5s"]) == 12
    for name, md5 in stage["md5s"].items():
        path = Path(stage["stage_dir"]) / name
        assert hashlib.md5(path.read_bytes()).hexdigest() == md5
        assert stage["payload_md5s"][name] == dep.md5_file(path, skip=dep.HDR_SIZE)
    # the pairing comes from the ledger, not from the tool's defaults
    assert (stage["scale"], stage["ib_scale"]) == (8064, 32)
    dry = runner.calls[0]
    assert "--upload" not in dry and "--no-registry" in dry
    assert "--scale" in dry and dry[dry.index("--scale") + 1] == "8064"
    assert dry[dry.index("--ib-scale") + 1] == "32"
    # the recorded upload command is the dry run plus --upload, and NEVER
    # carries --no-registry (an unregistered live upload is forbidden)
    assert stage["upload_command"][-1] == "--upload"
    assert "--no-registry" not in stage["upload_command"]
    assert stage["save_defaults_flag"] == ["--save-defaults"]
    assert stage["upload_command_str"].endswith("--upload")
    # the checks that ran are the format types and both slot counts
    assert {c["name"] for c in stage["checks"]} >= {
        "cb_format_type",
        "cb_populated_slots",
        "ib_format_type",
        "ib_populated_slots",
    }


def test_stage_fails_on_slot_count_mismatch(cal_settings: Settings, monkeypatch):
    """An IB mask that does not cover the CB antenna set is rejected."""
    summary = make_build(cal_settings, tag="cal_mismatch")
    tag = summary["tag"]
    write_ib_h5(Path(summary["paths"]["ib_h5"]), antennas=ANTENNAS[:-1])
    sdir = dep.stage_dir(cal_settings, tag)
    sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dep, "_run", fake_deploy_runner(sdir))
    with pytest.raises(dep.DeployError, match="ib_populated_slots"):
        dep.run_stage({"build_tag": tag})
    stage = dep.load_stage(cal_settings, tag)
    assert stage is not None and stage["checks_ok"] is False


def test_stage_fails_when_scale_pairing_unparseable(cal_settings: Settings, monkeypatch):
    make_build(cal_settings, tag="cal_noscale")
    write_ledger(Path(cal_settings.deployed_weights_csv), notes="deployed as usual")
    monkeypatch.setattr(dep, "_run", fake_deploy_runner(dep.stage_dir(cal_settings, "cal_noscale")))
    with pytest.raises(ValueError, match="SCALE pairing"):
        dep.run_stage({"build_tag": "cal_noscale"})


# ---------------------------------------------------------------------------
# 4. the upload gate
# ---------------------------------------------------------------------------

@pytest.fixture
def staged(cal_settings: Settings, monkeypatch) -> tuple[Settings, str]:
    summary = make_build(cal_settings)
    tag = summary["tag"]
    sdir = dep.stage_dir(cal_settings, tag)
    sdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dep, "_run", fake_deploy_runner(sdir))
    dep.run_stage({"build_tag": tag})
    monkeypatch.setattr(dep, "casm_track_processes", lambda: [])
    return cal_settings, tag


def allow(settings: Settings) -> Settings:
    return dataclasses.replace(settings, allow_upload=True)


def authorize(
    settings: Settings,
    tag: str,
    *,
    confirm_tag: str | None = None,
    save_defaults: bool = False,
    note: str | None = None,
    stage: dict[str, Any] | None = None,
) -> int:
    """Mint the single-use authorization the browser route would mint."""
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        _job_id, auth_id, refusal = store.submit_upload_job_authorized(
            "deploy_upload",
            build_tag=tag,
            confirm_tag=tag if confirm_tag is None else confirm_tag,
            stage_digest=dep.stage_digest(stage or dep.load_stage(settings, tag) or {}),
            note=note,
            save_defaults=save_defaults,
            # the tests mint several authorizations in a row without a worker
            # draining the queue; the pending guard is the route's, not the
            # authorization's
            refuse_if_pending=False,
        )
        assert refusal is None, refusal
        return int(auth_id)
    finally:
        store.close()


def csrf_client(settings: Settings) -> TestClient:
    """A TestClient that has done the CSRF handshake (GET /api/cal/status)."""
    client = TestClient(create_app(settings))
    client.__enter__()
    client.get("/api/cal/status")
    return client


def csrf_headers(client: TestClient) -> dict[str, str]:
    from casm_monitor.web.cal import CSRF_COOKIE, CSRF_HEADER

    return {CSRF_HEADER: client.cookies[CSRF_COOKIE]}


def test_casm_track_processes_matches_exactly(monkeypatch):
    """A real casm-track invocation matches; a bash snapshot merely mentioning
    the string, or ``grep casm-track``, must NOT (2026-09-09: the old
    ``pgrep -af`` substring test flagged a
    ``/bin/bash -c source .../shell-snapshots/...`` line and refused every
    upload)."""
    ps_lines = "\n".join(
        [
            "20001 casm-track spec.json --live",
            "20002 /bin/bash -c source /home/casm/.claude/shell-snapshots/foo.sh casm-track",
            "20003 grep casm-track",
        ]
    )

    def fake_run_cmd(argv, timeout=10.0):
        assert argv[0] == "ps"
        return 0, ps_lines, ""

    monkeypatch.setattr(dep, "run_cmd", fake_run_cmd)
    procs = dep.casm_track_processes()
    assert len(procs) == 1
    assert procs[0].startswith("20001 casm-track")


def test_casm_track_processes_matches_python_module_invocation(monkeypatch):
    """``python -m casm_beam_scheduler...`` and ``python /path/to/casm-track``
    both match (the console script wraps ``casm_beam_scheduler.cli:main``)."""
    ps_lines = "\n".join(
        [
            "30001 /usr/bin/python3.13 -m casm_beam_scheduler.cli spec.json --live",
            "30002 /usr/bin/python3 /home/casm/venv/bin/casm-track spec.json",
            "30003 /usr/bin/python3 -c print('casm-track')",
        ]
    )

    def fake_run_cmd(argv, timeout=10.0):
        return 0, ps_lines, ""

    monkeypatch.setattr(dep, "run_cmd", fake_run_cmd)
    procs = dep.casm_track_processes()
    pids = {p.split(" ", 1)[0] for p in procs}
    assert pids == {"30001", "30002"}


def test_upload_refusal_ignores_bash_snapshot_mentioning_casm_track(staged, monkeypatch):
    """End-to-end: a bash shell-snapshot line containing the string
    ``casm-track`` must not block an upload."""
    settings, tag = staged
    ps_lines = (
        "40001 /bin/bash -c source /home/casm/.claude/shell-snapshots/x.sh casm-track\n"
        "40002 grep casm-track"
    )
    # The ``staged`` fixture stubs casm_track_processes() -> [] outright;
    # restore the real function so ``ps``/argv parsing is actually exercised.
    monkeypatch.setattr(dep, "casm_track_processes", dep_casm_track_processes)
    monkeypatch.setattr(dep, "run_cmd", lambda argv, timeout=10.0: (0, ps_lines, ""))
    assert dep.upload_refusal(allow(settings), tag, tag) is None


def test_upload_refused_when_flag_off(staged):
    settings, tag = staged
    assert "CASM_MONITOR_ALLOW_UPLOAD" in dep.upload_refusal(settings, tag, tag)


def test_upload_refused_on_confirm_tag_mismatch(staged):
    settings, tag = staged
    assert "confirm_tag" in dep.upload_refusal(allow(settings), tag, "cal_something_else")


def test_upload_refused_when_not_staged(cal_settings: Settings):
    make_build(cal_settings, tag="cal_unstaged")
    reason = dep.upload_refusal(allow(cal_settings), "cal_unstaged", "cal_unstaged")
    assert "has not been staged" in reason


def test_upload_refused_on_stale_md5(staged):
    settings, tag = staged
    target = dep.stage_dir(settings, tag) / "direct.dada.0"
    target.write_bytes(target.read_bytes() + b"tampered")
    assert "has changed since the dry run" in dep.upload_refusal(allow(settings), tag, tag)


def test_upload_refused_when_casm_track_running(staged, monkeypatch):
    settings, tag = staged
    monkeypatch.setattr(dep, "casm_track_processes", lambda: ["4242 casm-track --beams 448"])
    assert "casm-track is running" in dep.upload_refusal(allow(settings), tag, tag)


def test_upload_refused_on_scale_pairing_change(staged):
    settings, tag = staged
    write_ledger(
        Path(settings.deployed_weights_csv),
        notes="Uploaded --upload --scale 32 --ib-scale 32 (back to the default pairing)",
    )
    assert "SCALE pairing changed" in dep.upload_refusal(allow(settings), tag, tag)


def test_upload_refused_on_layout_change(staged):
    settings, tag = staged
    write_layout(Path(settings.layout_csv), antennas=ANTENNAS + [22])
    assert "antenna layout changed" in dep.upload_refusal(allow(settings), tag, tag)


def test_upload_refused_when_stage_checks_failed(staged):
    settings, tag = staged
    path = dep.stage_json_path(settings, tag)
    stage = json.loads(path.read_text())
    stage["checks_ok"] = False
    stage["checks"] = [{"name": "cb_populated_slots", "ok": False, "detail": "x"}]
    path.write_text(json.dumps(stage))
    assert "failed its checks" in dep.upload_refusal(allow(settings), tag, tag)


def test_upload_writes_audit_row_and_event(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    calls: list[list[str]] = []

    def fake_run(argv, timeout=0.0):
        calls.append(list(argv))
        return 0, "Uploading CB to live pipeline\nOK\nDone.\n"

    monkeypatch.setattr(dep, "_run", fake_run)
    monkeypatch.setattr(
        dep, "ensure_registered", lambda s, stage: {"product_id": "abc123", "registered_here": True}
    )
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)

    auth_id = authorize(settings, tag, save_defaults=True, note="vishnu clicked")
    result = dep.run_upload({"authorization_id": auth_id, "build_tag": tag})
    assert result["exit_code"] == 0 and result["product_id"] == "abc123"
    assert result["registry"] == "ok" and result["payload_mismatch"] is False
    assert calls[0][-1] == "--save-defaults" and "--upload" in calls[0]
    # the argv was REBUILT from the build's own files, not read out of the JSON
    assert calls[0][2] == str(Path(dep.load_stage(settings, tag)["weights_h5"]))
    assert "--no-registry" not in calls[0]

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        rows = store.uploads(build_tag=tag)
        assert len(rows) == 1
        row = rows[0]
        assert row["note"] == "vishnu clicked" and row["exit_code"] == 0
        assert row["save_defaults"] is True and row["state"] == "done"
        assert (row["scale"], row["ib_scale"]) == (8064, 32)
        assert row["command"] == calls[0]
        assert len(row["md5s"]) == 12
        assert row["product_id"] == "abc123" and row["registry"] == "ok"
        assert row["auth_id"] == auth_id
        # the audit row carries every hash the gate bound to
        assert len(row["hashes"]["files_before"]) == 12
        assert set(row["hashes"]["sources"]) == {"weights_h5", "ib_h5"}
        assert row["hashes"]["layout_snapshot_sha256"] == row["hashes"]["layout_sha256"]
        events = store.events(kind="weights_uploaded")
        assert events and events[0]["detail"]["tag"] == tag
        # and the inhibit marker is gone once the upload is over
        assert not dep.inhibit_path(settings).exists()
    finally:
        store.close()


def test_upload_route_status_codes(staged, monkeypatch):
    settings, tag = staged
    with TestClient(create_app(settings)) as client:
        client.get("/api/cal/status")
        r = client.post(
            f"/api/cal/builds/{tag}/upload",
            json={"confirm_tag": tag},
            headers=csrf_headers(client),
        )
        assert r.status_code == 403 and "CASM_MONITOR_ALLOW_UPLOAD" in r.json()["detail"]

    client = csrf_client(allow(settings))
    try:
        headers = csrf_headers(client)
        bad = client.post(
            f"/api/cal/builds/{tag}/upload", json={"confirm_tag": "nope"}, headers=headers
        )
        assert bad.status_code == 400
        make_build(settings, tag="cal_unstaged2")
        not_staged = client.post(
            "/api/cal/builds/cal_unstaged2/upload",
            json={"confirm_tag": "cal_unstaged2"},
            headers=headers,
        )
        assert not_staged.status_code == 409
        ok = client.post(
            f"/api/cal/builds/{tag}/upload",
            json={"confirm_tag": tag, "note": "go"},
            headers=headers,
        )
        assert ok.status_code == 200 and ok.json()["tag"] == tag
        job = client.get(f"/api/jobs/{ok.json()['job_id']}").json()
        # the job carries ONLY the authorization id (+ the tag it displays)
        assert job["kind"] == "deploy_upload"
        assert set(job["params"]) == {"authorization_id", "build_tag"}
        assert job["params"]["authorization_id"] == ok.json()["auth_id"]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 4b. the gates the 2026-09-09 security review asked for
# ---------------------------------------------------------------------------

def test_generic_job_route_refuses_privileged_kinds(cal_settings: Settings):
    """POST /api/jobs takes an arbitrary kind and an arbitrary params object;
    the three cal kinds are not reachable through it (finding 1)."""
    from casm_monitor.jobs.kinds import KINDS

    assert {k for k, v in KINDS.items() if v.privileged} == {
        "cal_build",
        "deploy_stage",
        "deploy_upload",
    }
    with TestClient(create_app(allow(cal_settings))) as client:
        for kind in ("deploy_upload", "deploy_stage", "cal_build"):
            r = client.post("/api/jobs", json={"kind": kind, "params": {"build_tag": "x"}})
            assert r.status_code == 403, kind
            assert "privileged" in r.json()["detail"]
        assert client.get("/api/jobs").json()["jobs"] == []
        # an unprivileged kind still submits
        assert client.post("/api/jobs", json={"kind": "noop", "params": {}}).status_code == 200


def test_upload_route_requires_csrf_double_submit(staged):
    settings, tag = staged
    client = csrf_client(allow(settings))
    try:
        no_header = client.post(f"/api/cal/builds/{tag}/upload", json={"confirm_tag": tag})
        assert no_header.status_code == 403 and "CSRF" in no_header.json()["detail"]
        wrong = client.post(
            f"/api/cal/builds/{tag}/upload",
            json={"confirm_tag": tag},
            headers={"X-CSRF-Token": "not-the-cookie"},
        )
        assert wrong.status_code == 403
        assert client.get("/api/jobs").json()["jobs"] == []
    finally:
        client.__exit__(None, None, None)


def test_forged_job_params_cannot_upload(staged, monkeypatch):
    """A job row inserted by hand (or replayed) with plausible params but no
    authorization fails immediately, before anything runs (finding 1)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    ran: list[list[str]] = []
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (ran.append(argv), (0, ""))[1])

    with pytest.raises(dep.DeployError, match="requires an authorization_id"):
        dep.run_upload({"build_tag": tag, "confirm_tag": tag, "save_defaults": True})
    with pytest.raises(dep.DeployError, match="does not exist or has already been consumed"):
        dep.run_upload({"authorization_id": 4242, "build_tag": tag})
    assert ran == []


def test_consumed_authorization_cannot_be_reused(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (0, "Done.\n"))
    monkeypatch.setattr(dep, "ensure_registered", lambda s, st: {"product_id": "p1"})

    auth_id = authorize(settings, tag)
    assert dep.run_upload({"authorization_id": auth_id, "build_tag": tag})["exit_code"] == 0
    with pytest.raises(dep.DeployError, match="already been consumed"):
        dep.run_upload({"authorization_id": auth_id, "build_tag": tag})
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        assert len(store.uploads(build_tag=tag)) == 1
    finally:
        store.close()


def test_authorization_for_another_tag_is_refused(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (0, ""))
    auth_id = authorize(settings, tag)
    with pytest.raises(dep.DeployError, match="but authorization"):
        dep.run_upload({"authorization_id": auth_id, "build_tag": "cal_something_else"})


def test_mutated_stage_json_is_refused(staged, monkeypatch):
    """stage.json is display-only: an edited command, an edited md5 or an
    edited file list all refuse (findings 1, 2, 3)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    ran: list[list[str]] = []
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (ran.append(argv), (0, ""))[1])
    path = dep.stage_json_path(settings, tag)
    original = json.loads(path.read_text())

    # (a) an injected command: the authorization is minted against the ORIGINAL
    # digest, and the argv is rebuilt anyway
    auth_id = authorize(settings, tag, stage=original)
    mutated = dict(original)
    mutated["upload_command"] = list(original["upload_command"]) + ["--save-defaults"]
    mutated["upload_command_str"] = " ".join(mutated["upload_command"])
    path.write_text(json.dumps(mutated))
    with pytest.raises(dep.DeployError, match="is not the one stage.json recorded"):
        dep.run_upload({"authorization_id": auth_id, "build_tag": tag})

    # (b) a shrunken file set (the "empty md5 map" bypass)
    path.write_text(json.dumps({**original, "md5s": {"direct.dada.0": "x" * 32}}))
    with pytest.raises(dep.DeployError, match="an upload requires exactly"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})

    # (c) a foreign weights file
    path.write_text(json.dumps({**original, "weights_h5": "/tmp/evil.h5"}))
    with pytest.raises(dep.DeployError, match="stage.json names"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})

    # (d) the stage restored, but edited AFTER the authorization was minted
    path.write_text(json.dumps(original))
    auth_id = authorize(settings, tag, stage=original)
    tampered = dict(original)
    tampered["md5s"] = {**original["md5s"], "direct.dada.0": "0" * 32}
    path.write_text(json.dumps(tampered))
    with pytest.raises(dep.DeployError, match="has changed since the dry run"):
        dep.run_upload({"authorization_id": auth_id, "build_tag": tag})
    assert ran == []


def test_stage_digest_change_after_authorization_is_refused(staged, monkeypatch):
    """Even a stage that re-passes every check on its own must match the digest
    the human authorized."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (0, ""))
    stage = dep.load_stage(settings, tag)
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        _job, auth_id, _ref = store.submit_upload_job_authorized(
            "deploy_upload",
            build_tag=tag,
            confirm_tag=tag,
            stage_digest="0" * 64,  # what a stale click would carry
        )
    finally:
        store.close()
    with pytest.raises(dep.DeployError, match="changed after this upload was authorized"):
        dep.run_upload({"authorization_id": auth_id, "build_tag": tag})
    assert dep.stage_digest(stage) != "0" * 64


def test_source_hdf5_change_after_staging_is_refused(staged, monkeypatch):
    """The DADA bytes are derived from the HDF5 files the tool re-reads at
    upload time, so those are bound too (finding 3)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    stage = dep.load_stage(settings, tag)
    write_cb_h5(Path(stage["weights_h5"]), antennas=ANTENNAS[:2])
    reason = dep.upload_refusal(settings, tag, tag)
    assert "has changed since the dry run" in reason
    with pytest.raises(dep.DeployError, match="has changed since the dry run"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})


def test_symlinked_build_dir_is_refused(staged, monkeypatch):
    """A build directory that is a symlink to ANOTHER build inside the same
    root resolves to a contained path and passed the old check (finding 4)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    alias = "cal_alias_build"
    (Path(settings.cal_builds_root) / alias).symlink_to(build_dir(settings, tag))
    reason = dep.upload_refusal(settings, alias, alias)
    assert "symlink" in reason
    with pytest.raises(dep.DeployError, match="symlink"):
        dep.run_upload(
            {"authorization_id": authorize(settings, alias, stage=dep.load_stage(settings, tag)),
             "build_tag": alias}
        )


def test_symlinked_staged_file_is_refused(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    sdir = dep.stage_dir(settings, tag)
    target = sdir / "direct.dada.0"
    payload = target.read_bytes()
    elsewhere = Path(settings.cal_builds_root) / "sneaky.dada"
    elsewhere.write_bytes(payload)
    target.unlink()
    target.symlink_to(elsewhere)
    assert "symlink" in dep.upload_refusal(settings, tag, tag)


def test_tag_cross_check_between_url_summary_and_stage(staged, monkeypatch):
    """URL tag == summary tag == stage tag == authorization tag == confirm
    tag; any disagreement refuses (finding 4)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    assert "does not match build_tag" in dep.upload_refusal(settings, tag, "other")

    summary_path = build_dir(settings, tag) / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary_path.write_text(json.dumps({**summary, "tag": "cal_other"}))
    assert "summary.json says tag" in dep.upload_refusal(settings, tag, tag)
    summary_path.write_text(json.dumps(summary))

    stage_path = dep.stage_json_path(settings, tag)
    stage = json.loads(stage_path.read_text())
    stage_path.write_text(json.dumps({**stage, "build_tag": "cal_other"}))
    assert "says build_tag" in dep.upload_refusal(settings, tag, tag)


def test_layout_snapshot_tamper_is_refused(staged):
    """The live layout may still match the recorded hash while the snapshot
    the solve actually used has been edited (finding 5)."""
    settings, tag = staged
    settings = allow(settings)
    snapshot = Path(json.loads((build_dir(settings, tag) / "summary.json").read_text())["layout"]["snapshot"])
    snapshot.write_text(snapshot.read_text() + "99,0.0,99.0,0.0,1,1\n")
    reason = dep.upload_refusal(settings, tag, tag)
    assert "layout snapshot" in reason and "not the recorded" in reason


def test_ps_failure_fails_closed(staged, monkeypatch):
    """A ps that cannot be run or parsed is a refusal, not "nothing is
    running" (finding 6)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "casm_track_processes", dep_casm_track_processes)

    monkeypatch.setattr(dep, "run_cmd", lambda argv, timeout=10.0: (1, "", "ps: cannot open /proc"))
    with pytest.raises(dep.CasmTrackCheckError, match="ps exited 1"):
        dep_casm_track_processes()
    assert "refusing rather than assuming none" in dep.upload_refusal(settings, tag, tag)

    monkeypatch.setattr(dep, "run_cmd", lambda argv, timeout=10.0: (0, '5 sh -c "unbalanced\n', ""))
    with pytest.raises(dep.CasmTrackCheckError, match="could not parse"):
        dep_casm_track_processes()
    assert "could not parse" in dep.upload_refusal(settings, tag, tag)


def test_status_route_reports_a_broken_ps_as_running(cal_settings: Settings, monkeypatch):
    import casm_monitor.web.cal as web_cal

    monkeypatch.setattr(
        web_cal, "casm_track_processes",
        lambda: (_ for _ in ()).throw(dep.CasmTrackCheckError("ps exited 1")),
    )
    with TestClient(create_app(cal_settings)) as client:
        status = client.get("/api/cal/status").json()
        assert status["casm_track_running"] is True
        assert "ps exited 1" in status["casm_track_error"]


def test_upload_holds_the_deploy_lock_and_writes_the_inhibit_marker(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "ensure_registered", lambda s, st: {"product_id": "p"})
    seen: dict[str, Any] = {}

    def fake_run(argv, timeout=0.0):
        store = Store(settings.db_path, store_root=settings.store_root)
        try:
            seen["lock"] = dep.deploy_lock_holder(store)
        finally:
            store.close()
        seen["inhibit"] = dep.inhibit_path(settings).is_file()
        return 0, "Done.\n"

    monkeypatch.setattr(dep, "_run", fake_run)
    dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})
    assert seen["inhibit"] is True
    assert seen["lock"] and tag in seen["lock"]["holder"]
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        assert dep.deploy_lock_holder(store) is None  # released
    finally:
        store.close()
    assert not dep.inhibit_path(settings).exists()


def test_upload_refused_while_the_deploy_lock_is_held(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    ran: list[Any] = []
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (ran.append(argv), (0, ""))[1])
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        assert dep.acquire_deploy_lock(store, "somebody else") is not None
    finally:
        store.close()
    with pytest.raises(dep.DeployError, match="holds the deploy.lock lock"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})
    assert ran == []


def test_audit_row_is_written_before_exec_and_finalised(staged, monkeypatch):
    """The row exists with state 'started' while the tool runs; a tool that
    fails leaves state 'failed' with its exit code (finding 7)."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    during: list[dict[str, Any]] = []

    def fake_run(argv, timeout=0.0):
        store = Store(settings.db_path, read_only=True, store_root=settings.store_root)
        try:
            during.extend(store.uploads(build_tag=tag))
        finally:
            store.close()
        return 3, "ssh: connect to host casm-corr2 port 22: No route to host\n"

    monkeypatch.setattr(dep, "_run", fake_run)
    with pytest.raises(dep.DeployError, match="exited 3"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})
    assert len(during) == 1
    assert during[0]["state"] == "started" and during[0]["exit_code"] is None
    assert during[0]["command"] and len(during[0]["hashes"]["files_before"]) == 12

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        row = store.uploads(build_tag=tag)[0]
        assert row["state"] == "failed" and row["exit_code"] == 3
        assert "No route to host" in row["output_tail"]
    finally:
        store.close()


def test_registry_failure_is_reported_not_swallowed(staged, monkeypatch):
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "_run", lambda argv, timeout=0.0: (0, "Done.\n"))

    def boom(_settings, _stage):
        raise RuntimeError("registry root is read-only")

    monkeypatch.setattr(dep, "ensure_registered", boom)
    with pytest.raises(dep.DeployError, match="LIVE and UNREGISTERED"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        row = store.uploads(build_tag=tag)[0]
        assert row["registry"] == "failed" and row["exit_code"] == 0
        assert row["state"] == "failed"
        errors = store.events(kind="weights_registry_failed")
        assert errors and errors[0]["severity"] == "error"
    finally:
        store.close()


def test_payload_regeneration_mismatch_is_evented(staged, monkeypatch):
    """The tool regenerates the staged files under -o before uploading. A new
    DADA header (UTC_START) is expected; a changed PAYLOAD is not."""
    settings, tag = staged
    settings = allow(settings)
    monkeypatch.setattr(dep, "load_settings", lambda _p=None: settings)
    monkeypatch.setattr(dep, "ensure_registered", lambda s, st: {"product_id": "p"})
    sdir = dep.stage_dir(settings, tag)

    def rewrite(argv, timeout=0.0):
        # header rewritten (fine) AND one payload byte changed (not fine)
        for name in sorted(p.name for p in dep.dada_files(sdir)):
            path = sdir / name
            body = path.read_bytes()
            head = b"UTC_START 2026-09-09-11:11:11\n".ljust(dep.HDR_SIZE, b"\x00")
            tail = body[dep.HDR_SIZE:]
            if name == "direct.dada.3":
                tail = tail + b"!"
            path.write_bytes(head + tail)
        return 0, "Done.\n"

    monkeypatch.setattr(dep, "_run", rewrite)
    with pytest.raises(dep.DeployError, match="differ from the approved ones"):
        dep.run_upload({"authorization_id": authorize(settings, tag), "build_tag": tag})
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        events = store.events(kind="deploy_payload_mismatch")
        assert events and events[0]["severity"] == "error"
        assert list(events[0]["detail"]["drift"]) == ["direct.dada.3"]
        row = store.uploads(build_tag=tag)[0]
        # every other file's payload survived the header rewrite untouched
        assert row["hashes"]["files_after"] != row["hashes"]["files_before"]
        assert len(row["hashes"]["payload_drift"]) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 5. read routes
# ---------------------------------------------------------------------------

def test_build_listing_and_artifacts(staged):
    settings, tag = staged
    with TestClient(create_app(settings)) as client:
        rows = client.get("/api/cal/builds").json()["builds"]
        row = next(r for r in rows if r["tag"] == tag)
        assert row["has_weights"] and row["staged"] and not row["uploaded"]
        assert row["rank1_median"] == 7.5 and row["n_ant"] == 4

        detail = client.get(f"/api/cal/builds/{tag}").json()
        assert detail["summary"]["tag"] == tag
        assert detail["stage"]["scale"] == 8064
        assert detail["uploads"] == []

        png = client.get(f"/api/cal/builds/{tag}/figs/rank1_vs_freq_{tag}.png")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        etag = png.headers["etag"]
        assert client.get(
            f"/api/cal/builds/{tag}/figs/rank1_vs_freq_{tag}.png",
            headers={"If-None-Match": etag},
        ).status_code == 304
        # the stem the build detail publishes resolves to the same file
        assert client.get(f"/api/cal/builds/{tag}/figs/rank1_vs_freq.png").status_code == 200
        assert (
            detail["summary"]["figs"][0]["name"] == "rank1_vs_freq"
        ), detail["summary"]["figs"]
        assert detail["summary"]["figs_files"] == [f"rank1_vs_freq_{tag}.png"]
        assert detail["state"] == "done" and detail["stage"]["command"].endswith("--upload")
        assert len(detail["stage"]["files"]) == 12
        # only figures the summary lists can be served
        assert client.get(f"/api/cal/builds/{tag}/figs/secret.png").status_code == 404
        assert client.get(f"/api/cal/builds/{tag}/notebook").status_code == 404

        log = client.get(f"/api/cal/builds/{tag}/log")
        assert log.status_code == 200 and "fake log" in log.text

        status = client.get("/api/cal/status").json()
        # this app was built with the default (upload disabled) settings
        assert status["allow_upload"] is False
        assert status["ledger_row"]["n_ant_set"] == "4"
        assert status["casm_track_running"] is False


def test_stage_route_submits_job(cal_settings: Settings):
    make_build(cal_settings, tag="cal_stage_route")
    with TestClient(create_app(cal_settings)) as client:
        r = client.post("/api/cal/builds/cal_stage_route/stage", json={})
        assert r.status_code == 200
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["kind"] == "deploy_stage" and job["params"]["build_tag"] == "cal_stage_route"
        again = client.post("/api/cal/builds/cal_stage_route/stage", json={})
        assert again.status_code == 409
        assert client.post("/api/cal/builds/nope/stage", json={}).status_code == 404
