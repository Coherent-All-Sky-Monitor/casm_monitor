import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Panel from "../components/Panel";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import { usePlotly } from "../lib/usePlotly";
import { useUrlParam } from "../lib/useUrlParam";
import { resolveSince } from "../lib/timeRange";
import { PLOT_CONFIG, axis, layout, line } from "../lib/plotStyle";
import { COLORSCALE, FAINT, INK, MUTED, SANS, SIGNAL } from "../lib/theme";
import { hottestBeamsSentence, summarySentence } from "../lib/searchText";
import {
  getSearchBeamMap,
  getSearchFunnel,
  getSearchHist,
  getSearchRate,
  getSearchScatter,
  getSearchSummary,
} from "../lib/api";
import {
  mockGetSearchBeamMap,
  mockGetSearchFunnel,
  mockGetSearchHist,
  mockGetSearchRate,
  mockGetSearchScatter,
  mockGetSearchSummary,
} from "../lib/mockSearch";
import type {
  SearchBeamMapResponse,
  SearchFunnelResponse,
  SearchHistResponse,
  SearchRateResponse,
  SearchScatterResponse,
  SearchSummaryResponse,
} from "../lib/types";

const REFRESH_MS = 30_000;
const N_BEAM_ROWS = 16;
const N_BEAM_COLS = 32;

/** Bin edges/counts as a stepped blue line, matching the "one signal-blue
 * line" rule rather than adding a bar-chart look to the design. */
function stepLine(edges: number[], counts: number[]) {
  const x: number[] = [];
  const y: number[] = [];
  for (let k = 0; k < counts.length; k++) {
    x.push(edges[k], edges[k + 1]);
    y.push(counts[k], counts[k]);
  }
  return { ...line(x, y), line: { width: 1, color: SIGNAL, shape: "hv" } };
}

function isoWindow(range: string, customFrom: string, customTo: string): { t0: string; t1: string } {
  const t1 = range === "custom" && customTo ? new Date(customTo).toISOString() : new Date().toISOString();
  const t0 = resolveSince(range, customFrom) || new Date(Date.now() - 3600_000).toISOString();
  return { t0, t1 };
}

