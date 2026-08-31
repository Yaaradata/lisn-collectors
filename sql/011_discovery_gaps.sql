-- Contiguity gaps between completed discovery windows.
--
-- Failed and running windows are deliberately excluded — a failed window IS a
-- gap and will show as one (LEAD jumps from the prior complete to the next),
-- which is the intent.
--
-- This query REPORTS gaps. It does not collect or backfill them. Deciding to
-- backfill is a scheduling decision and belongs with LiSN.

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
)
SELECT
  source,
  window_field,
  window_to AS gap_from,
  next_from AS gap_to,
  next_from - window_to AS gap_duration,
  request_id AS before_request_id,
  next_request_id AS after_request_id
FROM ordered
WHERE next_from IS NOT NULL
  AND window_to < next_from
ORDER BY source, window_field, gap_from;
