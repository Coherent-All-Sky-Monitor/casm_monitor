import type { ReactNode } from "react";

interface PageProps {
  title: string;
  milestone?: string; // e.g. "M1" — renders a "coming in M<n>" tag
  children: ReactNode;
}

/** Shared layout for every tab page: title + optional milestone tag + body. */
export default function Page({ title, milestone, children }: PageProps) {
  return (
    <div className="page">
      <div className="page-title">
        <h2>{title}</h2>
        {milestone && <span className="milestone-tag">coming in {milestone}</span>}
      </div>
      <div className="page-body">{children}</div>
    </div>
  );
}
