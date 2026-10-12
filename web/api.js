// Talking to the control server.
//
// The panel can be served by the lab PC itself or opened from anywhere and
// pointed at it, which is why the base address is configurable and stored in
// the browser: the lab machine's IP has moved before and will move again.
// The server sends permissive CORS headers so that cross-host case works.
//
// Every call carries a session (lab/auth.py) in the Authorization header --
// never in a cookie, which a browser would attach to other sites' requests,
// and never in a URL, which ends up in logs and history. A browser holds up
// to two: a person's, after signing in, and a paired tablet's, which only
// watches and is what the panel falls back to when the person signs out.

const SETTINGS_KEY = "vacuum-lab-settings";
const SESSION_KEY = "vacuum-lab-session";

const DEFAULTS = {
  host: "",            // empty means "wherever this page came from"
  pollMs: 2000,
  theme: "auto",
  alarmSound: true,
};

export function loadSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    delete stored.token;           // the shared token is gone; accounts replaced it
    return { ...DEFAULTS, ...stored };
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

// --- sessions ---------------------------------------------------------------

function loadSessions() {
  try {
    const stored = JSON.parse(localStorage.getItem(SESSION_KEY) || "{}");
    return { person: stored.person || null, device: stored.device || null };
  } catch {
    return { person: null, device: null };
  }
}

let sessions = loadSessions();
const listeners = new Set();

function storeSessions(next, reason = "") {
  sessions = { ...sessions, ...next };
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(sessions));
  } catch { /* kept in memory for this page only */ }
  listeners.forEach((listener) => listener(sessions, reason));
}

/** The sessions this browser holds: { person, device }. */
export function sessionTokens() { return sessions; }

/**
 * Called whenever a session is gained or lost, with the sessions and why:
 * "ended" when the server refused one that was in use (expired, signed out
 * when idle on a tablet, reset elsewhere). Returns the unsubscribe.
 */
export function onSessionChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setPersonSession(token) { storeSessions({ person: token || null }); }
export function setDeviceSession(token) { storeSessions({ device: token || null }); }

/** Why a request was refused, when the answer is "sign in", not an error. */
export class AuthError extends Error {
  constructor(message, payload = {}) {
    super(message);
    this.reauth = !!payload.reauth;
    this.login = !!payload.login;
    this.mustChange = !!payload.must_change;
  }
}

const problems = new Set();

/**
 * Called with an AuthError whenever the server asks for the password again
 * or for a new one. The panel opens the matching dialog wherever the action
 * came from, instead of every button handling it.
 */
export function onAuthProblem(listener) {
  problems.add(listener);
  return () => problems.delete(listener);
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = sessions.person || sessions.device;
  if (token) headers.Authorization = `Bearer ${token}`;
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
  if (options.raw && response.ok) return response;

  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`unexpected reply (HTTP ${response.status})`);
  }
  if (response.status === 401 && payload.login) {
    // The session is gone -- expired, signed out elsewhere, password reset.
    // Drop whichever one was used; a tablet falls back to watching.
    if (sessions.person && token === sessions.person) storeSessions({ person: null }, "ended");
    else if (token && token === sessions.device) setDeviceSession(null);
    throw new AuthError(payload.error || "sign in", payload);
  }
  if (payload.reauth || payload.must_change) {
    const problem = new AuthError(payload.error || "sign in again", payload);
    problems.forEach((listener) => listener(problem));
    throw problem;
  }
  if (!response.ok && payload.error) {
    const error = new Error(payload.error);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export const get = (path) => request(path);
export const post = (path, body) =>
  request(path, { method: "POST", body: JSON.stringify(body || {}) });

/**
 * Download a file the way a link would, but with the session in a header.
 * The browser saves it from memory; a link with the token in its URL would
 * leave the token in the history, the logs and anything the URL is pasted
 * into.
 */
export async function download(path, fallbackName = "download") {
  const response = await request(path, { raw: true });
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] || fallbackName;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 0);
}

// --- one poll, one redraw ------------------------------------------------------
//
// The panel used to run a clock per chart -- the state poll and each history
// window on their own two-second timers -- so the screen redrew twice or
// more every cycle, out of step. Now the state poll is the only clock: a
// component that wants something fetched with it registers here, its fetch
// runs alongside the state request, and every result is applied in the same
// tick, so Preact draws once per cycle.

const participants = new Set();

/** Join the poll: `fetch()` runs with each cycle, `apply(result)` after it. */
export function joinPoll(participant) {
  participants.add(participant);
  return () => participants.delete(participant);
}

/** Run every participant's fetch; returns a function that applies them all. */
export async function pollParticipants() {
  const entries = [...participants];
  const results = await Promise.allSettled(entries.map((p) => p.fetch()));
  return () => results.forEach((result, index) => {
    if (participants.has(entries[index])) entries[index].apply(result);
  });
}

// --- endpoints ------------------------------------------------------------

