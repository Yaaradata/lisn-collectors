"""Sentinel mock reference constants.

Source: Flipkart Sentinel console screenshots and two export dumps reviewed
3 Aug 2026. Every constant is tagged OBSERVED (seen in a screenshot or export)
or INFERRED (deduced, not seen). Unverified items are tagged so a guess is
never mistaken for a fact later.
"""

# SSI — the six issue types the CT team downloads, with SOP multipliers.
# OBSERVED.
SSI = (
    ("Delay in Shipping", 1),  # OBSERVED
    ("Delay in Delivery", 3),  # OBSERVED
    ("Wishmaster refused doorstep delivery", 3),  # OBSERVED
    ("FE/Delivery Boy/Person details required", 1),  # OBSERVED
    ("Status check", 2),  # OBSERVED
    ("Request for Reschedule Delivery", 1),  # OBSERVED
)

# ISSUE_IDS — only one real mapping is known from export; others are
# deterministic placeholders: 3000 + (zlib.crc32(name) % 9000), bumped if
# colliding with the OBSERVED id 3267.
ISSUE_IDS = {
    "Delay in Delivery": 3267,  # OBSERVED
    "Delay in Shipping": 8717,  # INFERRED
    "Wishmaster refused doorstep delivery": 9193,  # INFERRED
    "FE/Delivery Boy/Person details required": 3792,  # INFERRED
    "Status check": 9759,  # INFERRED
    "Request for Reschedule Delivery": 7570,  # INFERRED
}

# STATUS — (status_id, status_status, status_status_type).
STATUS = (
    (2, "Solved", "RESOLVED"),  # OBSERVED
    (1, "Unresolved", "UNRESOLVED"),  # OBSERVED
    (8, "Updated", "UNRESOLVED"),  # id INFERRED, values OBSERVED
)

# STATUS_UI — console dropdown values, for documentation.
STATUS_UI = (
    "Unresolved",  # OBSERVED
    "Waiting for Customer Updates",  # OBSERVED
    "Solved",  # OBSERVED
    "Waiting for Inter Updates",  # OBSERVED
    "Solved NRR",  # OBSERVED
    "Solved-Others",  # OBSERVED
)

# THREAD_ENTRY_TYPES — (id, name).
THREAD_ENTRY_TYPES = (
    (1, "Note"),  # OBSERVED
    (5, "Outbound"),  # OBSERVED
    (6, "Rule Response"),  # OBSERVED
    (9, "Email"),  # OBSERVED
    (30, "Elixir Updates"),  # OBSERVED
    (1005, "Proactive"),  # OBSERVED
)

# CHANNELS — (id, name).
CHANNELS = (
    (5, "Outbound"),  # OBSERVED
    (9, "Email"),  # OBSERVED
    (1005, "Proactive"),  # OBSERVED
)

# AGING_SCORES — values seen in the dumps.
AGING_SCORES = [
    10,  # OBSERVED
    14,  # OBSERVED
    17,  # OBSERVED
    30,  # OBSERVED
    45,  # OBSERVED
    281,  # OBSERVED
    300,  # OBSERVED
    322,  # OBSERVED
    450,  # OBSERVED
    902,  # OBSERVED
]

# TRACKING_PREFIXES — FMPC / FMPP / FMPN (+ 10 digits in real data).
TRACKING_PREFIXES = [
    "FMPC",  # OBSERVED
    "FMPP",  # OBSERVED
    "FMPN",  # OBSERVED
]

# SYSTEM_USERS / HUMAN_USERS — actors seen on threads and incidents.
SYSTEM_USERS = [
    "fk_crm_automation",  # OBSERVED
    "fk_crm_matrix",  # OBSERVED
    "svc_frontend",  # OBSERVED
]
HUMAN_USERS = [
    "abdul.wahid",  # OBSERVED
    "peramanar",  # OBSERVED
    "mamidikira",  # OBSERVED
    "vanamvasi",  # OBSERVED
]

# QUEUE_NAME — queue label on incidents.
QUEUE_NAME = "IMS V2"  # OBSERVED

# ---------------------------------------------------------------------------
# Generation parameters (not Flipkart API fields; control mock/data generators)
# ---------------------------------------------------------------------------

# ~14% of incidents have no tracking ID and need an FDP lookup per the SOP.
NULL_TRACKING_RATE = 0.14

# The real export is thread-exploded; a single order occupied 4 rows in the
# dump we have. Inclusive range of thread rows per incident.
THREADS_PER_INCIDENT = (1, 4)

# Multi Track states this limit on screen; assumed for Sentinel until Flipkart
# confirms.
MAX_IDS_PER_CALL = 50
