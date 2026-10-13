/* S3Vault console.
 *
 * Talks to the real administrative API. There is no mock data and no sample
 * content: every figure rendered here comes from a live response, and any
 * value the API reports as unavailable is shown as "unavailable" rather than
 * as zero.
 *
 * Served with a strict CSP (script-src 'self'), so this file is an ES module
 * and nothing is injected via innerHTML with untrusted values.
 */

const CSRF_COOKIE = "s3vault_csrf";
const CSRF_HEADER = "X-CSRF-Token";

const state = {
  user: null,
  buckets: [],
  credentials: [],
  currentBucket: null,
  prefix: "",
};

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
};

function csrfToken() {
  const match = document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : "";
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers[CSRF_HEADER] = csrfToken();

  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401) {
    showLogin();
    throw new Error("Your session has ended. Please sign in again.");
  }
  if (raw) return response;

  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const detail = payload.error || {};
    const error = new Error(detail.message || `Request failed (${response.status})`);
    error.code = detail.code;
    error.details = detail.details || {};
    throw error;
  }
  return payload;
}

function flash(message, kind = "ok") {
  const node = $("flash");
  node.textContent = message;
  node.className = `flash ${kind}`;
  node.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

function formatBytes(value) {
  if (value === null || value === undefined) return "unavailable";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let n = Number(value);
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

function formatNumber(value) {
  return value === null || value === undefined ? "unavailable" : Number(value).toLocaleString();
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function confirmAction(message) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    $("confirm-message").textContent = message;
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.showModal();
  });
}

/** Render a measured value with an explicit provenance label. */
function measured(value, source, formatter = formatNumber) {
  if (value === null || value === undefined || source === "unavailable") {
    return el("span", { className: "muted", textContent: "unavailable" });
  }
  return el("span", { textContent: formatter(value) });
}

// ------------------------------------------------------------------ auth
function showLogin() {
  state.user = null;
  $("login-view").hidden = false;
  $("app-view").hidden = true;
  $("login-username").focus();
}

async function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("whoami").textContent = `${state.user.username} (${state.user.role})`;
  await loadOverview();
}

async function restoreSession() {
  try {
    const me = await api("/api/v1/auth/me");
    state.user = me.user;
    await showApp();
  } catch {
    showLogin();
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = $("login-error");
  errorNode.hidden = true;
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/api/v1/auth/login", {
      method: "POST",
      body: {
        username: $("login-username").value,
        password: $("login-password").value,
      },
    });
    state.user = result.user;
    $("login-password").value = "";
    await showApp();
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/v1/auth/logout", { method: "POST" });
  } catch { /* the session is going away regardless */ }
  showLogin();
});

// ------------------------------------------------------------- navigation
const VIEWS = ["overview", "buckets", "credentials", "audit", "settings"];

