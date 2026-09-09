"""Read-only SNAP board reader, shipped to zapdos on stdin.

This file never runs on corr1: the job kind pipes it into
``ssh zapdos python3 - <ip> [<ip> ...]``, so nothing is left on zapdos' disk
and zapdos' system python 3.8 (which owns ``casm_f``/``casperfpga``) executes
it. Keep it python 3.8 compatible and free of anything outside the standard
library, numpy and casm_f.

Hard rule (docs/plan.md): **read-only**. The only board calls made here are the
constructor and getters — ``SnapFengine(..., use_microblaze=True)`` (which does
not initialise anything), ``fpga.is_programmed``, ``autocorr.get_new_spectra``,
``input.get_status``, ``eq.get_coeffs``, ``pfb.get_fft_shift``,
``pfb.get_overflow_count``, packetizer BRAM reads and the ``sync`` counters. No
``program_*``, no ``initialize()``, no ``health_sweep``, no ``arm_sync`` /
``sw_sync``, no ``set_coeffs``.

Boards are read strictly in sequence in this one process, each with a
wall-clock budget (default 60 s). The budget is enforced per CALL, hard, with
``signal.alarm``: a KATCP getter that hangs (a half-dead board answering its
socket but not the request) is interrupted after whatever is left of the
board's budget, that board is abandoned with a ``timeout`` error, and the run
moves on to the next one. Without the alarm one hung call could spend the whole
multi-board ssh timeout. Every individual call is also wrapped in
try/except, so a golden-image or half-dead board produces an error string
instead of killing the run.

Output: exactly one ``np.savez_compressed`` archive, written at the very end to
the ORIGINAL file descriptor 1, which is saved and taken away from python
before anything else runs (``sys.stdout`` and fd 1 are redirected to stderr for
the whole run). A stray ``print`` in casperfpga/casm_f therefore cannot land in
the middle of the archive and corrupt it. Logging goes to stderr, which the
caller keeps in the job log.
"""

from __future__ import print_function

import io
import json
import math
import os
import signal
import sys
import time
import traceback

import numpy as np

SCRIPT_VERSION = 1
DEFAULT_BUDGET_S = 60.0
N_INPUTS = 12
N_CHANS = 4096
N_EQ_COEFFS = 512
SYNC_CTRL_LOOPBACK_BIT = 11


def log(msg):
    sys.stderr.write("snap_read_remote: %s\n" % msg)
    sys.stderr.flush()


class CallTimeout(Exception):
    """Raised in the main thread by SIGALRM when one getter overruns."""


def _alarm_handler(signum, frame):
    raise CallTimeout("hard per-call timeout")


def _set_alarm(seconds):
    """Arm (or, with 0, cancel) the per-call alarm; a no-op where unsupported."""
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - posix only in practice
        return
    signal.alarm(int(seconds))


