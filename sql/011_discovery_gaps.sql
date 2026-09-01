-- Discovery coverage gaps: boundary holes + truncated partial windows.
--
-- Boundary gaps (reason=not_scheduled): completed windows whose ranges do not
-- meet — time that was never scheduled/collected.
--
-- Truncated gaps (reason=truncated, uncertain=true): a partial window stopped
-- at the ID cap, not the end of the data. The true uncovered end is unknown;
-- report the whole window range conservatively so under-reporting coverage is
-- avoided.
--
-- Failed and running windows are excluded from the boundary chain — a failed
-- window IS a gap and will show as one (LEAD jumps from the prior complete to
-- the next), which is the intent. Partial windows are excluded from the chain
-- because they must not masquerade as full coverage.
--
-- This query REPORTS gaps. It does not collect or backfill them.

WITH ordered AS (
  SELECT
    source,
    window_field,
    window_from,
    window_to,
    request_id,
    LEAD(window_from) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_from,
    LEAD(request_id) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_request_id
  FROM discovery_window
  WHERE status = 'complete'
),
boundary_gaps AS (
  SELECT
    source,
    window_field,
    window_to AS gap_from,
    next_from AS gap_to,
    next_from - window_to AS gap_duration,
    request_id AS before_request_id,
    next_request_id AS after_request_id,
    'not_scheduled' AS reason,
    false AS uncertain
  FROM ordered
  WHERE next_from IS NOT NULL
    AND window_to < next_from
),
truncated_gaps AS (
  SELECT
    source,
    window_field,
    window_from AS gap_from,
    window_to AS gap_to,
    window_to - window_from AS gap_duration,
    request_id AS before_request_id,
    NULL::uuid AS after_request_id,
    'truncated' AS reason,
    true AS uncertain
  FROM discovery_window
  WHERE status = 'partial'
)
SELECT * FROM boundary_gaps
UNION ALL
SELECT * FROM truncated_gaps
ORDER BY source, window_field, gap_from;