async function navigate(view) {
  for (const name of VIEWS) {
    $(`view-${name}`).hidden = name !== view;
  }
  document.querySelectorAll(".tab").forEach((tab) => {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  try {
    if (view === "buckets") await loadBuckets();
    if (view === "credentials") await loadCredentials();
    if (view === "audit") await loadAudit();
    if (view === "settings") await loadSettings();
    if (view === "overview") await loadOverview();
  } catch (error) {
    flash(error.message, "bad");
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => navigate(tab.dataset.view));
});

// --------------------------------------------------------------- overview
async function loadOverview() {
  const data = await api("/api/v1/overview");
  const grid = $("stat-grid");
  grid.replaceChildren();

  const stat = (label, node, source) => {
    const dl = el("dl", { className: "stat" }, [
      el("dt", { textContent: label }),
      el("dd", {}, [node]),
      source ? el("div", { className: "source", textContent: source }) : null,
    ]);
    grid.append(dl);
  };

  const totals = data.totals;
  stat("Buckets", measured(totals.buckets.value, totals.buckets.source), "database");
  stat("Objects", measured(totals.object_count.value, totals.object_count.source), totals.object_count.source);
  stat("Storage used", measured(totals.storage_bytes.value, totals.storage_bytes.source, formatBytes), totals.storage_bytes.source);
  stat("Users", measured(totals.users.value, totals.users.source), "database");
  stat("Active keys", measured(totals.credentials.value, totals.credentials.source), "database");

  const quota = data.quota?.user;
  if (quota) {
    stat(
      "Your quota",
      quota.max_bytes === -1
        ? el("span", { textContent: "unlimited" })
        : measured(quota.used_bytes, "database", formatBytes),
      quota.max_bytes === -1 ? "no user quota set" : `limit ${formatBytes(quota.max_bytes)}`,
    );
  }

  const panel = $("engine-panel");
  panel.replaceChildren();
  const engine = data.engine;
  panel.append(
    el("p", {}, [
      el("strong", { textContent: engine.name }),
      document.createTextNode(` ${engine.version || ""} — `),
      el("span", {
        className: `badge ${engine.healthy ? "ok" : "bad"}`,
        textContent: engine.healthy ? "healthy" : "unavailable",
      }),
    ]),
  );

  const missing = Object.entries(engine.unsupported_features || {});
  if (missing.length) {
    const list = el("ul", {}, missing.map(([feature, reason]) =>
      el("li", {}, [
        el("code", { textContent: feature }),
        document.createTextNode(` — ${reason}`),
      ]),
    ));
    panel.append(el("h3", { textContent: "Not available on this engine" }), list);
  }

  const capacity = data.capacity;
  panel.append(
    el("p", { className: "muted" }, [
      document.createTextNode(
        capacity.available
          ? `Capacity reported by the engine: ${formatBytes(capacity.capacity_bytes)}`
          : `Capacity: unavailable (${capacity.reason || "not reported by the engine"})`,
      ),
    ]),
  );

  if (data.backup) {
    panel.append(el("p", { className: "muted" }, [
      document.createTextNode(
        `Last backup: ${data.backup.status} at ${formatTime(data.backup.started_at)}`,
      ),
    ]));
  }
}

// ---------------------------------------------------------------- buckets
async function loadBuckets() {
  const data = await api("/api/v1/buckets");
  state.buckets = data.buckets;
  const body = $("bucket-rows");
  body.replaceChildren();
  $("buckets-empty").hidden = state.buckets.length > 0;

  for (const bucket of state.buckets) {
    body.append(el("tr", {}, [
      el("td", { className: "mono" }, [el("button", {
        className: "btn ghost small",
        textContent: bucket.name,
        onclick: () => openBucket(bucket.name),
      })]),
      el("td", { textContent: bucket.owner || "—" }),
      el("td", {}, [el("span", {
        className: `badge ${bucket.versioning_enabled ? "ok" : "warn"}`,
        textContent: bucket.versioning_enabled ? "enabled" : "suspended",
      })]),
      el("td", { textContent: formatTime(bucket.created_at) }),
      el("td", {}, [
        el("button", {
          className: "btn danger small",
          textContent: "Delete",
          onclick: () => deleteBucket(bucket.name),
        }),
      ]),
    ]));
  }
}

$("create-bucket-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("new-bucket");
  const name = input.value.trim();
  if (!name) return;
  try {
    await api("/api/v1/buckets", { method: "POST", body: { name } });
    input.value = "";
    flash(`Bucket ${name} created.`);
    await loadBuckets();
  } catch (error) {
    flash(`${error.message}${error.details?.suggested ? ` (try ${error.details.suggested})` : ""}`, "bad");
  }
});

async function deleteBucket(name) {
  const confirmed = await confirmAction(
    `Delete bucket "${name}"? Non-empty buckets are refused unless you force it, and this cannot be undone.`,
  );
  if (!confirmed) return;
  try {
    await api(`/api/v1/buckets/${encodeURIComponent(name)}`, {
      method: "DELETE",
      body: { confirm_name: name },
    });
    flash(`Bucket ${name} deleted.`);
    await loadBuckets();
  } catch (error) {
    flash(error.message, "bad");
  }
}

// ---------------------------------------------------------------- objects
async function openBucket(name) {
  state.currentBucket = name;
  state.prefix = "";
  $("objects-bucket").textContent = `in ${name}`;
  document.querySelectorAll(".panel").forEach((panel) => { panel.hidden = true; });
  $("view-objects").hidden = false;
  document.querySelectorAll(".tab").forEach((tab) => tab.removeAttribute("aria-current"));
  await loadObjects();
}

$("back-buckets").addEventListener("click", () => navigate("buckets"));

