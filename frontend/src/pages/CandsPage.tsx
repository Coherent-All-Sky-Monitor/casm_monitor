import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import Panel from "../components/Panel";
import Segmented from "../components/Segmented";
import { CandidateGallery } from "../components/CandidateGallery";
import { useUrlParam } from "../lib/useUrlParam";
import { formatUtcStamp } from "../lib/statusSentence";
import { eventsSentence, nowSentence, statsSentence } from "../lib/candsText";
import {
  candStatsPlotUrl,
  getCandEvents,
  getCandFrbs,
  getCandInjections,
  getCandStats,
} from "../lib/api";
import {
  mockCandStatsPlotUrl,
  mockGetCandEvents,
  mockGetCandFrbs,
  mockGetCandInjections,
  mockGetCandStats,
} from "../lib/mockCands";
import type {
  CandEventRow,
  CandFrbRow,
  CandInjectionRow,
  CandInjectionsResponse,
  CandStatsResponse,
} from "../lib/types";

const REFRESH_MS = 15_000;
const HOURS_OPTIONS = [
  { value: "12", label: "12 h" },
  { value: "24", label: "24 h" },
  { value: "48", label: "48 h" },
  { value: "168", label: "1 week" },
  { value: "720", label: "1 month" },
];

/** The Candidates tab (docs/plan.md M5): the events table plus three
 * sub-views (stats, injections, frbs). Row click navigates to
 * `/cands/:name` (CandEventPage.tsx). */