class BoardReader(object):
    """One board, one budget, one dict of results plus its arrays."""

    def __init__(self, ip, budget_s=DEFAULT_BUDGET_S):
        self.ip = ip
        self.budget_s = float(budget_s)
        self.t_start = time.time()
        self.errors = {}
        self.timings = {}
        # Set when a call is killed by the alarm: the rest of THIS board is
        # abandoned (its state is unknown and every further call would likely
        # hang the same way), the next board still gets its full budget.
        self.aborted = False
        self.meta = {
            "ip": ip,
            "programmed": None,
            "acc_len": None,
            "fft_shift": None,
            "overflow_count": None,
            "feng_ids_hw": None,
            "switch_position": [None] * N_INPUTS,
            "pps": {},
            "n_inputs": N_INPUTS,
            "n_chans": N_CHANS,
        }
        self.spectra = np.full((N_INPUTS, N_CHANS), np.nan, dtype=np.float32)
        self.adc_rms = np.full(N_INPUTS, np.nan, dtype=np.float64)
        self.adc_mean = np.full(N_INPUTS, np.nan, dtype=np.float64)
        self.adc_power = np.full(N_INPUTS, np.nan, dtype=np.float64)
        self.eq_coeffs = np.full((N_INPUTS, N_EQ_COEFFS), np.nan, dtype=np.float32)

    # -- helpers --------------------------------------------------------
    def remaining_s(self):
        return self.budget_s - (time.time() - self.t_start)

    def call(self, label, fn):
        """Run one board getter under a hard alarm; record timing/error/result.

        The alarm is set to whatever is left of the board's budget (at least
        one second, since ``alarm`` has second resolution), so the sum of the
        calls can never exceed the budget by more than that rounding.
        """
        if self.aborted:
            self.errors[label] = "board_aborted"
            return None
        remaining = self.remaining_s()
        if remaining <= 0:
            self.errors[label] = "budget_exceeded"
            return None
        limit = max(1, int(math.ceil(remaining)))
        t0 = time.time()
        try:
            _set_alarm(limit)
            value = fn()
        except CallTimeout:
            _set_alarm(0)
            self.aborted = True
            self.errors[label] = "timeout after %ds (board abandoned)" % limit
            self.timings[label] = round(time.time() - t0, 3)
            log("%s %s timed out after %ds; abandoning this board" % (self.ip, label, limit))
            return None
        except Exception as exc:  # a dead/golden board raises anything
            _set_alarm(0)
            self.errors[label] = "%s: %s" % (type(exc).__name__, exc)
            self.timings[label] = round(time.time() - t0, 3)
            log("%s %s failed: %s" % (self.ip, label, self.errors[label]))
            return None
        finally:
            _set_alarm(0)
        self.timings[label] = round(time.time() - t0, 3)
        return value

    # -- the read sequence ----------------------------------------------
    def read(self):
        snap = self.call("connect", self._connect)
        if snap is None:
            return self.result()

        programmed = self.call("is_programmed", lambda: bool(snap.fpga.is_programmed()))
        self.meta["programmed"] = programmed
        # PPS/sync is worth trying on an unprogrammed board too: the relay
        # boards (.59 .68 .69) are not in the PPS status script and we want to
        # record which of these calls answer at all.
        self._read_sync(snap)
        if not programmed:
            # Recorded as an error string, not just a log line: the ingesting
            # side refuses a board that comes back with neither spectra nor a
            # reason, so "why there is no data" has to travel in the archive.
            self.errors["autocorr"] = "skipped: programmed=%s" % (programmed,)
            log("%s not programmed (golden image?): skipping data reads" % self.ip)
            return self.result()

        self._read_autocorr(snap)
        self._read_adc_stats(snap)
        self._read_eq(snap)
        self._read_pfb(snap)
        self._read_feng_ids(snap)
        return self.result()

    def _connect(self):
        from casm_f import snap_fengine

        return snap_fengine.SnapFengine(self.ip, use_microblaze=True)

    def _read_autocorr(self, snap):
        acc_len = self.call("acc_len", snap.autocorr.get_acc_len)
        self.meta["acc_len"] = None if acc_len is None else int(acc_len)
        if not self.meta["acc_len"]:
            # acc_len 0 means this board is not accumulating (the relay boards
            # are like this): every get_new_spectra would then block in
            # _wait_for_acc until its internal timeout, spending ~45 s of the
            # budget to return infinities. Skip the reads and say why.
            self.errors["autocorr"] = "skipped: acc_len=%s" % (self.meta["acc_len"],)
            return
        per_block = int(getattr(snap.autocorr, "n_signals_per_block", 2)) or 2
        n_blocks = N_INPUTS // per_block
        # Single mux core: the blocks are read strictly one after another.
        for block in range(n_blocks):
            spec = self.call(
                "autocorr%d" % block,
                lambda b=block: snap.autocorr.get_new_spectra(signal_block=b),
            )
            if spec is None:
                continue
            spec = np.asarray(spec, dtype=np.float32)
            if spec.ndim != 2 or spec.shape[1] != N_CHANS:
                self.errors["autocorr%d" % block] = "unexpected shape %s" % (spec.shape,)
                continue
            for j in range(spec.shape[0]):
                idx = block * per_block + j
                if idx < N_INPUTS:
                    self.spectra[idx] = spec[j]

    def _read_adc_stats(self, snap):
        got = self.call("input_status", snap.input.get_status)
        if got is None:
            return
        stats = got[0] if isinstance(got, tuple) else got
        for i in range(N_INPUTS):
            self.adc_rms[i] = _as_float(stats.get("rms%02d" % i))
            self.adc_mean[i] = _as_float(stats.get("mean%02d" % i))
            self.adc_power[i] = _as_float(stats.get("power%02d" % i))
            pos = stats.get("switch_position%02d" % i)
            self.meta["switch_position"][i] = None if pos is None else str(pos)

    def _read_eq(self, snap):
        for i in range(N_INPUTS):
            coeffs = self.call("eq%02d" % i, lambda k=i: snap.eq.get_coeffs(k))
            if coeffs is None:
                continue
            arr = np.asarray(coeffs, dtype=np.float32).ravel()
            n = min(arr.size, N_EQ_COEFFS)
            self.eq_coeffs[i, :n] = arr[:n]

    def _read_pfb(self, snap):
        shift = self.call("fft_shift", snap.pfb.get_fft_shift)
        overflow = self.call("overflow_count", snap.pfb.get_overflow_count)
        self.meta["fft_shift"] = None if shift is None else int(shift)
        self.meta["overflow_count"] = None if overflow is None else int(overflow)

    def _read_feng_ids(self, snap):
        self.meta["feng_ids_hw"] = self.call("feng_ids_hw", lambda: _read_feng_ids(snap))

    def _read_sync(self, snap):
        # fs_hz is a constructor attribute, not a board read: the PPS period is
        # counted in these clock ticks (250 MHz on CASM), so the consumer needs
        # it to judge the period rather than assuming a rate.
        self.meta["fs_hz"] = float(getattr(snap, "fs_hz", float("nan")))
        pps = {}
        pps["count_pps"] = _as_int(self.call("count_pps", snap.sync.count_pps))
        pps["count_ext"] = _as_int(self.call("count_ext", snap.sync.count_ext))
        pps["period_pps"] = _as_int(self.call("period_pps", snap.sync.period_pps))
        pps["period"] = _as_int(self.call("period", snap.sync.period))
        ctrl = self.call("sync_ctrl", lambda: snap.sync.read_uint("ctrl"))
        pps["ctrl"] = _as_int(ctrl)
        pps["loopback"] = None if ctrl is None else bool((int(ctrl) >> SYNC_CTRL_LOOPBACK_BIT) & 1)
        self.meta["pps"] = pps

    def result(self):
        self.meta["errors"] = self.errors
        self.meta["timings"] = self.timings
        self.meta["elapsed_s"] = round(time.time() - self.t_start, 3)
        self.meta["ts"] = time.time()
        return self.meta


