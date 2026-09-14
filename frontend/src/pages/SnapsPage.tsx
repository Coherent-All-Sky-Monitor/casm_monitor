import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import BoardSection, { type PanelData } from "../components/snaps/BoardSection";
import InputDetail from "../components/snaps/InputDetail";
import SnapFigureView, { type SnapFigureViewKind } from "../components/snaps/SnapFigureView";
import { useUrlParam } from "../lib/useUrlParam";
import { resolveSince } from "../lib/timeRange";
import { formatAge } from "../lib/snapConstants";
import { orderInputs, relayLine } from "../lib/snapText";
import type { SnapInputSetMode } from "../lib/snapText";
import { formatUnixUtc } from "../lib/statusSentence";
import {
  getJob,
  getSnapBoardRead,
  getSnapBoards,
  getSnapHistory,
  getSnapLive,
  postSnapBoardRead,
} from "../lib/api";
import {
  mockGetJob,
  mockGetSnapBoardRead,
  mockGetSnapBoards,
  mockGetSnapHistory,
  mockGetSnapLive,
  mockPostSnapBoardRead,
} from "../lib/mockSnaps";
import { emitError } from "../lib/toast";
import type {
  SnapBoardInfo,
  SnapBoardReadResponse,
  SnapHistoryResponse,
  SnapLiveResponse,
} from "../lib/types";

const LIVE_POLL_MS = 10_000;
const PANEL_NCHAN = 768;
const JOB_POLL_MS = 2_000;
/** Per-input cap for the history grid: 12 inputs x 4 boards are fetched at
 * once, so ask the server for a coarse pyramid level, not the full window. */
const HISTORY_MAX_CELLS = 40_000;

