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
          suiteMeta.textContent = `Could not start (${result.status})`;
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
  const eventIdInput = document.getElementById("event-id");
  const reviewAs = document.getElementById("review-as");
  let loadedEvent = null;

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

  function renderRisk(event) {
    const now = Date.now();
    const disp = event.disposition;
    if (disp && disp.assigned_at) {
      const { h, m } = elapsedParts(event.detected_at, Date.parse(disp.assigned_at));
      riskEl.textContent =
        `Detected ${event.detected_at}, classified ${disp.assigned_at}: ` +
        `${h} h ${m} min elapsed before classification` +
        (event.deadline_utc ? ` · deadline_utc ${event.deadline_utc}` : " · no deadline");
    } else {
      const { h, m } = elapsedParts(event.detected_at, now);
      riskEl.textContent =
        `Detected at ${event.detected_at}: ${h} h ${m} min elapsed; ` +
        "the legal window may already be running" +
        (event.deadline_utc ? ` · deadline_utc ${event.deadline_utc}` : "");
    }
    riskEl.hidden = false;
  }

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

  function renderHistory(event) {
    const disp = event.disposition;
    const lines = [
      `state ${event.state}`,
      `tier ${disp ? disp.tier : "(none)"}`,
      `deadline_utc ${event.deadline_utc ?? "null"}`,
      `revisions ${(event.revisions || []).length}`,
    ];
    for (const rev of event.revisions || []) {
      lines.push(
        `  - ${rev.revised_at || rev.assigned_at || "?"} ${rev.prior_tier || "?"} -> ${rev.tier}`
      );
    }
    const anchor = disp && disp.assigned_at;
    for (const row of escalationInstants(anchor, event.deadline_utc)) {
      lines.push(`reminder ${row.pct}% at ${row.at}`);
    }
    reviewHistory.textContent = lines.join("\n");
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
          <label><input type="radio" name="status-${key}" value="CONFIRMED" checked /> Confirmed</label>
          <label><input type="radio" name="status-${key}" value="NOT_APPLICABLE" /> Not applicable</label>
        </div>
        <label class="stack">Evidence override
          <textarea data-evidence rows="2" placeholder="optional"></textarea>
        </label>`;
      card.querySelector(".evidence").textContent = evidence;
      if (field.status === "NOT_APPLICABLE") {
        card.querySelector(`input[value="NOT_APPLICABLE"]`).checked = true;
      }
      harmRoot.appendChild(card);
    }
  }

  function collectHarmFields() {
    const out = {};
    for (const card of harmRoot.querySelectorAll(".harm-card")) {
      const key = card.dataset.field;
      const status = card.querySelector(`input[name="status-${key}"]:checked`).value;
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
      setReview({ status: result.status, body: result.data });
      return;
    }
    loadedEvent = result.data;
    renderHarmFields(loadedEvent);
    renderRisk(loadedEvent);
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
    setReview({ status: result.status, body: result.data });
    if (result.ok && result.data) {
      loadedEvent = result.data;
      renderHarmFields(loadedEvent);
      renderRisk(loadedEvent);
      renderHistory(loadedEvent);
    }
  });

  refreshHealth().catch(() => {
    healthChip.dataset.state = "bad";
    healthLabel.textContent = "unreachable";
  });
  fetchSuite().catch(() => {});
})();
