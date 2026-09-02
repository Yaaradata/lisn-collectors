"use client";

import { useEffect, useState } from "react";

/** Tick elapsed ms while `active` — for showing counters after 2s. */
export function useElapsed(active: boolean): number {
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    if (!active) {
      setElapsedMs(0);
      return;
    }
    const started = Date.now();
    setElapsedMs(0);
    const id = window.setInterval(() => {
      setElapsedMs(Date.now() - started);
    }, 250);
    return () => window.clearInterval(id);
  }, [active]);

  return elapsedMs;
}

export function showElapsedCounter(elapsedMs: number): boolean {
  return elapsedMs >= 2000;
}

export function formatElapsedLabel(elapsedMs: number): string {
  const sec = Math.floor(elapsedMs / 1000);
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}
