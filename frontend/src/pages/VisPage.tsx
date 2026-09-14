import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import AutosGrid from "../components/vis/AutosGrid";
import CrossesGrid from "../components/vis/CrossesGrid";
import CrossDetail from "../components/vis/CrossDetail";
import MatrixView from "../components/vis/MatrixView";
import CoherenceView from "../components/vis/CoherenceView";
import WaterfallMatrix from "../components/vis/WaterfallMatrix";
import VisFigureView, { type FigureQuantity, type FigureView } from "../components/vis/VisFigureView";
import { useUrlParam } from "../lib/useUrlParam";
import { resolveSince } from "../lib/timeRange";
import { formatUnixUtc } from "../lib/statusSentence";
import { integrationSentence } from "../lib/visText";
import { getVisInputs, getVisSpectra, getVisTimes } from "../lib/api";
import { mockGetVisInputs, mockGetVisSpectra, mockGetVisTimes } from "../lib/mockVis";
import type {
  VisInputInfo,
  VisInputsResponse,
  VisPairs,
  VisQuantity,
  VisRef,
  VisSet,
  VisSpectraResponse,
  VisUnits,
} from "../lib/types";

const LIVE_POLL_MS = 10_000;

type FrontQuantity = "amplitude" | "phase" | "real" | "imag" | "coherence";
type FrontRef = "raw" | "sun" | "cal";

const QUANTITY_TO_API: Record<FrontQuantity, VisQuantity> = {
  amplitude: "amp",
  phase: "phase",
  real: "real",
  imag: "imag",
  coherence: "coh",
};

const REF_TO_API: Record<FrontRef, VisRef> = { raw: "raw", sun: "sun", cal: "cal" };

/**
 * The Visibilities tab (docs/plan.md section 2). The default view is the
 * server-rendered figure (one `<img>`, cached and prefetched, per the
 * operator's 2026-09-08 "these plots load one by one on webpage, ensure its
 * cached in browser so its quite fast"); the previous interactive
 * Plotly/uPlot views (autos/crosses/matrix/coherence) are kept behind a
 * fourth `view=interactive` choice, not the default.
 */
