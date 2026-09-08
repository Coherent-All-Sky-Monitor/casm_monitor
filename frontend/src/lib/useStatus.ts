// Drives the status strip: subscribes to /ws/status with automatic
// reconnect, falls back to polling /api/status every 10 s while the socket
// is down, and flags "stale" when nothing has arrived for 30 s.
import { useEffect, useRef, useState } from "react";
import { getStatus, statusWebSocketUrl } from "./api";
import type { StatusResponse } from "./types";

const POLL_MS = 10_000;
const STALE_AFTER_MS = 30_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 15_000;

export interface UseStatusResult {
  status: StatusResponse | null;
  connected: boolean; // websocket currently connected
  backendStale: boolean; // no update received for STALE_AFTER_MS
  lastUpdate: number | null; // ms epoch of last received payload
}

export function useStatus(): UseStatusResult {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const [backendStale, setBackendStale] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttempt = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const closedByUs = useRef(false);

  useEffect(() => {
    closedByUs.current = false;

    function accept(payload: StatusResponse) {
      setStatus(payload);
      setLastUpdate(Date.now());
    }

    function startPolling() {
      if (closedByUs.current) return;
      if (pollTimer.current) return;
      // Poll immediately, then on the interval.
      getStatus()
        .then(accept)
        .catch(() => undefined);
      pollTimer.current = setInterval(() => {
        getStatus()
          .then(accept)
          .catch(() => undefined);
      }, POLL_MS);
    }

    function stopPolling() {
      if (pollTimer.current) {
        clearInterval(pollTimer.current);
        pollTimer.current = null;
      }
    }

    function connect() {
      if (closedByUs.current) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(statusWebSocketUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectAttempt.current = 0;
        setConnected(true);
        stopPolling();
      };

      ws.onmessage = (evt) => {
        try {
          const payload = JSON.parse(evt.data) as StatusResponse;
          accept(payload);
        } catch {
          // ignore malformed frame; next one will land
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        setConnected(false);
        if (closedByUs.current) return;
        startPolling();
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    function closeSocket(ws: WebSocket) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
    }

    function scheduleReconnect() {
      if (closedByUs.current) return;
      // Add jitter so many clients reconnecting at once do not thunder in
      // lockstep, while keeping the same exponential backoff cap.
      const base = Math.min(
        RECONNECT_MAX_MS,
        RECONNECT_BASE_MS * 2 ** reconnectAttempt.current,
      );
      const jitter = base * (0.2 * Math.random() - 0.1);
      const delay = Math.min(RECONNECT_MAX_MS, Math.max(0, base + jitter));
      reconnectAttempt.current += 1;
      reconnectTimer.current = setTimeout(connect, delay);
    }

    // Start polling right away so the strip has data before/alongside the
    // websocket handshake, then attempt the socket.
    startPolling();
    connect();

    return () => {
      closedByUs.current = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      stopPolling();
      if (wsRef.current) closeSocket(wsRef.current);
      wsRef.current = null;
    };
  }, []);

  // Single long-lived interval that reads lastUpdate via a ref, rather than
  // a new setInterval on every payload (the previous [lastUpdate] dependency
  // meant a fresh timer was created on every message).
  const lastUpdateRef = useRef<number | null>(null);
  lastUpdateRef.current = lastUpdate;

  useEffect(() => {
    const timer = setInterval(() => {
      const last = lastUpdateRef.current;
      setBackendStale(last === null || Date.now() - last > STALE_AFTER_MS);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  return { status, connected, backendStale, lastUpdate };
}