async function loadObjects() {
  const params = new URLSearchParams({ delimiter: "/", max_keys: "1000" });
  if (state.prefix) params.set("prefix", state.prefix);

  const bucket = state.currentBucket;
  const data = await api(
    `/api/v1/buckets/${encodeURIComponent(bucket)}/objects?${params.toString()}`,
  );

  const crumbs = $("breadcrumb");
  crumbs.replaceChildren();
  const parts = state.prefix ? state.prefix.split("/").filter(Boolean) : [];
  crumbs.append(el("button", {
    className: "btn ghost small",
    textContent: bucket,
    onclick: () => { state.prefix = ""; loadObjects(); },
  }));
  let accumulated = "";
  parts.forEach((part) => {
    accumulated += `${part}/`;
    crumbs.append(document.createTextNode(" / "));
    const target = accumulated;
    crumbs.append(el("button", {
      textContent: part,
      onclick: () => { state.prefix = target; loadObjects(); },
    }));
  });

  const body = $("object-rows");
  body.replaceChildren();

  for (const common of data.common_prefixes) {
    const name = common.replace(/\/$/, "");
    body.append(el("tr", {}, [
      el("td", { className: "mono" }, [el("button", {
        className: "btn ghost small",
        textContent: `${name}/`,
        onclick: () => { state.prefix = `${common}`; loadObjects(); },
      })]),
      el("td", { className: "muted", textContent: "prefix" }),
      el("td", {}), el("td", {}), el("td", {}),
    ]));
  }

  for (const object of data.objects) {
    body.append(el("tr", {}, [
      el("td", { className: "mono", textContent: object.key }),
      el("td", { textContent: formatBytes(object.size) }),
      el("td", { textContent: object.content_type || "—" }),
      el("td", { textContent: formatTime(object.last_modified) }),
      el("td", {}, [
        el("button", {
          className: "btn small",
          textContent: "Download",
          onclick: () => downloadObject(bucket, object.key),
        }),
        " ",
        el("button", {
          className: "btn danger small",
          textContent: "Delete",
          onclick: () => deleteObjects(bucket, [object.key]),
        }),
      ]),
    ]));
  }

  $("objects-empty").hidden = data.objects.length > 0 || data.common_prefixes.length > 0;
}

async function downloadObject(bucket, key) {
  try {
    const presigned = await api(
      `/api/v1/buckets/${encodeURIComponent(bucket)}/presigned-download`,
      { method: "POST", body: { key } },
    );
    // The signed URL is used immediately and never logged or stored.
    window.location.assign(presigned.url);
  } catch (error) {
    flash(error.message, "bad");
  }
}

async function deleteObjects(bucket, keys) {
  const confirmed = await confirmAction(`Delete ${keys.length} object(s) from ${bucket}? This cannot be undone.`);
  if (!confirmed) return;
  try {
    const result = await api(`/api/v1/buckets/${encodeURIComponent(bucket)}/objects`, {
      method: "DELETE",
      body: { keys },
    });
    flash(`Deleted ${result.deleted} object(s).`);
    await loadObjects();
  } catch (error) {
    flash(error.message, "bad");
  }
}

$("upload-btn").addEventListener("click", async () => {
  const input = $("upload-file");
  if (!input.files || input.files.length === 0) {
    flash("Choose a file first.", "bad");
    return;
  }
  const file = input.files[0];
  const key = file.name;
  const button = $("upload-btn");
  button.disabled = true;
  try {
    const presigned = await api(
      `/api/v1/buckets/${encodeURIComponent(state.currentBucket)}/presigned-upload`,
      {
        method: "POST",
        body: {
          key,
          content_type: file.type || "application/octet-stream",
        },
      },
    );
    const response = await fetch(presigned.url, {
      method: presigned.method,
      headers: presigned.headers,
      body: file,
    });
    if (!response.ok) throw new Error(`engine rejected the upload (${response.status})`);
    flash(`Uploaded ${key}.`);
    input.value = "";
    await loadObjects();
  } catch (error) {
    flash(error.message, "bad");
  } finally {
    button.disabled = false;
  }
});

// ------------------------------------------------------------ credentials
async function loadCredentials() {
  const data = await api("/api/v1/credentials");
  state.credentials = data.credentials;
  const body = $("credential-rows");
  body.replaceChildren();
  $("credentials-empty").hidden = state.credentials.length > 0;

  for (const credential of state.credentials) {
    body.append(el("tr", {}, [
      el("td", { className: "mono" }, [el("code", { textContent: credential.access_key_id })]),
      el("td", { textContent: (credential.permissions || []).join(", ") }),
      el("td", {
        textContent: (credential.bucket_scope || []).length
          ? `${credential.bucket_scope.join(", ")}${credential.prefix_scope ? `/${credential.prefix_scope}` : ""}`
          : "all buckets",
      }),
      el("td", { textContent: formatTime(credential.last_used_at) }),
      el("td", { textContent: formatTime(credential.created_at) }),
      el("td", {}, [
        el("button", {
          className: "btn danger small",
          textContent: "Revoke",
          onclick: () => revokeCredential(credential),
        }),
      ]),
    ]));
  }
}

