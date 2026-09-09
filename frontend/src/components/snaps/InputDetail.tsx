import { useEffect, useMemo, useState } from "react";
import TimeRangePicker from "../TimeRangePicker";
import { usePlotly } from "../../lib/usePlotly";
import { useUrlParam } from "../../lib/useUrlParam";
import { resolveSince } from "../../lib/timeRange";
import { convertSeries } from "../../lib/spectrumUtils";
import { PLOT_CONFIG, axis, layout, line, vline } from "../../lib/plotStyle";
import { BOARD_BAND_MHZ, CORR_BAND_MHZ } from "../../lib/snapConstants";
import { panelTitle } from "../../lib/snapText";
import { COLORSCALE, FAINT, MUTED, SANS } from "../../lib/theme";
import { getSnapBoardRead, getSnapHistory, getSnapLive, getSnapTrend } from "../../lib/api";
import {
  mockGetSnapBoardRead,
  mockGetSnapHistory,
  mockGetSnapLive,
  mockGetSnapTrend,
} from "../../lib/mockSnaps";
import type {
  SnapBoardInfo,
  SnapHistoryResponse,
  SnapInputInfo,
  SnapTrendResponse,
} from "../../lib/types";

const DIAGNOSIS =
  "A flat trace is a dead feed, a pinned constant level is a railed ADC, an " +
  "all-zero subband is an F-engine delivery problem, and a sinusoidal ripple " +
  "across the band is a cable reflection.";

export interface InputDetailProps {
  board: SnapBoardInfo;
  input: SnapInputInfo;
  units: "dB" | "linear";
  layer: "correlator" | "board";
  useMock: boolean;
  onBack: () => void;
}

/**
 * One input, full size: the full-resolution spectrum, the waterfall over the
 * chosen window (viridis, time up, frequency descending left to right as in
 * the team's figures), and the night-median trend with epoch changes as thin
 * verticals. Replaces the grid rather than floating over it.
 */
