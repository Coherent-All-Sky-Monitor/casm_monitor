import { useEffect, useState } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { useUrlParam } from "../../lib/useUrlParam";
import { convertSeries } from "../../lib/spectrumUtils";
import ToggleBar from "../ToggleBar";
import TimeRangePicker from "../TimeRangePicker";
import { resolveSince } from "../../lib/timeRange";
import { getSnapBoardRead, getSnapHistory, getSnapLive, getSnapTrend } from "../../lib/api";
import { mockGetSnapBoardRead, mockGetSnapHistory, mockGetSnapLive, mockGetSnapTrend } from "../../lib/mockSnaps";
import type { SnapHistoryResponse, SnapHistorySource, SnapTrendResponse } from "../../lib/types";

const DIAGNOSIS_LEGEND = [
  ["flat", "dead feed"],
  ["pinned constant", "railed ADC"],
  ["all-zero subband", "F-engine delivery"],
  ["sinusoidal ripple", "cable reflection"],
] as const;

export interface ExpandedPanelProps {
  ip: string;
  adc: number;
  packetIdx: number | null;
  label: string;
  /** Fallback spectrum (whatever the card grid already had, e.g. the
   * decimated live/history frame) shown until the full-res fetch below
   * lands, and used as-is in history mode. */
  freqMhz: number[];
  values: (number | null)[] | null;
  units: "dB" | "linear";
  /** "live" fetches its own full-res spectrum (nchan omitted / board read);
   * "history" trusts the frame already selected by the page's slider. */
  mode: "live" | "history";
  sourceLayer: "correlator" | "board";
  useMock: boolean;
  onClose: () => void;
}

/**
 * Full-res spectrum + waterfall + night-median trend for one clicked input,
 * per docs/plan.md's history-mode expanded panel. Fetches its own
 * history/trend window (via mock or the real API) independent of the card
 * grid's live polling.
 */
