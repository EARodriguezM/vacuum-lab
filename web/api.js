// Talking to the control server.
//
// The panel can be served by the lab PC itself or opened from anywhere and
// pointed at it, which is why the base address is configurable and stored in
// the browser: the lab machine's IP has moved before and will move again.
// The server sends permissive CORS headers so that cross-host case works.

const SETTINGS_KEY = "vacuum-lab-settings";

const DEFAULTS = {
  host: "",            // empty means "wherever this page came from"
  token: "",
  pollMs: 2000,
  theme: "auto",
};

export function loadSettings() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveSettings(settings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch { /* private browsing; the session still works, it just forgets */ }
  return settings;
}

/** Normalise "172.16.2.62", "172.16.2.62:8600" or a full URL into an origin. */
export function normaliseHost(host) {
  const text = String(host || "").trim().replace(/\/+$/, "");
  if (!text) return "";
  if (/^https?:\/\//i.test(text)) return text;
  return `http://${text.includes(":") ? text : `${text}:8600`}`;
}

let current = loadSettings();

export function settings() { return current; }

export function updateSettings(patch) {
  current = saveSettings({ ...current, ...patch });
  return current;
}

export function base() { return normaliseHost(current.host); }

export function url(path) {
  return `${base()}${path}`;
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (current.token) headers["X-Token"] = current.token;
  if (options.body) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(url(path), { ...options, headers });
  } catch (error) {
    // A network-level failure is the common case in this rig -- the server is
    // stopped, or the address is wrong -- so it gets a message that says so
    // rather than the browser's bare "Failed to fetch".
    throw new Error(
      `cannot reach the control server${base() ? ` at ${base()}` : ""}. ` +
      `Is webserver.py running, and is the address right?`
    );
  }
  if (response.status === 403) throw new Error("rejected: bad or missing token");

  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`unexpected reply (HTTP ${response.status})`);
  }
  if (!response.ok && payload.error) throw new Error(payload.error);
  return payload;
}

export const get = (path) => request(path);
export const post = (path, body) =>
  request(path, { method: "POST", body: JSON.stringify(body || {}) });

/** Download links must carry the token too, since they bypass fetch(). */
export function downloadUrl(path) {
  const separator = path.includes("?") ? "&" : "?";
  return current.token
    ? `${url(path)}${separator}token=${encodeURIComponent(current.token)}`
    : url(path);
}

// --- endpoints ------------------------------------------------------------

export const api = {
  ping:       () => get("/api/ping"),
  state:      () => get("/api/state"),
  live:       (limit = 600) => get(`/api/live?limit=${limit}`),
  procedures: () => get("/api/procedures"),
  stats:      () => get("/api/stats"),
  events:     (limit = 200) => get(`/api/events?limit=${limit}`),

  deviceState: (key) => get(`/api/device/${key}/state`),
  accessories: (key = "pump") => get(`/api/device/${key}/accessories`),
  gaugeDetails: (key = "gauge") => get(`/api/device/${key}/details`),

  command: (device, action, value, extra = {}) =>
    post("/api/command", { device, action, value, confirm: true, ...extra }),

  startRun: (body) => post("/api/run/start", body),
  stopRun:  (reason) => post("/api/run/stop", { reason }),
  acknowledgeStep: (note = "") => post("/api/run/acknowledge", { note }),

  ports: (kind = "all", includePhantom = false) =>
    get(`/api/ports?kind=${encodeURIComponent(kind)}${includePhantom ? "&all=1" : ""}`),
  actuators: () => get("/api/actuators"),
  setPort: (device, port, extra = {}) =>
    post("/api/device/port", { device, port, confirm: true, ...extra }),

  runs: (query = {}) => {
    const params = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== "" && v != null)
    );
    return get(`/api/runs${params.toString() ? `?${params}` : ""}`);
  },
  run:        (id) => get(`/api/runs/${id}`),
  runSamples: (id, max = 2000) => get(`/api/runs/${id}/samples?max=${max}`),
  runEvents:  (id) => get(`/api/runs/${id}/events`),
  updateRun:  (id, body) => post(`/api/runs/${id}/update`, body),
  deleteRun:  (id) => post(`/api/runs/${id}/delete`, { confirm: true }),
};
