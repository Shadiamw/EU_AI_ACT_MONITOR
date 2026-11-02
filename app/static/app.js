(() => {
  const DEFAULTS = {
    viewer: "test-viewer-key-viewer-1-8c4d2e6f0a1b3957",
    reviewer: "test-reviewer-key-rev-1-5a7e3b9c1d2f4086",
  };

  // Same-origin when empty (Docker). Vercel sets window.__EU_MONITOR_API__ via config.js.
  const API_BASE = String(window.__EU_MONITOR_API__ || "").replace(/\/$/, "");

  const viewerInput = document.getElementById("viewer-key");
  const reviewerInput = document.getElementById("reviewer-key");
  const opsOut = document.getElementById("ops-out");
  const suiteOut = document.getElementById("suite-out");
  const suiteMeta = document.getElementById("suite-meta");
  const healthChip = document.getElementById("health-chip");
  const healthLabel = document.getElementById("health-label");
  const healthzLink = document.getElementById("healthz-link");
  const apiBaseLabel = document.getElementById("api-base-label");

  viewerInput.value = DEFAULTS.viewer;
  reviewerInput.value = DEFAULTS.reviewer;
  if (healthzLink) healthzLink.href = `${API_BASE}/healthz`;
  if (apiBaseLabel) {
    apiBaseLabel.textContent = API_BASE ? `API ${API_BASE}` : "API same origin";
  }

  let pollTimer = null;

  function pretty(value) {
    if (typeof value === "string") {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch {
        return value;
      }
    }
    return JSON.stringify(value, null, 2);
  }

  // API errors are shown with their error_code and message (spec 4.6).
  function describeError(result) {
    const data = result && result.data;
    if (data && typeof data === "object" && data.error_code) {
      return `HTTP ${result.status} ${data.error_code}: ${data.message}`;
    }
    return `HTTP ${result.status}${result.text ? ` ${result.text}` : ""}`;
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.auth === "viewer") {
      headers.Authorization = `Bearer ${viewerInput.value}`;
    }
    if (options.auth === "reviewer") {
      headers.Authorization = `Bearer ${reviewerInput.value}`;
    }
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
    const text = await res.text();
    let data = text;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      /* keep text */
    }
    return { ok: res.ok, status: res.status, data, text };
  }

  function setOps(msg) {
    opsOut.textContent = typeof msg === "string" ? msg : pretty(msg);
  }

  async function refreshHealth() {
    const { status, data } = await api("/healthz");
    let state = "bad";
    let label = `health ${status}`;
    if (status === 200) {
      state = "ok";
      label = "healthy";
    } else if (status === 503) {
      state = "stale";
      label = "sweeper stale";
    }
    healthChip.dataset.state = state;
    healthLabel.textContent = label;
    return { status, data };
  }

  const actions = {
    async health() {
      const result = await refreshHealth();
      setOps(result.data ?? result);
    },
    async reset() {
      const result = await api("/v1/test/reset", {
        method: "POST",
        auth: "reviewer",
      });
      setOps({
        status: result.status,
        body: result.status === 204 ? null : result.data,
      });
      await refreshHealth();
    },
    async advance() {
      const result = await api("/v1/test/clock/advance", {
        method: "POST",
        auth: "reviewer",
        body: JSON.stringify({ seconds: 900 }),
      });
      setOps({ status: result.status, body: result.data });
      await refreshHealth();
    },
    async verify() {
      const result = await api("/v1/ledger/verify", { auth: "viewer" });
      setOps({ status: result.status, body: result.data });
    },
    async events() {
      const result = await api("/v1/events", { auth: "viewer" });
      setOps({ status: result.status, body: result.data });
    },
    async export() {
      const result = await api("/v1/ledger/export", { auth: "viewer" });
      setOps({ status: result.status, body: result.data });
    },
  };

  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.getAttribute("data-action");
      btn.disabled = true;
      try {
        setOps(`Running ${name}…`);
        await actions[name]();
      } catch (err) {
        setOps(String(err));
      } finally {
        btn.disabled = false;
      }
    });
  });

  function renderSuite(run) {
    if (!run) {
      suiteMeta.textContent = "No run yet.";
      return;
    }
    const elapsed =
      run.finished_at != null
        ? `${(run.finished_at - run.started_at).toFixed(1)}s`
        : `${(Date.now() / 1000 - run.started_at).toFixed(1)}s so far`;
    suiteMeta.textContent = [
      `id ${run.id}`,
      `scope ${run.scope}`,
      `status ${run.status}`,
      run.exit_code != null ? `exit ${run.exit_code}` : null,
      elapsed,
      run.summary || null,
    ]
      .filter(Boolean)
      .join(" · ");
    suiteOut.textContent = run.output || "(no output yet)";
    suiteOut.scrollTop = suiteOut.scrollHeight;
  }

  async function fetchSuite() {
    const result = await api("/v1/test/suite", { auth: "reviewer" });
    if (result.status === 404) {
      suiteMeta.textContent = "Suite endpoint unavailable (ENVIRONMENT must be test).";
      return null;
    }
    if (result.status === 401 || result.status === 403) {
      suiteMeta.textContent = `Suite needs a reviewer key (${result.status}).`;
      return null;
    }
    const run = result.data && result.data.run;
    renderSuite(run);
    return run;
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(async () => {
      const run = await fetchSuite();
      if (!run || run.status === "passed" || run.status === "failed") {
        stopPolling();
        setSuiteButtons(false);
      }
    }, 1500);
  }

  function setSuiteButtons(busy) {
    document.querySelectorAll("[data-suite]").forEach((btn) => {
      const scope = btn.getAttribute("data-suite");
      if (scope === "refresh") {
        btn.disabled = false;
        return;
      }
      btn.disabled = busy;
    });
  }

  document.querySelectorAll("[data-suite]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const scope = btn.getAttribute("data-suite");
      if (scope === "refresh") {
        await fetchSuite();
        return;
      }
      setSuiteButtons(true);
      suiteOut.textContent = `Starting ${scope}…`;
      try {
        const result = await api("/v1/test/suite", {
          method: "POST",
          auth: "reviewer",
          body: JSON.stringify({ scope }),
        });
        if (!result.ok) {
          suiteMeta.textContent = `Could not start: ${describeError(result)}`;
          suiteOut.textContent = pretty(result.data ?? result.text);
          setSuiteButtons(false);
          return;
        }
        if (result.data && result.data.accepted === false) {
          suiteMeta.textContent = result.data.message || "suite already running";
          renderSuite(result.data.run);
          startPolling();
          return;
        }
        renderSuite(result.data.run);
        startPolling();
      } catch (err) {
        suiteOut.textContent = String(err);
        setSuiteButtons(false);
      }
    });
  });

  const HARM_FIELDS = [
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
  ];

  const harmRoot = document.getElementById("harm-fields");
  const riskEl = document.getElementById("risk-indicator");
  const reviewOut = document.getElementById("review-out");
  const reviewHistory = document.getElementById("review-history");
  const revisionsBody = document.querySelector("#revisions-table tbody");
  const eventIdInput = document.getElementById("event-id");
  const reviewAs = document.getElementById("review-as");
  let loadedEvent = null;
  let loadedNow = null;

  function reviewAuth() {
    return reviewAs.value === "viewer" ? "viewer" : "reviewer";
  }

  function setReview(msg) {
    reviewOut.textContent = typeof msg === "string" ? msg : pretty(msg);
  }

  function elapsedParts(fromIso, toMs) {
    const from = Date.parse(fromIso);
    if (Number.isNaN(from)) return { h: 0, m: 0 };
    const mins = Math.max(0, Math.floor((toMs - from) / 60000));
    return { h: Math.floor(mins / 60), m: mins % 60 };
  }

  // The simulated "now" for the risk indicator (spec 4.6; decision 142). The only contract
  // source of simulated time is a clock advance of 0 seconds (Addendum rule 7), and an advance
  // also runs gaps.sweep, escalations.fire_due and notifications.deliver_due once (spec 4.5).
  // So it is read only when the user loads an event, never on a timer or a poll. Outside the
  // test environment the endpoint is 404, and the browser clock is used instead.
  async function simulatedNow() {
    try {
      const result = await api("/v1/test/clock/advance", {
        method: "POST",
        auth: reviewAuth(),
        body: JSON.stringify({ seconds: 0 }),
      });
      if (result.ok && result.data && typeof result.data.simulated_time === "string") {
        const ms = Date.parse(result.data.simulated_time);
        if (!Number.isNaN(ms)) return { ms, source: "simulated clock" };
      }
    } catch {
      /* fall back to the browser clock */
    }
    return { ms: Date.now(), source: "browser clock" };
  }

  function renderRisk(event, now) {
    const disp = event.disposition;
    if (disp && disp.assigned_at) {
      const { h, m } = elapsedParts(event.detected_at, Date.parse(disp.assigned_at));
      riskEl.textContent =
        `Detected ${event.detected_at}, classified ${disp.assigned_at}: ` +
        `${h} h ${m} min elapsed before classification` +
        ` · deadline_utc ${event.deadline_utc ?? "null"}`;
    } else {
      const { h, m } = elapsedParts(event.detected_at, now.ms);
      riskEl.textContent =
        `Detected at ${event.detected_at}: ${h} h ${m} min elapsed; ` +
        "the legal window may already be running" +
        ` · deadline_utc ${event.deadline_utc ?? "null"}` +
        ` · now from the ${now.source}`;
    }
    riskEl.hidden = false;
  }

  // The 50/80/100 % instants with Part 3's formula (spec 3, "Window"): instant for p =
  // anchor + ceil((deadline - anchor) * p / 100) in whole microseconds, where the anchor is the
  // first assigned_at (disposition.assigned_at) and the deadline is deadline_utc. Display only:
  // the reminders themselves are planned and fired by the service.
  function escalationInstants(anchorIso, deadlineIso) {
    if (!anchorIso || !deadlineIso) return [];
    const anchor = Date.parse(anchorIso);
    const deadline = Date.parse(deadlineIso);
    if (Number.isNaN(anchor) || Number.isNaN(deadline) || deadline <= anchor) return [];
    const windowUs = (deadline - anchor) * 1000;
    return [50, 80, 100].map((pct) => {
      const due = new Date(anchor + Math.ceil((windowUs * pct) / 100) / 1000);
      return { pct, at: due.toISOString().replace(/\.\d{3}Z$/, "Z") };
    });
  }

  function renderRevisions(event) {
    revisionsBody.innerHTML = "";
    const revisions = event.revisions || [];
    if (!revisions.length) {
      const row = revisionsBody.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.textContent = "no revisions";
      return;
    }
    // The contract's revision keys: prior_tier, new_tier, reviewer_id, revised_at.
    for (const rev of revisions) {
      const row = revisionsBody.insertRow();
      for (const value of [rev.prior_tier, rev.new_tier, rev.reviewer_id, rev.revised_at]) {
        row.insertCell().textContent = value ?? "";
      }
    }
  }

  function renderHistory(event) {
    const disp = event.disposition;
    const lines = [
      `state ${event.state}`,
      `current tier ${disp ? disp.tier : "(none)"}`,
      `deadline_utc ${event.deadline_utc ?? "null"}`,
    ];
    const anchor = disp && disp.assigned_at;
    for (const row of escalationInstants(anchor, event.deadline_utc)) {
      lines.push(`reminder ${row.pct}% at ${row.at}`);
    }
    reviewHistory.textContent = lines.join("\n");
    renderRevisions(event);
  }

  function renderHarmFields(event) {
    const fields = event.harm_fields || {};
    harmRoot.innerHTML = "";
    for (const key of HARM_FIELDS) {
      const field = fields[key] || {};
      const evidence =
        field.evidence == null || field.evidence === ""
          ? "no evidence available"
          : String(field.evidence);
      const card = document.createElement("div");
      card.className = "harm-card";
      card.dataset.field = key;
      card.innerHTML = `
        <strong>${key}</strong>
        <div class="evidence"></div>
        <div class="choices">
          <label><input type="radio" name="status-${key}" value="CONFIRMED" /> Confirmed</label>
          <label><input type="radio" name="status-${key}" value="NOT_APPLICABLE" /> Not applicable</label>
        </div>
        <label class="stack">Evidence (optional)
          <textarea data-evidence rows="2" placeholder="optional"></textarea>
        </label>`;
      card.querySelector(".evidence").textContent = evidence;
      // No choice is made for the reviewer (decision 142): only a decision already stored is
      // shown; a PREFILLED_PENDING_CONFIRMATION field starts with neither option selected.
      if (field.status === "CONFIRMED" || field.status === "NOT_APPLICABLE") {
        card.querySelector(`input[value="${field.status}"]`).checked = true;
      }
      harmRoot.appendChild(card);
    }
  }

  function collectHarmFields() {
    const out = {};
    for (const card of harmRoot.querySelectorAll(".harm-card")) {
      const key = card.dataset.field;
      const chosen = card.querySelector(`input[name="status-${key}"]:checked`);
      // A field left unchosen is sent as still pending, so the service answers 409
      // HARM_FIELDS_UNCONFIRMED instead of the console confirming anything.
      const status = chosen ? chosen.value : "PREFILLED_PENDING_CONFIRMATION";
      const evidence = card.querySelector("[data-evidence]").value;
      out[key] = { status };
      if (evidence.trim()) out[key].evidence = evidence;
    }
    return out;
  }

  document.getElementById("load-event").addEventListener("click", async () => {
    const eventId = eventIdInput.value.trim();
    if (!eventId) {
      setReview("Enter an event id.");
      return;
    }
    setReview(`Loading ${eventId}…`);
    const result = await api(`/v1/events/${encodeURIComponent(eventId)}`, {
      auth: reviewAuth(),
    });
    if (!result.ok) {
      setReview(describeError(result));
      return;
    }
    loadedEvent = result.data;
    loadedNow = await simulatedNow();
    renderHarmFields(loadedEvent);
    renderRisk(loadedEvent, loadedNow);
    renderHistory(loadedEvent);
    setReview(loadedEvent);
  });

  document.getElementById("submit-disposition").addEventListener("click", async () => {
    const eventId = eventIdInput.value.trim();
    if (!eventId) {
      setReview("Enter an event id.");
      return;
    }
    const body = {
      tier: document.getElementById("disposition-tier").value,
      rationale: document.getElementById("disposition-rationale").value,
      harm_fields: collectHarmFields(),
    };
    setReview("Submitting disposition…");
    const result = await api(`/v1/events/${encodeURIComponent(eventId)}/disposition`, {
      method: "POST",
      auth: reviewAuth(),
      body: JSON.stringify(body),
    });
    if (!result.ok) {
      setReview(describeError(result));
      return;
    }
    setReview({ status: result.status, body: result.data });
    if (result.data) {
      loadedEvent = result.data;
      renderHarmFields(loadedEvent);
      // The time read at the last load: a submit never reads the clock again.
      renderRisk(loadedEvent, loadedNow || { ms: Date.parse(loadedEvent.detected_at), source: "event" });
      renderHistory(loadedEvent);
    }
  });

  refreshHealth().catch(() => {
    healthChip.dataset.state = "bad";
    healthLabel.textContent = "unreachable";
  });
  fetchSuite().catch(() => {});
})();
