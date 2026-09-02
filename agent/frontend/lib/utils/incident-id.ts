/** Typical ids: IN26081800000000027963 — "IN" + date digits + sequence. */
const LIKELY_INCIDENT_ID = /^IN[0-9]{8}[0-9A-Za-z]*$/;
const MINIMAL_INCIDENT_ID = /^IN[A-Z0-9]{4,}$/i;

export function validateIncidentIdShape(id: string): {
  ok: boolean;
  warning: string | null;
} {
  const trimmed = id.trim();
  if (!trimmed) {
    return { ok: false, warning: "Incident id is required." };
  }
  if (!MINIMAL_INCIDENT_ID.test(trimmed)) {
    return {
      ok: true,
      warning:
        'Does not look like a Sentinel id (expected "IN" followed by digits, e.g. IN26081800000000027963). Submit anyway — the backend distinguishes malformed from not-at-source.',
    };
  }
  if (!LIKELY_INCIDENT_ID.test(trimmed)) {
    return {
      ok: true,
      warning:
        "Format differs from the usual IN + date + digits pattern. Submit anyway.",
    };
  }
  return { ok: true, warning: null };
}