def _read_feng_ids(snap):
    """feng_ids advertised by the packetizer BRAM (read-only BRAM reads)."""
    p = snap.packetizer
    n = int(p.n_total_blocks)
    ant_chans = np.frombuffer(p.read("ant_chan", n * 4), dtype=">u4")
    flags = np.frombuffer(p.read("flags", n), dtype=">u1")
    valid = np.where(flags & 1)[0]
    return sorted(set(int(ant_chans[i] >> 16) & 0xFFFF for i in valid))


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _take_stdout():
    """Save fd 1, point fd 1 and ``sys.stdout`` at stderr; return the saved fd.

    Everything printed by any library for the rest of the run goes to stderr,
    so the only bytes that ever reach the real stdout are the archive written
    through the returned descriptor.
    """
    try:
        saved = os.dup(1)
        os.dup2(2, 1)
    except OSError:  # pragma: no cover - no fds to juggle (a test harness)
        return None
    sys.stdout = sys.stderr
    return saved


def _write_archive(fd, payload):
    """Write the npz to the saved descriptor (or to ``sys.stdout`` if none)."""
    buf = io.BytesIO()
    np.savez_compressed(buf, **payload)
    data = buf.getvalue()
    if fd is None:  # pragma: no cover - only when fd juggling failed
        out = getattr(sys.stdout, "buffer", sys.stdout)
        out.write(data)
        out.flush()
        return len(data)
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])
    return written


def _restore_stdout(fd):
    """Put the saved descriptor back on fd 1 (tidy for any caller in-process)."""
    if fd is None:
        return
    try:
        os.dup2(fd, 1)
        os.close(fd)
    except OSError:  # pragma: no cover
        pass
    sys.stdout = sys.__stdout__


def main(argv):
    ips = []
    budget_s = DEFAULT_BUDGET_S
    for arg in argv:
        if arg.startswith("--budget="):
            budget_s = float(arg.split("=", 1)[1])
        else:
            ips.append(arg)
    if not ips:
        sys.stderr.write("usage: python3 - [--budget=S] <ip> [<ip> ...]\n")
        return 2

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm_handler)
    stdout_fd = _take_stdout()
    t_start = time.time()
    payload = {}
    boards = {}
    for ip in ips:  # strictly sequential: two board reads never overlap
        log("reading %s (budget %.0f s)" % (ip, budget_s))
        reader = BoardReader(ip, budget_s)
        try:
            meta = reader.read()
        except Exception as exc:  # belt and braces: read() already catches
            traceback.print_exc(file=sys.stderr)
            meta = dict(reader.meta)
            meta["errors"] = {"board": "%s: %s" % (type(exc).__name__, exc)}
            meta["ts"] = time.time()
            meta["elapsed_s"] = round(time.time() - reader.t_start, 3)
        boards[ip] = meta
        payload["%s__spectra" % ip] = reader.spectra
        payload["%s__adc_rms" % ip] = reader.adc_rms
        payload["%s__adc_mean" % ip] = reader.adc_mean
        payload["%s__adc_power" % ip] = reader.adc_power
        payload["%s__eq_coeffs" % ip] = reader.eq_coeffs
        log(
            "%s done in %.1f s (%d call errors)"
            % (ip, meta.get("elapsed_s", -1.0), len(meta.get("errors") or {}))
        )

    payload["meta_json"] = np.array(
        json.dumps(
            {
                # ``version`` is what the ingesting side checks before it trusts
                # anything else in here; ``ips`` is the list it REQUESTED, so a
                # board silently missing from ``boards`` is detectable.
                "version": SCRIPT_VERSION,
                "script_version": SCRIPT_VERSION,
                "ips": ips,
                "requested_ips": ips,
                "budget_s": budget_s,
                "t_start": t_start,
                "t_end": time.time(),
                "boards": boards,
            },
            default=str,
        )
    )
    n_bytes = _write_archive(stdout_fd, payload)
    _restore_stdout(stdout_fd)
    log("wrote %d board(s), %d bytes, in %.1f s" % (len(ips), n_bytes, time.time() - t_start))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