export default function VisPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";

  const [set, setSet] = useUrlParam("set", "live");
  // Top-level view: the three server-figure views, plus the old interactive
  // page behind its own choice (default was "autos" pre-figures; now
  // "waterfalls" per the operator's figures-first direction).
  const [view] = useUrlParam("view", "waterfalls");
  const isInteractive = view === "interactive";
  // The old page's own sub-view, now namespaced so it does not collide with
  // the new top-level `view` param.
  const [iview] = useUrlParam("iview", "autos");
  const [quantity] = useUrlParam("quantity", "amplitude");
  const [aunits] = useUrlParam("aunits", "linear");
  const [punits] = useUrlParam("punits", "degrees");
  const [ref] = useUrlParam("ref", "raw");
  const [mode] = useUrlParam("mode", "live");
  const [histIdx, setHistIdx] = useUrlParam("vis_hist_i", "0");
  const [crossSel, setCrossSel] = useUrlParam("cross", "");
  // The interactive crosses view defaults to the waterfall matrix (operator
  // feedback, 2026-09-08: the phase line grid "doesn't look great"); `spectra`
  // is the older per-baseline line grid, still available via the toggle.
  const [crossView] = useUrlParam("cview", "waterfalls");
  const [wfRange] = useUrlParam("vis_wf_range_range", "2h");
  const [wfFrom] = useUrlParam("vis_wf_range_from", "");
  const isCrossesWaterfalls = isInteractive && iview === "crosses" && crossView === "waterfalls";

  const effectiveQuantity: FrontQuantity =
    isInteractive && iview === "crosses"
      ? (quantity as FrontQuantity)
      : isInteractive && quantity === "coherence"
        ? "amplitude"
        : (quantity as FrontQuantity);
  const apiQuantity = QUANTITY_TO_API[effectiveQuantity];
  const apiUnits: VisUnits =
    effectiveQuantity === "phase" ? (punits === "radians" ? "rad" : "deg") : effectiveQuantity === "coherence" ? "linear" : aunits === "dB" ? "db" : (aunits as VisUnits);
  const apiRef = REF_TO_API[ref as FrontRef];
  const apiSet: VisSet = set === "wired" ? "wired" : "live";

  const [inputsResp, setInputsResp] = useState<VisInputsResponse | null>(null);
  useEffect(() => {
    if (!isInteractive) return;
    let cancelled = false;
    const fetcher = useMock ? mockGetVisInputs : getVisInputs;
    fetcher()
      .then((r) => !cancelled && setInputsResp(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [useMock, isInteractive]);

  const inputsByPacket = useMemo(() => {
    const m = new Map<number, VisInputInfo>();
    for (const inp of inputsResp?.inputs ?? []) m.set(inp.packet_idx, inp);
    return m;
  }, [inputsResp]);

  const selectedInputs = useMemo(() => {
    const idxs = apiSet === "live" ? inputsResp?.sets.live ?? [] : inputsResp?.sets.wired ?? [];
    return idxs.map((p) => inputsByPacket.get(p)).filter((x): x is VisInputInfo => !!x);
  }, [inputsResp, apiSet, inputsByPacket]);

  // --- history-mode time slider (interactive view only) -------------------
  const [histRange] = useUrlParam("vis_hist_range_range", "24h");
  const [histFrom] = useUrlParam("vis_hist_range_from", "");
  const [times, setTimes] = useState<number[]>([]);

  useEffect(() => {
    if (!isInteractive || mode !== "history") return;
    let cancelled = false;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(histRange, histFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const fetcher = useMock ? mockGetVisTimes : getVisTimes;
    fetcher(t0, t1)
      .then((r) => {
        if (cancelled) return;
        setTimes(r.t);
        setHistIdx("0");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isInteractive, mode, histRange, histFrom, useMock]);

  const sliderIdx = Math.min(Math.max(parseInt(histIdx, 10) || 0, 0), Math.max(times.length - 1, 0));
  const ts: "latest" | number = isInteractive && mode === "history" && times.length > 0 ? times[sliderIdx] : "latest";

  // --- spectra for the interactive autos/crosses sub-views -----------------
  const [spectra, setSpectra] = useState<VisSpectraResponse | null>(null);
  const pairs: VisPairs = iview === "crosses" ? "cross" : "auto";

  const fetchSpectra = useCallback(() => {
    const fetcher = useMock
      ? () => mockGetVisSpectra(ts, apiSet, pairs, apiQuantity, apiUnits, apiRef, 768)
      : () => getVisSpectra({ ts, set: apiSet, pairs, quantity: apiQuantity, units: apiUnits, ref: apiRef, nchan: 768 });
    fetcher()
      .then((r) => setSpectra(r))
      .catch(() => undefined);
  }, [useMock, ts, apiSet, pairs, apiQuantity, apiUnits, apiRef]);

  // The crosses view's waterfall-matrix subview fetches its own data per
  // baseline (WaterfallMatrix below); skip the line-grid spectra fetch
  // entirely then, so the default view does not pay for data it never shows.
  const needsSpectra = isInteractive && (iview === "autos" || (iview === "crosses" && crossView === "spectra"));

  useEffect(() => {
    if (!needsSpectra) return;
    fetchSpectra();
    if (mode !== "live") return;
    const timer = setInterval(fetchSpectra, LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [needsSpectra, mode, fetchSpectra]);

  const wfWindow = useMemo(() => {
    const t1 = new Date().toISOString();
    const t0 = resolveSince(wfRange, wfFrom) || new Date(Date.now() - 2 * 3600_000).toISOString();
    return { t0, t1 };
    // Re-derive t1 = "now" only when the range/from inputs themselves change,
    // not on every render, so WaterfallMatrix's cache keys stay stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wfRange, wfFrom]);

  const selected = useMemo(() => {
    if (!crossSel) return null;
    const [iStr, jStr] = crossSel.split(":");
    const i = Number(iStr);
    const j = Number(jStr);
    if (Number.isNaN(i) || Number.isNaN(j)) return null;
    return { i, j, antI: inputsByPacket.get(i)?.antenna ?? null, antJ: inputsByPacket.get(j)?.antenna ?? null };
  }, [crossSel, inputsByPacket]);

  if (isInteractive && !inputsResp) {
    return <p className="note">Loading the cached input inventory.</p>;
  }

  if (isInteractive && iview === "crosses" && selected) {
    return (
      <CrossDetail
        i={selected.i}
        j={selected.j}
        antI={selected.antI}
        antJ={selected.antJ}
        set={apiSet}
        quantity={apiQuantity}
        units={apiUnits}
        reference={apiRef}
        useMock={useMock}
        onBack={() => setCrossSel("")}
      />
    );
  }

  const quantityOptions = [
    { value: "amplitude", label: "amplitude" },
    { value: "phase", label: "phase" },
    { value: "real", label: "real" },
    { value: "imag", label: "imag" },
    ...(!isInteractive || iview === "crosses" ? [{ value: "coherence", label: "coherence" }] : []),
  ];

  const resolvedTs = ts === "latest" ? (spectra?.ts ?? null) : ts;

  return (
    <div>
      <div className="toolbar">
        <Segmented
          paramKey="set"
          defaultValue="live"
          options={[
            { value: "live", label: "intended" },
            { value: "wired", label: "wired" },
          ]}
          onChange={setSet}
        />
        <Segmented
          paramKey="view"
          defaultValue="waterfalls"
          options={[
            { value: "waterfalls", label: "waterfalls" },
            { value: "spectra", label: "spectra" },
            { value: "autos", label: "autos" },
            { value: "interactive", label: "interactive" },
          ]}
        />
        {isInteractive && (
          <Segmented
            paramKey="iview"
            defaultValue="autos"
            options={[
              { value: "autos", label: "autos" },
              { value: "crosses", label: "crosses" },
              { value: "matrix", label: "matrix" },
              { value: "coherence", label: "coherence" },
            ]}
          />
        )}
        {isInteractive && iview === "crosses" && (
          <Segmented
            paramKey="cview"
            defaultValue="waterfalls"
            options={[
              { value: "waterfalls", label: "waterfalls" },
              { value: "spectra", label: "spectra" },
            ]}
          />
        )}
        {(!isInteractive || iview !== "coherence") && (
          <Segmented paramKey="quantity" defaultValue="amplitude" options={quantityOptions} />
        )}
        {isInteractive && effectiveQuantity === "phase" && (
          <Segmented
            paramKey="punits"
            defaultValue="degrees"
            options={[
              { value: "degrees", label: "degrees" },
              { value: "radians", label: "radians" },
            ]}
          />
        )}
        {isInteractive && effectiveQuantity !== "phase" && effectiveQuantity !== "coherence" && (
          <Segmented
            paramKey="aunits"
            defaultValue="linear"
            options={[
              { value: "linear", label: "linear" },
              { value: "dB", label: "dB" },
              { value: "log10", label: "log10" },
            ]}
          />
        )}
        <Segmented
          paramKey="ref"
          defaultValue="raw"
          options={[
            { value: "raw", label: "raw" },
            { value: "sun", label: "Sun-stopped" },
            { value: "cal", label: "cal-divided" },
          ]}
        />
        {isInteractive && !isCrossesWaterfalls && (
          <Segmented
            paramKey="mode"
            defaultValue="live"
            options={[
              { value: "live", label: "live" },
              { value: "history", label: "history" },
            ]}
          />
        )}
        {isInteractive && !isCrossesWaterfalls && mode === "history" && (
          <TimeRangePicker paramPrefix="vis_hist_range" defaultRange="24h" />
        )}
        {isInteractive && isCrossesWaterfalls && <TimeRangePicker paramPrefix="vis_wf_range" defaultRange="2h" />}
      </div>

      {!isInteractive && (
        <VisFigureView
          set={apiSet}
          reference={apiRef}
          view={view as FigureView}
          quantity={apiQuantity as FigureQuantity}
        />
      )}

      {isInteractive && !isCrossesWaterfalls && mode === "history" && (
        <div className="slider">
          <input
            type="range"
            min={0}
            max={Math.max(times.length - 1, 0)}
            value={sliderIdx}
            onChange={(e) => setHistIdx(e.target.value)}
            aria-label="visibilities time"
          />
          <span>{times.length ? formatUnixUtc(times[sliderIdx]) : "no integrations in this window"}</span>
        </div>
      )}

      {isInteractive && needsSpectra && (
        <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
          {integrationSentence(resolvedTs, inputsResp?.obs ?? null, apiRef, spectra?.flags ?? {})}
        </p>
      )}

      {isInteractive && iview === "autos" && <AutosGrid inputs={selectedInputs} spectra={spectra} />}
      {isInteractive && iview === "crosses" && crossView === "spectra" && (
        <CrossesGrid
          inputs={selectedInputs}
          spectra={spectra}
          onSelect={(i, j) => setCrossSel(`${i}:${j}`)}
        />
      )}
      {isInteractive && isCrossesWaterfalls && (
        <WaterfallMatrix
          inputs={selectedInputs}
          quantity={apiQuantity}
          units={apiUnits}
          reference={apiRef}
          t0={wfWindow.t0}
          t1={wfWindow.t1}
          useMock={useMock}
          onSelect={(i, j) => setCrossSel(`${i}:${j}`)}
        />
      )}
      {isInteractive && iview === "matrix" && (
        <MatrixView
          ts={ts}
          set={apiSet}
          quantity={apiQuantity}
          units={apiUnits}
          inputsByPacket={inputsByPacket}
          useMock={useMock}
        />
      )}
      {isInteractive && iview === "coherence" && (
        <CoherenceView set={apiSet} inputsByPacket={inputsByPacket} useMock={useMock} />
      )}
    </div>
  );
}
