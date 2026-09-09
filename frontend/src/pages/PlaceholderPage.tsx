import { PLACEHOLDER_COPY } from "../lib/placeholderCopy";

/** A tab that is not built yet: one muted sentence, nothing else. */
export default function PlaceholderPage({ slug }: { slug: string }) {
  return <p className="note">{PLACEHOLDER_COPY[slug] ?? "Not built yet."}</p>;
}
