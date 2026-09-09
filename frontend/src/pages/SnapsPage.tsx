import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Page from "../components/Page";
import ToggleBar from "../components/ToggleBar";
import TimeRangePicker from "../components/TimeRangePicker";
import BoardCard, { type BoardCardTileData } from "../components/snaps/BoardCard";
import RelayCard from "../components/snaps/RelayCard";
import ExpandedPanel from "../components/snaps/ExpandedPanel";
import { useUrlParam } from "../lib/useUrlParam";
import { resolveSince } from "../lib/timeRange";
import {
  getJob,
  getSnapBoardRead,
  getSnapBoards,
  getSnapLive,
  getSnapHistory,
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
import { ANTENNA_IPS } from "../lib/snapConstants";
import { emitError } from "../lib/toast";
import type {
  SnapBoardInfo,
  SnapBoardReadResponse,
  SnapHistoryResponse,
  SnapLiveResponse,
} from "../lib/types";

const LIVE_POLL_MS = 10_000;
const CARD_NCHAN = 768;
const JOB_POLL_MS = 2_000;

function useMockFlag(params: URLSearchParams): boolean {
  return params.get("mock") === "1";
}

function tileLabel(board: SnapBoardInfo, adc: number): string {
  const input = board.inputs?.find((i) => i.adc === adc);
  if (!input || input.packet_idx === null) return `A${adc} (unwired)`;
  const station = input.station ? ` ${input.station}` : "";
  const ant = input.antenna !== null ? ` ant ${input.antenna}` : "";
  return `A${adc}${ant}${station} in${input.packet_idx}`.trim();
}

export default function SnapsPage() {
  const [searchParams] = useSearchParams();
  const useMock = useMockFlag(searchParams);
  const [mode] = useUrlParam("mode", "live");
  const [units] = useUrlParam("units", "dB");
  const [selectedIp, setSelectedIp] = useUrlParam("board", ANTENNA_IPS[0]);

  const [boards, setBoards] = useState<SnapBoardInfo[] | null>(null);
  const [liveByIp, setLiveByIp] = useState<Record<string, SnapLiveResponse>>({});
  const [boardReadByIp, setBoardReadByIp] = useState<Record<string, SnapBoardReadResponse>>({});
  const [expanded, setExpanded] = useState<{ ip: string; adc: number } | null>(null);

  // "Read boards now" state: a single countdown/lock covers both the global
  // and per-card buttons since the backend serializes reads with one
  // 5-minute lock across all boards (docs/plan.md).
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
              getSnapHistory({ packet_idx: packetIdx, t0, t1, source }),
          },
    [useMock],
  );

  // Board inventory: fetched once (cheap, semi-static per docs/api-snaps.md).
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
    () => (boards ?? []).filter((b) => b.role === "antenna").sort((a, b) => (a.feng_id ?? 0) - (b.feng_id ?? 0)),
    [boards],
  );
  const relayBoards = useMemo(() => (boards ?? []).filter((b) => b.role === "relay"), [boards]);

  // Live-mode polling: correlator bandpass every 10s per antenna board, plus
  // an initial board-read fetch for every board (never polled continuously —
  // only refreshed hourly server-side or after a manual read job completes).
  useEffect(() => {
    if (mode !== "live" || antennaBoards.length === 0) return;
    let cancelled = false;
    function pollLive() {
      for (const board of antennaBoards) {
        api
          .live(board.ip, CARD_NCHAN)
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
  }, [mode, antennaBoards, api]);

  const allBoardIps = useMemo(() => [...antennaBoards.map((b) => b.ip), ...relayBoards.map((b) => b.ip)], [antennaBoards, relayBoards]);

  const fetchBoardReads = useCallback(
    (ips: string[]) => {
      for (const ip of ips) {
        api
          .boardRead(ip)
          .then((r) => setBoardReadByIp((prev) => ({ ...prev, [ip]: r })))
          .catch(() => undefined);
      }
    },
    [api],
  );

  useEffect(() => {
    if (allBoardIps.length === 0) return;
    fetchBoardReads(allBoardIps);
    // Intentionally not polled: board reads only change hourly server-side
    // or after a manual "read boards now" job (handled below).
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
              emitError(`board read job ${jobId} ${job.state}`);
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

  const handleReadNow = useCallback(
    (ips: string[] | null) => {
      if (reading || retryAfterS) return;
      setReading(true);
      api
        .postRead(ips)
        .then((res) => {
          if (res.throttled) {
            setReading(false);
            setRetryAfterS(res.retryAfterS);
            emitError(res.detail);
            return;
          }
          activeJobId.current = res.jobId;
          pollJob(res.jobId, ips ?? allBoardIps);
        })
        .catch(() => setReading(false));
    },
    [api, reading, retryAfterS, pollJob, allBoardIps],
  );

  // --- History mode --------------------------------------------------
  const [histRange] = useUrlParam("hist_range_range", "24h");
  const [histFrom] = useUrlParam("hist_range_from", "");
  const [histSliderIdx, setHistSliderIdx] = useUrlParam("hist_i", "0");
  const selectedBoard = antennaBoards.find((b) => b.ip === selectedIp) ?? antennaBoards[0];
  const layerParamKey = selectedBoard ? `layer_${selectedBoard.ip.split(".").pop()}` : "layer_x";
  const [selectedLayer] = useUrlParam(layerParamKey, "correlator");
  const [histByPacketIdx, setHistByPacketIdx] = useState<Record<number, SnapHistoryResponse>>({});

  useEffect(() => {
    if (mode !== "history" || !selectedBoard) return;
    let cancelled = false;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(histRange, histFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const wiredInputs = (selectedBoard.inputs ?? []).filter((i) => i.packet_idx !== null);
    Promise.all(
      wiredInputs.map((i) =>
        api
          .history(i.packet_idx as number, t0, t1, selectedLayer === "board" ? "board" : "kafka")
          .then((h) => [i.packet_idx as number, h] as const),
      ),
    )
      .then((entries) => {
        if (cancelled) return;
        const map: Record<number, SnapHistoryResponse> = {};
        for (const [pidx, h] of entries) map[pidx] = h;
        setHistByPacketIdx(map);
        setHistSliderIdx("0");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, selectedBoard?.ip, selectedLayer, histRange, histFrom]);

  const sliderMax = useMemo(() => {
    const lens = Object.values(histByPacketIdx).map((h) => h.t.length);
    return lens.length ? Math.max(...lens) - 1 : 0;
  }, [histByPacketIdx]);
  const sliderIdx = Math.min(parseInt(histSliderIdx, 10) || 0, sliderMax);

  function buildTiles(board: SnapBoardInfo, source: "correlator" | "board"): BoardCardTileData[] {
    const inputs = board.inputs ?? [];
    if (mode === "history") {
      return inputs.map((input) => {
        const hist = input.packet_idx !== null ? histByPacketIdx[input.packet_idx] : undefined;
        const freqMhz = hist?.freq_mhz ?? (source === "board" ? [] : []);
        const values = hist ? hist.z_db[Math.min(sliderIdx, hist.t.length - 1)] ?? null : null;
        return {
          adc: input.adc,
          label: tileLabel(board, input.adc),
          freqMhz,
          values,
          bold: input.in_bf,
          dimmed: !input.functional,
        };
      });
    }
    if (source === "correlator") {
      const live = liveByIp[board.ip];
      return inputs.map((input) => {
        const li = live?.inputs.find((x) => x.adc === input.adc);
        return {
          adc: input.adc,
          label: tileLabel(board, input.adc),
          freqMhz: live?.freq_mhz ?? [],
          values: li?.bp ?? null,
          bold: input.in_bf,
          dimmed: !input.functional || li?.mapping === "unmapped",
          mismatch: li?.mapping === "mismatch",
        };
      });
    }
    const read = boardReadByIp[board.ip];
    return inputs.map((input) => ({
      adc: input.adc,
      label: tileLabel(board, input.adc),
      freqMhz: read?.freq_mhz ?? [],
      values: read?.spectra?.[input.adc] ?? null,
      bold: input.in_bf,
      dimmed: !input.functional,
    }));
  }

  const expandedBoard = expanded ? (boards ?? []).find((b) => b.ip === expanded.ip) : null;
  const expandedTile = useMemo(() => {
    if (!expanded || !expandedBoard) return null;
    const tiles = buildTiles(expandedBoard, mode === "history" ? (selectedLayer as "correlator" | "board") : "correlator");
    return tiles.find((t) => t.adc === expanded.adc) ?? null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, expandedBoard, liveByIp, boardReadByIp, histByPacketIdx, sliderIdx, mode, selectedLayer]);

  if (!boards) {
    return (
      <Page title="SNAPs">
        <p className="empty-note">loading board inventory…</p>
      </Page>
    );
  }

  return (
    <Page title="SNAPs">
      <div className="snap-page-toolbar">
        <ToggleBar
          paramKey="mode"
          defaultValue="live"
          options={[
            { value: "live", label: "live" },
            { value: "history", label: "history" },
          ]}
        />
        <ToggleBar
          paramKey="units"
          defaultValue="dB"
          options={[
            { value: "dB", label: "dB" },
            { value: "linear", label: "linear" },
          ]}
        />
        {mode === "live" && (
          <button className="snap-read-btn" onClick={() => handleReadNow(null)} disabled={reading || !!retryAfterS}>
            {reading ? "reading…" : retryAfterS ? `read boards now (${retryAfterS}s)` : "read boards now (all)"}
          </button>
        )}
        {mode === "history" && (
          <>
            <select value={selectedIp} onChange={(e) => setSelectedIp(e.target.value)}>
              {antennaBoards.map((b) => (
                <option key={b.ip} value={b.ip}>
                  {b.ip} (feng {b.feng_id})
                </option>
              ))}
            </select>
            <TimeRangePicker paramPrefix="hist_range" defaultRange="24h" />
          </>
        )}
      </div>

      {mode === "history" && selectedBoard && (
        <div className="snap-history-slider">
          <input
            type="range"
            min={0}
            max={sliderMax}
            value={sliderIdx}
            onChange={(e) => setHistSliderIdx(e.target.value)}
          />
          <span className="mono">
            frame {sliderIdx + 1} / {sliderMax + 1}
          </span>
        </div>
      )}

      <div className="snap-grid">
        {mode === "live" &&
          antennaBoards.map((board) => (
            <BoardCard
              key={board.ip}
              board={board}
              units={units as "dB" | "linear"}
              correlatorAgeS={liveByIp[board.ip]?.age_s ?? null}
              boardAgeS={boardReadByIp[board.ip]?.age_s ?? null}
              correlatorTiles={buildTiles(board, "correlator")}
              boardTiles={buildTiles(board, "board")}
              adcRms={boardReadByIp[board.ip]?.adc_rms ?? null}
              subbandsOk={liveByIp[board.ip]?.subbands_ok ?? null}
              fengIdHw={boardReadByIp[board.ip]?.feng_id_hw ?? null}
              eqEpoch={liveByIp[board.ip]?.eq_epoch ?? boardReadByIp[board.ip]?.eq_epoch ?? null}
              onReadNow={() => handleReadNow([board.ip])}
              readDisabled={!!retryAfterS}
              readCountdownS={retryAfterS}
              reading={reading}
              onExpandTile={(adc) => setExpanded({ ip: board.ip, adc })}
              historyMode={false}
            />
          ))}
        {mode === "history" && selectedBoard && (
          <BoardCard
            key={selectedBoard.ip}
            board={selectedBoard}
            units={units as "dB" | "linear"}
            correlatorAgeS={null}
            boardAgeS={boardReadByIp[selectedBoard.ip]?.age_s ?? null}
            correlatorTiles={buildTiles(selectedBoard, "correlator")}
            boardTiles={buildTiles(selectedBoard, "board")}
            adcRms={boardReadByIp[selectedBoard.ip]?.adc_rms ?? null}
            subbandsOk={null}
            fengIdHw={boardReadByIp[selectedBoard.ip]?.feng_id_hw ?? null}
            eqEpoch={boardReadByIp[selectedBoard.ip]?.eq_epoch ?? null}
            onReadNow={() => undefined}
            readDisabled
            readCountdownS={null}
            reading={false}
            onExpandTile={(adc) => setExpanded({ ip: selectedBoard.ip, adc })}
            historyMode
          />
        )}
        {mode === "live" && relayBoards.map((board) => <RelayCard key={board.ip} board={board} read={boardReadByIp[board.ip] ?? null} />)}
      </div>

      {expanded && expandedBoard && expandedTile && (
        <ExpandedPanel
          ip={expanded.ip}
          adc={expanded.adc}
          packetIdx={expandedBoard.inputs?.find((i) => i.adc === expanded.adc)?.packet_idx ?? null}
          label={tileLabel(expandedBoard, expanded.adc)}
          freqMhz={expandedTile.freqMhz}
          values={expandedTile.values}
          units={units as "dB" | "linear"}
          mode={mode === "history" ? "history" : "live"}
          sourceLayer={
            (mode === "history"
              ? selectedLayer
              : searchParams.get(`layer_${expanded.ip.split(".").pop()}`) ?? "correlator") as "correlator" | "board"
          }
          useMock={useMock}
          onClose={() => setExpanded(null)}
        />
      )}
    </Page>
  );
}
