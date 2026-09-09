import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import AutosGrid from "../components/vis/AutosGrid";
import CrossesGrid from "../components/vis/CrossesGrid";
import CrossDetail from "../components/vis/CrossDetail";
import MatrixView from "../components/vis/MatrixView";
import CoherenceView from "../components/vis/CoherenceView";
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
 * The Visibilities tab (docs/plan.md section 2). Every control is URL-backed
 * so a view is bookmarkable; the four views (autos, crosses, matrix,
 * coherence) share one toolbar of set/quantity/units/reference/live-history
 * controls, per frontend/DESIGN.md.
 */
export default function VisPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";

  const [set, setSet] = useUrlParam("set", "live");
  const [view] = useUrlParam("view", "autos");
  const [quantity] = useUrlParam("quantity", "amplitude");
  const [aunits] = useUrlParam("aunits", "linear");
  const [punits] = useUrlParam("punits", "degrees");
  const [ref] = useUrlParam("ref", "raw");
  const [mode] = useUrlParam("mode", "live");
  const [histIdx, setHistIdx] = useUrlParam("vis_hist_i", "0");
  const [crossSel, setCrossSel] = useUrlParam("cross", "");

  const effectiveQuantity: FrontQuantity = view === "crosses" ? (quantity as FrontQuantity) : quantity === "coherence" ? "amplitude" : (quantity as FrontQuantity);
  const apiQuantity = QUANTITY_TO_API[effectiveQuantity];
  const apiUnits: VisUnits =
    effectiveQuantity === "phase" ? (punits === "radians" ? "rad" : "deg") : effectiveQuantity === "coherence" ? "linear" : aunits === "dB" ? "db" : (aunits as VisUnits);
  const apiRef = REF_TO_API[ref as FrontRef];
  const apiSet: VisSet = set === "wired" ? "wired" : "live";

  const [inputsResp, setInputsResp] = useState<VisInputsResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    const fetcher = useMock ? mockGetVisInputs : getVisInputs;
    fetcher()
      .then((r) => !cancelled && setInputsResp(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [useMock]);

  const inputsByPacket = useMemo(() => {
    const m = new Map<number, VisInputInfo>();
    for (const inp of inputsResp?.inputs ?? []) m.set(inp.packet_idx, inp);
    return m;
  }, [inputsResp]);

  const selectedInputs = useMemo(() => {
    const idxs = apiSet === "live" ? inputsResp?.sets.live ?? [] : inputsResp?.sets.wired ?? [];
    return idxs.map((p) => inputsByPacket.get(p)).filter((x): x is VisInputInfo => !!x);
  }, [inputsResp, apiSet, inputsByPacket]);

  // --- history-mode time slider -------------------------------------------
  const [histRange] = useUrlParam("vis_hist_range_range", "24h");
  const [histFrom] = useUrlParam("vis_hist_range_from", "");
  const [times, setTimes] = useState<number[]>([]);

  useEffect(() => {
    if (mode !== "history") return;
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
  }, [mode, histRange, histFrom, useMock]);

  const sliderIdx = Math.min(Math.max(parseInt(histIdx, 10) || 0, 0), Math.max(times.length - 1, 0));
  const ts: "latest" | number = mode === "history" && times.length > 0 ? times[sliderIdx] : "latest";

  // --- spectra for autos/crosses -------------------------------------------
  const [spectra, setSpectra] = useState<VisSpectraResponse | null>(null);
  const pairs: VisPairs = view === "crosses" ? "cross" : "auto";

  const fetchSpectra = useCallback(() => {
    const fetcher = useMock
      ? () => mockGetVisSpectra(ts, apiSet, pairs, apiQuantity, apiUnits, apiRef, 768)
      : () => getVisSpectra({ ts, set: apiSet, pairs, quantity: apiQuantity, units: apiUnits, ref: apiRef, nchan: 768 });
    fetcher()
      .then((r) => setSpectra(r))
      .catch(() => undefined);
  }, [useMock, ts, apiSet, pairs, apiQuantity, apiUnits, apiRef]);

  useEffect(() => {
    if (view !== "autos" && view !== "crosses") return;
    fetchSpectra();
    if (mode !== "live") return;
    const timer = setInterval(fetchSpectra, LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [view, mode, fetchSpectra]);

  const selected = useMemo(() => {
    if (!crossSel) return null;
    const [iStr, jStr] = crossSel.split(":");
    const i = Number(iStr);
    const j = Number(jStr);
    if (Number.isNaN(i) || Number.isNaN(j)) return null;
    return { i, j, antI: inputsByPacket.get(i)?.antenna ?? null, antJ: inputsByPacket.get(j)?.antenna ?? null };
  }, [crossSel, inputsByPacket]);

  if (!inputsResp) {
    return <p className="note">Loading the cached input inventory.</p>;
  }

  if (view === "crosses" && selected) {
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
    ...(view === "crosses" ? [{ value: "coherence", label: "coherence" }] : []),
  ];

  const resolvedTs = ts === "latest" ? (spectra?.ts ?? null) : ts;

  return (
    <div>
      <div className="toolbar">
        <Segmented
          paramKey="set"
          defaultValue="live"
          options={[
            { value: "live", label: "live" },
            { value: "wired", label: "wired" },
          ]}
          onChange={setSet}
        />
        <Segmented
          paramKey="view"
          defaultValue="autos"
          options={[
            { value: "autos", label: "autos" },
            { value: "crosses", label: "crosses" },
            { value: "matrix", label: "matrix" },
            { value: "coherence", label: "coherence" },
          ]}
        />
        <Segmented paramKey="quantity" defaultValue="amplitude" options={quantityOptions} />
        {effectiveQuantity === "phase" ? (
          <Segmented
            paramKey="punits"
            defaultValue="degrees"
            options={[
              { value: "degrees", label: "degrees" },
              { value: "radians", label: "radians" },
            ]}
          />
        ) : effectiveQuantity !== "coherence" ? (
          <Segmented
            paramKey="aunits"
            defaultValue="linear"
            options={[
              { value: "linear", label: "linear" },
              { value: "dB", label: "dB" },
              { value: "log10", label: "log10" },
            ]}
          />
        ) : null}
        <Segmented
          paramKey="ref"
          defaultValue="raw"
          options={[
            { value: "raw", label: "raw" },
            { value: "sun", label: "Sun-stopped" },
            { value: "cal", label: "cal-divided" },
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
        {mode === "history" && <TimeRangePicker paramPrefix="vis_hist_range" defaultRange="24h" />}
      </div>

      {mode === "history" && (
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

      {(view === "autos" || view === "crosses") && (
        <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
          {integrationSentence(resolvedTs, inputsResp.obs, apiRef, spectra?.flags ?? {})}
        </p>
      )}

      {view === "autos" && <AutosGrid inputs={selectedInputs} spectra={spectra} />}
      {view === "crosses" && (
        <CrossesGrid
          inputs={selectedInputs}
          spectra={spectra}
          onSelect={(i, j) => setCrossSel(`${i}:${j}`)}
        />
      )}
      {view === "matrix" && (
        <MatrixView
          ts={ts}
          set={apiSet}
          quantity={apiQuantity}
          units={apiUnits}
          inputsByPacket={inputsByPacket}
          useMock={useMock}
        />
      )}
      {view === "coherence" && (
        <CoherenceView set={apiSet} inputsByPacket={inputsByPacket} useMock={useMock} />
      )}
    </div>
  );
}
