import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, Evidence, Json, Notice, ProductImage, stamp, useResource } from "../components/Workspace";
import CalibrationTimeField from "../components/CalibrationTimeField";
import CalibrationSkyPlanner from "../components/CalibrationSkyPlanner";
import { calibrationProgress } from "../components/calibrationProgress";
import { LOCAL } from "../components/timeZones";
import { playbackStamp, type PlaybackTimeZone } from "../components/snapPlaybackModel";
import "../calibration.css";

const ROOT = "/api/calibration";
const STEPS = ["Prepare", "Solve & inspect", "Validate on sky", "Generate beams", "Upload dry run"];
const SOURCES = [["sun", "Sun"], ["cyg-a", "Cyg A"], ["cas-a", "Cas A"]];
const asInput = (value: string) => value?.replace(" ", "T").replace(/Z$/, "").slice(0, 16) ?? "";
const human = (value: unknown) => typeof value === "number" ? value.toFixed(2) : value == null ? "—" : String(value);

function Source({value, onChange}: {value: string; onChange: (value: string) => void}) {
  return <select value={value} onChange={event => onChange(event.target.value)}>{SOURCES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>;
}

function figureTitle(item: Json) {
  const name = item.name.replace(/\.png$/i, "").replace(/[_-]+/g, " ");
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function Gallery({items, title}: {items: Json[]; title: string}) {
  const [filter, setFilter] = useState("");
  const [limit, setLimit] = useState(4);
  const images = items.filter(item => item.kind === "image" && item.name.toLowerCase().includes(filter.toLowerCase()));
  images.sort((first, second) => Number(second.name.startsWith("transit-")) - Number(first.name.startsWith("transit-")) || (second.created ?? 0) - (first.created ?? 0));
  return <section className="cal-gallery">
    <div className="cal-section-heading"><h3>{title}</h3><span>{images.length} plots · click to zoom</span></div>
    {images.length > 4 && <div className="cal-gallery-tools"><input aria-label="Filter plots" value={filter} onChange={event => {setFilter(event.target.value); setLimit(4);}} placeholder="Find a plot…"/></div>}
    {images.length ? <div className={`cal-plot-grid ${images.length === 1 ? "single" : ""}`}>{images.slice(0, limit).map(item => <figure key={item.id}>
      <ProductImage url={item.url} label={figureTitle(item)}/>
      <figcaption><span>{figureTitle(item)}</span><a href={item.url} download>PNG ↗</a></figcaption>
    </figure>)}</div> : <div className="cal-empty">No saved plots yet.</div>}
    {images.length > limit && <button className="cal-gallery-more" onClick={() => setLimit(current => current + 4)}>Show more plots</button>}
    <details><summary>Notebooks & data</summary><div className="cal-downloads">{items.filter(item => item.kind !== "image").map(item => <a key={item.id} href={item.url} download>{item.name}</a>)}</div></details>
  </section>;
}

function BeamSky({tag, date}: {tag: string; date: string}) {
  const [day, setDay] = useState(date || new Date().toISOString().slice(0, 10));
  const {data, error} = useResource(day ? `${ROOT}/builds/${tag}/sky?date=${day}` : "");
  return <section className="cal-card cal-sky">
    <div className="cal-section-heading"><h3>Beams & source tracks</h3><span>{data?.beam_count ?? 512} pointings</span></div>
    <div className="cal-sky-controls"><label>Track date · UTC<input type="date" value={day} onChange={event => setDay(event.target.value)}/></label><span>{data?.product_label ?? "Saved beam positions"} · ellipse view</span></div>
    {error ? <Notice>{error}</Notice> : data?.date === day && data?.url ? <ProductImage url={data.url} label="Beam ellipses and source tracks at OVRO"/> : <p className="cal-caption">Loading sky view…</p>}
    <p className="cal-caption">Ellipses are a visual guide, not a coverage or transit-timing prediction. Beam generation and checks use the exact response.</p>
  </section>;
}

export default function CalibrationWorkspacePage() {
  const {tag: routeTag} = useParams();
  const [date, setDate] = useState("");
  const [formRevision, setFormRevision] = useState(0);
  const [overview, setOverview] = useState<Json | null>(null);
  const [form, setForm] = useState<Json | null>(null);
  const [tag, setTag] = useState(() => routeTag ?? "");
  const [detail, setDetail] = useState<Json | null>(null);
  const [step, setStep] = useState(tag ? 1 : 0);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [staticOn, setStaticOn] = useState(true);
  const [timeZone, setTimeZone] = useState<PlaybackTimeZone>(LOCAL);
  const [timeErrors, setTimeErrors] = useState<Record<string, boolean>>({});
  const markTimeError = useCallback((id: string, invalid: boolean) => {
    setTimeErrors(current => current[id] === invalid ? current : {...current, [id]: invalid});
    if (invalid) setReviewed(false);
  }, []);
  const [phaseSource, setPhaseSource] = useState("sun");
  const [phaseView, setPhaseView] = useState("both");
  const [targets, setTargets] = useState<number[]>([]);
  const [phaseWindow, setPhaseWindow] = useState<string[]>([]);
  const [transit, setTransit] = useState<Json>({source: "cyg-a", date: "", fmin: 390.625, fmax: 484.375, off_minutes: 90, minimum_snr: 5, compare_reference: true});
  const [historyDays, setHistoryDays] = useState(3);
  const [altitude, setAltitude] = useState(20);
  const [plainGrid, setPlainGrid] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [accept, setAccept] = useState(false);
  const [logId, setLogId] = useState<number | null>(null);
  const [log, setLog] = useState("");
  const {data: stations} = useResource("/api/science/catalog");
  const {data: deployment} = useResource(ROOT + "/deployment/status", 15000);
  const [deployDetail, setDeployDetail] = useState<Json>({});

  const refreshOverview = () => api(ROOT + (date ? `?date=${date}` : "")).then(setOverview);
  useEffect(() => {let active = true; api(ROOT + (date ? `?date=${date}` : "")).then(result => {
    if (!active) return;
    setOverview(result);
    const defaults = result.defaults;
    setForm({...defaults, tag: `sun_${defaults.date.replace(/-/g, "")}_${new Date().toISOString().slice(11,19).replace(/:/g, "")}`, hardware_epoch_note: ""});
    setTimeErrors({});
    setStaticOn(Boolean(defaults.static_window));
    setTransit(current => ({...current, date: defaults.date}));
    setReviewed(false);
  }).catch(reason => setError(reason.message)); return () => {active = false;};}, [date, formRevision]);
  useEffect(() => {
    setDetail(null); setDeployDetail({}); setAccept(false); setTargets([]); setPhaseWindow([]);
    if (!tag) return;
    let active = true;
    const load = () => api(`${ROOT}/builds/${tag}`).then(result => {if (active) setDetail(result);}).catch(reason => {if (active) setError(reason.message);});
    load(); const timer = window.setInterval(load, 4000);
    return () => {active = false; clearInterval(timer);};
  }, [tag]);
  useEffect(() => {if (detail?.request && !phaseWindow.length) {
    const values = detail.request.params;
    setPhaseWindow(values.source_window);
    setTargets(values.antennas.filter((antenna: number) => antenna !== values.ref_ant).slice(0, 3));
  }}, [detail, phaseWindow.length]);
  useEffect(() => {if (!tag || !detail?.summary) return; let active = true;
    const load = () => api(`${ROOT}/deployment/builds/${tag}`).then(result => {if (active) setDeployDetail(result);}).catch(() => {});
    load(); const timer = window.setInterval(load, 6000); return () => {active = false; clearInterval(timer);};
  }, [tag, Boolean(detail?.summary)]);
  useEffect(() => {if (logId == null) return; let active = true;
    const load = () => api(`${ROOT}/jobs/${logId}/log`).then(result => {if (active) setLog(result.text);}).catch(reason => setError(reason.message));
    load(); const timer = window.setInterval(load, 3000); return () => {active = false; clearInterval(timer);};
  }, [logId]);

  const perform = async (url: string, body: Json, success: string, headers?: Record<string,string>) => {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api(url, body, headers);
      setMessage(success);
      if (result.tag && result.tag !== tag) setTag(result.tag);
      if (result.job_id) setLogId(result.job_id);
      await refreshOverview();
      if (tag) setDetail(await api(`${ROOT}/builds/${tag}`));
      return result;
    } catch (reason) {setError((reason as Error).message); return null;}
    finally {setBusy(false);}
  };
  const runAction = (action: string, values: Json = {}) => perform(`${ROOT}/builds/${tag}/actions`, {action, ...values}, "Queued. You can leave this page; the worker keeps the job and its outputs.");
  const patch = (key: string, value: unknown) => {setForm(current => ({...current, [key]: value})); setReviewed(false);};
  const chooseCandidate = (value: string) => {setTag(value); setDetail(null); setDeployDetail({}); setReviewed(false); setStep(value ? 1 : 0); if (!value) {setForm(null); setDate(""); setFormRevision(v => v + 1);}};
  const candidate = detail?.request?.params;
  const hasCal = Boolean(detail?.summary?.paths?.cal_h5);
  const hasWeights = Boolean(detail?.summary?.has_weights);
  const artifacts: Json[] = detail?.artifacts ?? [];
  const jobs: Json[] = detail?.jobs ?? [];
  const pending = jobs.some(job => ["running", "queued"].includes(job.state));
  const preparing = Boolean(date && form?.date !== date);
  const disabled = busy || pending || preparing;
  const invalidTimes = Object.entries(timeErrors).some(([id, invalid]) => invalid && (staticOn || !id.startsWith("static")));
  const sunPeak = overview?.defaults?.alt_max_utc ? Date.parse(overview.defaults.alt_max_utc) / 1000 : NaN;
  const transitResults: Json[] = (detail?.actions ?? []).filter((item: Json) => item.action === "transit");
  const latestTransit = transitResults[transitResults.length - 1];
  const progress = calibrationProgress(detail, deployDetail, reviewed);
  const label = (antenna: number) => {const station = stations?.inputs?.find((item: Json) => item.antenna === antenna)?.station; return station ? `${station} · ${antenna}` : `Ant ${antenna}`;};
  const stageItems = (actions: string[]) => {
    const latestIds = actions.map(action => [...(detail?.actions ?? [])].reverse().find((entry: Json) => entry.action === action)?.action_id).filter(Boolean);
    return artifacts.filter(item => latestIds.some(id => item.relative.includes(id)));
  };

  return <div className="cal-workspace">
    <header className="cal-hero"><div><h2>Calibration</h2><p>Solar solve · sky validation · 512 beams</p></div><span className="cal-safety"><span/> Upload testing · dry run only</span></header>
    <div className="cal-context"><label>Saved candidate<select value={tag} onChange={event => chooseCandidate(event.target.value)}><option value="">New calibration</option>{(overview?.builds ?? []).map((item: Json) => <option key={item.tag} value={item.tag}>{item.tag}{item.imported ? " · recorded reference" : item.has_weights ? " · 512 beams" : item.has_cal ? " · solved" : " · pending"}</option>)}</select></label><button onClick={() => chooseCandidate("")}>New calibration</button><button disabled={busy} onClick={() => perform(ROOT + "/import-reference", {date: overview?.defaults?.date}, "Recorded reference loaded for offline diagnostics; not a new solve.").then(result => {if (result) setStep(1);})}>Inspect deployed calibration</button><span className="cal-context-note">{candidate ? `${candidate.antennas.length} antennas · reference ${candidate.ref_ant}` : "Start from deployed antennas"}</span></div>
    <nav className="cal-steps" aria-label="Calibration workflow">{STEPS.map((name, index) => <button key={name} className={`${progress[index].state}${step === index ? " selected" : ""}`} data-state={progress[index].state} onClick={() => setStep(index)} aria-current={step === index ? "step" : undefined}><span>{String(index + 1).padStart(2, "0")}</span>{name}<small>{progress[index].label}</small></button>)}</nav>
    {(error || message) && <div className={error ? "cal-feedback error" : "cal-feedback"} role="status">{error || message}<button onClick={() => {setError(""); setMessage("");}} aria-label="Dismiss message">×</button></div>}
    {step === 0 && <div className="cal-columns"><section className="cal-card"><div className="cal-section-heading"><h3>Prepare a solar solve</h3><span>Phase-only · per-channel SVD</span></div>{form ? <>
      <div className="cal-fields"><label>Solar date<input type="date" value={date || overview?.defaults?.date || ""} onChange={event => {setReviewed(false); setDate(event.target.value);}}/></label><label>Calibrator<select value="sun" disabled><option value="sun">Sun</option><option disabled>Cyg A · solve not enabled</option><option disabled>Cas A · solve not enabled</option></select></label></div>
      {form.defaults_basis && <p className="cal-caption" aria-label="Calibration starting point">Starting point: {form.defaults_basis.label}{form.defaults_basis.date_uploaded ? ` · uploaded ${form.defaults_basis.date_uploaded.slice(0, 10)}` : ""}.</p>}
      <label>Revision tag<input value={form.tag} onChange={event => patch("tag", event.target.value)} maxLength={64}/></label>
      <section className="cal-sun-peak" aria-label="Sun altitude maximum"><div>Sun altitude maximum <strong>{human(overview?.defaults?.alt_max_deg)}°</strong></div><div className="cal-sun-clocks">{[LOCAL, "UTC"].map(zone => <div key={zone}><span>{zone === LOCAL ? "OVRO local" : "UTC"}</span><time dateTime={overview?.defaults?.alt_max_utc}>{Number.isFinite(sunPeak) ? playbackStamp(sunPeak, zone as PlaybackTimeZone) : "—"}</time></div>)}</div></section>
      <div className="cal-time-zone"><span>Window times</span><div role="group" aria-label="Solve time zone">{[LOCAL, "UTC"].map(zone => <button key={zone} type="button" aria-pressed={timeZone === zone} onClick={() => {if (invalidTimes) setReviewed(false); setTimeZone(zone as PlaybackTimeZone);}}>{zone === LOCAL ? "OVRO local · PDT/PST" : "UTC"}</button>)}</div></div>
      <div className="cal-fields">{[0, 1].map(index => <CalibrationTimeField key={index} id={`solve-${index}`} label={`Solve ${index ? "end" : "start"}`} zone={timeZone} value={form.source_window[index]} onError={markTimeError} onChange={value => {const window = [...form.source_window]; window[index] = value; patch("source_window", window);}}/>)}</div>
      <label className="cal-check"><input type="checkbox" checked={staticOn} onChange={event => {setStaticOn(event.target.checked); setReviewed(false);}}/>Subtract a matched night-time static template</label>
      {staticOn && <div className="cal-fields">{[0, 1].map(index => <CalibrationTimeField key={index} id={`static-${index}`} label={`Static ${index ? "end" : "start"}`} zone={timeZone} value={form.static_window?.[index] ?? ""} onError={markTimeError} onChange={value => {const window = [...(form.static_window ?? ["", ""])]; window[index] = value; patch("static_window", window);}}/>)}</div>}
      {preparing && <p className="cal-caption" role="status">Loading windows for the selected date…</p>}
      {staticOn && !preparing && <CalibrationSkyPlanner date={form.date} zone={timeZone} solve={form.source_window} staticWindow={form.static_window} onSelect={window => {setTimeErrors(current => ({...current, 'static-0': false, 'static-1': false})); patch('static_window', window);}}/>}
      <label>Hardware / static note<textarea rows={2} placeholder="Any wiring, EQ or timing changes? Is the night-time window suitable?" value={form.hardware_epoch_note} onChange={event => patch("hardware_epoch_note", event.target.value)}/></label>
      <label className="cal-check"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)}/>I reviewed the windows, static compatibility, and antenna selection.</label>
      <button className="cal-primary" disabled={disabled || invalidTimes || !reviewed || !form.antennas.includes(form.ref_ant)} onClick={() => perform(ROOT + "/builds", {...form, static_window: staticOn ? form.static_window : null, reviewed}, "Calibration queued. No beam weights or live deployment are requested.").then(result => {if (result) setStep(1);})}>{busy ? "Saving…" : "Calibrate with the Sun →"}</button>
      <p className="cal-caption">Up to 4 hours per window. Data coverage is checked when the job runs.</p>
    </> : <p>Loading source windows and layout…</p>}</section>
    <aside><section className="cal-card"><div className="cal-section-heading"><h3>Antenna membership</h3><span>{form?.antennas?.length ?? 0} selected</span></div><p className="cal-caption">{overview?.defaults?.antennas_source === "deployed" ? `Default: ${overview.defaults.antennas.length} deployed antennas.` : "Deployed weights unavailable; using layout defaults."} Edits affect only this solve.</p><div className="cal-antennas">{(overview?.defaults?.wired_antennas ?? []).map((antenna: number) => <label key={antenna} className={form?.antennas.includes(antenna) ? "included" : ""}><input type="checkbox" checked={form?.antennas.includes(antenna) ?? false} onChange={() => patch("antennas", form?.antennas.includes(antenna) ? form.antennas.filter((value: number) => value !== antenna) : [...(form?.antennas ?? []), antenna])}/>{label(antenna)}</label>)}</div><label>Reference antenna<select value={form?.ref_ant ?? ""} onChange={event => patch("ref_ant", Number(event.target.value))}>{(form?.antennas ?? []).map((antenna: number) => <option key={antenna} value={antenna}>{label(antenna)}</option>)}</select></label>{overview?.defaults?.antennas_mismatch_note && <Notice>{overview.defaults.antennas_mismatch_note}</Notice>}</section><section className="cal-card cal-guide"><h3>Saved with each solve</h3><ol><li>Antenna selection and recipe</li><li>Sun fringe stopping → phase-only SVD</li><li>Calibration, autos, singular values and phase diagnostics</li><li>Executed notebook and report</li></ol><p>Delay subtraction is a diagnostic, not the input to SVD. Rank-1 is not a sensitivity score.</p></section></aside></div>}
    {step > 0 && !tag && <div className="cal-empty"><h3>Choose a calibration to continue</h3><p>Create a revision, or inspect the recorded calibration without running a new solve.</p><button onClick={() => setStep(0)}>Prepare a calibration</button></div>}
    {step === 1 && tag && <><Gallery title="Matched phase views" items={stageItems(["phase"]).filter(item => item.kind !== "image" || phaseView === "both" || item.name.includes(phaseView))}/><details open={!stageItems(["phase"]).length}><summary>Phase settings</summary><section className="cal-card"><div className="cal-section-heading"><h3>Phase progression</h3><span>Raw → fringe-stopped → delay-subtracted</span></div><p className="cal-caption">Phase vs frequency and time · fixed −π…π scale · same selected baselines.</p><div className="cal-fields"><label>Fringe-stop direction<Source value={phaseSource} onChange={setPhaseSource}/></label><label>Presentation<select value={phaseView} onChange={event => setPhaseView(event.target.value)}><option value="both">Curves & waterfalls</option><option value="curve">1D sawtooth curves</option><option value="waterfall">Frequency × time waterfalls</option></select></label></div><div className="cal-fields">{[0, 1].map(index => <label key={index}>Phase {index ? "end" : "start"} · UTC<input type="datetime-local" value={asInput(phaseWindow[index])} onChange={event => {const window = [...phaseWindow]; window[index] = event.target.value + ":00Z"; setPhaseWindow(window);}}/></label>)}</div><div className="cal-targets">{(candidate?.antennas ?? []).filter((antenna: number) => antenna !== candidate.ref_ant).map((antenna: number) => <label key={antenna}><input type="checkbox" checked={targets.includes(antenna)} disabled={!targets.includes(antenna) && targets.length >= 3} onChange={() => setTargets(current => current.includes(antenna) ? current.filter(value => value !== antenna) : [...current, antenna])}/>{label(antenna)}</label>)}</div><button className="cal-primary" disabled={disabled || !hasCal || !targets.length} onClick={() => runAction("phase", {source: phaseSource, targets, window: phaseWindow, static: true})}>Render matched phase views</button><span className="cal-inline-note">Up to three reference baselines · fixed delay fit over this interval</span></section></details>
      <section className="cal-card"><div className="cal-section-heading"><h3>Across the last few days</h3><span>Matched solar-transit windows</span></div><div className="cal-fields"><label>Latest day · UTC<input type="date" value={transit.date} onChange={event => setTransit(current => ({...current, date: event.target.value}))}/></label><label>Days<select value={historyDays} onChange={event => setHistoryDays(Number(event.target.value))}>{[1,2,3].map(day => <option key={day}>{day}</option>)}</select></label></div><p className="cal-caption">Canonical sawtooth comparison against the recorded reference, three longest baselines. Each day's solar window matches the solve duration (up to four hours). No cross-day static is silently reused.</p><button disabled={disabled || !hasCal} onClick={() => runAction("history", {date: transit.date, days: historyDays})}>Compare recent sawtooths</button></section>
      <Gallery title="Solve & historical diagnostics" items={stageItems(["solve", "history"])}/></>}
    {step === 2 && tag && <><Gallery title="Transit waterfall, light curve & chromaticity" items={stageItems(["transit"])}/><details open={!latestTransit}><summary>Transit settings</summary><section className="cal-card"><div className="cal-section-heading"><h3>A source crossing a stationary beam</h3><span>5 hours · cross-correlations only</span></div><p className="cal-caption">Hold the beam at transit. Compare against the deployed calibration on matched antennas and channels.</p><div className="cal-fields"><label>Validation source<Source value={transit.source} onChange={value => setTransit(current => ({...current, source: value}))}/></label><label>Transit date · UTC<input type="date" value={transit.date} onChange={event => setTransit(current => ({...current, date: event.target.value}))}/></label></div>{transit.source === "sun" && <Notice>Sun is a same-source self-check, not independent validation.</Notice>}<details className="cal-advanced"><summary>Band, background & channel timing</summary><div className="cal-fields">{[["fmin", "Band start · MHz"], ["fmax", "Band end · MHz"], ["off_minutes", "Off-source beyond ± minutes"], ["minimum_snr", "Minimum channel peak / off-RMS"]].map(([key, text]) => <label key={key}>{text}<input type="number" value={transit[key]} onChange={event => setTransit(current => ({...current, [key]: Number(event.target.value)}))}/></label>)}</div><label className="cal-check"><input type="checkbox" checked={transit.compare_reference} onChange={event => setTransit(current => ({...current, compare_reference: event.target.checked}))}/>Compare recorded reference on common antennas and common channel mask</label></details><button className="cal-primary" disabled={disabled || !hasCal} onClick={() => runAction("transit", transit)}>Run stationary transit check →</button></section></details>
      {latestTransit && <><div className="cal-metrics">{[["Peak offset", "broadband_peak_min", "min"], ["Transit FWHM", "fwhm_min", "min"], ["Peak / off-RMS", "peak_off_rms", ""], ["Timing scatter", "timing_scatter_min", "min"], ["Accepted channels", "accepted_channels", ""]].map(([title, key, unit]) => <div key={key}><span>{title}</span><strong>{human(latestTransit.result.metrics?.candidate?.[key])} <small>{unit}</small></strong></div>)}</div><p className="cal-caption">Timing scatter is robust scatter after subtracting the exact geometric prediction. Weak or edge-peaking channels are omitted, not forced to line up. Absolute power remains available alongside normalized shape.</p><Evidence value={latestTransit.result}/></>}</>}
    {step === 3 && tag && <><BeamSky key={tag} tag={tag} date={candidate?.source_window?.[0]?.slice(0, 10) ?? overview?.defaults?.date ?? ""}/><section className="cal-card"><div className="cal-section-heading"><h3>Generate 512 beams</h3><span>Saved calibration → CB + IB</span></div><p className="cal-caption">Reuse this calibration and antenna selection. Generate the coherent beams and matching incoherent mask together.</p><div className="cal-fields"><label>Minimum elevation · degrees<input type="number" min={10} max={60} value={altitude} onChange={event => setAltitude(Number(event.target.value))}/></label><label>Grid objective<input value="Exact rate coverage · full azimuth" readOnly/></label></div><label className="cal-check"><input type="checkbox" checked={plainGrid} onChange={event => setPlainGrid(event.target.checked)}/>Build a new plain grid; do not preserve custom / factorial cells in the recorded product.</label><button className="cal-primary" disabled={disabled || !hasCal || hasWeights || !plainGrid || detail?.request?.imported} onClick={() => runAction("grid", {alt_min: altitude, replace_custom_cells: plainGrid})}>Generate 512 beams + matching IB →</button>{detail?.request?.imported && <p className="cal-caption">Recorded calibrations are diagnostic references. Create a new reviewed solve revision to generate uploadable products.</p>}</section><details><summary>Exact-response checks & product files</summary><Gallery title="Product checks" items={stageItems(["grid"])}/></details></>}
    {step === 4 && tag && <div className="cal-columns">
      <section className="cal-card"><h3>Review this calibration</h3>
        <div className="cal-checklist">{[[hasCal, "Calibration saved"], [Boolean(detail?.summary?.notebook_executed), "Diagnostics notebook complete"], [transitResults.some(item => item.selection.source !== "sun"), "Independent-source transit saved"], [hasWeights, "512 beams + matching IB saved"]].map(([done, text]) => <p key={String(text)}><span className={done ? "complete" : ""}>{done ? "✓" : "○"}</span>{text}</p>)}</div>
        <label>Review note<textarea value={reviewNote} onChange={event => setReviewNote(event.target.value)} placeholder="What did the phase and transit checks show?"/></label>
        <label className="cal-check"><input type="checkbox" checked={accept} onChange={event => setAccept(event.target.checked)}/>I inspected and accept this revision.</label>
        <button disabled={disabled || !hasWeights || !accept || !reviewNote.trim()} onClick={() => perform(`${ROOT}/builds/${tag}/review`, {accepted: accept, note: reviewNote}, "Review saved for this revision.")}>Save review</button>
        <hr/><h3>Test the upload files</h3><p className="cal-caption">Build and inspect local CB / IB payloads. Nothing is sent to the telescope.</p>
        <button className="cal-primary" disabled={disabled || !detail?.review?.accepted || !hasWeights || Boolean(detail?.request?.imported)} onClick={() => perform(`${ROOT}/deployment/builds/${tag}/stage`, {save_defaults: false}, "Dry run queued. Deployed weights, registry and restart defaults are unchanged.")}>Run upload dry run</button>
        {deployDetail?.stage && <><p className="cal-caption">{deployDetail.stage.checks_ok ? "Payload checks passed." : "Inspect the payload checks below."}</p><Evidence value={deployDetail.stage}/></>}
      </section>
      <aside><section className="cal-card"><h3>Deployed reference</h3><div className="cal-receipts">
        <div><strong>Last recorded upload</strong><span>{deployment?.deployed?.date_deployed ?? "Unavailable"}</span></div>
        <div><strong>Antennas</strong><span>{deployment?.deployed?.n_ant_set ?? "Unknown"}</span></div>
        <div><strong>Product</strong><span>{deployment?.deployed?.product_id ?? "Unknown"}</span></div>
        <div><strong>CB / IB scale</strong><span>{deployment?.deployed?.scale ?? "—"} / {deployment?.deployed?.ib_scale ?? "—"}</span></div>
      </div><p className="cal-caption">The last uploaded product stays the reference. Trial calibrations do not replace it.</p><Evidence value={deployment?.deployed}/></section>
      <section className="cal-card"><h3>Dry run only</h3><p className="cal-caption">Live upload is unavailable here. A successful dry run does not activate weights.</p></section></aside>
    </div>}
    {tag && <section className="cal-card cal-jobs"><div className="cal-section-heading"><h3>Jobs & files</h3><span>Saved between visits</span></div>{jobs.length ? jobs.map(job => <div className="cal-job" key={job.id}><span className={`cal-job-state ${job.state}`}>{job.state}</span><strong>{job.params.action ?? job.kind}</strong><span>{stamp(job.created)}</span><button onClick={() => setLogId(job.id)}>View log #{job.id}</button>{["queued", "running"].includes(job.state) && <button onClick={() => perform(`${ROOT}/jobs/${job.id}/cancel`, {}, "Cancellation requested; the worker terminates the process group.")}>Cancel</button>}</div>) : <p className="cal-caption">No new job submitted for this reference.</p>}{logId != null && <details open className="cal-log"><summary>Worker log #{logId}<button onClick={() => setLogId(null)}>Close</button></summary><pre>{log}</pre></details>}<Evidence value={{request: detail?.request, summary: detail?.summary, isolated_store: overview?.isolation}}/></section>}
  </div>;
}
