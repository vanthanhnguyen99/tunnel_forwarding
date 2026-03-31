const state = {
  user: null,
  endpoints: [],
  sessions: [],
  overview: null,
  overallSeries: [],
  selectedEndpointDetail: null,
  selectedEndpointSeries: [],
  eventSource: null,
  poller: null,
  pendingSaveMode: "save",
  refreshTimeout: null,
};

const els = {
  loginView: document.querySelector("#login-view"),
  dashboardView: document.querySelector("#dashboard-view"),
  loginForm: document.querySelector("#login-form"),
  loginUsername: document.querySelector("#login-username"),
  loginPassword: document.querySelector("#login-password"),
  notice: document.querySelector("#notice"),
  refreshAllButton: document.querySelector("#refresh-all-button"),
  refreshTableButton: document.querySelector("#refresh-table-button"),
  newEndpointButton: document.querySelector("#new-endpoint-button"),
  logoutButton: document.querySelector("#logout-button"),
  userChip: document.querySelector("#user-chip"),
  searchInput: document.querySelector("#search-input"),
  statusFilter: document.querySelector("#status-filter"),
  endpointsTableBody: document.querySelector("#endpoints-table-body"),
  sessionsTableBody: document.querySelector("#sessions-table-body"),
  detailTitle: document.querySelector("#detail-title"),
  detailBody: document.querySelector("#detail-body"),
  statTotalEndpoints: document.querySelector("#stat-total-endpoints"),
  statActiveEndpoints: document.querySelector("#stat-active-endpoints"),
  statActiveSessions: document.querySelector("#stat-active-sessions"),
  statTotalTraffic: document.querySelector("#stat-total-traffic"),
  topEndpoints: document.querySelector("#top-endpoints"),
  connectionsChart: document.querySelector("#connections-chart"),
  trafficChart: document.querySelector("#traffic-chart"),
  connectionsChartLatest: document.querySelector("#connections-chart-latest"),
  trafficChartLatest: document.querySelector("#traffic-chart-latest"),
  modalBackdrop: document.querySelector("#modal-backdrop"),
  modalTitle: document.querySelector("#modal-title"),
  modalCloseButton: document.querySelector("#modal-close-button"),
  endpointForm: document.querySelector("#endpoint-form"),
  endpointId: document.querySelector("#endpoint-id"),
  endpointName: document.querySelector("#endpoint-name"),
  listenHost: document.querySelector("#listen-host"),
  listenPort: document.querySelector("#listen-port"),
  destinationHost: document.querySelector("#destination-host"),
  destinationPort: document.querySelector("#destination-port"),
  sshHost: document.querySelector("#ssh-host"),
  sshPort: document.querySelector("#ssh-port"),
  sshUsername: document.querySelector("#ssh-username"),
  sshPrivateKeyPath: document.querySelector("#ssh-private-key-path"),
  sshKnownHostsPath: document.querySelector("#ssh-known-hosts-path"),
  sshOptions: document.querySelector("#ssh-options"),
  allowedClientCidr: document.querySelector("#allowed-client-cidr"),
  maxClients: document.querySelector("#max-clients"),
  idleTimeout: document.querySelector("#idle-timeout"),
  tags: document.querySelector("#tags"),
  description: document.querySelector("#description"),
  enabled: document.querySelector("#enabled"),
  saveStartButton: document.querySelector("#save-start-button"),
  saveButton: document.querySelector("#save-button"),
  cancelButton: document.querySelector("#cancel-button"),
};

