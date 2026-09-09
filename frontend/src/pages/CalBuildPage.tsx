import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { buildSummarySentence, RANK1_FIGURE_CAPTION } from "../lib/calText";
import { formatUtcStamp } from "../lib/statusSentence";
import {
  calFigureUrl,
  calLogUrl,
  calNotebookUrl,
  getCalBuild,
  getCalStatus,
  getJob,
  postCalStage,
  postCalUpload,
} from "../lib/api";
import {
  mockCalFigureUrl,
  mockGetCalBuild,
  mockGetCalStatus,
  mockGetJob,
  mockPostCalStage,
  mockPostCalUpload,
} from "../lib/mockCal";
import type { CalBuildDetailResponse, CalStatusResponse } from "../lib/types";

const JOB_POLL_MS = 1_500;
const STATUS_POLL_MS = 10_000;

// Fixed figure order per the spec ("Build page"); any name the backend sends
// that is not in this list is appended after the known ones, so nothing
// silently disappears (docs/api-cal.md).
const FIG_ORDER = [
  "phase_raw_sawtooth",
  "phase_stage2_fringe_stopped",
  "phase_stage3_calibrated",
  "gain_delay_fits",
  "svd_vs_freq",
  "rank1_vs_freq",
  "cal_diff",
  "beam_grid",
  "source_transit",
  "autocorr",
];

function orderFigs<T extends { name: string }>(figs: T[]): T[] {
  const rank = (name: string) => {
    const i = FIG_ORDER.indexOf(name);
    if (i !== -1) return i;
    if (name.startsWith("beam_check_")) return FIG_ORDER.indexOf("rank1_vs_freq") + 0.5;
    return FIG_ORDER.length + 1;
  };
  return [...figs].sort((a, b) => rank(a.name) - rank(b.name));
}

/** The Calibration build detail page (`/cal/:tag`, docs/plan.md M3): the
 * summary sentence, the server-rendered figures in a fixed order, links to
 * the notebook and log, then the Deploy section (stage dry-run + the
 * confirm-to-upload block, kept impossible to trigger by accident). */
