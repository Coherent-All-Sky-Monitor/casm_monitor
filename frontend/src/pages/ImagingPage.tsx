import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Segmented from "../components/Segmented";
import TimeRangePicker from "../components/TimeRangePicker";
import { useUrlParam } from "../lib/useUrlParam";
import { resolveSince } from "../lib/timeRange";
import { formatUtcStamp } from "../lib/statusSentence";
import {
  cutoutSentence,
  imagingSectionSentence,
  IMAGING_READING_GUIDE,
  NO_CUTOUTS_SENTENCE,
  sourceLabel,
  stripSentence,
} from "../lib/imagingText";
import { getImagingHistory, getImagingManifest, imagingFigureUrl } from "../lib/api";
import { mockGetImagingHistory, mockGetImagingManifest, mockImagingFigureUrl } from "../lib/mockImaging";
import type { ImagingCutout, ImagingHistoryFrame, ImagingManifest } from "../lib/types";

const REFRESH_MS = 30_000;
const VIEWS = [
  { value: "latest", label: "latest" },
  { value: "cutouts", label: "cutouts" },
  { value: "strip", label: "strip" },
  { value: "movie", label: "movie" },
  { value: "scrub", label: "scrub" },
];

function isoWindow(range: string, customFrom: string, customTo: string): { t0: string; t1: string } {
  const t1 = range === "custom" && customTo ? new Date(customTo).toISOString() : new Date().toISOString();
  const t0 = resolveSince(range, customFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
  return { t0, t1 };
}

/**
 * The Imaging tab (M4, docs/plan.md). One server-rendered image on screen at
 * a time, same pattern as the Visibilities/SNAPs figure views: a single
 * `<img>` with a `srcset` 1x/2x pair and a `?v=` cache-busting param taken
 * from the manifest, a muted status sentence and an "open full size" link.
 * `view=movie` swaps the `<img>` for a plain `<video controls>`; `view=scrub`
 * swaps it for a slider over `/api/imaging/history` frames.
 */
export default function ImagingPage() {
  const [searchParams] = useSearchParams();
  const useMock = searchParams.get("mock") === "1";

  const [view] = useUrlParam("view", "latest");

  const [manifest, setManifest] = useState<ImagingManifest | null>(null);
  const [manifestError, setManifestError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    function load() {
      const fetcher = useMock ? mockGetImagingManifest : getImagingManifest;
      fetcher()
        .then((m) => {
          if (cancelled) return;
          setManifest(m);
          setManifestError(false);
        })
        .catch(() => !cancelled && setManifestError(true));
    }
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [useMock]);

  const figureSrc = (file: string, v?: string | null) =>
    useMock ? mockImagingFigureUrl(file) : imagingFigureUrl(file, v);

  return (
    <div>
      <p className="page__lede">
        {manifestError ? "No imaging figures rendered yet (the imaging collector may not have run)." : imagingSectionSentence(manifest)}
      </p>

      <div className="toolbar">
        <Segmented paramKey="view" defaultValue="latest" options={VIEWS} />
      </div>

      {!manifestError && view === "latest" && manifest && (
        <LatestView manifest={manifest} figureSrc={figureSrc} />
      )}
      {!manifestError && view === "cutouts" && manifest && (
        <CutoutsView manifest={manifest} figureSrc={figureSrc} />
      )}
      {!manifestError && view === "strip" && manifest && (
        <StripView manifest={manifest} figureSrc={figureSrc} />
      )}
      {!manifestError && view === "movie" && manifest && <MovieView manifest={manifest} figureSrc={figureSrc} />}
      {!manifestError && view === "scrub" && (
        <ScrubView manifest={manifest} useMock={useMock} figureSrc={figureSrc} />
      )}

      <p className="note">{IMAGING_READING_GUIDE}</p>
    </div>
  );
}

type FigureSrc = (file: string, v?: string | null) => string;

function LatestView({ manifest, figureSrc }: { manifest: ImagingManifest; figureSrc: FigureSrc }) {
  const [loadError, setLoadError] = useState(false);
  const src1x = figureSrc(manifest.latest.file_1x, manifest.rendered_utc);
  const src2x = figureSrc(manifest.latest.file_2x, manifest.rendered_utc);
  if (loadError) {
    return <p className="note">No latest image rendered yet.</p>;
  }
  return (
    <div className="vis-figure">
      <img
        className="vis-figure__img"
        src={src1x}
        srcSet={`${src1x} 1x, ${src2x} 2x`}
        loading="eager"
        alt="latest all-sky dirty image"
        onError={() => setLoadError(true)}
      />
      <p className="note">
        Latest integration {formatUtcStamp(manifest.latest.ts)}.{" "}
        <a href={src2x} target="_blank" rel="noreferrer">
          open full size
        </a>
      </p>
    </div>
  );
}

/** The per-source cutouts (M4): one `image_around_source` image per source
 * that is up, still ONE image on screen at a time — the source picker swaps
 * which one, exactly like the view picker above it. */
function CutoutsView({ manifest, figureSrc }: { manifest: ImagingManifest; figureSrc: FigureSrc }) {
  const cutouts = manifest.cutouts ?? [];
  // Segmented writes the same URL param, so reading it here is all this
  // needs — the picker and the image can never disagree.
  const [source] = useUrlParam("cutout", cutouts[0]?.source ?? "");
  const [loadError, setLoadError] = useState(false);
  const current: ImagingCutout | null =
    cutouts.find((c) => c.source === source) ?? cutouts[0] ?? null;

  // A failed load belongs to ONE source; switching sources must show the new
  // image, not the previous one's error.
  useEffect(() => setLoadError(false), [source]);

  if (cutouts.length === 0 || !current) {
    return <p className="note">{NO_CUTOUTS_SENTENCE}</p>;
  }
  const src1x = figureSrc(current.file_1x, manifest.rendered_utc);
  const src2x = figureSrc(current.file_2x, manifest.rendered_utc);
  return (
    <div>
      {cutouts.length > 1 && (
        <div className="toolbar">
          <Segmented
            paramKey="cutout"
            defaultValue={cutouts[0].source}
            options={cutouts.map((c) => ({ value: c.source, label: sourceLabel(c.source) }))}
          />
        </div>
      )}
      {loadError ? (
        <p className="note">No cutout rendered yet for {sourceLabel(current.source)}.</p>
      ) : (
        <div className="vis-figure">
          <img
            className="vis-figure__img"
            src={src1x}
            srcSet={`${src1x} 1x, ${src2x} 2x`}
            loading="eager"
            alt={`${sourceLabel(current.source)} cutout`}
            onError={() => setLoadError(true)}
          />
          <p className="note">
            {cutoutSentence(current)}{" "}
            <a href={src2x} target="_blank" rel="noreferrer">
              open full size
            </a>
          </p>
        </div>
      )}
    </div>
  );
}

function StripView({ manifest, figureSrc }: { manifest: ImagingManifest; figureSrc: FigureSrc }) {
  const [loadError, setLoadError] = useState(false);
  const src1x = figureSrc(manifest.strip.file_1x, manifest.rendered_utc);
  const src2x = figureSrc(manifest.strip.file_2x, manifest.rendered_utc);
  if (loadError) {
    return <p className="note">No 24 h strip rendered yet.</p>;
  }
  return (
    <div className="vis-figure">
      <img
        className="vis-figure__img"
        src={src1x}
        srcSet={`${src1x} 1x, ${src2x} 2x`}
        loading="eager"
        alt="24 h strip of all-sky dirty images"
        onError={() => setLoadError(true)}
      />
      <p className="note">
        {stripSentence(manifest)}{" "}
        <a href={src2x} target="_blank" rel="noreferrer">
          open full size
        </a>
      </p>
    </div>
  );
}

function MovieView({ manifest, figureSrc }: { manifest: ImagingManifest; figureSrc: FigureSrc }) {
  if (!manifest.movie.file) {
    return <p className="note">No 24 h movie rendered yet.</p>;
  }
  const src = figureSrc(manifest.movie.file, manifest.rendered_utc);
  return (
    <div className="vis-figure">
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <video className="vis-figure__img" src={src} controls />
      <p className="note">24 h all-sky movie, {manifest.movie.fps} fps.</p>
    </div>
  );
}

function ScrubView({
  manifest,
  useMock,
  figureSrc,
}: {
  manifest: ImagingManifest | null;
  useMock: boolean;
  figureSrc: FigureSrc;
}) {
  const [range] = useUrlParam("img_range_range", "24h");
  const [from] = useUrlParam("img_range_from", "");
  const [to] = useUrlParam("img_range_to", "");
  const [idxParam, setIdxParam] = useUrlParam("img_hist_i", "");

  const { t0, t1 } = useMemo(() => isoWindow(range, from, to), [range, from, to]);

  const [frames, setFrames] = useState<ImagingHistoryFrame[]>([]);
  const [loadError, setLoadError] = useState(false);
  const prefetched = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    const fetcher = useMock ? mockGetImagingHistory(t0, t1) : getImagingHistory({ t0, t1 });
    fetcher
      .then((r) => {
        if (cancelled) return;
        setFrames(r.frames);
        setLoadError(false);
        // Default to the most recent frame in the window rather than the
        // oldest, so opening scrub starts at "now" like the other views.
        setIdxParam(String(Math.max(r.frames.length - 1, 0)));
      })
      .catch(() => !cancelled && setLoadError(true));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t0, t1, useMock]);

  const idx = Math.min(Math.max(parseInt(idxParam, 10) || 0, 0), Math.max(frames.length - 1, 0));
  const frame = frames[idx] ?? null;

  // Prefetch the immediate neighbours' @1x images so dragging the slider one
  // step is a cache hit, keeping the browser cache warm as the range slider
  // moves (docs/plan.md M4 "scrub").
  useEffect(() => {
    for (const neighbour of [frames[idx - 1], frames[idx + 1]]) {
      if (!neighbour) continue;
      const key = neighbour.file_1x;
      if (prefetched.current.has(key)) continue;
      prefetched.current.add(key);
      const img = new Image();
      img.src = figureSrc(neighbour.file_1x, neighbour.ts);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frames, idx]);

  return (
    <div>
      <div className="toolbar">
        <TimeRangePicker paramPrefix="img_range" defaultRange="24h" />
      </div>

      {loadError && <p className="note">Could not load the imaging history for this window.</p>}

      {!loadError && frames.length === 0 && <p className="note">No integrations in this window.</p>}

      {!loadError && frame && (
        <div className="vis-figure">
          <img
            className="vis-figure__img"
            src={figureSrc(frame.file_1x, frame.ts)}
            loading="eager"
            alt="all-sky dirty image, scrub view"
            onError={() => setLoadError(true)}
          />
          <p className="note">
            {formatUtcStamp(frame.ts)}
            {manifest ? ` — ${manifest.antennas.length} antennas, cal ${manifest.cal_file}.` : "."}
          </p>
        </div>
      )}

      {!loadError && frames.length > 0 && (
        <div className="slider">
          <input
            type="range"
            min={0}
            max={Math.max(frames.length - 1, 0)}
            value={idx}
            onChange={(e) => setIdxParam(e.target.value)}
            aria-label="imaging time"
          />
          <span>{frame ? formatUtcStamp(frame.ts) : "no integrations in this window"}</span>
        </div>
      )}
    </div>
  );
}