export default function SearchPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";

  const [range] = useUrlParam("range_range", "1h");
  const [customFrom] = useUrlParam("range_from", "");
  const [customTo] = useUrlParam("range_to", "");
  const [scale] = useUrlParam("scale", "linear");
  const isLog = scale === "log";
  const live = range !== "custom";

  const { t0, t1 } = useMemo(() => isoWindow(range, customFrom, customTo), [range, customFrom, customTo]);

  const [summary, setSummary] = useState<SearchSummaryResponse | null>(null);
  const [rate, setRate] = useState<SearchRateResponse | null>(null);
  const [snrHist, setSnrHist] = useState<SearchHistResponse | null>(null);
  const [dmHist, setDmHist] = useState<SearchHistResponse | null>(null);
  const [widthHist, setWidthHist] = useState<SearchHistResponse | null>(null);
  const [snrDm, setSnrDm] = useState<SearchScatterResponse | null>(null);
  const [dmTime, setDmTime] = useState<SearchScatterResponse | null>(null);
  const [beamMap, setBeamMap] = useState<SearchBeamMapResponse | null>(null);
  const [funnel, setFunnel] = useState<SearchFunnelResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      const summaryFetch = useMock ? mockGetSearchSummary(t0, t1) : getSearchSummary(t0, t1);
      const rateFetch = useMock ? mockGetSearchRate(t0, t1, 60) : getSearchRate(t0, t1, 60);
      const snrFetch = useMock
        ? mockGetSearchHist("snr", t0, t1, 30, isLog)
        : getSearchHist({ field: "snr", t0, t1, bins: 30, log: isLog });
      const dmFetch = useMock
        ? mockGetSearchHist("dm", t0, t1, 30, isLog)
        : getSearchHist({ field: "dm", t0, t1, bins: 30, log: isLog });
      const widthFetch = useMock
        ? mockGetSearchHist("width", t0, t1, 30, false)
        : getSearchHist({ field: "width", t0, t1, bins: 30, log: false });
      const snrDmFetch = useMock
        ? mockGetSearchScatter("dm", "snr", t0, t1, 20000)
        : getSearchScatter({ x: "dm", y: "snr", t0, t1, max_points: 20000 });
      const dmTimeFetch = useMock
        ? mockGetSearchScatter("time", "dm", t0, t1, 20000)
        : getSearchScatter({ x: "time", y: "dm", t0, t1, max_points: 20000 });
      const beamFetch = useMock ? mockGetSearchBeamMap(t0, t1) : getSearchBeamMap(t0, t1);
      const funnelFetch = useMock ? mockGetSearchFunnel(t0, t1, 600) : getSearchFunnel(t0, t1, 600);

      summaryFetch.then((r) => !cancelled && setSummary(r)).catch(() => undefined);
      rateFetch.then((r) => !cancelled && setRate(r)).catch(() => undefined);
      snrFetch.then((r) => !cancelled && setSnrHist(r)).catch(() => undefined);
      dmFetch.then((r) => !cancelled && setDmHist(r)).catch(() => undefined);
      widthFetch.then((r) => !cancelled && setWidthHist(r)).catch(() => undefined);
      snrDmFetch.then((r) => !cancelled && setSnrDm(r)).catch(() => undefined);
      dmTimeFetch.then((r) => !cancelled && setDmTime(r)).catch(() => undefined);
      beamFetch.then((r) => !cancelled && setBeamMap(r)).catch(() => undefined);
      funnelFetch.then((r) => !cancelled && setFunnel(r)).catch(() => undefined);
    }
    load();
    if (!live) return () => {
      cancelled = true;
    };
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [t0, t1, isLog, live, useMock]);

  // --- candidate rate -------------------------------------------------------
  const rateData = useMemo(() => {
    if (!rate) return [];
    const times = rate.t.map((s) => new Date(s * 1000).toISOString());
    const traces = Object.entries(rate.per_job).map(([, series]) => ({
      ...line(times, series, FAINT),
      line: { width: 1, color: FAINT },
    }));
    traces.push({ ...line(times, rate.total), line: { width: 1, color: SIGNAL } });
    return traces;
  }, [rate]);
  const rateLayout = useMemo(
    () => layout({ height: 220, margin: { l: 44, r: 8, t: 4, b: 32 }, xaxis: axis({}), yaxis: axis({}) }),
    [],
  );
  const rateRef = usePlotly(rateData, rateLayout, PLOT_CONFIG);

  // --- histograms -------------------------------------------------------
  function histPlot(hist: SearchHistResponse | null, logAxis: boolean) {
    const data = hist ? [stepLine(hist.edges, hist.counts)] : [];
    const l = layout({
      height: 220,
      margin: { l: 44, r: 8, t: 4, b: 32 },
      xaxis: axis({ type: logAxis ? "log" : "linear" }),
      yaxis: axis({}),
    });
    return { data, layout: l };
  }
  const snrPlot = useMemo(() => histPlot(snrHist, isLog), [snrHist, isLog]);
  const dmPlot = useMemo(() => histPlot(dmHist, isLog), [dmHist, isLog]);
  const widthPlot = useMemo(() => histPlot(widthHist, false), [widthHist]);
  const snrRef = usePlotly(snrPlot.data, snrPlot.layout, PLOT_CONFIG);
  const dmRef = usePlotly(dmPlot.data, dmPlot.layout, PLOT_CONFIG);
  const widthRef = usePlotly(widthPlot.data, widthPlot.layout, PLOT_CONFIG);

  // --- scatters -------------------------------------------------------
  const scatterMarker = { mode: "markers", type: "scattergl", marker: { size: 3, color: SIGNAL, opacity: 0.4 } };
  const snrDmData = useMemo(
    () => (snrDm ? [{ x: snrDm.x, y: snrDm.y, ...scatterMarker }] : []),
    [snrDm],
  );
  const snrDmLayout = useMemo(
    () =>
      layout({
        height: 260,
        margin: { l: 44, r: 8, t: 4, b: 32 },
        xaxis: axis({ type: isLog ? "log" : "linear", title: { text: "DM", font: { size: 11, color: MUTED, family: SANS } } }),
        yaxis: axis({ type: isLog ? "log" : "linear", title: { text: "SNR", font: { size: 11, color: MUTED, family: SANS } } }),
      }),
    [isLog],
  );
  const snrDmRef = usePlotly(snrDmData, snrDmLayout, PLOT_CONFIG);

  const dmTimeData = useMemo(
    () =>
      dmTime
        ? [{ x: dmTime.x.map((s) => new Date(s * 1000).toISOString()), y: dmTime.y, ...scatterMarker }]
        : [],
    [dmTime],
  );
  const dmTimeLayout = useMemo(
    () =>
      layout({
        height: 260,
        margin: { l: 44, r: 8, t: 4, b: 32 },
        xaxis: axis({}),
        yaxis: axis({ title: { text: "DM", font: { size: 11, color: MUTED, family: SANS } } }),
      }),
    [],
  );
  const dmTimeRef = usePlotly(dmTimeData, dmTimeLayout, PLOT_CONFIG);

  // --- beam occupancy -------------------------------------------------------
  const beamGrid = useMemo(() => {
    if (!beamMap) return null;
    const rows: number[][] = [];
    for (let r = 0; r < N_BEAM_ROWS; r++) {
      rows.push(beamMap.counts.slice(r * N_BEAM_COLS, (r + 1) * N_BEAM_COLS));
    }
    return rows;
  }, [beamMap]);
  const beamData = useMemo(
    () =>
      beamGrid
        ? [
            {
              z: beamGrid,
              type: "heatmap",
              colorscale: COLORSCALE,
              colorbar: { thickness: 8, outlinewidth: 0, tickfont: { size: 10, color: MUTED, family: SANS } },
              hoverinfo: "x+y+z",
            },
          ]
        : [],
    [beamGrid],
  );
  const beamLayout = useMemo(
    () =>
      layout({
        height: 260,
        margin: { l: 44, r: 8, t: 4, b: 32 },
        xaxis: axis({ title: { text: "beam mod 32", font: { size: 11, color: MUTED, family: SANS } } }),
        yaxis: axis({ title: { text: "beam / 32", font: { size: 11, color: MUTED, family: SANS } } }),
      }),
    [],
  );
  const beamRef = usePlotly(beamData, beamLayout, PLOT_CONFIG);

  // --- T2 funnel -------------------------------------------------------
  const funnelData = useMemo(() => {
    if (!funnel) return [];
    const times = funnel.t.map((s) => new Date(s * 1000).toISOString());
    return [
      { ...line(times, funnel.n_cands), line: { width: 1, color: SIGNAL } },
      { ...line(times, funnel.n_clusters), line: { width: 1, color: INK } },
      { ...line(times, funnel.n_stored), line: { width: 1, color: MUTED } },
      { ...line(times, funnel.n_vetoed), line: { width: 1, color: FAINT } },
    ];
  }, [funnel]);
  const funnelLayout = useMemo(
    () => layout({ height: 220, margin: { l: 44, r: 8, t: 4, b: 32 }, xaxis: axis({}), yaxis: axis({}) }),
    [],
  );
  const funnelRef = usePlotly(funnelData, funnelLayout, PLOT_CONFIG);

  return (
    <div>
      <div className="toolbar">
        <TimeRangePicker paramPrefix="range" defaultRange="1h" />
        <Segmented
          paramKey="scale"
          defaultValue="linear"
          options={[
            { value: "linear", label: "linear" },
            { value: "log", label: "log" },
          ]}
        />
      </div>

      <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
        {summary ? summarySentence(summary) : "Loading the candidate summary."}
      </p>

      <div className="grid-2col">
        <Panel title="candidate rate, per job in grey, total in blue">
          <div ref={rateRef} />
        </Panel>
        <Panel title="T2 funnel: candidates, clusters, stored, vetoed">
          <div ref={funnelRef} />
          <p className="legend-text">candidates, clusters, stored, vetoed (darkest to lightest).</p>
        </Panel>
        <Panel title="SNR histogram">
          <div ref={snrRef} />
        </Panel>
        <Panel title="DM histogram">
          <div ref={dmRef} />
        </Panel>
        <Panel title="width histogram">
          <div ref={widthRef} />
        </Panel>
        <Panel title="SNR vs DM">
          <div ref={snrDmRef} />
        </Panel>
        <Panel title="DM vs time">
          <div ref={dmTimeRef} />
        </Panel>
        <Panel title="beam occupancy, 512 beams as 16 x 32">
          <div ref={beamRef} />
          <p className="legend-text">{beamMap ? hottestBeamsSentence(beamMap.counts) : ""}</p>
        </Panel>
      </div>
    </div>
  );
}
