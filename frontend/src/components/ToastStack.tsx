import { useEffect, useState } from "react";
import { dismissToast, subscribeToasts, type ToastMessage } from "../lib/toast";

export default function ToastStack() {
  const [messages, setMessages] = useState<ToastMessage[]>([]);

  useEffect(() => subscribeToasts(setMessages), []);

  if (messages.length === 0) return null;

  return (
    <div className="toast-stack">
      {messages.map((m) => (
        <div className="toast" key={m.id}>
          <span>{m.text}</span>
          <button onClick={() => dismissToast(m.id)} aria-label="dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
