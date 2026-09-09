import { useEffect, useState } from "react";
import { dismissToast, subscribeToasts, type ToastMessage } from "../lib/toast";

/**
 * One plain alert sentence at the top of the page for the most recent API
 * error, with a "dismiss" word. Replaces the toast stack: errors are text,
 * not stacked cards.
 */
export default function AlertLine() {
  const [messages, setMessages] = useState<ToastMessage[]>([]);
  useEffect(() => subscribeToasts(setMessages), []);

  const latest = messages[messages.length - 1];
  if (!latest) return null;

  return (
    <div className="alert-line" role="alert">
      <span>{latest.text}</span>
      <button className="text-button" onClick={() => messages.forEach((m) => dismissToast(m.id))}>
        dismiss
      </button>
    </div>
  );
}