export default function CandsPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";
  const navigate = useNavigate();
  const mockSuffix = useMock ? "?mock=1" : "";

  const api = useMemo(
    () =>
      useMock
        ? {
            events: mockGetCandEvents,
            stats: mockGetCandStats,
            statsPlotUrl: mockCandStatsPlotUrl,
            injections: mockGetCandInjections,
            frbs: mockGetCandFrbs,
          }
        : {
            events: getCandEvents,
            stats: getCandStats,
            statsPlotUrl: candStatsPlotUrl,
            injections: getCandInjections,
            frbs: getCandFrbs,
          },
    [useMock],
  );

  const [section] = useUrlParam("section", "events");
  const [cview] = useUrlParam("cview", "candidates");
  const [tier] = useUrlParam("tier", "");
  const [label] = useUrlParam("label", "");
  const [hours] = useUrlParam("hours", "24");

  // --- events -----------------------------------------------------------
  const [events, setEvents] = useState<CandEventRow[]>([]);
  const [eventsError,setEventsError]=useState(''),[eventsLoaded,setEventsLoaded]=useState(false);
  useEffect(() => {
    if (section !== "events") return;
    let cancelled = false;
    let busy = false;
    setEvents([]);setEventsLoaded(false);setEventsError('');
    function load() {
      if(busy)return;
      busy=true;
      api
        .events({ tier: tier || undefined, tag: label || undefined, view: cview as "candidates" | "all", include_plots:true })
        .then((r) => {if(!cancelled){setEvents(r.events);setEventsLoaded(true);setEventsError('');}})
        .catch(() => {if(!cancelled)setEventsError('Candidate refresh failed. Previously loaded events may be stale.');})
        .finally(()=>{busy=false;});
    }
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [api, section, tier, label, cview]);

  // --- stats --------------------------------------------------------------
  const [stats, setStats] = useState<CandStatsResponse | null>(null);
  useEffect(() => {
    if (section !== "stats") return;
    let cancelled = false;
    function load() {
      api
        .stats(Number(hours))
        .then((r) => !cancelled && setStats(r))
        .catch(() => undefined);
    }
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [api, section, hours]);

  // --- injections -----------------------------------------------------------
  const [injections, setInjections] = useState<CandInjectionsResponse | null>(null);
  useEffect(() => {
    if (section !== "injections") return;
    let cancelled = false;
    api
      .injections()
      .then((r) => !cancelled && setInjections(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api, section]);

  // --- frbs -----------------------------------------------------------------
  const [frbs, setFrbs] = useState<CandFrbRow[]>([]);
  useEffect(() => {
    if (section !== "frbs") return;
    let cancelled = false;
    api
      .frbs()
      .then((r) => !cancelled && setFrbs(r.frbs))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api, section]);

  return (
    <div>
      <div className="toolbar">
        <Segmented
          paramKey="section"
          defaultValue="events"
          options={[
            { value: "events", label: "events" },
            { value: "stats", label: "stats" },
            { value: "injections", label: "injections" },
            { value: "frbs", label: "frbs" },
          ]}
        />
        {section === "events" && (
          <>
            <Segmented
              paramKey="cview"
              defaultValue="candidates"
              options={[
                { value: "candidates", label: "dump attempts" },
                { value: "all", label: "all stored" },
              ]}
            />
            <Segmented
              paramKey="tier"
              defaultValue=""
              options={[
                { value: "", label: "all tiers" },
                { value: "A", label: "A" },
                { value: "B", label: "B" },
                { value: "C", label: "C" },
              ]}
            />
            <Segmented
              paramKey="label"
              defaultValue=""
              options={[
                { value: "", label: "any label" },
                { value: "frb", label: "frb" },
                { value: "pulsar", label: "pulsar" },
                { value: "rfi", label: "rfi" },
                { value: "unsure", label: "unsure" },
              ]}
            />
          </>
        )}
        {section === "stats" && <Segmented paramKey="hours" defaultValue="24" options={HOURS_OPTIONS} />}
      </div>

      {section === "events" && (
        <div>
          <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
            {eventsSentence(events)}
          </p>
          {eventsError&&<p role="status" className="workspace-notice">{eventsError}</p>}
          {!eventsLoaded&&!eventsError?<p className="note">Loading candidates…</p>:
            <CandidateGallery key={`${cview}/${tier}/${label}/${useMock}`} events={events} useMock={useMock}/>}
        </div>
      )}

      {section === "stats" && (
        <div>
          <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
            {stats ? statsSentence(stats) : "Loading the T2 funnel."}
          </p>
          <p className="note">{stats ? nowSentence(stats.now) : ""}</p>
          <Panel title={`T1 -> T2 funnel, ${stats?.win_label ?? ""}`}>
            <img
              className="vis-figure__img"
              src={api.statsPlotUrl(Number(hours))}
              alt="T1 to T2 funnel"
              loading="lazy"
            />
          </Panel>
          {stats && stats.now && (
            <table className="cal-table" style={{ marginTop: 24 }}>
              <thead>
                <tr>
                  <th>source</th>
                  <th>alt (deg)</th>
                  <th>az (deg)</th>
                  <th>transit</th>
                </tr>
              </thead>
              <tbody>
                {stats.now.sources.map((s) => (
                  <tr key={s.name}>
                    <td className="ink">{s.name}</td>
                    <td>{s.alt.toFixed(1)}</td>
                    <td>{s.az.toFixed(1)}</td>
                    <td>{s.transit}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {section === "injections" && (
        <div>
          <p className="note" style={{ marginBottom: "var(--section-gap)" }}>
            {injections
              ? `${injections.day.n} injections in the last 24 h, ${injections.day.t1 ?? 0} recovered at T1, ${
                  injections.day.tr ?? 0
                } would have triggered.`
              : "Loading injections."}
          </p>
          {injections && injections.injections.length > 0 ? (
            <table className="cal-table">
              <thead>
                <tr>
                  <th>time UTC</th>
                  <th>beam</th>
                  <th>DM</th>
                  <th>injected SNR</th>
                  <th>recovered SNR</th>
                  <th>event</th>
                </tr>
              </thead>
              <tbody>
                {injections.injections.map((row: CandInjectionRow) => (
                  <tr key={row.id}>
                    <td>{formatUtcStamp(`${row.inject_utc}Z`)}</td>
                    <td>{row.beam}</td>
                    <td>{row.dm.toFixed(1)}</td>
                    <td>{row.est_snr?.toFixed(1) ?? "-"}</td>
                    <td className={row.rec_snr ? "ok" : "no"}>{row.rec_snr?.toFixed(1) ?? "lost"}</td>
                    <td>{row.event_name ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="note">No injections yet.</p>
          )}
        </div>
      )}

      {section === "frbs" && (
        <div>
          {frbs.length === 0 ? (
            <p className="note">No confirmed FRBs yet.</p>
          ) : (
            <table className="cal-table">
              <thead>
                <tr>
                  <th>name</th>
                  <th>time UTC</th>
                  <th>SNR</th>
                  <th>DM</th>
                  <th>beam</th>
                  <th>notes</th>
                </tr>
              </thead>
              <tbody>
                {frbs.map((f) => (
                  <tr
                    key={f.name}
                    className="clickable"
                    onClick={() => navigate(`/cands/${f.name}${mockSuffix}`)}
                  >
                    <td className="ink">{f.name}</td>
                    <td>{formatUtcStamp(`${f.event_utc}Z`)}</td>
                    <td>{f.snr.toFixed(1)}</td>
                    <td>{f.dm.toFixed(1)}</td>
                    <td>{f.beam}</td>
                    <td>{f.notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
