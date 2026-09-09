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


def write_ledger(path: Path, notes: str = LEDGER_NOTES, cal_file: str = "/tmp/cal_prev.h5") -> Path:
    path.write_text(
        "date_deployed,weights_file,cal_file,n_ant_set,notes\n"
        f'2026-09-04 08:42:33 UTC,/tmp/w.h5 (+ ib_w.h5),{cal_file},4,"{notes}"\n'
    )
    return path


def write_cb_h5(path: Path, antennas=ANTENNAS, n_chan: int = 1501, fmt="int8_snap_weights") -> Path:
    """A CB weights file with the real attrs/datasets the checks read."""
    w = np.zeros((2, n_chan, 1, 4, 64), dtype=np.int8)
    for ant in antennas:
        w[:, :, :, :, ant - 1] = 100
    with h5py.File(path, "w") as f:
        f.attrs["format_type"] = fmt
        f.attrs["n_beams"] = 4
        f.create_dataset("weights_int8", data=w)
        f.create_dataset("array_config/antenna_ids", data=np.arange(1, 73))
        f.create_dataset(
            "array_config/active_mask",
            data=np.array([a in antennas for a in range(1, 73)]),
        )
        f.create_dataset("pointings/alt_deg", data=np.linspace(20, 80, 4))
        f.create_dataset("pointings/az_deg", data=np.linspace(0, 300, 4))
    return path


def write_ib_h5(path: Path, antennas=ANTENNAS, fmt="int8_incoh_bf_weights") -> Path:
    mask = np.zeros((16, 132), dtype=np.uint8)
    for ant in antennas:
        mask[:, ant - 1] = 1
    with h5py.File(path, "w") as f:
        f.attrs["format_type"] = fmt
        f.create_dataset("weights_int8", data=mask)
    return path


@pytest.fixture
def cal_settings(settings: Settings, tmp_path: Path, monkeypatch) -> Settings:
    layout = write_layout(tmp_path / "layout.csv")
    ledger = write_ledger(tmp_path / "deployed_weights.csv", cal_file=str(tmp_path / "prev_cal.h5"))
    (tmp_path / "prev_cal.h5").write_bytes(b"not really hdf5, only its existence is checked")
    cal_settings = dataclasses.replace(
        settings,
        snap_layout_csv=layout,
        deployed_weights_csv=ledger,
        registry_dir=tmp_path / "registry",
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
        "layout": cd.layout_info(cal_settings.layout_csv),
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
        ({"antennas": [9, 99]}, "include_in_beamforming"),
        ({"ref_ant": 44}, "not in the antenna set"),
        ({"source_window": ["2026-09-08 20:00:00", "2026-09-08 19:00:00"]}, "before it starts"),
        ({"source_window": ["not a time", "2026-09-08 19:00:00"]}, "not a UTC timestamp"),
        ({"prev_cal_path": "/nonexistent/cal.h5"}, "does not exist"),
    ],
)
def test_validate_refusals(cal_settings: Settings, over, match):
    with pytest.raises(ParamError, match=match):
        validate(base_params(**over), cal_settings)


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

    result = dep.run_upload(
        {"build_tag": tag, "confirm_tag": tag, "save_defaults": True, "note": "vishnu clicked"}
    )
    assert result["exit_code"] == 0 and result["product_id"] == "abc123"
    assert calls[0][-1] == "--save-defaults" and "--upload" in calls[0]

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        rows = store.uploads(build_tag=tag)
        assert len(rows) == 1
        row = rows[0]
        assert row["note"] == "vishnu clicked" and row["exit_code"] == 0
        assert row["save_defaults"] is True
        assert (row["scale"], row["ib_scale"]) == (8064, 32)
        assert row["command"] == calls[0]
        assert len(row["md5s"]) == 12
        assert row["product_id"] == "abc123"
        events = store.events(kind="weights_uploaded")
        assert events and events[0]["detail"]["tag"] == tag
    finally:
        store.close()


def test_upload_route_status_codes(staged, monkeypatch):
    settings, tag = staged
    with TestClient(create_app(settings)) as client:
        r = client.post(f"/api/cal/builds/{tag}/upload", json={"confirm_tag": tag})
        assert r.status_code == 403 and "CASM_MONITOR_ALLOW_UPLOAD" in r.json()["detail"]

    with TestClient(create_app(allow(settings))) as client:
        bad = client.post(f"/api/cal/builds/{tag}/upload", json={"confirm_tag": "nope"})
        assert bad.status_code == 400
        make_build(settings, tag="cal_unstaged2")
        not_staged = client.post(
            "/api/cal/builds/cal_unstaged2/upload", json={"confirm_tag": "cal_unstaged2"}
        )
        assert not_staged.status_code == 409
        ok = client.post(
            f"/api/cal/builds/{tag}/upload", json={"confirm_tag": tag, "note": "go"}
        )
        assert ok.status_code == 200 and ok.json()["tag"] == tag
        job = client.get(f"/api/jobs/{ok.json()['job_id']}").json()
        assert job["kind"] == "deploy_upload" and job["params"]["confirm_tag"] == tag


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
