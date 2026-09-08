import { useEffect, useState } from "react";

const UTC_FMT = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const PACIFIC_FMT = new Intl.DateTimeFormat("en-GB", {
  timeZone: "America/Los_Angeles",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** UTC + local Pacific clock in the header. LST is intentionally skipped (M0 spec). */
export default function Clock() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="clocks">
      <span>
        UTC <strong>{UTC_FMT.format(now)}</strong>
      </span>
      <span>
        PT <strong>{PACIFIC_FMT.format(now)}</strong>
      </span>
    </div>
  );
}