function formatBytes(value) {
  const amount = Number(value || 0);
  if (amount < 1024) return `${amount} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let current = amount / 1024;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  return `${current.toFixed(current >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function formatDateTime(iso) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function api(path, options = {}) {
  const config = {
    method: options.method || "GET",
    headers: { ...(options.headers || {}) },
  };
  if (options.body !== undefined) {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(options.body);
  }

  const response = await fetch(path, config);
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }

  if (!response.ok) {
    const err = new Error(payload.error || `Request failed with status ${response.status}`);
    err.status = response.status;
    throw err;
  }
  return payload;
}

function showNotice(message, tone = "info") {
  if (!message) {
    els.notice.classList.add("hidden");
    els.notice.textContent = "";
    els.notice.dataset.tone = "";
    return;
  }
  els.notice.textContent = message;
  els.notice.dataset.tone = tone;
  els.notice.classList.remove("hidden");
  window.clearTimeout(showNotice._timer);
  showNotice._timer = window.setTimeout(() => {
    els.notice.classList.add("hidden");
  }, 3500);
}

function setAuthenticatedView(isAuthenticated) {
  els.loginView.classList.toggle("hidden", isAuthenticated);
  els.dashboardView.classList.toggle("hidden", !isAuthenticated);
  els.logoutButton.classList.toggle("hidden", !isAuthenticated);
  els.refreshAllButton.classList.toggle("hidden", !isAuthenticated);
  els.userChip.classList.toggle("hidden", !isAuthenticated);
  els.userChip.textContent = state.user ? state.user.username : "";
}

async function checkSession() {
  try {
    const payload = await api("/api/me");
    state.user = payload.user;
    setAuthenticatedView(true);
    await bootstrapDashboard();
  } catch (error) {
    state.user = null;
    teardownEventStream();
    stopPoller();
    setAuthenticatedView(false);
  }
}

async function bootstrapDashboard() {
  await Promise.all([loadOverview(), loadEndpoints(), loadSessions(), loadOverallSeries()]);
  if (state.selectedEndpointDetail) {
    await loadEndpointDetail(state.selectedEndpointDetail.endpoint.id);
  }
  renderAll();
  connectEventStream();
  startPoller();
}

async function loadOverview() {
  const payload = await api("/api/metrics/overview");
  state.overview = payload.item;
}

async function loadEndpoints() {
  const payload = await api("/api/endpoints");
  state.endpoints = payload.items || [];
  if (state.selectedEndpointDetail) {
    const stillExists = state.endpoints.some((item) => item.id === state.selectedEndpointDetail.endpoint.id);
    if (!stillExists) {
      state.selectedEndpointDetail = null;
      state.selectedEndpointSeries = [];
    }
  }
}

async function loadSessions() {
  const payload = await api("/api/sessions");
  state.sessions = payload.items || [];
}

async function loadOverallSeries() {
  const payload = await api("/api/metrics/timeseries?window=300");
  state.overallSeries = payload.items || [];
}

async function loadEndpointDetail(endpointId) {
  const payload = await api(`/api/endpoints/${endpointId}/metrics`);
  state.selectedEndpointDetail = payload.item;
  state.selectedEndpointSeries = payload.item.timeseries || [];
  renderDetail();
}

function startPoller() {
  stopPoller();
  state.poller = window.setInterval(async () => {
    try {
      await Promise.all([loadEndpoints(), loadSessions()]);
      if (state.selectedEndpointDetail) {
        await loadEndpointDetail(state.selectedEndpointDetail.endpoint.id);
      }
      renderAll();
    } catch (error) {
      handleRequestError(error);
    }
  }, 8000);
}

function stopPoller() {
  if (state.poller) {
    window.clearInterval(state.poller);
    state.poller = null;
  }
}

function scheduleRefresh(delay = 300) {
  window.clearTimeout(state.refreshTimeout);
  state.refreshTimeout = window.setTimeout(async () => {
    try {
      await Promise.all([loadEndpoints(), loadSessions(), loadOverview()]);
      if (state.selectedEndpointDetail) {
        await loadEndpointDetail(state.selectedEndpointDetail.endpoint.id);
      }
      renderAll();
    } catch (error) {
      handleRequestError(error);
    }
  }, delay);
}

function connectEventStream() {
  teardownEventStream();
  state.eventSource = new EventSource("/api/events");

  state.eventSource.addEventListener("metrics.tick", (event) => {
    const payload = JSON.parse(event.data);
    state.overview = payload.overview;
    state.overallSeries = [...state.overallSeries, payload.overall_point].slice(-300);
    if (state.selectedEndpointDetail) {
      const point = (payload.endpoint_points || []).find(
        (item) => item.endpoint_id === state.selectedEndpointDetail.endpoint.id,
      );
      if (point) {
        state.selectedEndpointSeries = [...state.selectedEndpointSeries, point].slice(-300);
      }
    }
    renderOverview();
    renderCharts();
    renderDetailChartsOnly();
  });

  ["endpoint.created", "endpoint.updated", "endpoint.started", "endpoint.stopped", "endpoint.deleted", "session.opened", "session.closed"].forEach(
    (eventName) => {
      state.eventSource.addEventListener(eventName, () => scheduleRefresh(200));
    },
  );

  state.eventSource.onerror = () => {
    if (state.eventSource?.readyState === EventSource.CLOSED) {
      teardownEventStream();
      window.setTimeout(() => {
        if (state.user) connectEventStream();
      }, 1500);
    }
  };
}

function teardownEventStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function handleRequestError(error) {
  if (error?.status === 401) {
    showNotice("Session expired. Please sign in again.", "warning");
    checkSession();
    return;
  }
  showNotice(error.message || "Request failed", "error");
}

function getFilteredEndpoints() {
  const search = els.searchInput.value.trim().toLowerCase();
  const status = els.statusFilter.value;
  return state.endpoints.filter((endpoint) => {
    const matchesSearch =
      !search ||
      endpoint.name.toLowerCase().includes(search) ||
      endpoint.listen.toLowerCase().includes(search) ||
      endpoint.forward_to.toLowerCase().includes(search) ||
      (endpoint.ssh_target || "").toLowerCase().includes(search) ||
      (endpoint.docker_nat_ip || "").toLowerCase().includes(search) ||
      (endpoint.docker_container_name || "").toLowerCase().includes(search);
    const matchesStatus = status === "all" || endpoint.runtime_status === status;
    return matchesSearch && matchesStatus;
  });
}

function renderAll() {
  renderOverview();
  renderCharts();
  renderEndpoints();
  renderSessions();
  renderDetail();
}

function renderOverview() {
  const overview = state.overview || {
    total_endpoints: 0,
    active_endpoints: 0,
    total_active_sessions: 0,
    total_traffic_up: 0,
    total_traffic_down: 0,
    top_endpoints: [],
  };
  els.statTotalEndpoints.textContent = overview.total_endpoints;
  els.statActiveEndpoints.textContent = overview.active_endpoints;
  els.statActiveSessions.textContent = overview.total_active_sessions;
  els.statTotalTraffic.textContent = formatBytes(overview.total_traffic_up + overview.total_traffic_down);

  const topEndpoints = overview.top_endpoints || [];
  if (!topEndpoints.length) {
    els.topEndpoints.innerHTML = `<div class="empty-state">No runtime activity yet.</div>`;
    return;
  }

  const maxTraffic = Math.max(...topEndpoints.map((item) => Number(item.traffic_total) || 0), 1);
  els.topEndpoints.innerHTML = topEndpoints
    .map((item) => {
      const width = Math.max(10, Math.round(((Number(item.traffic_total) || 0) / maxTraffic) * 100));
      return `
        <div class="top-endpoint-row">
          <div class="top-endpoint-meta">
            <strong>${escapeHtml(item.name)}</strong>
            <span>${item.active_clients} clients · ${formatBytes(item.traffic_total)}</span>
          </div>
          <div class="meter">
            <div class="meter-fill" style="width:${width}%"></div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderEndpoints() {
  const endpoints = getFilteredEndpoints();
  if (!endpoints.length) {
    els.endpointsTableBody.innerHTML = `<tr><td colspan="10" class="empty-cell">No endpoints match the current filter.</td></tr>`;
    return;
  }

  els.endpointsTableBody.innerHTML = endpoints
    .map((endpoint) => {
      const canStart = endpoint.runtime_status !== "running";
      return `
        <tr>
          <td>
            <div class="row-title">${escapeHtml(endpoint.name)}</div>
            <div class="row-subtitle">${escapeHtml(endpoint.description || endpoint.transport || "No description")}</div>
          </td>
          <td>SSH Local</td>
          <td><code>${escapeHtml(endpoint.listen)}</code></td>
          <td><code>${escapeHtml(endpoint.ssh_target || "unconfigured")}</code></td>
          <td><code>${escapeHtml(endpoint.forward_to)}</code></td>
          <td>
            <code>${escapeHtml(endpoint.docker_nat_ip || "unassigned")}</code>
            <div class="row-subtitle">${escapeHtml(endpoint.docker_container_name || "container pending")}</div>
          </td>
          <td>
            <span class="status-badge status-${endpoint.runtime_status}">
              ${escapeHtml(endpoint.runtime_status)}
            </span>
            ${
              endpoint.status_message
                ? `<div class="row-subtitle danger-text">${escapeHtml(endpoint.status_message)}</div>`
                : ""
            }
          </td>
          <td>${endpoint.active_clients}</td>
          <td>${formatBytes(endpoint.traffic_total)}</td>
          <td>
            <div class="action-group">
              <button class="table-button" data-action="${canStart ? "start" : "stop"}" data-id="${endpoint.id}">
                ${canStart ? "Start" : "Stop"}
              </button>
              <button class="table-button" data-action="edit" data-id="${endpoint.id}">Edit</button>
              <button class="table-button" data-action="view" data-id="${endpoint.id}">View Sessions</button>
              <button class="table-button danger" data-action="delete" data-id="${endpoint.id}">Delete</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderSessions() {
  if (!state.sessions.length) {
    els.sessionsTableBody.innerHTML = `<tr><td colspan="8" class="empty-cell">No active sessions.</td></tr>`;
    return;
  }

  els.sessionsTableBody.innerHTML = state.sessions
    .map(
      (session) => `
        <tr>
          <td>#${session.id}</td>
          <td>${escapeHtml(session.endpoint_name)}</td>
          <td><code>${escapeHtml(`${session.client_ip}:${session.client_port}`)}</code></td>
          <td>
            <code>${escapeHtml(`${session.upstream_ip}:${session.upstream_port}`)}</code>
            <div class="row-subtitle">${escapeHtml(session.ssh_target || "")}</div>
          </td>
          <td>${formatDateTime(session.connected_at)}</td>
          <td>${formatBytes(session.bytes_up)}</td>
          <td>${formatBytes(session.bytes_down)}</td>
          <td>
            <button class="table-button danger" data-session-action="disconnect" data-id="${session.id}">
              Disconnect
            </button>
          </td>
        </tr>
      `,
    )
    .join("");
}

function renderDetail() {
  if (!state.selectedEndpointDetail) {
    els.detailTitle.textContent = "Select an endpoint";
    els.detailBody.className = "detail-body empty-state";
  els.detailBody.innerHTML =
      'Choose <strong>View Sessions</strong> on any endpoint to inspect its SSH route, current sessions and dedicated traffic chart.';
    return;
  }

  const { endpoint, sessions } = state.selectedEndpointDetail;
  els.detailTitle.textContent = endpoint.name;
  els.detailBody.className = "detail-body";
  els.detailBody.innerHTML = `
      <div class="detail-grid">
      <div class="detail-item">
        <span>Container Listen</span>
        <strong>${escapeHtml(endpoint.listen)}</strong>
      </div>
      <div class="detail-item">
        <span>Container Bind</span>
        <strong>${escapeHtml(endpoint.container_bind || "pending")}</strong>
      </div>
      <div class="detail-item">
        <span>Forward To</span>
        <strong>${escapeHtml(endpoint.forward_to)}</strong>
      </div>
      <div class="detail-item">
        <span>SSH Server</span>
        <strong>${escapeHtml(endpoint.ssh_target || "unconfigured")}</strong>
      </div>
      <div class="detail-item">
        <span>Status</span>
        <strong>${escapeHtml(endpoint.runtime_status)}</strong>
      </div>
      <div class="detail-item">
        <span>Docker NAT IP</span>
        <strong><code>${escapeHtml(endpoint.docker_nat_ip || "unassigned")}</code></strong>
      </div>
      <div class="detail-item">
        <span>Docker Network</span>
        <strong>${escapeHtml(endpoint.docker_network_name || "tunnel_nat")}</strong>
      </div>
      <div class="detail-item">
        <span>Container Name</span>
        <strong>${escapeHtml(endpoint.docker_container_name || "pending")}</strong>
      </div>
      <div class="detail-item">
        <span>Allowed CIDR</span>
        <strong>${escapeHtml(endpoint.allowed_client_cidr || "Any")}</strong>
      </div>
      <div class="detail-item">
        <span>Max Clients</span>
        <strong>${endpoint.max_clients || "Unlimited"}</strong>
      </div>
      <div class="detail-item">
        <span>Idle Timeout</span>
        <strong>${endpoint.idle_timeout ? `${endpoint.idle_timeout}s` : "Disabled"}</strong>
      </div>
      <div class="detail-item">
        <span>Private Key</span>
        <strong>${escapeHtml(endpoint.ssh_private_key_path || "ssh-agent / default key")}</strong>
      </div>
      <div class="detail-item">
        <span>Extra SSH Options</span>
        <strong>${escapeHtml(endpoint.ssh_options || "None")}</strong>
      </div>
      <div class="detail-item">
        <span>Docker Compose</span>
        <strong><code>${escapeHtml(endpoint.docker_compose_path || "Not generated")}</code></strong>
      </div>
      <div class="detail-item">
        <span>Runtime Config</span>
        <strong><code>${escapeHtml(endpoint.docker_endpoint_config_path || "Not generated")}</code></strong>
      </div>
    </div>
    <div class="detail-description">${escapeHtml(endpoint.description || "No description provided.")}</div>
    <div class="detail-chart card-subpanel">
      <div class="panel-heading">
        <div>
          <span class="section-kicker">Endpoint Metrics</span>
          <h4>Dedicated Traffic</h4>
        </div>
      </div>
      <svg id="detail-chart" class="chart-svg" viewBox="0 0 600 220" preserveAspectRatio="none"></svg>
    </div>
    <div class="detail-session-list card-subpanel">
      <div class="panel-heading">
        <div>
          <span class="section-kicker">Current Clients</span>
          <h4>${sessions.length} active session${sessions.length === 1 ? "" : "s"}</h4>
        </div>
      </div>
      ${
        sessions.length
          ? sessions
              .map(
                (session) => `
                <div class="detail-session-row">
                  <div>
                    <strong>${escapeHtml(`${session.client_ip}:${session.client_port}`)}</strong>
                    <span>${formatDateTime(session.connected_at)}</span>
                  </div>
                  <div class="detail-session-traffic">
                    <span>Up ${formatBytes(session.bytes_up)}</span>
                    <span>Down ${formatBytes(session.bytes_down)}</span>
                  </div>
                </div>
              `,
              )
              .join("")
          : `<div class="empty-state">No active clients on this endpoint.</div>`
      }
    </div>
  `;
  renderDetailChartsOnly();
}

function renderDetailChartsOnly() {
  const detailChart = document.querySelector("#detail-chart");
  if (!detailChart) return;
  renderMultiLineChart(detailChart, state.selectedEndpointSeries, [
    { key: "bytes_up_per_sec", color: "#ffb74d", label: "Bytes up/s" },
    { key: "bytes_down_per_sec", color: "#6ec2ff", label: "Bytes down/s" },
  ]);
}

function renderCharts() {
  renderLineChart(els.connectionsChart, state.overallSeries, "active_connections", "#75f2c6", (value) =>
    String(Math.round(Number(value || 0))),
  );
  renderMultiLineChart(els.trafficChart, state.overallSeries, [
    { key: "bytes_up_per_sec", color: "#ffb74d", label: "Bytes up/s" },
    { key: "bytes_down_per_sec", color: "#6ec2ff", label: "Bytes down/s" },
  ], formatBytes);
  const latest = state.overallSeries.at(-1) || { active_connections: 0, bytes_up_per_sec: 0, bytes_down_per_sec: 0 };
  els.connectionsChartLatest.textContent = latest.active_connections ?? 0;
  els.trafficChartLatest.textContent = `${formatBytes(latest.bytes_up_per_sec || 0)} / ${formatBytes(latest.bytes_down_per_sec || 0)}`;
}

function renderLineChart(svg, points, valueKey, color, formatter = formatBytes) {
  renderMultiLineChart(svg, points, [{ key: valueKey, color, label: valueKey }], formatter);
}

function renderMultiLineChart(svg, points, series, valueFormatter = formatBytes) {
  const width = 600;
  const height = 220;
  const padding = 28;
  if (!points?.length) {
    svg.innerHTML = `<text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" class="chart-empty">No data yet</text>`;
    return;
  }

  const maxValue = Math.max(
    1,
    ...points.flatMap((point) => series.map((item) => Number(point[item.key]) || 0)),
  );
  const minTs = Number(points[0].ts);
  const maxTs = Number(points.at(-1).ts);
  const xRange = Math.max(1, maxTs - minTs);
  const toX = (ts) => padding + ((Number(ts) - minTs) / xRange) * (width - padding * 2);
  const toY = (value) => height - padding - ((Number(value) || 0) / maxValue) * (height - padding * 2);

  const gridLines = [0.25, 0.5, 0.75, 1].map((ratio) => {
    const y = height - padding - (height - padding * 2) * ratio;
    return `<line x1="${padding}" y1="${y}" x2="${width - padding}" y2="${y}" class="chart-grid" />`;
  });

  const seriesLines = series
    .map((item) => {
      const polyline = points
        .map((point) => `${toX(point.ts).toFixed(2)},${toY(point[item.key]).toFixed(2)}`)
        .join(" ");
      return `<polyline class="chart-line" style="--line-color:${item.color}" points="${polyline}" />`;
    })
    .join("");

  const startLabel = formatTime(points[0].ts);
  const endLabel = formatTime(points.at(-1).ts);

  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" rx="16" class="chart-bg"></rect>
    ${gridLines.join("")}
    ${seriesLines}
    <text x="${padding}" y="${height - 8}" class="chart-label">${escapeHtml(startLabel)}</text>
    <text x="${width - padding}" y="${height - 8}" text-anchor="end" class="chart-label">${escapeHtml(endLabel)}</text>
    <text x="${padding}" y="18" class="chart-label">${escapeHtml(valueFormatter(maxValue))}</text>
  `;
}

function formatTime(ts) {
  const date = new Date(Number(ts) * 1000);
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function openModal(endpoint = null) {
  state.pendingSaveMode = "save";
  els.endpointForm.reset();
  els.endpointId.value = endpoint?.id ?? "";
  els.modalTitle.textContent = endpoint ? `Edit ${endpoint.name}` : "New Tunnel";
  els.endpointName.value = endpoint?.name ?? "";
  els.listenHost.value = endpoint?.listen_host ?? "0.0.0.0";
  els.listenPort.value = endpoint?.listen_port ?? "";
  els.destinationHost.value = endpoint?.destination_host ?? "";
  els.destinationPort.value = endpoint?.destination_port ?? "";
  els.sshHost.value = endpoint?.ssh_host ?? "";
  els.sshPort.value = endpoint?.ssh_port ?? 22;
  els.sshUsername.value = endpoint?.ssh_username ?? "";
  els.sshPrivateKeyPath.value = endpoint?.ssh_private_key_path ?? "";
  els.sshKnownHostsPath.value = endpoint?.ssh_known_hosts_path ?? "";
  els.sshOptions.value = endpoint?.ssh_options ?? "";
  els.allowedClientCidr.value = endpoint?.allowed_client_cidr ?? "";
  els.maxClients.value = endpoint?.max_clients ?? 0;
  els.idleTimeout.value = endpoint?.idle_timeout ?? 0;
  els.tags.value = endpoint?.tags ?? "";
  els.description.value = endpoint?.description ?? "";
  els.enabled.checked = Boolean(endpoint?.enabled);
  els.modalBackdrop.classList.remove("hidden");
}

function closeModal() {
  els.modalBackdrop.classList.add("hidden");
}

function validatePortInput(input, label) {
  const rawValue = input.value.trim();
  let message = "";

  if (!rawValue) {
    message = `${label} is required.`;
  } else {
    const value = Number(rawValue);
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      message = `${label} must be an integer between 1 and 65535.`;
    }
  }

  input.setCustomValidity(message);
  return message === "";
}

function validateEndpointForm() {
  const isListenPortValid = validatePortInput(els.listenPort, "Listen Port");
  const isDestinationPortValid = validatePortInput(els.destinationPort, "Destination Port");
  const isFormValid = isListenPortValid && isDestinationPortValid;
  if (!isFormValid) {
    els.endpointForm.reportValidity();
  }
  return isFormValid;
}

function collectFormPayload() {
  return {
    name: els.endpointName.value.trim(),
    listen_host: els.listenHost.value.trim(),
    listen_port: Number(els.listenPort.value),
    destination_host: els.destinationHost.value.trim(),
    destination_port: Number(els.destinationPort.value),
    ssh_host: els.sshHost.value.trim(),
    ssh_port: Number(els.sshPort.value || 22),
    ssh_username: els.sshUsername.value.trim(),
    ssh_private_key_path: els.sshPrivateKeyPath.value.trim(),
    ssh_known_hosts_path: els.sshKnownHostsPath.value.trim(),
    ssh_options: els.sshOptions.value.trim(),
    allowed_client_cidr: els.allowedClientCidr.value.trim(),
    max_clients: Number(els.maxClients.value || 0),
    idle_timeout: Number(els.idleTimeout.value || 0),
    tags: els.tags.value.trim(),
    description: els.description.value.trim(),
    enabled: state.pendingSaveMode === "save-start" ? true : els.enabled.checked,
    tunnel_type: "ssh_local_forward",
  };
}

async function submitEndpointForm(event) {
  event.preventDefault();
  if (!validateEndpointForm()) {
    return;
  }
  const endpointId = els.endpointId.value;
  const payload = collectFormPayload();
  try {
    if (endpointId) {
      await api(`/api/endpoints/${endpointId}`, { method: "PUT", body: payload });
      showNotice("Endpoint updated.", "success");
    } else {
      await api("/api/endpoints", { method: "POST", body: payload });
      showNotice("Endpoint created.", "success");
    }
    closeModal();
    await Promise.all([loadEndpoints(), loadSessions(), loadOverview(), loadOverallSeries()]);
    renderAll();
  } catch (error) {
    handleRequestError(error);
  }
}

async function handleEndpointAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const endpointId = Number(button.dataset.id);
  const action = button.dataset.action;
  const endpoint = state.endpoints.find((item) => item.id === endpointId);
  if (!endpoint) return;

  try {
    if (action === "start") {
      await api(`/api/endpoints/${endpointId}/start`, { method: "POST" });
      showNotice(`Started ${endpoint.name}.`, "success");
    } else if (action === "stop") {
      await api(`/api/endpoints/${endpointId}/stop`, { method: "POST" });
      showNotice(`Stopped ${endpoint.name}.`, "warning");
    } else if (action === "edit") {
      openModal(endpoint);
      return;
    } else if (action === "delete") {
      if (!window.confirm(`Delete endpoint "${endpoint.name}"?`)) return;
      await api(`/api/endpoints/${endpointId}`, { method: "DELETE" });
      showNotice(`Deleted ${endpoint.name}.`, "success");
    } else if (action === "view") {
      await loadEndpointDetail(endpointId);
      renderDetail();
      return;
    }

    await Promise.all([loadEndpoints(), loadSessions(), loadOverview(), loadOverallSeries()]);
    if (state.selectedEndpointDetail?.endpoint.id === endpointId) {
      await loadEndpointDetail(endpointId);
    }
    renderAll();
  } catch (error) {
    handleRequestError(error);
  }
}

async function handleSessionAction(event) {
  const button = event.target.closest("[data-session-action]");
  if (!button) return;
  const sessionId = Number(button.dataset.id);
  const action = button.dataset.sessionAction;
  if (action !== "disconnect") return;
  try {
    await api(`/api/sessions/${sessionId}/disconnect`, { method: "POST" });
    showNotice(`Disconnected session #${sessionId}.`, "success");
    await Promise.all([loadSessions(), loadEndpoints(), loadOverview()]);
    if (state.selectedEndpointDetail) {
      await loadEndpointDetail(state.selectedEndpointDetail.endpoint.id);
    }
    renderAll();
  } catch (error) {
    handleRequestError(error);
  }
}

async function handleLoginSubmit(event) {
  event.preventDefault();
  try {
    const username = els.loginUsername.value.trim();
    const password = els.loginPassword.value;
    await api("/api/login", { method: "POST", body: { username, password } });
    showNotice("Signed in.", "success");
    els.loginPassword.value = "";
    await checkSession();
  } catch (error) {
    handleRequestError(error);
  }
}

async function handleLogout() {
  try {
    await api("/api/logout", { method: "POST" });
  } catch (error) {
    handleRequestError(error);
  }
  state.user = null;
  state.endpoints = [];
  state.sessions = [];
  state.overview = null;
  state.overallSeries = [];
  state.selectedEndpointDetail = null;
  state.selectedEndpointSeries = [];
  teardownEventStream();
  stopPoller();
  setAuthenticatedView(false);
}

function bindEvents() {
  els.loginForm.addEventListener("submit", handleLoginSubmit);
  els.logoutButton.addEventListener("click", handleLogout);
  els.refreshAllButton.addEventListener("click", async () => {
    try {
      await bootstrapDashboard();
      showNotice("Dashboard refreshed.", "success");
    } catch (error) {
      handleRequestError(error);
    }
  });
  els.refreshTableButton.addEventListener("click", () => scheduleRefresh(0));
  els.newEndpointButton.addEventListener("click", () => openModal());
  els.modalCloseButton.addEventListener("click", closeModal);
  els.cancelButton.addEventListener("click", closeModal);
  els.saveStartButton.addEventListener("click", () => {
    state.pendingSaveMode = "save-start";
    els.endpointForm.requestSubmit();
  });
  els.saveButton.addEventListener("click", () => {
    state.pendingSaveMode = "save";
  });
  els.endpointForm.addEventListener("submit", submitEndpointForm);
  els.listenPort.addEventListener("input", () => validatePortInput(els.listenPort, "Listen Port"));
  els.destinationPort.addEventListener("input", () => validatePortInput(els.destinationPort, "Destination Port"));
  els.searchInput.addEventListener("input", renderEndpoints);
  els.statusFilter.addEventListener("change", renderEndpoints);
  els.endpointsTableBody.addEventListener("click", handleEndpointAction);
  els.sessionsTableBody.addEventListener("click", handleSessionAction);
  els.modalBackdrop.addEventListener("click", (event) => {
    if (event.target === els.modalBackdrop) closeModal();
  });
}

bindEvents();
checkSession();