$("create-credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const buckets = $("cred-buckets").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    const result = await api("/api/v1/credentials", {
      method: "POST",
      body: {
        permissions: [$("cred-permissions").value],
        bucket_scope: buckets,
        prefix_scope: $("cred-prefix").value.trim() || null,
        description: $("cred-description").value.trim(),
      },
    });
    $("reveal-akid").textContent = result.credential.access_key_id;
    $("reveal-secret").textContent = result.credential.secret_access_key;
    $("secret-reveal").hidden = false;
    $("cred-description").value = "";
    await loadCredentials();
  } catch (error) {
    flash(error.message, "bad");
  }
});

$("dismiss-secret").addEventListener("click", async () => {
  $("secret-reveal").hidden = true;
  $("reveal-secret").textContent = "";
  $("reveal-akid").textContent = "";
});

async function revokeCredential(credential) {
  const confirmed = await confirmAction(
    `Revoke access key ${credential.access_key_id}? Clients using it will immediately lose access.`,
  );
  if (!confirmed) return;
  try {
    await api(`/api/v1/credentials/${encodeURIComponent(credential.id)}`, { method: "DELETE" });
    flash("Access key revoked.");
    await loadCredentials();
  } catch (error) {
    flash(error.message, "bad");
  }
}

// ------------------------------------------------------------------ audit
async function loadAudit() {
  const data = await api("/api/v1/audit-events?limit=200");
  const body = $("audit-rows");
  body.replaceChildren();
  $("audit-empty").hidden = data.events.length > 0;

  for (const event of data.events) {
    body.append(el("tr", {}, [
      el("td", { textContent: formatTime(event.occurred_at) }),
      el("td", { textContent: event.actor || event.actor_id || "system" }),
      el("td", {}, [el("code", { textContent: event.action })]),
      el("td", { className: "mono", textContent: event.resource_id || event.resource_type }),
      el("td", {}, [el("span", {
        className: `badge ${event.outcome === "success" ? "ok" : event.outcome === "denied" ? "warn" : "bad"}`,
        textContent: event.outcome,
      })]),
    ]));
  }
}

// --------------------------------------------------------------- settings
async function loadSettings() {
  const settings = await api("/api/v1/settings");
  const container = $("settings-body");
  container.replaceChildren();

  const section = (title, entries) => {
    const table = el("table", { className: "table" }, [
      el("caption", { className: "sr-only", textContent: title }),
      el("tbody", {}, entries.map(([key, value]) => el("tr", {}, [
        el("th", { scope: "row", className: "mono", textContent: key }),
        el("td", { textContent: value }),
      ]))),
    ]);
    container.append(el("h3", { textContent: title }), table);
  };

  section("Public surface", [
    ["Environment", settings.environment],
    ["Hostname", settings.public_hostname],
    ["Console URL", settings.console_url],
    ["S3 endpoint", settings.s3_public_endpoint],
    ["Storage engine", `${settings.storage_engine_endpoint} (${settings.addressing_style} addressing)`],
  ]);

  section("Uploads", [
    ["Maximum object size", formatBytes(settings.uploads.max_object_bytes)],
    ["Part size range", `${formatBytes(settings.uploads.min_part_bytes)} – ${formatBytes(settings.uploads.max_part_bytes)}`],
    ["Multipart threshold", formatBytes(settings.uploads.multipart_threshold_bytes)],
    ["Proxy request limit", formatBytes(settings.uploads.request_body_limit_bytes)],
  ]);

  section("Sessions", [
    ["Absolute lifetime", `${settings.sessions.ttl_seconds}s`],
    ["Idle timeout", `${settings.sessions.idle_ttl_seconds}s`],
    ["Secure cookie", String(settings.sessions.cookie_secure)],
    ["SameSite", settings.sessions.cookie_samesite],
  ]);

  section("Safety gates", [
    ["Require dry run", String(settings.safety.require_dry_run)],
    ["Require backup", String(settings.safety.require_backup)],
    ["Production changes allowed", String(settings.safety.allow_production_changes)],
    ["Backup retention", `${settings.safety.backup_retention_days} days`],
  ]);

  // Secret material is reported as a boolean only; the API never returns values.
  section("Secrets", Object.entries(settings.secrets).map(([name, info]) => [
    name,
    info.configured ? "configured (value never returned)" : "not configured",
  ]));

  if (settings.issues?.length) {
    const list = el("ul", { className: "warn" }, settings.issues.map((i) => el("li", { textContent: i })));
    container.append(el("h3", { textContent: "Configuration warnings" }), list);
  }
}

// ------------------------------------------------------------------- boot
restoreSession();