export default function SnapsPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";
  const [layer] = useUrlParam("layer", "correlator");
  const [units] = useUrlParam("units", "dB");
  const [mode] = useUrlParam("mode", "live");
  const [inputSet] = useUrlParam("input_set", "beamforming");
  // The server-rendered figure is the default view (operator, 2026-09-08);
  // "interactive" is the pre-existing live/history Plotly-style page below.
  const [view] = useUrlParam("view", "correlator");
  const [selected, setSelected] = useUrlParam("input", "");

  const [boards, setBoards] = useState<SnapBoardInfo[] | null>(null);
  const [liveByIp, setLiveByIp] = useState<Record<string, SnapLiveResponse>>({});
  const [readByIp, setReadByIp] = useState<Record<string, SnapBoardReadResponse>>({});
  const [histByPacketIdx, setHistByPacketIdx] = useState<Record<number, SnapHistoryResponse>>({});

  // "Read boards now": the backend serializes reads behind one lock, so a
  // single reading/countdown state covers the whole page.
  const [reading, setReading] = useState(false);
  const [retryAfterS, setRetryAfterS] = useState<number | null>(null);
  const activeJobId = useRef<number | null>(null);

  const api = useMemo(
    () =>
      useMock
        ? {
            boards: mockGetSnapBoards,
            live: mockGetSnapLive,
            boardRead: mockGetSnapBoardRead,
            postRead: mockPostSnapBoardRead,
            job: mockGetJob,
            history: mockGetSnapHistory,
          }
        : {
            boards: getSnapBoards,
            live: getSnapLive,
            boardRead: getSnapBoardRead,
            postRead: postSnapBoardRead,
            job: (id: number) => getJob(id),
            history: (packetIdx: number, t0: string, t1: string, source: "kafka" | "board") =>
              getSnapHistory({ packet_idx: packetIdx, t0, t1, source, max_cells: HISTORY_MAX_CELLS }),
          },
    [useMock],
  );

  // Board inventory: fetched once (semi-static per docs/api-snaps.md).
  useEffect(() => {
    let cancelled = false;
    api
      .boards()
      .then((r) => {
        if (!cancelled) setBoards(r.boards);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api]);

  const antennaBoards = useMemo(
    () =>
      (boards ?? [])
        .filter((b) => b.role === "antenna")
        .sort((a, b) => (a.feng_id ?? 0) - (b.feng_id ?? 0)),
    [boards],
  );
  const relayBoards = useMemo(() => (boards ?? []).filter((b) => b.role === "relay"), [boards]);
  const allBoardIps = useMemo(
    () => [...antennaBoards.map((b) => b.ip), ...relayBoards.map((b) => b.ip)],
    [antennaBoards, relayBoards],
  );

  // Live layer: the correlator bandpass, polled every 10 s. The board layer
  // is never polled — it changes hourly server-side or after a manual read.
  useEffect(() => {
    if (view !== "interactive" || mode !== "live" || antennaBoards.length === 0) return;
    let cancelled = false;
    function pollLive() {
      for (const board of antennaBoards) {
        api
          .live(board.ip, PANEL_NCHAN)
          .then((r) => {
            if (!cancelled) setLiveByIp((prev) => ({ ...prev, [board.ip]: r }));
          })
          .catch(() => undefined);
      }
    }
    pollLive();
    const timer = setInterval(pollLive, LIVE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [view, mode, antennaBoards, api]);

  const fetchBoardReads = useCallback(
    (ips: string[]) => {
      for (const ip of ips) {
        api
          .boardRead(ip)
          .then((r) => setReadByIp((prev) => ({ ...prev, [ip]: r })))
          .catch(() => undefined);
      }
    },
    [api],
  );

  useEffect(() => {
    if (allBoardIps.length === 0) return;
    fetchBoardReads(allBoardIps);
    // Intentionally not polled; see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allBoardIps.join(",")]);

  // Countdown ticker for a 429's retry_after_s.
  useEffect(() => {
    if (retryAfterS === null) return;
    if (retryAfterS <= 0) {
      setRetryAfterS(null);
      return;
    }
    const t = setTimeout(() => setRetryAfterS((s) => (s === null ? null : Math.max(0, s - 1))), 1000);
    return () => clearTimeout(t);
  }, [retryAfterS]);

  const pollJob = useCallback(
    (jobId: number, ips: string[]) => {
      const poll = () => {
        api
          .job(jobId)
          .then((job) => {
            if (job.state === "done") {
              setReading(false);
              activeJobId.current = null;
              fetchBoardReads(ips);
            } else if (job.state === "failed" || job.state === "cancelled") {
              setReading(false);
              activeJobId.current = null;
              emitError(`The board read job ${jobId} ${job.state}.`);
            } else {
              setTimeout(poll, JOB_POLL_MS);
            }
          })
          .catch(() => {
            setReading(false);
            activeJobId.current = null;
          });
      };
      poll();
    },
    [api, fetchBoardReads],
  );

  const handleReadNow = useCallback(() => {
    if (reading || retryAfterS) return;
    setReading(true);
    api
      .postRead(null)
      .then((res) => {
        if (res.throttled) {
          setReading(false);
          setRetryAfterS(res.retryAfterS);
          emitError(res.detail);
          return;
        }
        activeJobId.current = res.jobId;
        pollJob(res.jobId, allBoardIps);
      })
      .catch(() => setReading(false));
  }, [api, reading, retryAfterS, pollJob, allBoardIps]);

  // --- history mode ------------------------------------------------------
  const [histRange] = useUrlParam("hist_range_range", "1h");
  const [histFrom] = useUrlParam("hist_range_from", "");
  const [histIdx, setHistIdx] = useUrlParam("hist_i", "0");

  useEffect(() => {
    if (view !== "interactive" || mode !== "history" || antennaBoards.length === 0) return;
    let cancelled = false;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(histRange, histFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const source = layer === "board" ? "board" : "kafka";
    const wanted = antennaBoards.flatMap((b) =>
      (b.inputs ?? []).filter((i) => i.packet_idx !== null).map((i) => i.packet_idx as number),
    );
    Promise.all(
      wanted.map((pidx) =>
        api.history(pidx, t0, t1, source).then((h) => [pidx, h] as const).catch(() => null),
      ),
    )
      .then((entries) => {
        if (cancelled) return;
        const map: Record<number, SnapHistoryResponse> = {};
        for (const entry of entries) {
          if (entry) map[entry[0]] = entry[1];
        }
        setHistByPacketIdx(map);
        setHistIdx("0");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, mode, antennaBoards, layer, histRange, histFrom]);

  const histFrames = useMemo(() => Object.values(histByPacketIdx), [histByPacketIdx]);
  const sliderMax = histFrames.length ? Math.max(...histFrames.map((h) => h.t.length)) - 1 : 0;
  const sliderIdx = Math.min(Math.max(parseInt(histIdx, 10) || 0, 0), Math.max(sliderMax, 0));
  const sliderTime = useMemo(() => {
    const withT = histFrames.find((h) => h.t.length > sliderIdx);
    return withT ? formatUnixUtc(withT.t[sliderIdx]) : null;
  }, [histFrames, sliderIdx]);

  // --- panel data --------------------------------------------------------
  const panelsFor = useCallback(
    (board: SnapBoardInfo): PanelData[] => {
      const inputs = orderInputs(board.inputs ?? [], (inputSet === "all" ? "all" : "beamforming") as SnapInputSetMode);
      const read = readByIp[board.ip];
      const live = liveByIp[board.ip];
      return inputs.map((input) => {
        const rms = layer === "board" ? read?.adc_rms?.[input.adc] ?? null : null;
        if (mode === "history") {
          const hist = input.packet_idx !== null ? histByPacketIdx[input.packet_idx] : undefined;
          const frame = hist ? hist.z_db[Math.min(sliderIdx, hist.t.length - 1)] ?? null : null;
          return { input, freqMhz: hist?.freq_mhz ?? [], values: frame, rms };
        }
        if (layer === "board") {
          return { input, freqMhz: read?.freq_mhz ?? [], values: read?.spectra?.[input.adc] ?? null, rms };
        }
        const li = live?.inputs.find((x) => x.adc === input.adc);
        return { input, freqMhz: live?.freq_mhz ?? [], values: li?.bp ?? null, rms };
      });
    },
    [inputSet, readByIp, liveByIp, layer, mode, histByPacketIdx, sliderIdx],
  );

  // --- selection ---------------------------------------------------------
  const selectedInput = useMemo(() => {
    if (!selected) return null;
    const [ip, adcStr] = selected.split(":");
    const board = (boards ?? []).find((b) => b.ip === ip);
    const input = board?.inputs?.find((i) => i.adc === Number(adcStr));
    return board && input ? { board, input } : null;
  }, [selected, boards]);

  if (!boards) {
    return <p className="note">Loading the board inventory.</p>;
  }

  if (selectedInput) {
    return (
      <InputDetail
        board={selectedInput.board}
        input={selectedInput.input}
        units={units as "dB" | "linear"}
        layer={layer as "correlator" | "board"}
        useMock={useMock}
        onBack={() => setSelected("")}
      />
    );
  }

  const lastRead = Object.values(readByIp)
    .map((r) => r.age_s)
    .filter((a): a is number => a !== null)
    .sort((a, b) => a - b)[0];

  const figureInputSet = inputSet === "all" ? "all12" : "beamforming";

  return (
    <div>
      <div className="toolbar">
        <Segmented
          paramKey="view"
          defaultValue="correlator"
          options={[
            { value: "correlator", label: "correlator spectra" },
            { value: "board", label: "board spectra" },
            { value: "waterfalls", label: "waterfalls" },
            { value: "trend", label: "trend" },
            { value: "interactive", label: "interactive" },
          ]}
        />
        <Segmented
          paramKey="input_set"
          defaultValue="beamforming"
          options={[
            { value: "beamforming", label: "intended" },
            { value: "all", label: "all 12 ADCs" },
          ]}
        />
        {view === "interactive" && (
          <>
            <Segmented
              paramKey="layer"
              defaultValue="correlator"
              options={[
                { value: "correlator", label: "correlator" },
                { value: "board", label: "board" },
              ]}
            />
            <Segmented
              paramKey="units"
              defaultValue="dB"
              options={[
                { value: "dB", label: "dB" },
                { value: "linear", label: "linear" },
              ]}
            />
            <Segmented
              paramKey="mode"
              defaultValue="live"
              options={[
                { value: "live", label: "live" },
                { value: "history", label: "history" },
              ]}
            />
          </>
        )}
        <span>
          <button className="text-button" onClick={handleReadNow} disabled={reading || !!retryAfterS} type="button">
            {reading ? "reading boards" : "Read boards now"}
          </button>
          <span className="aside">
            {retryAfterS
              ? `not again for ${retryAfterS} s`
              : lastRead !== undefined
                ? `boards read ${formatAge(lastRead)}`
                : "boards never read"}
          </span>
        </span>
        {view === "interactive" && mode === "history" && (
          <TimeRangePicker paramPrefix="hist_range" defaultRange="1h" />
        )}
      </div>

      {view !== "interactive" && (
        <SnapFigureView inputSet={figureInputSet} view={view as SnapFigureViewKind} />
      )}

      {view === "interactive" && (
        <>
          {mode === "history" && (
            <div className="slider">
              <input
                type="range"
                min={0}
                max={Math.max(sliderMax, 0)}
                value={sliderIdx}
                onChange={(e) => setHistIdx(e.target.value)}
                aria-label="history time"
              />
              <span>{sliderTime ?? "no frames in this window"}</span>
            </div>
          )}

          {antennaBoards.map((board) => (
            <BoardSection
              key={board.ip}
              board={board}
              read={readByIp[board.ip] ?? null}
              live={mode === "history" ? null : liveByIp[board.ip] ?? null}
              panels={panelsFor(board)}
              units={units as "dB" | "linear"}
              layer={layer as "correlator" | "board"}
              onSelect={(adc) => setSelected(`${board.ip}:${adc}`)}
            />
          ))}

          {relayBoards.length > 0 && (
            <section className="board">
              {relayBoards.map((board) => (
                <p key={board.ip} className="board__line">
                  {relayLine(board, readByIp[board.ip] ?? null)}
                </p>
              ))}
              <p className="note">
                Relay boards carry no antenna inputs and sit in the PPS timing path only; a board on
                the golden image cannot report PPS at all, so silence here does not mean the chain
                skips the slot.
              </p>
            </section>
          )}
        </>
      )}
    </div>
  );
}
