/**
 * LiSN Collector Console — connection, status bar, Mode 1 + Mode 2.
 *
 * Do NOT put identity tokens in browser JavaScript.
 * Do NOT make the collector-api publicly invokable to work around auth.
 * Use: gcloud run services proxy collector-api --region=asia-south1 --port=8080
 * so the browser talks to localhost and the proxy attaches the token.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "lisn.collector.apiBase";
  var DEFAULT_BASE = "http://localhost:8080";
  var DETAIL_INTERVAL_MS = 10000;
  var BATCH_CAP = 50;
  var POLL_MS = 1000;
  var MAX_WINDOW_MS = 15 * 24 * 60 * 60 * 1000;
  // Last 24h of the seeded mock range (UTC wall times for datetime-local).
  var DEFAULT_FROM = "2026-08-24T23:59";
  var DEFAULT_TO = "2026-08-25T23:59";
  // Mirrored from mock/reference.py SSI — keep in sync.
  var SSI_ISSUE_NAMES = [
    "Delay in Shipping",
    "Delay in Delivery",
    "Wishmaster refused doorstep delivery",
    "FE/Delivery Boy/Person details required",
    "Status check",
    "Request for Reschedule Delivery",
  ];

  var apiBaseInput = document.getElementById("api-base");
  var btnTest = document.getElementById("btn-test");
  var healthPill = document.getElementById("health-pill");
  var healthBody = document.getElementById("health-body");
  var elWorkers = document.getElementById("stat-workers");
  var elInflight = document.getElementById("stat-inflight");
  var elUnloaded = document.getElementById("stat-unloaded");
  var elDead = document.getElementById("stat-dead");
  var elUpdated = document.getElementById("stat-updated");

  var idList = document.getElementById("id-list");
  var idCount = document.getElementById("id-count");
  var btnSample = document.getElementById("btn-sample-ids");
  var btnCollect = document.getElementById("btn-collect");
  var btnInvalid = document.getElementById("btn-invalid");

  var updatedFrom = document.getElementById("updated-from");
  var updatedTo = document.getElementById("updated-to");
  var issueList = document.getElementById("issue-list");
  var btnDiscover = document.getElementById("btn-discover");
  var discoverClientError = document.getElementById("discover-client-error");
  var stageDiscover = document.getElementById("stage-discover");
  var stageReview = document.getElementById("stage-review");
  var stageEnrich2 = document.getElementById("stage-enrich2");
  var reviewBody = document.getElementById("review-body");
  var reviewLockNote = document.getElementById("review-lock-note");
  var reviewFound = document.getElementById("review-found");
  var reviewWarning = document.getElementById("review-warning");
  var enrichHowMany = document.getElementById("enrich-how-many");
  var btnLoadBridge = document.getElementById("btn-load-bridge");
  var reviewError = document.getElementById("review-error");
  var bridgeMeta = document.getElementById("bridge-meta");
  var bridgeSample = document.getElementById("bridge-sample");
  var enrich2Body = document.getElementById("enrich2-body");
  var enrich2LockNote = document.getElementById("enrich2-lock-note");
  var enrich2Ready = document.getElementById("enrich2-ready");
  var btnEnrich2 = document.getElementById("btn-enrich2");
  var enrich2Bridge = document.getElementById("enrich2-bridge");
  var resultsPanel = document.getElementById("results-panel");
  var resultsRequest = document.getElementById("results-request");
  var tilePages = document.getElementById("tile-pages");
  var tileGcs = document.getElementById("tile-gcs");
  var tileBq = document.getElementById("tile-bq");
  var tileBqSub = document.getElementById("tile-bq-sub");
  var tileUnloaded = document.getElementById("tile-unloaded");
  var resultsJobsBody = document.querySelector("#results-jobs tbody");
  var bqExplainer = document.getElementById("bq-explainer");
  var btnResetDry = document.getElementById("btn-reset-dry");
  var btnResetReal = document.getElementById("btn-reset-real");
  var resetConfirm = document.getElementById("reset-confirm");
  var resetConfirmBlock = document.getElementById("reset-confirm-block");
  var resetDryOut = document.getElementById("reset-dry-out");
  var resetRealOut = document.getElementById("reset-real-out");
  var resetError = document.getElementById("reset-error");
  var resetPreserved = document.getElementById("reset-preserved");
  var preservedLines = document.getElementById("preserved-lines");
  var workersBanner = document.getElementById("workers-banner");
  var workersSummary = document.getElementById("workers-summary");
  var workersTableBody = document.querySelector("#workers-table tbody");

  var detailTimer = null;
  var activePoll = null;
  var lastDryRun = null;
  var preservedBeforeReset = null;
  var mode2 = {
    discoveredCount: 0,
    bridgeIds: [],
    bridgePendingBefore: 0,
  };

  function normalizeBase(raw) {
    var base = (raw || "").trim().replace(/\/+$/, "");
    return base || DEFAULT_BASE;
  }

  function loadBase() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved) return normalizeBase(saved);
    } catch (_) {
      /* private mode */
    }
    return DEFAULT_BASE;
  }

  function saveBase(base) {
    try {
      localStorage.setItem(STORAGE_KEY, base);
    } catch (_) {
      /* ignore */
    }
  }

  function apiUrl(path) {
    var base = normalizeBase(apiBaseInput.value);
    if (!path.startsWith("/")) path = "/" + path;
    return base + path;
  }

  function setPill(state, label) {
    healthPill.className = "pill " + state;
    healthPill.textContent = label;
  }

  function formatJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_) {
      return String(value);
    }
  }

  function formatInt(n) {
    return Number(n).toLocaleString("en-US");
  }

  function sumInFlight(counts) {
    var total = 0;
    if (!counts || typeof counts !== "object") return 0;
    Object.keys(counts).forEach(function (source) {
      var row = counts[source] || {};
      total += Number(row.in_progress || 0);
    });
    return total;
  }

  function parseIds(raw) {
    return String(raw || "")
      .split(/[\n,]+/)
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
  }

  function selectedKeyType() {
    var el = document.querySelector('input[name="key-type"]:checked');
    return el ? el.value : "incident_ids";
  }

  function setKeyType(value) {
    var el = document.querySelector(
      'input[name="key-type"][value="' + value + '"]'
    );
    if (el) el.checked = true;
  }

  function updateIdCount() {
    var ids = parseIds(idList.value);
    var pages = ids.length ? Math.ceil(ids.length / BATCH_CAP) : 0;
    idCount.textContent =
      formatInt(ids.length) +
      " IDs · " +
      formatInt(pages) +
      " pages at " +
      BATCH_CAP +
      " per page";
    btnCollect.disabled = ids.length === 0;
  }

  function showBox(el, text) {
    if (!el) return;
    el.hidden = false;
    var textEl = el.querySelector(".error-text");
    if (textEl) textEl.textContent = text;
    else el.textContent = text;
  }

  function hideBox(el) {
    if (!el) return;
    el.hidden = true;
    var textEl = el.querySelector(".error-text");
    if (textEl) textEl.textContent = "";
    else if (!el.querySelector(".dismiss")) el.textContent = "";
  }

  function wireDismiss(root) {
    (root || document).querySelectorAll(".error-box.dismissible .dismiss").forEach(
      function (btn) {
        btn.addEventListener("click", function () {
          var box = btn.closest(".error-box");
          if (box) hideBox(box);
        });
      }
    );
  }

  async function requestJson(method, path, body) {
    var opts = {
      method: method,
      headers: { Accept: "application/json" },
      cache: "no-store",
    };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    var response = await fetch(apiUrl(path), opts);
    var text = await response.text();
    var parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch (_) {
      parsed = { raw: text };
    }
    return { ok: response.ok, status: response.status, body: parsed, text: text };
  }

  function errorMessage(result) {
    var body = result.body;
    if (body && typeof body.detail === "string") return body.detail;
    if (body && body.detail != null) return formatJson(body.detail);
    if (body) return formatJson(body);
    return result.text || "(empty body)";
  }

  function formatHttpError(result) {
    if (result.status === 400) return errorMessage(result);
    return "HTTP " + result.status + "\n" + errorMessage(result);
  }

  function elapsedLabel(startedAt) {
    var sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return (m > 0 ? m + "m " : "") + s + "s";
  }

  function makeProgressUI(ids) {
    return {
      panel: document.getElementById(ids.panel),
      requestId: document.getElementById(ids.requestId),
      elapsed: document.getElementById(ids.elapsed),
      bar: document.getElementById(ids.bar),
      barWrap: document.getElementById(ids.barWrap),
      label: document.getElementById(ids.label),
      pills: document.getElementById(ids.pills),
      summary: document.getElementById(ids.summary),
      error: document.getElementById(ids.error),
    };
  }

  function shortTs(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString();
    } catch (_) {
      return String(iso);
    }
  }

  async function showResults(requestId) {
    if (!requestId) return;
    resultsPanel.hidden = false;
    resultsRequest.textContent = "request_id " + requestId;
    tilePages.textContent = "…";
    tileGcs.textContent = "…";
    tileBq.textContent = "…";
    tileBqSub.textContent = "";
    tileUnloaded.textContent = "…";
    resultsJobsBody.innerHTML = "";
    try {
      var result = await requestJson(
        "GET",
        "/v1/requests/" + encodeURIComponent(requestId) + "/results"
      );
      if (!result.ok) {
        tilePages.textContent = "—";
        resultsRequest.textContent +=
          " · results error HTTP " + result.status;
        return;
      }
      var body = result.body || {};
      var pages = body.pages || {};
      tilePages.textContent =
        formatInt(pages.done || 0) + " / " + formatInt(pages.total || 0);
      tileGcs.textContent = formatInt(body.gcs_objects || 0);
      tileBq.textContent =
        body.bigquery_rows == null ? "—" : formatInt(body.bigquery_rows);
      if (body.bigquery_distinct != null) {
        tileBqSub.textContent =
          formatInt(body.bigquery_distinct) + " distinct incidents";
      }
      tileUnloaded.textContent = formatInt(body.unloaded || 0);
      bqExplainer.hidden = body.source === "sentinel_discovery";
      (body.recent_jobs || []).forEach(function (job) {
        var tr = document.createElement("tr");
        [
          job.page_no,
          job.status,
          job.owner || "—",
          job.record_count == null ? "—" : formatInt(job.record_count),
          shortTs(job.raw_written_at),
          shortTs(job.loaded_at),
        ].forEach(function (cell) {
          var td = document.createElement("td");
          td.textContent = String(cell);
          tr.appendChild(td);
        });
        resultsJobsBody.appendChild(tr);
      });
      resultsPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      resultsRequest.textContent +=
        " · " + (err && err.message ? err.message : err);
    }
  }

  function paintStatusPills(root, counts) {
    ["pending", "in_progress", "done", "dead"].forEach(function (key) {
      var n = Number((counts && counts[key]) || 0);
      var el = root.querySelector('.pill[data-status="' + key + '"]');
      if (!el) return;
      el.textContent = key + " " + formatInt(n);
      if (n > 0) el.classList.add("active");
      else el.classList.remove("active");
    });
  }

  function paintProgress(ui, counts, totalPages, startedAt) {
    var done = Number((counts && counts.done) || 0);
    var pct = totalPages > 0 ? Math.min(100, (done / totalPages) * 100) : 0;
    ui.bar.style.width = pct + "%";
    ui.barWrap.setAttribute("aria-valuenow", String(Math.round(pct)));
    ui.label.textContent =
      formatInt(done) + " / " + formatInt(totalPages) + " pages";
    paintStatusPills(ui.pills, counts || {});
    ui.elapsed.textContent = "elapsed " + elapsedLabel(startedAt);
  }

  function stopPoll() {
    if (activePoll && activePoll.timer) {
      clearInterval(activePoll.timer);
    }
    activePoll = null;
  }

  function startPolling(ui, requestId, totalPages, onComplete) {
    stopPoll();
    var startedAt = Date.now();
    ui.panel.hidden = false;
    ui.summary.hidden = true;
    ui.requestId.textContent = "request_id " + requestId;
    paintProgress(ui, {}, totalPages, startedAt);

    async function tick() {
      try {
        var result = await requestJson(
          "GET",
          "/v1/requests/" + encodeURIComponent(requestId) + "/counts"
        );
        if (!result.ok) {
          stopPoll();
          showBox(ui.error, formatHttpError(result));
          return;
        }
        var counts = (result.body && result.body.counts) || {};
        var records = Number((result.body && result.body.records) || 0);
        paintProgress(ui, counts, totalPages, startedAt);
        var done = Number(counts.done || 0);
        if (done >= totalPages && totalPages > 0) {
          stopPoll();
          ui.summary.hidden = false;
          ui.summary.textContent =
            "Complete · " +
            formatInt(done) +
            " / " +
            formatInt(totalPages) +
            " pages · elapsed " +
            elapsedLabel(startedAt);
          if (onComplete) {
            onComplete({
              counts: counts,
              records: records,
              requestId: requestId,
            });
          }
        }
      } catch (err) {
        stopPoll();
        showBox(ui.error, String(err && err.message ? err.message : err));
      }
    }

    activePoll = { timer: null, requestId: requestId };
    tick();
    activePoll.timer = setInterval(tick, POLL_MS);
  }

  async function postCollect(source, querySpec, ui, onComplete) {
    hideBox(ui.error);
    stopPoll();
    ui.panel.hidden = true;
    ui.summary.hidden = true;

    try {
      var result = await requestJson("POST", "/v1/collect", {
        source: source,
        query_spec: querySpec,
      });
      if (!result.ok) {
        showBox(ui.error, formatHttpError(result));
        return null;
      }
      var requestId = result.body && result.body.request_id;
      var totalPages = Number((result.body && result.body.total_pages) || 0);
      if (!requestId) {
        showBox(
          ui.error,
          "Collect succeeded but response had no request_id:\n" +
            formatJson(result.body)
        );
        return null;
      }
      startPolling(ui, requestId, totalPages, onComplete);
      return result.body;
    } catch (err) {
      showBox(ui.error, String(err && err.message ? err.message : err));
      return null;
    }
  }

  /* ---------- Mode 1 ---------- */

  var mode1UI = makeProgressUI({
    panel: "enrich-progress",
    requestId: "enrich-request-id",
    elapsed: "enrich-elapsed",
    bar: "enrich-bar",
    barWrap: "enrich-bar-wrap",
    label: "enrich-progress-label",
    pills: "enrich-status-pills",
    summary: "enrich-summary",
    error: "enrich-error",
  });

  async function submitCollectMode1(querySpec, opts) {
    opts = opts || {};
    btnCollect.disabled = true;
    btnInvalid.disabled = true;
    try {
      if (opts.skipProgress) {
        hideBox(mode1UI.error);
        var result = await requestJson("POST", "/v1/collect", {
          source: "sentinel",
          query_spec: querySpec,
        });
        if (!result.ok) showBox(mode1UI.error, formatHttpError(result));
        return;
      }
      await postCollect("sentinel", querySpec, mode1UI, function (info) {
        showResults(info.requestId);
      });
    } finally {
      updateIdCount();
      btnInvalid.disabled = false;
    }
  }

  async function onCollect() {
    var ids = parseIds(idList.value);
    if (!ids.length) return;
    var keyType = selectedKeyType();
    var querySpec = {};
    querySpec[keyType] = ids;
    await submitCollectMode1(querySpec);
  }

  async function onInvalid() {
    await submitCollectMode1({ status: "open" }, { skipProgress: true });
  }

  async function onLoadSample() {
    hideBox(mode1UI.error);
    btnSample.disabled = true;
    try {
      var result = await requestJson(
        "GET",
        "/v1/admin/sample-ids?source=sentinel&limit=1000"
      );
      if (!result.ok) {
        showBox(mode1UI.error, formatHttpError(result));
        return;
      }
      var ids = (result.body && result.body.ids) || [];
      if (!ids.length) {
        showBox(
          mode1UI.error,
          (result.body && result.body.message) ||
            "No sample IDs returned — run a collection or use date mode first"
        );
        return;
      }
      setKeyType("incident_ids");
      idList.value = ids.join("\n");
      updateIdCount();
    } catch (err) {
      showBox(mode1UI.error, String(err && err.message ? err.message : err));
    } finally {
      btnSample.disabled = false;
    }
  }

  /* ---------- Mode 2 ---------- */

  var discoverUI = makeProgressUI({
    panel: "discover-progress",
    requestId: "discover-request-id",
    elapsed: "discover-elapsed",
    bar: "discover-bar",
    barWrap: "discover-bar-wrap",
    label: "discover-progress-label",
    pills: "discover-status-pills",
    summary: "discover-summary",
    error: "discover-error",
  });

  var enrich2UI = makeProgressUI({
    panel: "enrich2-progress",
    requestId: "enrich2-request-id",
    elapsed: "enrich2-elapsed",
    bar: "enrich2-bar",
    barWrap: "enrich2-bar-wrap",
    label: "enrich2-progress-label",
    pills: "enrich2-status-pills",
    summary: "enrich2-summary",
    error: "enrich2-error",
  });

  function renderIssueCheckboxes() {
    issueList.innerHTML = "";
    SSI_ISSUE_NAMES.forEach(function (name, i) {
      var id = "issue-" + i;
      var label = document.createElement("label");
      label.innerHTML =
        '<input type="checkbox" id="' +
        id +
        '" value="' +
        name.replace(/"/g, "&quot;") +
        '" checked />' +
        "<span></span>";
      label.querySelector("span").textContent = name;
      issueList.appendChild(label);
    });
  }

  function selectedIssues() {
    return Array.prototype.slice
      .call(issueList.querySelectorAll('input[type="checkbox"]:checked'))
      .map(function (el) {
        return el.value;
      });
  }

  /** datetime-local value → ISO-8601 UTC string for the API. */
  function localInputToIso(value) {
    if (!value) return null;
    // Treat the wall clock as UTC for the seeded demo dataset.
    return value.length === 16 ? value + ":00Z" : value + "Z";
  }

  function validateDiscoverDates() {
    hideBox(discoverClientError);
    var fromVal = updatedFrom.value;
    var toVal = updatedTo.value;
    if (!fromVal || !toVal) {
      showBox(discoverClientError, "Both Updated from and Updated to are required.");
      return null;
    }
    var fromMs = Date.parse(localInputToIso(fromVal));
    var toMs = Date.parse(localInputToIso(toVal));
    if (Number.isNaN(fromMs) || Number.isNaN(toMs)) {
      showBox(discoverClientError, "Dates must be valid.");
      return null;
    }
    if (fromMs > toMs) {
      showBox(discoverClientError, "Updated from must be before Updated to.");
      return null;
    }
    if (toMs - fromMs > MAX_WINDOW_MS) {
      showBox(
        discoverClientError,
        "Window exceeds 15 days. The real Sentinel console enforces this."
      );
      return null;
    }
    return {
      updated_from: localInputToIso(fromVal),
      updated_to: localInputToIso(toVal),
    };
  }

  function unlockReview(discoveredCount) {
    mode2.discoveredCount = discoveredCount;
    stageDiscover.classList.add("complete");
    stageReview.classList.remove("locked");
    reviewLockNote.hidden = true;
    reviewBody.hidden = false;
    reviewFound.textContent =
      "Found " + formatInt(discoveredCount) + " incident IDs in this window.";
    var pages = Math.ceil(discoveredCount / BATCH_CAP);
    var seconds = pages / 3;
    var minutes = seconds / 60;
    if (discoveredCount >= 5000) {
      reviewWarning.hidden = false;
      reviewWarning.textContent =
        formatInt(discoveredCount) +
        " IDs = " +
        formatInt(pages) +
        " pages ≈ " +
        (minutes >= 1
          ? minutes.toFixed(1) + " minutes"
          : Math.round(seconds) + " seconds") +
        " at 3 requests/second.";
    } else {
      reviewWarning.hidden = true;
    }
    enrichHowMany.max = String(Math.max(1, discoveredCount));
    enrichHowMany.value = String(
      Math.min(1000, Math.max(1, discoveredCount))
    );
    // Reset stage 3
    stageEnrich2.classList.add("locked");
    stageEnrich2.classList.remove("complete");
    enrich2LockNote.hidden = false;
    enrich2Body.hidden = true;
    mode2.bridgeIds = [];
    hideBox(reviewError);
    bridgeMeta.hidden = true;
    bridgeSample.hidden = true;
  }

  function unlockEnrich2(ids, pendingTotal) {
    mode2.bridgeIds = ids.slice();
    mode2.bridgePendingBefore = pendingTotal;
    stageReview.classList.add("complete");
    stageEnrich2.classList.remove("locked");
    enrich2LockNote.hidden = true;
    enrich2Body.hidden = false;
    enrich2Ready.textContent =
      formatInt(ids.length) +
      " IDs ready to enrich (" +
      formatInt(pendingTotal) +
      " still pending on the bridge).";
    enrich2Bridge.hidden = true;
    hideBox(enrich2UI.error);
    enrich2UI.panel.hidden = true;
  }

  async function onDiscover() {
    var windowSpec = validateDiscoverDates();
    if (!windowSpec) return;
    var issues = selectedIssues();
    if (!issues.length) {
      showBox(discoverClientError, "Select at least one issue type.");
      return;
    }
    btnDiscover.disabled = true;
    try {
      await postCollect(
        "sentinel_discovery",
        {
          updated_from: windowSpec.updated_from,
          updated_to: windowSpec.updated_to,
          issue_names: issues,
          limit: 1000,
        },
        discoverUI,
        function (info) {
          var n = Number(info.records || 0);
          discoverUI.summary.textContent =
            "Found " +
            formatInt(n) +
            " incident IDs in this window.";
          unlockReview(n);
        }
      );
    } finally {
      btnDiscover.disabled = false;
    }
  }

  async function onLoadBridge() {
    hideBox(reviewError);
    var want = Number(enrichHowMany.value || 0);
    if (!want || want < 1) {
      showBox(reviewError, "Enrich how many? must be at least 1.");
      return;
    }
    if (mode2.discoveredCount > 0) {
      want = Math.min(want, mode2.discoveredCount);
    }
    btnLoadBridge.disabled = true;
    try {
      var result = await requestJson(
        "GET",
        "/v1/discovered/pending?limit=" + encodeURIComponent(String(want))
      );
      if (!result.ok) {
        showBox(reviewError, formatHttpError(result));
        return;
      }
      var ids = (result.body && result.body.ids) || [];
      var pendingTotal = Number(
        (result.body && result.body.pending_total) || ids.length
      );
      if (!ids.length) {
        showBox(
          reviewError,
          "Bridge returned 0 pending IDs — everything discovered may already be enriched, or discovery wrote nothing."
        );
        bridgeMeta.hidden = true;
        bridgeSample.hidden = true;
        return;
      }
      bridgeMeta.hidden = false;
      bridgeMeta.textContent =
        "Loaded " +
        formatInt(ids.length) +
        " of " +
        formatInt(pendingTotal) +
        " pending (bridge: discovered not in incidents_current).";
      bridgeSample.hidden = false;
      bridgeSample.textContent = ids.slice(0, 12).join("\n") +
        (ids.length > 12 ? "\n…" : "");
      unlockEnrich2(ids, pendingTotal);
    } catch (err) {
      showBox(reviewError, String(err && err.message ? err.message : err));
    } finally {
      btnLoadBridge.disabled = false;
    }
  }

  async function onEnrich2() {
    if (!mode2.bridgeIds.length) return;
    btnEnrich2.disabled = true;
    enrich2Bridge.hidden = true;
    var before = mode2.bridgePendingBefore;
    try {
      await postCollect(
        "sentinel",
        { incident_ids: mode2.bridgeIds },
        enrich2UI,
        async function (info) {
          stageEnrich2.classList.add("complete");
          showResults(info.requestId);
          try {
            var result = await requestJson(
              "GET",
              "/v1/discovered/pending?limit=1"
            );
            var after =
              result.ok && result.body
                ? Number(result.body.pending_total || 0)
                : NaN;
            enrich2Bridge.hidden = false;
            if (!Number.isNaN(after)) {
              enrich2Bridge.textContent =
                "Bridge: " +
                formatInt(before) +
                " → " +
                formatInt(after) +
                ". " +
                (after === 0
                  ? "Everything discovered has been collected."
                  : "Remaining pending IDs still need enrichment.");
            } else {
              enrich2Bridge.textContent =
                "Enrichment finished; could not re-read bridge (" +
                formatHttpError(result) +
                ").";
            }
          } catch (err) {
            enrich2Bridge.hidden = false;
            enrich2Bridge.textContent =
              "Enrichment finished; bridge re-read failed: " +
              (err && err.message ? err.message : err);
          }
        }
      );
    } finally {
      btnEnrich2.disabled = false;
    }
  }

  /* ---------- Connection + status bar ---------- */

  async function testConnection() {
    var base = normalizeBase(apiBaseInput.value);
    apiBaseInput.value = base;
    saveBase(base);
    btnTest.disabled = true;
    setPill("idle", "Testing…");
    healthBody.hidden = true;
    try {
      var result = await requestJson("GET", "/health");
      healthBody.hidden = false;
      healthBody.textContent = formatJson(result.body);
      if (result.ok && result.body && result.body.status === "ok") {
        setPill("ok", "Connected · HTTP " + result.status);
        refreshDetail();
      } else {
        setPill("bad", "Unhealthy · HTTP " + result.status);
      }
    } catch (err) {
      healthBody.hidden = false;
      healthBody.textContent =
        String(err && err.message ? err.message : err) +
        "\n\nIf the API is up, this is often a CORS block. Use the gcloud proxy " +
        "for auth — do not put tokens here or make the API public.";
      setPill("bad", "Unreachable");
    } finally {
      btnTest.disabled = false;
    }
  }

  function paintWorkers(detail) {
    var live =
      typeof detail.live_workers === "number" ? detail.live_workers : 0;
    var workers = Array.isArray(detail.workers) ? detail.workers : [];
    workersSummary.textContent =
      "Live workers: " + formatInt(live) + " · registered " + formatInt(workers.length);
    workersBanner.hidden = live > 0;
    workersTableBody.innerHTML = "";
    workers.forEach(function (w) {
      var tr = document.createElement("tr");
      var age =
        typeof w.heartbeat_age_seconds === "number"
          ? w.heartbeat_age_seconds.toFixed(1)
          : "—";
      [w.id, age].forEach(function (cell) {
        var td = document.createElement("td");
        td.textContent = String(cell);
        tr.appendChild(td);
      });
      workersTableBody.appendChild(tr);
    });
  }

  function paintDetail(body) {
    var workers =
      typeof body.live_workers === "number"
        ? body.live_workers
        : Array.isArray(body.workers)
          ? body.workers.length
          : "—";
    elWorkers.textContent = workers;
    elInflight.textContent = sumInFlight(body.counts);
    elUnloaded.textContent =
      typeof body.unloaded === "number" ? body.unloaded : "—";
    elDead.textContent = typeof body.dead === "number" ? body.dead : "—";
    elUpdated.textContent =
      "Last updated " + new Date().toLocaleTimeString();
    paintWorkers(body);
  }

  async function refreshDetail() {
    try {
      var result = await requestJson("GET", "/v1/health/detail");
      if (!result.ok) {
        elUpdated.textContent =
          "Last updated — status poll failed · HTTP " + result.status;
        return;
      }
      paintDetail(result.body || {});
    } catch (_) {
      elUpdated.textContent = "Last updated — status poll unreachable";
    }
  }

  function startDetailPolling() {
    if (detailTimer) clearInterval(detailTimer);
    refreshDetail();
    detailTimer = setInterval(refreshDetail, DETAIL_INTERVAL_MS);
  }

  function renderPreserved(preserved, changed) {
    resetPreserved.hidden = false;
    resetPreserved.classList.toggle("warn", !!changed);
    var inc = preserved && preserved.sentinel_incident;
    var thr = preserved && preserved.sentinel_thread;
    var workers = preserved && preserved.procrastinate_workers;
    preservedLines.innerHTML = "";
    function line(label, value, bad) {
      var p = document.createElement("p");
      p.style.margin = "0.25rem 0";
      if (bad) p.style.color = "var(--bad)";
      p.textContent =
        label +
        ": " +
        (value == null ? "—" : formatInt(value)) +
        (bad ? " ⚠ changed across reset" : "");
      preservedLines.appendChild(p);
    }
    var before = preservedBeforeReset || {};
    line(
      "sentinel_incident",
      inc,
      before.sentinel_incident != null &&
        inc != null &&
        before.sentinel_incident !== inc
    );
    line(
      "sentinel_thread",
      thr,
      before.sentinel_thread != null &&
        thr != null &&
        before.sentinel_thread !== thr
    );
    line("procrastinate_workers", workers, false);
    if (changed) {
      var warn = document.createElement("p");
      warn.style.fontWeight = "700";
      warn.textContent =
        "Sample data counts changed — this cannot be recovered by re-running a collection.";
      preservedLines.appendChild(warn);
    }
  }

  async function onResetDry() {
    hideBox(resetError);
    resetRealOut.hidden = true;
    btnResetDry.disabled = true;
    try {
      var result = await requestJson(
        "DELETE",
        "/v1/admin/collector-data?confirm=reset-collector-data&dry_run=true"
      );
      if (!result.ok) {
        showBox(resetError, formatHttpError(result));
        return;
      }
      lastDryRun = result.body;
      preservedBeforeReset = (result.body && result.body.preserved) || null;
      resetDryOut.hidden = false;
      resetDryOut.textContent = formatJson(result.body);
      resetConfirmBlock.hidden = false;
      resetConfirm.value = "";
      btnResetReal.disabled = true;
      renderPreserved(preservedBeforeReset, false);
    } catch (err) {
      showBox(resetError, String(err && err.message ? err.message : err));
    } finally {
      btnResetDry.disabled = false;
    }
  }

  async function onResetReal() {
    if (resetConfirm.value.trim() !== "reset-collector-data") return;
    hideBox(resetError);
    btnResetReal.disabled = true;
    try {
      var result = await requestJson(
        "DELETE",
        "/v1/admin/collector-data?confirm=reset-collector-data&dry_run=false&force=true"
      );
      resetRealOut.hidden = false;
      resetRealOut.textContent = formatJson(result.body);
      if (!result.ok) {
        showBox(resetError, formatHttpError(result));
        return;
      }
      var after = (result.body && result.body.preserved) || {};
      var before = preservedBeforeReset || {};
      var changed =
        (before.sentinel_incident != null &&
          after.sentinel_incident != null &&
          before.sentinel_incident !== after.sentinel_incident) ||
        (before.sentinel_thread != null &&
          after.sentinel_thread != null &&
          before.sentinel_thread !== after.sentinel_thread);
      renderPreserved(after, changed);
      refreshDetail();
    } catch (err) {
      showBox(resetError, String(err && err.message ? err.message : err));
    } finally {
      btnResetReal.disabled =
        resetConfirm.value.trim() !== "reset-collector-data";
    }
  }

  /* ---------- boot ---------- */

  apiBaseInput.value = loadBase();
  apiBaseInput.addEventListener("change", function () {
    saveBase(normalizeBase(apiBaseInput.value));
  });
  btnTest.addEventListener("click", testConnection);

  idList.addEventListener("input", updateIdCount);
  document.querySelectorAll('input[name="key-type"]').forEach(function (el) {
    el.addEventListener("change", updateIdCount);
  });
  btnCollect.addEventListener("click", onCollect);
  btnSample.addEventListener("click", onLoadSample);
  btnInvalid.addEventListener("click", onInvalid);

  updatedFrom.value = DEFAULT_FROM;
  updatedTo.value = DEFAULT_TO;
  renderIssueCheckboxes();
  btnDiscover.addEventListener("click", onDiscover);
  btnLoadBridge.addEventListener("click", onLoadBridge);
  btnEnrich2.addEventListener("click", onEnrich2);

  btnResetDry.addEventListener("click", onResetDry);
  btnResetReal.addEventListener("click", onResetReal);
  resetConfirm.addEventListener("input", function () {
    btnResetReal.disabled =
      resetConfirm.value.trim() !== "reset-collector-data";
  });

  // Enter submits the active form region.
  idList.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      if (!btnCollect.disabled) onCollect();
    }
  });
  document.getElementById("stage-discover").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && ev.target && ev.target.tagName !== "TEXTAREA") {
      if (ev.target.tagName === "BUTTON") return;
      ev.preventDefault();
      onDiscover();
    }
  });
  enrichHowMany.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      onLoadBridge();
    }
  });
  resetConfirm.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !btnResetReal.disabled) {
      ev.preventDefault();
      onResetReal();
    }
  });

  wireDismiss(document);
  updateIdCount();
  startDetailPolling();
})();