export default function InputDetail({ board, input, units, layer, useMock, onBack }: InputDetailProps) {
  const [range] = useUrlParam("detail_range_range", "24h");
  const [customFrom] = useUrlParam("detail_range_from", "");
  const [spectrum, setSpectrum] = useState<{ freqMhz: number[]; values: (number | null)[] | null } | null>(null);
  const [history, setHistory] = useState<SnapHistoryResponse | null>(null);
  const [trend, setTrend] = useState<SnapTrendResponse | null>(null);

  // Full resolution for this one input (the grid only ever fetched nchan=768).
  useEffect(() => {
    let cancelled = false;
    if (layer === "board") {
      const p = useMock ? mockGetSnapBoardRead(board.ip) : getSnapBoardRead(board.ip);
      p.then((r) => {
        if (!cancelled) setSpectrum({ freqMhz: r.freq_mhz ?? [], values: r.spectra?.[input.adc] ?? null });
      }).catch(() => undefined);
    } else {
      const p = useMock ? mockGetSnapLive(board.ip) : getSnapLive(board.ip);
      p.then((r) => {
        if (cancelled) return;
        const li = r.inputs.find((x) => x.adc === input.adc);
        setSpectrum({ freqMhz: r.freq_mhz, values: li?.bp ?? null });
      }).catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [board.ip, input.adc, layer, useMock]);

  useEffect(() => {
    if (input.packet_idx === null) return;
    let cancelled = false;
    const pidx = input.packet_idx;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(range, customFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const source = layer === "board" ? "board" : "kafka";
    const h = useMock
      ? mockGetSnapHistory(pidx, t0, t1, source)
      : getSnapHistory({ packet_idx: pidx, t0, t1, source });
    const tr = useMock ? mockGetSnapTrend(pidx, t0, t1) : getSnapTrend({ packet_idx: pidx, t0, t1 });
    h.then((r) => !cancelled && setHistory(r)).catch(() => undefined);
    tr.then((r) => !cancelled && setTrend(r)).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [input.packet_idx, layer, range, customFrom, useMock]);

  const band = layer === "board" ? BOARD_BAND_MHZ : CORR_BAND_MHZ;

  const spectrumData = useMemo(
    () => (spectrum ? [line(spectrum.freqMhz, convertSeries(spectrum.values ?? [], units))] : []),
    [spectrum, units],
  );
  const spectrumLayout = useMemo(
    () =>
      layout({
        height: 300,
        margin: { l: 56, r: 8, t: 4, b: 40 },
        xaxis: axis({ range: [band[1], band[0]], title: { text: "freq [MHz]", font: { size: 11, color: MUTED, family: SANS } } }),
        yaxis: axis({ title: { text: units, font: { size: 11, color: MUTED, family: SANS } } }),
      }),
    [band, units],
  );
  const spectrumRef = usePlotly(spectrumData, spectrumLayout, PLOT_CONFIG);

  const waterfallData = useMemo(() => {
    if (!history) return [];
    return [
      {
        x: history.freq_mhz,
        y: history.t.map((s) => new Date(s * 1000).toISOString()),
        z: history.z_db,
        type: "heatmap",
        colorscale: COLORSCALE,
        colorbar: {
          title: { text: "dB", font: { size: 11, color: MUTED, family: SANS } },
          thickness: 10,
          outlinewidth: 0,
          tickfont: { size: 10, color: MUTED, family: SANS },
        },
        hoverinfo: "x+y+z",
      },
    ];
  }, [history]);
  const waterfallLayout = useMemo(
    () =>
      layout({
        height: 380,
        margin: { l: 90, r: 8, t: 4, b: 40 },
        xaxis: axis({
          range: [band[1], band[0]],
          showgrid: false,
          title: { text: "freq [MHz]", font: { size: 11, color: MUTED, family: SANS } },
        }),
        yaxis: axis({ showgrid: false }),
      }),
    [band],
  );
  const waterfallRef = usePlotly(waterfallData, waterfallLayout, PLOT_CONFIG);

  const trendData = useMemo(
    () =>
      trend
        ? [line(trend.t.map((s) => new Date(s * 1000).toISOString()), trend.night_median_db)]
        : [],
    [trend],
  );
  const trendLayout = useMemo(
    () =>
      layout({
        height: 260,
        margin: { l: 56, r: 8, t: 20, b: 40 },
        xaxis: axis({}),
        yaxis: axis({ title: { text: "night median [dB]", font: { size: 11, color: MUTED, family: SANS } } }),
        shapes: trend?.epochs.map((ep) => vline(ep.ts)) ?? [],
        annotations:
          trend?.epochs.map((ep) => ({
            x: ep.ts,
            y: 1,
            yref: "paper",
            yanchor: "bottom",
            showarrow: false,
            text: ep.label,
            font: { size: 10, color: FAINT, family: SANS },
          })) ?? [],
      }),
    [trend],
  );
  const trendRef = usePlotly(trendData, trendLayout, PLOT_CONFIG);

  return (
    <div>
      <button className="detail__back" onClick={onBack} type="button">
        back to all inputs
      </button>
      <h2 className="detail__title">{panelTitle(board, input)}</h2>
      <p className="board__line">
        SNAP {board.feng_id ?? "?"}, {board.ip}, adc {input.adc}.
      </p>

      <div className="detail__block" ref={spectrumRef} />

      <div className="detail__block">
        <div className="toolbar">
          <TimeRangePicker paramPrefix="detail_range" defaultRange="24h" />
        </div>
        {history ? <div ref={waterfallRef} /> : <p className="note">No history for this input yet.</p>}
        <p className="detail__caption">{DIAGNOSIS}</p>
      </div>

      <div className="detail__block">
        {trend ? <div ref={trendRef} /> : <p className="note">No trend for this input yet.</p>}
      </div>
    </div>
  );
}