export default function ExpandedPanel({
  ip,
  adc,
  packetIdx,
  label,
  freqMhz,
  values,
  units,
  mode,
  sourceLayer,
  useMock,
  onClose,
}: ExpandedPanelProps) {
  const [source] = useUrlParam("exp_source", "kafka");
  const [range] = useUrlParam("exp_range_range", "24h");
  const [customFrom] = useUrlParam("exp_range_from", "");
  const [history, setHistory] = useState<SnapHistoryResponse | null>(null);
  const [trend, setTrend] = useState<SnapTrendResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fullRes, setFullRes] = useState<{ freqMhz: number[]; values: (number | null)[] | null } | null>(null);

  // Full resolution for the spectrum panel: the card grid feeds us a
  // decimated (nchan=768) or already-slider-picked frame; in live mode fetch
  // the un-decimated version once per input/layer (docs/plan.md "full res in
  // the expanded view").
  useEffect(() => {
    if (mode !== "live") {
      setFullRes(null);
      return;
    }
    let cancelled = false;
    if (sourceLayer === "board") {
      const p = useMock ? mockGetSnapBoardRead(ip) : getSnapBoardRead(ip);
      p.then((r) => {
        if (cancelled) return;
        setFullRes({ freqMhz: r.freq_mhz ?? [], values: r.spectra?.[adc] ?? null });
      }).catch(() => undefined);
    } else {
      const p = useMock ? mockGetSnapLive(ip) : getSnapLive(ip);
      p.then((r) => {
        if (cancelled) return;
        const li = r.inputs.find((x) => x.adc === adc);
        setFullRes({ freqMhz: r.freq_mhz, values: li?.bp ?? null });
      }).catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [mode, sourceLayer, ip, adc, useMock]);

  const spectrumFreq = fullRes?.freqMhz ?? freqMhz;
  const spectrumValues = fullRes?.values ?? values;

  useEffect(() => {
    if (packetIdx === null) return;
    let cancelled = false;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(range, customFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const src = source as SnapHistorySource;
    const historyPromise = useMock
      ? mockGetSnapHistory(packetIdx, t0, t1, src)
      : getSnapHistory({ packet_idx: packetIdx, t0, t1, source: src });
    const trendPromise = useMock
      ? mockGetSnapTrend(packetIdx, t0, t1)
      : getSnapTrend({ packet_idx: packetIdx, t0, t1 });
    Promise.all([historyPromise, trendPromise])
      .then(([h, tr]) => {
        if (cancelled) return;
        setHistory(h);
        setTrend(tr);
        setLoadError(null);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [packetIdx, source, range, customFrom, useMock]);

  const spectrumData = [
    {
      x: spectrumFreq,
      y: convertSeries(spectrumValues ?? [], units),
      type: "scatter",
      mode: "lines",
      line: { width: 1, color: "#2258d6" },
    },
  ];
  const spectrumLayout = {
    margin: { l: 50, r: 10, t: 10, b: 30 },
    height: 220,
    xaxis: { title: "MHz" },
    yaxis: { title: units },
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
  };
  const spectrumRef = usePlotly(spectrumData, spectrumLayout);

  // Waterfall: freq on y (descending, rendered exactly as given per
  // docs/plan.md's explicit note so the band is not flipped upside down),
  // time on x, dB colorscale.
  const waterfallData = history
    ? [
        {
          x: history.t.map((s) => new Date(s * 1000).toISOString()),
          y: history.freq_mhz,
          z: transposeForHeatmap(history.z_db),
          type: "heatmap",
          colorscale: "Viridis",
          colorbar: { title: "dB", titleside: "right" },
        },
      ]
    : [];
  const waterfallLayout = {
    margin: { l: 60, r: 10, t: 10, b: 40 },
    height: 260,
    xaxis: { title: "time" },
    yaxis: { title: "MHz", autorange: history ? undefined : true },
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
  };
  const waterfallRef = usePlotly(waterfallData, waterfallLayout);

  const trendData = trend
    ? [
        {
          x: trend.t.map((s) => new Date(s * 1000).toISOString()),
          y: trend.night_median_db,
          type: "scatter",
          mode: "lines+markers",
          name: "night median (dB)",
          line: { color: "#2258d6" },
        },
        {
          x: trend.t.map((s) => new Date(s * 1000).toISOString()),
          y: trend.band_power_db,
          type: "scatter",
          mode: "lines",
          name: "band power (dB)",
          line: { color: "#9aa2b1", dash: "dot" },
        },
      ]
    : [];
  const trendShapes =
    trend?.epochs.map((ep) => ({
      type: "line",
      xref: "x",
      yref: "paper",
      x0: ep.ts,
      x1: ep.ts,
      y0: 0,
      y1: 1,
      line: { color: epochColor(ep.kind), width: 1, dash: "dash" },
    })) ?? [];
  const trendLayout = {
    margin: { l: 50, r: 10, t: 10, b: 30 },
    height: 220,
    xaxis: { title: "time" },
    yaxis: { title: "dB" },
    shapes: trendShapes,
    legend: { orientation: "h", font: { size: 9 } },
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
  };
  const trendRef = usePlotly(trendData, trendLayout);

  return (
    <div className="snap-expanded-backdrop" onClick={onClose}>
      <div className="snap-expanded-panel" onClick={(e) => e.stopPropagation()}>
        <div className="snap-expanded-panel__header">
          <h3>
            {ip} — {label}
          </h3>
          <button onClick={onClose} aria-label="close">
            close
          </button>
        </div>

        <section>
          <h4>Full-resolution spectrum</h4>
          <div ref={spectrumRef} />
        </section>

        <section>
          <div className="snap-expanded-panel__toolbar">
            <h4>Waterfall</h4>
            <ToggleBar
              paramKey="exp_source"
              defaultValue="kafka"
              options={[
                { value: "kafka", label: "correlator (kafka)" },
                { value: "board", label: "board reads" },
              ]}
            />
            <TimeRangePicker paramPrefix="exp_range" defaultRange="24h" />
          </div>
          {loadError && <p className="empty-note">{loadError}</p>}
          {!loadError && !history && <p className="empty-note">loading…</p>}
          <div ref={waterfallRef} />
        </section>

        <section>
          <h4>Night-median trend</h4>
          <div ref={trendRef} />
        </section>

        <section>
          <h4>Diagnosis legend</h4>
          <ul className="snap-legend">
            {DIAGNOSIS_LEGEND.map(([pattern, meaning]) => (
              <li key={pattern}>
                <strong>{pattern}</strong> = {meaning}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}

function transposeForHeatmap(zByTime: number[][]): number[][] {
  // history.z_db is [t][freq]; Plotly heatmap with y=freq, x=time wants
  // z[freqIndex][timeIndex].
  if (zByTime.length === 0) return [];
  const nFreq = zByTime[0].length;
  const out: number[][] = Array.from({ length: nFreq }, () => new Array(zByTime.length));
  for (let ti = 0; ti < zByTime.length; ti++) {
    for (let fi = 0; fi < nFreq; fi++) {
      out[fi][ti] = zByTime[ti][fi];
    }
  }
  return out;
}

function epochColor(kind: string): string {
  if (kind === "eq") return "#2258d6";
  if (kind === "adc_gain") return "#c98a00";
  return "#8a1f1f";
}