export default function CalBuildPage() {
  const { tag = "" } = useParams();
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";
  const navigate = useNavigate();

  const api = useMemo(
    () =>
      useMock
        ? {
            build: mockGetCalBuild,
            status: mockGetCalStatus,
            stage: mockPostCalStage,
            upload: mockPostCalUpload,
            job: mockGetJob,
            figUrl: (name: string) => mockCalFigureUrl(tag, name),
            // The mock has no real log/notebook bytes to serve; point at a
            // harmless anchor rather than a 404 so the links still render.
            logUrl: () => "#",
            notebookUrl: () => "#",
          }
        : {
            build: getCalBuild,
            status: getCalStatus,
            stage: postCalStage,
            upload: postCalUpload,
            job: (id: number) => getJob(id),
            figUrl: (name: string) => calFigureUrl(tag, name),
            logUrl: () => calLogUrl(tag),
            notebookUrl: () => calNotebookUrl(tag),
          },
    [useMock, tag],
  );

  const [detail, setDetail] = useState<CalBuildDetailResponse | null>(null);
  const [status, setStatus] = useState<CalStatusResponse | null>(null);
  const [loadError, setLoadError] = useState(false);

  const loadDetail = useCallback(() => {
    api
      .build(tag)
      .then((d) => setDetail(d))
      .catch(() => setLoadError(true));
  }, [api, tag]);

  useEffect(() => {
    setDetail(null);
    setLoadError(false);
    loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    api
      .status()
      .then(setStatus)
      .catch(() => undefined);
    const timer = setInterval(() => {
      api
        .status()
        .then(setStatus)
        .catch(() => undefined);
    }, STATUS_POLL_MS);
    return () => clearInterval(timer);
  }, [api]);

  // --- stage dry-run ---------------------------------------------------------
  const [staging, setStaging] = useState(false);
  const [stageJobState, setStageJobState] = useState<string | null>(null);

  const pollJob = useCallback(
    (jobId: number, onDone: () => void, setState: (s: string | null) => void) => {
      const poll = () => {
        api
          .job(jobId)
          .then((job) => {
            setState(job.state);
            if (job.state === "done") {
              onDone();
            } else if (job.state === "failed" || job.state === "cancelled") {
              onDone();
            } else {
              setTimeout(poll, JOB_POLL_MS);
            }
          })
          .catch(() => onDone());
      };
      poll();
    },
    [api],
  );

  const handleStage = useCallback(() => {
    if (staging) return;
    setStaging(true);
    setStageJobState("queued");
    api
      .stage(tag)
      .then((res) => {
        if (!res.ok || !res.body) {
          setStaging(false);
          setStageJobState(res.detail);
          return;
        }
        pollJob(
          res.body.job_id,
          () => {
            setStaging(false);
            loadDetail();
          },
          setStageJobState,
        );
      })
      .catch(() => setStaging(false));
  }, [api, staging, tag, pollJob, loadDetail]);

  // --- upload ----------------------------------------------------------------
  const [confirmTag, setConfirmTag] = useState("");
  const [saveDefaults, setSaveDefaults] = useState(false);
  const [note, setNote] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadJobState, setUploadJobState] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const handleUpload = useCallback(() => {
    if (uploading || confirmTag !== tag) return;
    setUploading(true);
    setUploadJobState("queued");
    setUploadError(null);
    api
      .upload(tag, { confirm_tag: confirmTag, save_defaults: saveDefaults, note })
      .then((res) => {
        if (!res.ok || !res.body) {
          setUploading(false);
          setUploadError(res.detail ?? "the upload request failed");
          return;
        }
        pollJob(
          res.body.job_id,
          () => {
            setUploading(false);
            loadDetail();
          },
          setUploadJobState,
        );
      })
      .catch(() => setUploading(false));
  }, [api, uploading, confirmTag, tag, saveDefaults, note, pollJob, loadDetail]);

  if (loadError) {
    return (
      <div>
        <p className="note">No build found for tag {tag}.</p>
        <button className="text-button" onClick={() => navigate(`/cal${useMock ? "?mock=1" : ""}`)}>
          back to Calibration
        </button>
      </div>
    );
  }

  if (!detail) {
    return <p className="note">Loading {tag}...</p>;
  }

  const figs = detail.summary ? orderFigs(detail.summary.figs) : [];
  const trackRunning = status?.casm_track_running === true;
  const allowUpload = status?.allow_upload === true;
  const staged = detail.stage !== null;

  return (
    <div>
      <button className="detail__back" onClick={() => navigate(`/cal${useMock ? "?mock=1" : ""}`)}>
        back to Calibration
      </button>
      <h2 className="detail__title">{tag}</h2>
      <p className="note">state: {detail.state}</p>

      {detail.summary ? (
        <p className="page__lede">{buildSummarySentence(detail.summary)}</p>
      ) : (
        <p className="note">No summary yet ({detail.state}).</p>
      )}

      {figs.length > 0 && (
        <section className="cal-section">
          {figs.map((f) => (
            <div key={f.name} className="vis-figure">
              <img className="vis-figure__img" src={api.figUrl(f.name)} alt={f.title} loading="lazy" />
              <p className="note">
                {f.title}
                {f.name === "rank1_vs_freq" ? ` — ${RANK1_FIGURE_CAPTION}` : ""}
              </p>
            </div>
          ))}
        </section>
      )}

      {detail.summary && (
        <p className="note">
          {detail.summary.notebook && (
            <>
              <a href={api.notebookUrl()}>notebook</a>
              {" · "}
            </>
          )}
          <a href={api.logUrl()} target="_blank" rel="noreferrer">
            log
          </a>
        </p>
      )}

      <section className="cal-section">
        <h2 className="cal-section__title">Deploy</h2>

        <div className="cal-field">
          <button className="text-button" onClick={handleStage} disabled={staging || detail.summary === null}>
            Stage dry-run
          </button>
          {stageJobState && <span className="note">state: {stageJobState}</span>}
        </div>

        {detail.stage && (
          <div style={{ marginTop: 12 }}>
            <table className="cal-table">
              <thead>
                <tr>
                  <th>file</th>
                  <th>md5</th>
                  <th>scale</th>
                </tr>
              </thead>
              <tbody>
                {detail.stage.files.map((f) => (
                  <tr key={f.name}>
                    <td className="ink">{f.name}</td>
                    <td>{f.md5}</td>
                    <td>{f.scale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="note" style={{ marginTop: 8 }}>
              staged {formatUtcStamp(detail.stage.staged_utc)}
            </p>
            <pre className="code-block">{detail.stage.command}</pre>
            <ul className="checks-list">
              {detail.stage.checks.map((c) => (
                <li key={c.name} className={c.ok ? "ok" : "fail"}>
                  {c.name}: {c.ok ? "ok" : "failed"}
                  {c.detail ? ` (${c.detail})` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div style={{ marginTop: 24 }}>
          {trackRunning && (
            <p style={{ color: "var(--alert)" }}>casm-track is running; wait before uploading new weights.</p>
          )}
          {!allowUpload ? (
            <>
              <p className="note">
                Uploads are disabled on this service. Set CASM_MONITOR_ALLOW_UPLOAD=1 in the jobs unit to enable
                them.
              </p>
              <button className="text-button text-button--alert" disabled>
                Upload weights
              </button>
            </>
          ) : !staged ? (
            <p className="note">Stage the build before uploading.</p>
          ) : (
            <div className="cal-form">
              <div className="cal-field">
                <span className="cal-field__label">confirm tag</span>
                <input
                  type="text"
                  value={confirmTag}
                  onChange={(e) => setConfirmTag(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.preventDefault();
                  }}
                  placeholder="type the build tag to confirm"
                />
              </div>
              <div className="cal-field">
                <label className="checkbox">
                  <input type="checkbox" checked={saveDefaults} onChange={(e) => setSaveDefaults(e.target.checked)} />
                  save as defaults
                </label>
              </div>
              <div className="cal-field">
                <span className="cal-field__label">note</span>
                <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
              </div>
              <div className="cal-field">
                <button
                  type="button"
                  className="text-button text-button--alert"
                  disabled={uploading || trackRunning || confirmTag !== tag}
                  onClick={handleUpload}
                >
                  Upload weights
                </button>
                {uploadJobState && <span className="note">state: {uploadJobState}</span>}
                {uploadError && <span style={{ color: "var(--alert)" }}>{uploadError}</span>}
              </div>
            </div>
          )}
        </div>

        {detail.uploads.length > 0 && (
          <table className="cal-table" style={{ marginTop: 24 }}>
            <thead>
              <tr>
                <th>time</th>
                <th>exit</th>
                <th>command</th>
                <th>note</th>
                <th>registry id</th>
              </tr>
            </thead>
            <tbody>
              {detail.uploads.map((u) => (
                <tr key={u.ts + u.registry_id}>
                  <td>{formatUtcStamp(u.ts)}</td>
                  <td className={u.exit_code === 0 ? "ok" : "no"}>{u.exit_code}</td>
                  <td>{u.command}</td>
                  <td>{u.note}</td>
                  <td>{u.registry_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
