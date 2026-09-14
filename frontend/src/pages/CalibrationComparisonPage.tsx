import { useEffect, useMemo, useState } from "react";
import { api, Evidence, Json, LOCAL, Notice, ProductPlots, useResource } from "../components/Workspace";
import { wallTime } from "../components/TimeControls";

const today = () => wallTime(new Date().toISOString(), LOCAL).slice(0, 10);
const clock = (s: string) => new Intl.DateTimeFormat("en-US", {
  timeZone: LOCAL, month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short"
}).format(new Date(s));

export default function CalibrationComparisonPage() {
  const [day, setDay] = useState(today), [referenceId, setReferenceId] = useState("");
  const [selected, setSelected] = useState<string[]>([]), [fmin, setFmin] = useState("390.625"), [fmax, setFmax] = useState("484.375");
  const [product, setProduct] = useState<Json | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const { data, error: referenceError } = useResource(`/api/science/calibration-references?comparison_date=${encodeURIComponent(day)}`);
  const { data: catalog, error: catalogError } = useResource("/api/science/catalog");
  const references: Json[] = data?.references ?? [];
  const reference = references.find(r => r.id === referenceId) ?? references[0];
  const currentPlan = data?.comparison_date === day;
  useEffect(() => {
    if (data?.default_id && !references.some(r => r.id === referenceId)) setReferenceId(data.default_id);
  }, [data, referenceId]);
  const baselines = useMemo(() => {
    const antennas = new Set<number>(reference?.antennas ?? []);
    const inputAnt = new Map<number, number>((catalog?.inputs ?? []).map((i: Json) => [i.packet_idx, i.antenna]));
    return ((catalog?.baselines ?? []) as Json[]).filter(b => antennas.has(inputAnt.get(b.i) ?? -1) && antennas.has(inputAnt.get(b.j) ?? -1));
  }, [catalog, reference]);
  const key = (b: Json) => `${b.i},${b.j}`;
  useEffect(() => {
    if (!baselines.length) return;
    setSelected(current => {
      const valid = current.filter(p => baselines.some(b => key(b) === p));
      return valid.length ? valid : [key(baselines.find(b => b.orientation === "NS" && b.length_m >= 7) ?? baselines[0])];
    });
  }, [baselines]);
  useEffect(() => { setProduct(null); setError(""); }, [day, referenceId, selected.join(";"), fmin, fmax]);
  const render = async () => {
    if (!reference || !currentPlan || !reference.can_render || busy) return;
    setBusy(true); setError(""); setProduct(null);
    try {
      const request = {
        pairs: selected.map(p => p.split(",").map(Number)),
        t0: reference.comparison_window[0], t1: reference.comparison_window[1],
        compare_t0: reference.source_window[0], compare_t1: reference.source_window[1],
        fmin: Number(fmin), fmax: Number(fmax), kind: "phase_spectrum", reference: "sun",
        resolution: "recorded", time_tz: LOCAL, calibration_reference_id: reference.id,
      };
      setProduct(await api("/api/science/render", request));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="workspace-page">
    <div className="page-heading"><div><p className="eyebrow">Calibration comparison</p><h2>Does the baseline phase still match?</h2>
      <p className="muted">Start with the recorded calibration, choose a day, and compare the same baselines against its actual Sun solve window.</p></div></div>
    {(referenceError || catalogError || error) && <Notice>{error || referenceError || catalogError}</Notice>}
    {data?.state === "unavailable" && <Notice>{data.reason}</Notice>}
    <div className="control-surface">
      <div className="field-row"><label>Recorded calibration reference<select aria-label="Recorded calibration reference" value={reference?.id ?? ""} disabled={!references.length || busy} onChange={e => setReferenceId(e.target.value)}>
        {!references.length && <option value="">No verified reference available</option>}
        {references.map(r => <option value={r.id} key={r.id}>{r.label} · solved {r.source_local_date}</option>)}
      </select></label><label>Compare with · OVRO local day<input aria-label="Comparison day" type="date" value={day} max={today()} disabled={busy} onChange={e => e.target.value && setDay(e.target.value)} /></label>
      <button disabled={busy} onClick={() => setDay(today())}>Today</button><button disabled={busy} onClick={() => {
        const d = new Date(today() + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() - 1); setDay(d.toISOString().slice(0, 10));
      }}>Yesterday</button></div>
      {reference && <section aria-label="Comparison windows" className="evidence-summary">
        <h3>Two matched clock windows</h3>
        <p><strong>Calibration day:</strong> {clock(reference.source_window[0])} – {clock(reference.source_window[1])}</p>
        <p><strong>Selected day:</strong> {clock(reference.comparison_window[0])} – {clock(reference.comparison_window[1])}</p>
        <p className="muted">Window comes from the saved recipe report, not the filename. Both days are Sun fringe-stopped independently. The saved calibration is not applied to either curve.</p>
        <p className="muted">Recorded product {reference.product_id ?? "ID unknown"} · {reference.antennas.length} reported calibration antennas: {reference.antennas.join(", ")}</p>
        <details><summary>Product, report and layout evidence</summary><Evidence value={reference} /></details>
        {!currentPlan && <p role="status">Updating comparison day…</p>}
        {reference.reason && <Notice>{reference.reason}</Notice>}
      </section>}
      <h3>Choose physical baselines</h3><p className="muted">Starts with one long N–S baseline. Add up to three, all within the recorded calibration antenna set.</p>
      {selected.map((pair, index) => <div className="field-row" key={index}><label>Baseline {index + 1}<select aria-label={`Comparison baseline ${index + 1}`} value={pair} disabled={busy} onChange={e => setSelected(v => v.map((p, n) => n === index ? e.target.value : p))}>
        {baselines.filter(b => key(b) === pair || !selected.includes(key(b))).map(b => <option key={key(b)} value={key(b)}>{Number(b.length_m).toFixed(2)} m {b.orientation} · {b.label}</option>)}
      </select></label>{selected.length > 1 && <button disabled={busy} onClick={() => setSelected(v => v.filter((_, n) => n !== index))}>Remove</button>}</div>)}
      <button disabled={busy || selected.length >= 3 || selected.length >= baselines.length} onClick={() => {
        const next = baselines.find(b => !selected.includes(key(b))); if (next) setSelected(v => [...v, key(next)]);
      }}>Add baseline</button>
      <div className="field-row"><label>Frequency min · MHz<input type="number" value={fmin} step="0.01" disabled={busy} onChange={e => setFmin(e.target.value)} /></label>
        <label>Frequency max · MHz<input type="number" value={fmax} step="0.01" disabled={busy} onChange={e => setFmax(e.target.value)} /></label></div>
      <button className="primary" disabled={busy || !currentPlan || !reference?.can_render || !selected.length} onClick={() => void render()}>{busy ? "Reading two selected windows and rendering…" : "Read both windows and compare phase"}</button>
      <p className="muted">Explicit native-recording read, at most one hour and 64 MiB of selected complex samples per window. No new calibration, deployment or automatic analysis.</p>
    </div>
    {(data?.warnings ?? []).map((warning: string, n: number) => <Notice key={n}>{warning}</Notice>)}
    {product && <ProductPlots product={product} />}
    {!product && !busy && <div className="empty-science"><p>The result will show selected-day and calibration-day phase versus frequency for each baseline. A drift prompts investigation, not a deployment decision.</p></div>}
  </div>;
}
