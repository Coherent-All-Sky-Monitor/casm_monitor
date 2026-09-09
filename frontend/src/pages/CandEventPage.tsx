import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { formatUtcStamp } from "../lib/statusSentence";
import { candPlotUrl, getCandEvent, postCandLabel } from "../lib/api";
import { mockCandPlotUrl, mockGetCandEvent, mockPostCandLabel } from "../lib/mockCands";
import type { CandEventDetailResponse, CandLabel } from "../lib/types";

const POLL_MS = 15_000;

function def(label: string, value: unknown): { label: string; value: string } {
  if (value === null || value === undefined || value === "") return { label, value: "-" };
  return { label, value: String(value) };
}

/** The Candidates event detail page (`/cands/:name`, docs/plan.md M5): the
 * candidate PNG(s) full width, the trigger card as a compact definition
 * list, and the label buttons. */
export default function CandEventPage() {
  const { name = "" } = useParams();
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";
  const navigate = useNavigate();

  const api = useMemo(
    () =>
      useMock
        ? { event: mockGetCandEvent, plotUrl: mockCandPlotUrl, label: mockPostCandLabel }
        : { event: getCandEvent, plotUrl: candPlotUrl, label: postCandLabel },
    [useMock],
  );

  const [detail, setDetail] = useState<CandEventDetailResponse | null>(null);
  const [loadError, setLoadError] = useState(false);

  const load = useCallback(() => {
    api
      .event(name)
      .then((d) => setDetail(d))
      .catch(() => setLoadError(true));
  }, [api, name]);

  useEffect(() => {
    setDetail(null);
    setLoadError(false);
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const [note, setNote] = useState("");
  const [labelling, setLabelling] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);

  const handleLabel = useCallback(
    (value: CandLabel) => {
      if (labelling) return;
      setLabelling(true);
      setLabelError(null);
      api
        .label(name, value, note)
        .then((res) => {
          setLabelling(false);
          if (!res.ok) {
            setLabelError(res.detail ?? "the label failed to save");
            return;
          }
          setNote("");
          load();
        })
        .catch(() => setLabelling(false));
    },
    [api, labelling, name, note, load],
  );

  if (loadError) {
    return (
      <div>
        <p className="note">No event found for {name}.</p>
        <button className="detail__back" onClick={() => navigate(`/cands${useMock ? "?mock=1" : ""}`)}>
          back to Candidates
        </button>
      </div>
    );
  }

  if (!detail) {
    return <p className="note">Loading {name}...</p>;
  }

  const ev = detail.event as Record<string, unknown>;
  const currentLabel = detail.labels[0]?.label ?? null;

  return (
    <div>
      <button className="detail__back" onClick={() => navigate(`/cands${useMock ? "?mock=1" : ""}`)}>
        back to Candidates
      </button>
      <h2 className="detail__title">{name}</h2>
      <p className="note">{detail.data_status}</p>

      {detail.plots.length > 0 ? (
        <section className="cal-section">
          {detail.plots.map((fname) => (
            <div key={fname} className="vis-figure">
              <img className="vis-figure__img" src={api.plotUrl(name, fname)} alt={fname} loading="lazy" />
            </div>
          ))}
        </section>
      ) : (
        <p className="note">No plot for this event.</p>
      )}

      <section className="cal-section">
        <h2 className="cal-section__title">Trigger card</h2>
        <dl className="def-list">
          {[
            def("event UTC", formatUtcStamp(`${ev.event_utc}Z`)),
            def("SNR", typeof ev.snr === "number" ? ev.snr.toFixed(1) : ev.snr),
            def("DM", typeof ev.dm === "number" ? ev.dm.toFixed(1) : ev.dm),
            def("width", ev.width),
            def("beam", ev.beam),
            def("tier", ev.tier),
            def("tags", detail.tags_display.join(", ")),
            def("n members", ev.n_members),
            def("n beams", ev.n_beams),
            def("alt / az (deg)", `${ev.alt_deg ?? "-"} / ${ev.az_deg ?? "-"}`),
          ].map((row) => (
            <div key={row.label} style={{ display: "contents" }}>
              <dt>{row.label}</dt>
              <dd className="ink">{row.value}</dd>
            </div>
          ))}
        </dl>

        {detail.triggers.length > 0 && (
          <table className="cal-table" style={{ marginTop: 16 }}>
            <thead>
              <tr>
                <th>time UTC</th>
                <th>action</th>
                <th>detail</th>
              </tr>
            </thead>
            <tbody>
              {detail.triggers.map((t) => (
                <tr key={t.id}>
                  <td>{formatUtcStamp(`${t.created_utc}Z`)}</td>
                  <td className="ink">{t.action}</td>
                  <td>{t.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="cal-section">
        <h2 className="cal-section__title">Label</h2>
        <p className="note">current label: {currentLabel ?? "none"}</p>
        <div className="label-row">
          {detail.label_choices.map((choice) => (
            <button
              key={choice}
              type="button"
              className={`text-button${choice === currentLabel ? " selected" : ""}`}
              disabled={labelling}
              onClick={() => handleLabel(choice)}
            >
              {choice}
            </button>
          ))}
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="note (optional)"
          />
        </div>
        {labelError && <p style={{ color: "var(--alert)" }}>{labelError}</p>}

        {detail.labels.length > 0 && (
          <table className="cal-table" style={{ marginTop: 16 }}>
            <thead>
              <tr>
                <th>time UTC</th>
                <th>label</th>
                <th>who</th>
                <th>note</th>
              </tr>
            </thead>
            <tbody>
              {detail.labels.map((l) => (
                <tr key={l.id}>
                  <td>{formatUtcStamp(`${l.created_utc}Z`)}</td>
                  <td className="ink">{l.label}</td>
                  <td>{l.who}</td>
                  <td>{l.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