export const api = {
  ping:       () => get("/api/ping"),
  state:      () => get("/api/state"),
  live:       (limit = 600) => get(`/api/live?limit=${limit}`),
  // The whole run in progress, decimated to about `points` rows, keeping
  // every extreme -- the live charts want the run from its first sample.
  liveRun:    (points = 1500) => get(`/api/live?points=${points}`),
  // Only the run's samples newer than `since` (its own `t`, seconds).
  liveSince:  (since) => get(`/api/live?since=${since}`),
  // The rolling record of the rig, kept whether or not a run is going.
  // With `since`, only the rows newer than that (undecimated).
  history: (seconds, points = 1500, since = null) => get(
    `/api/history?${since != null ? `since=${since}` : `seconds=${seconds}`}` +
    `&points=${points}`),
  procedures: () => get("/api/procedures"),
  stats:      () => get("/api/stats"),
  trends:     () => get("/api/trends"),
  events:     (limit = 200) => get(`/api/events?limit=${limit}`),

  deviceState: (key) => get(`/api/device/${key}/state`),
  accessories: (key = "pump", fresh = false) =>
    get(`/api/device/${key}/accessories${fresh ? "?fresh=1" : ""}`),
  gaugeDetails: (key = "gauge") => get(`/api/device/${key}/details`),

  command: (device, action, value, extra = {}) =>
    post("/api/command", { device, action, value, confirm: true, ...extra }),

  startRun: (body) => post("/api/run/start", body),
  // `shutdown` asks for the pump to be brought to rest as well. Without it
  // the run ends and the rig is left exactly as it is, still under vacuum.
  stopRun:  (reason, shutdown = false) =>
    post("/api/run/stop", { reason, shutdown }),
  shutdownPump: (reason = "requested by the operator") =>
    post("/api/pump/shutdown", { reason, confirm: true }),
  // Motor off, fault acknowledged, forevacuum pump kept on.
  securePump: (reason = "requested by the operator") =>
    post("/api/pump/secure", { reason, confirm: true }),
  cancelShutdown: () => post("/api/pump/cancel-shutdown", { confirm: true }),
  // `fresh`: a person asked -- read the drive now, not a minute-old reading.
  pumpDiagnostics: (key = "pump", fresh = false) =>
    get(`/api/device/${key}/diagnostics${fresh ? "?fresh=1" : ""}`),
  acknowledgeAlarms: (id = "all") => post("/api/alarms/ack", { id }),
  setAutoRecovery: (on) => post("/api/supervisor", { auto_recover: !!on, confirm: true }),
  acknowledgeStep: (note = "") => post("/api/run/acknowledge", { note }),

  ports: (kind = "all", includePhantom = false) =>
    get(`/api/ports?kind=${encodeURIComponent(kind)}${includePhantom ? "&all=1" : ""}`),
  actuators: () => get("/api/actuators"),
  // What is physically on the rig; a procedure needing what is not is off.
  equipment: () => get("/api/equipment"),
  setEquipment: (key, installed) =>
    post("/api/equipment", { key, installed: !!installed, confirm: true }),
  // What answers on every free port: probed with read-only queries.
  portSurvey: () => get("/api/ports/survey"),
  setPortSearch: (enabled) => post("/api/ports/searching", { enabled: !!enabled }),
  searchDevice: (key) => post(`/api/device/${key}/search`, {}),
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

  // --- accounts (lab/auth.py) ---
  // On a paired tablet the tablet's own session goes along: the server then
  // signs the person out when they walk away, whatever this page does.
  login:   (username, password) =>
    post("/api/login", { username, password, device: sessions.device || undefined }),
  signup:  (body) => post("/api/signup", body),
  pair:    (code) => post("/api/pair", { code }),
  logout:  () => post("/api/logout", {}),
  me:      () => get("/api/me"),
  // A deliberate touch on the screen: keeps a person's session able to act.
  touch:   () => post("/api/session/touch", {}),
  reauth:  (password) => post("/api/session/reauth", { password }),
  changePassword: (current, next) => post("/api/me/password", { current, new: next }),
  requestRole: (role, note = "") => post("/api/role-requests", { role, note }),
  takeControl: (force = false) => post("/api/control/take", { force }),
  releaseControl: () => post("/api/control/release", {}),

  users:        () => get("/api/users"),
  approveUser:  (id, role) => post("/api/users/approve", { id, role }),
  declineUser:  (id) => post("/api/users/decline", { id }),
  setRole:      (id, role) => post("/api/users/role", { id, role }),
  setEnabled:   (id, enabled) => post("/api/users/status", { id, enabled }),
  resetPassword: (id) => post("/api/users/reset-password", { id }),
  transferOwner: (id, password) => post("/api/users/transfer-owner", { id, password }),
  decideRequest: (id, approve) => post("/api/role-requests/decide", { id, approve }),
  addTablet:    (label) => post("/api/devices/add", { label }),
  tabletCode:   (id) => post("/api/devices/code", { id }),
  removeTablet: (id) => post("/api/devices/remove", { id }),
};
