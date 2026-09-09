import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { defaultsSentence, RANK1_CAPTION, windowCell } from "../lib/calText";
import { formatUtcStamp } from "../lib/statusSentence";
import { getCalBuilds, getCalDefaults, getJob, postCalBuild } from "../lib/api";
import { mockGetCalBuilds, mockGetCalDefaults, mockGetJob, mockPostCalBuild } from "../lib/mockCal";
import type { CalBuildListItem, CalDefaultsResponse } from "../lib/types";

const BUILDS_POLL_MS = 15_000;
const JOB_POLL_MS = 1_500;

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** "2026-09-08T19:20:00Z" -> "2026-09-08T19:20" for a datetime-local input;
 * the value is treated as UTC by convention (the field is labelled "UTC"),
 * same pattern as TimeRangePicker's custom-range inputs. */
function toInputValue(iso: string): string {
  return iso.slice(0, 16);
}

function fromInputValue(value: string): string {
  if (!value) return "";
  return value.length === 16 ? `${value}:00Z` : value;
}

/** The Calibration tab's "New solve" + "Builds" sections (docs/plan.md M3).
 * The build detail page lives at CalBuildPage.tsx (route `/cal/:tag`). */
export default function CalPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";
  const navigate = useNavigate();

  const api = useMemo(
    () =>
      useMock
        ? { defaults: mockGetCalDefaults, builds: mockGetCalBuilds, build: mockPostCalBuild, job: mockGetJob }
        : { defaults: getCalDefaults, builds: getCalBuilds, build: postCalBuild, job: (id: number) => getJob(id) },
    [useMock],
  );

  const [date, setDate] = useState(todayUtc());
  const [defaults, setDefaults] = useState<CalDefaultsResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .defaults(date)
      .then((d) => !cancelled && setDefaults(d))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [api, date]);

  // --- form state, seeded from defaults whenever a fresh date's defaults
  // arrive (not on every render — the operator may then edit freely). -----
  const [source, setSource] = useState("sun");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [staticEnabled, setStaticEnabled] = useState(true);
  const [staticStart, setStaticStart] = useState("");
  const [staticEnd, setStaticEnd] = useState("");
  const [antennaOptions, setAntennaOptions] = useState<number[]>([]);
  const [antennasOn, setAntennasOn] = useState<Set<number>>(new Set());
  const [refAnt, setRefAnt] = useState<number | "">("");
  const [tag, setTag] = useState("");
  const seededForDate = useRef<string | null>(null);

  useEffect(() => {
    if (!defaults || seededForDate.current === defaults.date) return;
    seededForDate.current = defaults.date;
    setSource(defaults.source);
    setWindowStart(toInputValue(defaults.source_window[0]));
    setWindowEnd(toInputValue(defaults.source_window[1]));
    setStaticEnabled(defaults.static_window !== null);
    setStaticStart(defaults.static_window ? toInputValue(defaults.static_window[0]) : "");
    setStaticEnd(defaults.static_window ? toInputValue(defaults.static_window[1]) : "");
    const wired = Math.max(defaults.layout.n_wired, ...defaults.antennas, 0);
    setAntennaOptions(Array.from({ length: wired }, (_, i) => i + 1));
    setAntennasOn(new Set(defaults.antennas));
    setRefAnt(defaults.ref_ant);
    setTag(defaults.tag);
  }, [defaults]);

  const toggleAntenna = useCallback((n: number) => {
    setAntennasOn((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });
  }, []);

  // --- builds table --------------------------------------------------------
  const [builds, setBuilds] = useState<CalBuildListItem[]>([]);
  const loadBuilds = useCallback(() => {
    api
      .builds()
      .then((r) => setBuilds(r.builds))
      .catch(() => undefined);
  }, [api]);
  useEffect(() => {
    loadBuilds();
    const timer = setInterval(loadBuilds, BUILDS_POLL_MS);
    return () => clearInterval(timer);
  }, [loadBuilds]);

  // --- submit ----------------------------------------------------------------
  const [submitting, setSubmitting] = useState(false);
  const [jobState, setJobState] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const pollJob = useCallback(
    (jobId: number) => {
      const poll = () => {
        api
          .job(jobId)
          .then((job) => {
            setJobState(job.state);
            if (job.state === "done") {
              setSubmitting(false);
              loadBuilds();
            } else if (job.state === "failed" || job.state === "cancelled") {
              setSubmitting(false);
              setSubmitError(`solve ${job.state}`);
            } else {
              setTimeout(poll, JOB_POLL_MS);
            }
          })
          .catch(() => setSubmitting(false));
      };
      poll();
    },
    [api, loadBuilds],
  );

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (submitting || !windowStart || !windowEnd || refAnt === "" || !tag) return;
      setSubmitting(true);
      setSubmitError(null);
      setJobState("queued");
      api
        .build({
          source,
          source_window: [fromInputValue(windowStart), fromInputValue(windowEnd)],
          static_window: staticEnabled && staticStart && staticEnd ? [fromInputValue(staticStart), fromInputValue(staticEnd)] : null,
          antennas: Array.from(antennasOn).sort((a, b) => a - b),
          ref_ant: refAnt as number,
          tag,
        })
        .then((res) => {
          if (!res.ok || !res.body) {
            setSubmitting(false);
            setSubmitError(res.detail ?? "the build request failed");
            return;
          }
          pollJob(res.body.job_id);
        })
        .catch(() => setSubmitting(false));
    },
    [api, submitting, source, windowStart, windowEnd, staticEnabled, staticStart, staticEnd, antennasOn, refAnt, tag, pollJob],
  );

  return (
    <div>
      <section className="cal-section">
        <h2 className="cal-section__title">New solve</h2>
        {defaults ? (
          <p className="note">{defaultsSentence(defaults)}</p>
        ) : (
          <p className="note">Loading today's defaults...</p>
        )}
        <form className="cal-form" onSubmit={handleSubmit}>
          <div className="cal-field">
            <span className="cal-field__label">date</span>
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </div>

          <div className="cal-field">
            <span className="cal-field__label">source</span>
            <div className="segmented" role="group" aria-label="source">
              {(defaults?.sources ?? [{ name: "sun", enabled: true }]).map((s) => (
                <button
                  key={s.name}
                  type="button"
                  disabled={!s.enabled}
                  className={s.name === source ? "selected" : ""}
                  style={!s.enabled ? { color: "var(--faint)", cursor: "default" } : undefined}
                  onClick={() => s.enabled && setSource(s.name)}
                  title={s.enabled ? undefined : "not yet"}
                >
                  {s.name}
                  {!s.enabled ? " (not yet)" : ""}
                </button>
              ))}
            </div>
          </div>

          <div className="cal-field">
            <span className="cal-field__label">window (UTC)</span>
            <input
              type="datetime-local"
              value={windowStart}
              onChange={(e) => setWindowStart(e.target.value)}
              aria-label="window start (UTC)"
            />
            <span className="note">to</span>
            <input
              type="datetime-local"
              value={windowEnd}
              onChange={(e) => setWindowEnd(e.target.value)}
              aria-label="window end (UTC)"
            />
          </div>

          <div className="cal-field">
            <span className="cal-field__label">static window</span>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={!staticEnabled}
                onChange={(e) => setStaticEnabled(!e.target.checked)}
              />
              none
            </label>
            {staticEnabled && (
              <>
                <input
                  type="datetime-local"
                  value={staticStart}
                  onChange={(e) => setStaticStart(e.target.value)}
                  aria-label="static window start (UTC)"
                />
                <span className="note">to</span>
                <input
                  type="datetime-local"
                  value={staticEnd}
                  onChange={(e) => setStaticEnd(e.target.value)}
                  aria-label="static window end (UTC)"
                />
              </>
            )}
          </div>

          <div className="cal-field">
            <span className="cal-field__label">antennas</span>
            <div className="antenna-row">
              {antennaOptions.map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`antenna-toggle${antennasOn.has(n) ? " on" : ""}`}
                  onClick={() => toggleAntenna(n)}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>

          <div className="cal-field">
            <span className="cal-field__label">reference antenna</span>
            <select value={refAnt} onChange={(e) => setRefAnt(Number(e.target.value))}>
              {antennaOptions
                .filter((n) => antennasOn.has(n))
                .map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
            </select>
          </div>

          <div className="cal-field">
            <span className="cal-field__label">tag</span>
            <input type="text" value={tag} onChange={(e) => setTag(e.target.value)} />
          </div>

          <div className="cal-field">
            <button type="submit" className="text-button" disabled={submitting}>
              Run solve
            </button>
            {jobState && <span className="note">state: {jobState}</span>}
            {submitError && <span style={{ color: "var(--alert)" }}>{submitError}</span>}
          </div>
        </form>
      </section>

      <section className="cal-section">
        <h2 className="cal-section__title">Builds</h2>
        {builds.length === 0 ? (
          <p className="note">No builds yet.</p>
        ) : (
          <table className="cal-table">
            <thead>
              <tr>
                <th>tag</th>
                <th>created</th>
                <th>window</th>
                <th>antennas</th>
                <th>rank-1 median ({RANK1_CAPTION})</th>
                <th>staged</th>
                <th>uploaded</th>
              </tr>
            </thead>
            <tbody>
              {builds.map((b) => (
                <tr key={b.tag} className="clickable" onClick={() => navigate(`/cal/${b.tag}${useMock ? "?mock=1" : ""}`)}>
                  <td className="ink">{b.tag}</td>
                  <td>{formatUtcStamp(b.created)}</td>
                  <td>{windowCell(b.source_window)}</td>
                  <td>{b.n_ant}</td>
                  <td>{b.rank1_median === null ? "-" : b.rank1_median.toFixed(2)}</td>
                  <td className={b.staged ? "ok" : "no"}>{b.staged ? "staged" : "not staged"}</td>
                  <td className={b.uploaded ? "ok" : "no"}>{b.uploaded ? "uploaded" : "not uploaded"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
