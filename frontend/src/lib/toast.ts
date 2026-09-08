// Minimal pub/sub so api.ts (and anything else) can surface errors as a
// toast/banner without importing React. ToastStack subscribes in main.tsx.

export interface ToastMessage {
  id: number;
  text: string;
}

type Listener = (messages: ToastMessage[]) => void;

let nextId = 1;
let messages: ToastMessage[] = [];
const listeners = new Set<Listener>();

function notify() {
  for (const l of listeners) l([...messages]);
}

export function emitError(text: string): void {
  const msg: ToastMessage = { id: nextId++, text };
  messages = [...messages, msg];
  notify();
  // Auto-dismiss after 10 s so the stack does not grow unbounded during an
  // outage (the stale banner already covers the "backend is down" case).
  setTimeout(() => dismissToast(msg.id), 10_000);
}

export function dismissToast(id: number): void {
  messages = messages.filter((m) => m.id !== id);
  notify();
}

export function subscribeToasts(listener: Listener): () => void {
  listeners.add(listener);
  listener([...messages]);
  return () => listeners.delete(listener);
}
