import Page from "../components/Page";
import SmokeSparkline from "../components/SmokeSparkline";
import { PLACEHOLDER_COPY } from "../lib/placeholderCopy";

interface PlaceholderPageProps {
  slug: keyof typeof PLACEHOLDER_COPY;
  /** Render the Plotly smoke test once, on the first placeholder tab only. */
  showSmokeTest?: boolean;
}

export default function PlaceholderPage({ slug, showSmokeTest }: PlaceholderPageProps) {
  const copy = PLACEHOLDER_COPY[slug];
  return (
    <Page title={copy.title} milestone={copy.milestone}>
      <p>{copy.body}</p>
      {showSmokeTest && <SmokeSparkline />}
    </Page>
  );
}
