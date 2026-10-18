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
      const result = await api("/v1/test/reset", { method: "POST" });
      setOps({ status: result.status, body: result.data });
      await refreshHealth();
    },
    async advance() {
      const result = await api("/v1/test/clock/advance", {
        method: "POST",
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
    const result = await api("/v1/test/suite");
    if (result.status === 404) {
      suiteMeta.textContent = "Suite endpoint unavailable (ENVIRONMENT must be test).";
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

  refreshHealth().catch(() => {
    healthChip.dataset.state = "bad";
    healthLabel.textContent = "unreachable";
  });
  fetchSuite().catch(() => {});
})();
