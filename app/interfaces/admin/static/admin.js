/* ============================================================
   Wellness Admin — Application JavaScript
   ============================================================ */

// ---- State ----
let usersOffset = 0;
const usersLimit = 25;
let selectedUserId = null;
let selectedUserIds = new Set();
let userNameMap = {};
let consoleSessionId = null;
let consoleProcessing = false;
let feedPaused = false;
let feedBuffer = [];
let usersSearchTerm = "";
let currentPsychProfile = null;
let currentPsychMeta = null;

// ---- DOM Refs (populated on DOMContentLoaded) ----
let els = {};

// ============================================================
// TOAST NOTIFICATION SYSTEM
// ============================================================
function showToast(message, type = "info", duration = 4000) {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  const icons = {
    success: "\u2713",
    error: "\u2717",
    warning: "\u26A0",
    info: "\u2139",
  };
  toast.innerHTML = `<span style="font-size:16px;">${icons[type] || icons.info}</span><span>${message}</span>`;
  container.appendChild(toast);
  toast.addEventListener("click", () => {
    toast.classList.add("removing");
    setTimeout(() => toast.remove(), 300);
  });
  setTimeout(() => {
    if (toast.parentElement) {
      toast.classList.add("removing");
      setTimeout(() => toast.remove(), 300);
    }
  }, duration);
}

// ============================================================
// MODAL SYSTEM (replaces alert/confirm)
// ============================================================
function showModal(title, message, options = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    let bodyHTML = "";
    if (options.input) {
      bodyHTML = `<textarea id="modal-input" rows="${options.inputRows || 3}" placeholder="${options.inputPlaceholder || ""}" style="width:100%;">${options.inputValue || ""}</textarea>`;
    }

    overlay.innerHTML = `
      <div class="modal-box">
        <h3>${title}</h3>
        <p>${message}</p>
        ${bodyHTML}
        <div class="modal-actions">
          ${options.showCancel !== false ? '<button class="ghost" id="modal-cancel">Cancel</button>' : ""}
          <button class="${options.danger ? "danger" : ""}" id="modal-confirm">${options.confirmText || "OK"}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const close = (val) => {
      overlay.remove();
      resolve(val);
    };
    overlay
      .querySelector("#modal-cancel")
      ?.addEventListener("click", () => close(null));
    overlay.querySelector("#modal-confirm").addEventListener("click", () => {
      if (options.input) close(document.getElementById("modal-input").value);
      else close(true);
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(null);
    });
    // Focus confirm
    setTimeout(() => overlay.querySelector("#modal-confirm").focus(), 50);
  });
}

async function confirmDangerous(title, message) {
  return showModal(title, message, { danger: true, confirmText: "Confirm" });
}

async function alertModal(title, message) {
  return showModal(title, message, { showCancel: false, confirmText: "OK" });
}

// ============================================================
// TAB SWITCHING
// ============================================================
const _tabLoaded = new Set();

function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document
    .querySelectorAll(".tab-content")
    .forEach((c) => c.classList.remove("active"));
  const target = document.getElementById(`tab-${tabName}`);
  if (target) target.classList.add("active");
  window.location.hash = tabName;

  // Lazy-load tab data on first visit
  if (!_tabLoaded.has(tabName)) {
    _tabLoaded.add(tabName);
    if (tabName === "analytics") {
      loadSystemMetrics();
      loadAppMetrics();
      loadLatencyLive();
    }
    if (tabName === "system") {
      loadModels();
      loadLLMDefaults();
    }
    if (tabName === "moderation") {
      loadModeration();
      loadCrisisAlerts();
    }
    if (tabName === "users") {
      loadUsers();
    }
    if (tabName === "psych") {
      loadUserNames();
    }
    if (tabName === "reminders") {
      loadUserNames();
    }
    if (tabName === "planner-shadow") {
      loadPlannerShadow();
    }
    if (tabName === "memory") {
      loadUserNames();
    }
  } else {
    // Refresh on revisit for tabs that need live data
    if (tabName === "analytics") {
      loadSystemMetrics();
      loadAppMetrics();
      loadLatencyLive();
    }
  }
}

function switchSubtab(container, subtabName) {
  const nav = container.querySelector(".subtab-nav");
  if (!nav) return;
  nav.querySelectorAll(".subtab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.subtab === subtabName);
  });
  container
    .querySelectorAll(".subtab-content")
    .forEach((c) => c.classList.remove("active"));
  const target = container.querySelector(`#subtab-${subtabName}`);
  if (target) target.classList.add("active");
}

// ============================================================
// USER NAME UTILITIES
// ============================================================
function getUserDisplayName(userId) {
  return userNameMap[String(userId)] || `User ${userId}`;
}

function timeAgo(dateStr) {
  if (!dateStr) return "never";
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString();
}

function formatDateTime(dateStr) {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleString();
}

async function fetchJsonSafe(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_err) {
    data = { detail: text || res.statusText };
  }
  return { res, data };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function _formatContextValue(val) {
  // Render parsed JSON as human-readable HTML instead of raw JSON
  if (val === null || val === undefined) return "";
  if (typeof val === "string") return escapeHtml(val);
  if (typeof val === "number" || typeof val === "boolean")
    return escapeHtml(String(val));
  if (Array.isArray(val)) {
    if (!val.length) return '<span class="text-muted">None</span>';
    return (
      '<ul style="margin:2px 0 2px 16px;padding:0;">' +
      val
        .map(
          (item) =>
            `<li style="margin:1px 0;">${typeof item === "object" ? _formatContextValue(item) : escapeHtml(String(item))}</li>`,
        )
        .join("") +
      "</ul>"
    );
  }
  if (typeof val === "object") {
    const entries = Object.entries(val);
    if (!entries.length) return '<span class="text-muted">Empty</span>';
    return entries
      .map(([k, v]) => {
        const label = k
          .replace(/_/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase());
        if (typeof v === "object" && v !== null) {
          return `<div style="margin:3px 0;"><strong style="color:#93c5fd;">${escapeHtml(label)}:</strong>${_formatContextValue(v)}</div>`;
        }
        return `<div style="margin:2px 0;"><strong style="color:#93c5fd;">${escapeHtml(label)}:</strong> ${escapeHtml(String(v))}</div>`;
      })
      .join("");
  }
  return escapeHtml(String(val));
}

function parseMaybeJson(raw, fallback = {}) {
  if (raw === null || raw === undefined) return fallback;
  if (typeof raw === "object") return raw;
  if (typeof raw !== "string") return fallback;
  const text = raw.trim();
  if (!text) return fallback;
  try {
    return JSON.parse(text);
  } catch (_err) {
    return fallback;
  }
}

// ============================================================
// STRUCTURED DATA DISPLAY (replaces JSON dumps)
// ============================================================
function renderDataFields(container, data, fields) {
  container.innerHTML = "";
  fields.forEach(([label, key, formatter]) => {
    const val = typeof key === "function" ? key(data) : data[key];
    const div = document.createElement("div");
    div.className = "data-field";
    div.innerHTML = `
      <div class="field-label">${label}</div>
      <div class="field-value${formatter === "mono" ? " mono" : ""}">${formatter && typeof formatter === "function" ? formatter(val, data) : (val ?? "-")}</div>
    `;
    container.appendChild(div);
  });
}

function renderStatusBadge(status) {
  const cls = {
    open: "badge-open",
    resolved: "badge-resolved",
    new: "badge-new",
    triage: "badge-triage",
    reviewing: "badge-triage",
    wont_fix: "badge-inactive",
  };
  return `<span class="badge-status ${cls[status] || "badge-inactive"}">${status || "unknown"}</span>`;
}

function renderSeverity(sev) {
  const labels = {
    1: "Low",
    2: "Medium",
    3: "High",
    4: "Critical",
    5: "Emergency",
  };
  return `<span class="severity-${sev || 1}">${labels[sev] || sev || "-"}</span>`;
}

// ============================================================
// DASHBOARD
// ============================================================
async function loadStatus() {
  try {
    const res = await fetch("/readyz");
    const data = await res.json();
    const el = document.getElementById("status-cards");
    if (!el) return;
    el.innerHTML = "";
    const checks = data.checks || {};
    Object.entries(checks).forEach(([k, v]) => {
      const ok = v === "ok";
      const div = document.createElement("div");
      div.className = "card stat-card";
      div.innerHTML = `
        <div class="stat-value"><span class="status-dot ${ok ? "ok" : "error"}"></span></div>
        <div class="stat-label">${k}</div>
        <div class="stat-sub">${ok ? "Healthy" : v}</div>
      `;
      el.appendChild(div);
    });
  } catch (e) {
    console.error("loadStatus:", e);
  }
}

function startFeed() {
  const feedEl = document.getElementById("live-feed");
  if (!feedEl) return;
  const evtSource = new EventSource("/live/stream");
  evtSource.onmessage = function (e) {
    if (feedPaused) {
      feedBuffer.push(e.data);
      return;
    }
    const div = document.createElement("div");
    div.textContent = e.data;
    feedEl.appendChild(div);
    feedEl.scrollTop = feedEl.scrollHeight;
  };
  evtSource.onerror = function () {
    const div = document.createElement("div");
    div.textContent = "[feed] disconnected — reconnecting...";
    div.style.color = "#f59e0b";
    feedEl.appendChild(div);
  };
}

// ============================================================
// USERS
// ============================================================
async function loadUsers() {
  try {
    const res = await fetch(`/users?limit=${usersLimit}&offset=${usersOffset}`);
    const data = await res.json();
    const list = document.getElementById("users-list");
    if (!list) return;
    list.innerHTML = "";
    let users = data.users || [];

    // Client-side search filter
    if (usersSearchTerm) {
      const term = usersSearchTerm.toLowerCase();
      users = users.filter(
        (u) =>
          String(u.id).includes(term) ||
          (u.username || "").toLowerCase().includes(term) ||
          (u.display_name || "").toLowerCase().includes(term),
      );
    }

    if (users.length === 0) {
      list.innerHTML =
        '<div class="empty-state"><div class="empty-text">No users found</div></div>';
      return;
    }

    users.forEach((u) => {
      const isSelected = selectedUserIds.has(u.id);
      const isActive = selectedUserId === u.id;
      const lastActive = timeAgo(u.last_active_at);
      const isRecentlyActive =
        u.last_active_at &&
        Date.now() - new Date(u.last_active_at).getTime() < 86400000;

      const div = document.createElement("div");
      div.className = `user-row${isActive ? " selected" : ""}`;
      div.innerHTML = `
        <input type="checkbox" class="user-select-box" data-user-id="${u.id}" ${isSelected ? "checked" : ""} />
        <div class="user-info">
          <div class="user-name">${u.display_name || u.username || "user_" + u.id}</div>
          <div class="user-meta">ID: ${u.id}${u.username ? " @" + u.username : ""}</div>
        </div>
        <div class="user-activity">
          <span class="badge-status ${isRecentlyActive ? "badge-active" : "badge-inactive"}">${isRecentlyActive ? "active" : "inactive"}</span>
          <div class="text-xs text-muted" style="margin-top:2px;">${lastActive}</div>
        </div>
      `;
      div.addEventListener("click", (e) => {
        if (e.target.type === "checkbox") return;
        selectedUserId = u.id;
        document.getElementById("user-id-input").value = u.id;
        openUserDrawer(u.id);
        // Update selection styling
        list
          .querySelectorAll(".user-row")
          .forEach((r) => r.classList.remove("selected"));
        div.classList.add("selected");
      });
      list.appendChild(div);
    });

    // Checkbox listeners
    list.querySelectorAll(".user-select-box").forEach((box) => {
      box.addEventListener("change", (e) => {
        e.stopPropagation();
        const uid = parseInt(e.target.dataset.userId, 10);
        if (e.target.checked) selectedUserIds.add(uid);
        else selectedUserIds.delete(uid);
      });
    });

    // Pagination info
    const pageInfo = document.getElementById("users-page-info");
    if (pageInfo) {
      const start = usersOffset + 1;
      const end = usersOffset + data.users.length;
      pageInfo.textContent = `Showing ${start}–${end}`;
    }
    const prevBtn = document.getElementById("btn-users-prev");
    const nextBtn = document.getElementById("btn-users-next");
    if (prevBtn) prevBtn.disabled = usersOffset === 0;
    if (nextBtn) nextBtn.disabled = data.users.length < usersLimit;
  } catch (e) {
    console.error("loadUsers:", e);
  }
}

// ---- User Detail Drawer ----
async function openUserDrawer(userId) {
  // Close existing drawer
  closeDrawer();

  const overlay = document.createElement("div");
  overlay.className = "drawer-overlay";
  overlay.id = "user-drawer-overlay";
  overlay.addEventListener("click", closeDrawer);

  const drawer = document.createElement("div");
  drawer.className = "drawer";
  drawer.id = "user-drawer";
  drawer.innerHTML = `
    <div class="drawer-header">
      <h2>User Detail</h2>
      <button class="close-btn" onclick="closeDrawer()">&times;</button>
    </div>
    <div id="drawer-content"><div class="text-muted">Loading...</div></div>
  `;
  document.body.appendChild(overlay);
  document.body.appendChild(drawer);

  try {
    const res = await fetch(`/users/${userId}`);
    if (!res.ok) throw new Error("User not found");
    const data = await res.json();
    const content = drawer.querySelector("#drawer-content");

    const user = data.user || data;
    const profileCtx = data.profile_context || [];
    let onboardingObj = null;
    try {
      onboardingObj = user.onboarding_data
        ? JSON.parse(user.onboarding_data)
        : null;
    } catch {
      onboardingObj = null;
    }

    content.innerHTML = `
      <section style="margin-bottom:12px;">
        <h3>Extended Profile</h3>
        <div id="drawer-profile"></div>
      </section>
      <section style="margin-bottom:12px;">
        <div class="collapsible-header" onclick="this.parentElement.querySelector('.collapsible-body').classList.toggle('hidden')">
          <h3 style="margin:0;">Onboarding Data</h3><span class="text-xs text-muted">click to expand</span>
        </div>
        <div class="collapsible-body hidden" id="drawer-onboarding"></div>
      </section>
      <section style="margin-bottom:12px;">
        <div class="collapsible-header" onclick="this.parentElement.querySelector('.collapsible-body').classList.toggle('hidden')">
          <h3 style="margin:0;">AI-Saved Context</h3><span class="text-xs text-muted">${profileCtx.length} entries</span>
        </div>
        <div class="collapsible-body hidden" id="drawer-ai-context"></div>
      </section>
      <section style="margin-bottom:12px;">
        <div class="flex items-center justify-between mb-8">
          <h3 style="margin:0;">Messages</h3>
          <button class="sm secondary" onclick="openUserMessagesModal(${userId})">Open Messages</button>
        </div>
        <div id="drawer-messages" class="text-muted text-sm">Open in popup with search.</div>
      </section>
      <section style="margin-bottom:12px;">
        <div class="flex items-center justify-between mb-8">
          <h3 style="margin:0;">Reminders</h3>
          <button class="sm secondary" onclick="openUserRemindersModal(${userId})">Open Reminders</button>
        </div>
        <div id="drawer-reminders" class="text-muted text-sm">Open in popup for easier management.</div>
      </section>
      <section style="margin-bottom:12px;">
        <div class="flex items-center justify-between mb-8">
          <h3 style="margin:0;">Images</h3>
          <button class="sm secondary" onclick="openUserImagesModal(${userId})">Open Images</button>
        </div>
        <div id="drawer-images" class="text-muted text-sm">Open in popup gallery.</div>
      </section>
      <section>
        <h3>Actions</h3>
        <div class="flex gap-8 flex-wrap">
          <button class="sm secondary" onclick="exportSingleUser(${userId})">Export Data</button>
          <button class="sm danger" onclick="deleteUser(${userId})">Delete User</button>
        </div>
      </section>
    `;

    // Render profile fields
    const profileEl = content.querySelector("#drawer-profile");
    renderDataFields(profileEl, user, [
      ["ID", "id"],
      ["Display Name", "display_name"],
      ["Username", "telegram_username"],
      ["Telegram ID", "telegram_user_id"],
      ["Personality", "personality"],
      [
        "Onboarding",
        (d) => (d.onboarding_completed ? "Completed" : "Incomplete"),
      ],
      ["Last Active", "last_active_at", formatDateTime],
      ["Checkins Configured", () => (data.checkins || []).length],
      ["Recent Messages Stored", () => (data.messages || []).length],
      ["Reminders", () => (data.reminders || []).length],
    ]);

    // Render onboarding data
    const onbEl = content.querySelector("#drawer-onboarding");
    if (onboardingObj && typeof onboardingObj === "object") {
      let onbHtml = '<div style="padding:6px 0;">';
      Object.entries(onboardingObj).forEach(([k, v]) => {
        const val =
          typeof v === "object" ? JSON.stringify(v, null, 2) : String(v);
        onbHtml += `<div style="margin-bottom:6px;"><strong style="color:#94a3b8;">${escapeHtml(k)}:</strong> <span style="color:#e2e8f0;white-space:pre-wrap;">${escapeHtml(val)}</span></div>`;
      });
      onbHtml += "</div>";
      onbEl.innerHTML = onbHtml;
    } else {
      onbEl.innerHTML =
        '<div class="text-muted text-sm">No onboarding data saved.</div>';
    }

    // Render AI-saved context
    const aiEl = content.querySelector("#drawer-ai-context");
    if (profileCtx.length) {
      let aiHtml = "";
      profileCtx.forEach((entry) => {
        let displayVal = entry.value || "";
        let isHtml = false;
        try {
          const parsed = JSON.parse(displayVal);
          displayVal = _formatContextValue(parsed);
          isHtml = true;
        } catch {
          /* plain text — use as-is */
        }
        const prettyKey = escapeHtml(
          entry.key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        );
        aiHtml += `<div style="margin-bottom:8px;padding:6px;background:#0f172a;border-radius:4px;">
          <div style="display:flex;justify-content:space-between;"><strong style="color:#93c5fd;">${prettyKey}</strong><span class="text-xs text-muted">${formatDateTime(entry.updated_at)}</span></div>
          <div style="margin:4px 0 0;font-size:12px;color:#e2e8f0;max-height:200px;overflow:auto;">${isHtml ? displayVal : escapeHtml(displayVal)}</div>
        </div>`;
      });
      aiEl.innerHTML = aiHtml;
    } else {
      aiEl.innerHTML =
        '<div class="text-muted text-sm">No AI-saved context for this user.</div>';
    }
  } catch (e) {
    drawer.querySelector("#drawer-content").innerHTML =
      `<div class="text-error">${e.message}</div>`;
  }
}

function openContentModal(title, html) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-box" style="max-width:90vw;width:980px;max-height:85vh;overflow:auto;">
      <h3>${title}</h3>
      <div>${html}</div>
      <div class="modal-actions">
        <button class="ghost" id="modal-close-only">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector("#modal-close-only")?.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  return overlay;
}

async function openUserMessagesModal(userId) {
  const { res, data } = await fetchJsonSafe(
    `/users/${userId}/messages?limit=200`,
  );
  if (!res.ok) {
    showToast(`Load messages failed: ${data.detail || res.status}`, "error");
    return;
  }
  const msgs = data.messages || [];
  const overlay = openContentModal(
    `Messages • ${getUserDisplayName(userId)}`,
    `
    <input id="msg-search" type="text" placeholder="Search messages..." style="margin-bottom:8px;" />
    <div id="msg-list"></div>
  `,
  );
  const list = overlay.querySelector("#msg-list");
  const render = (term = "") => {
    const t = term.toLowerCase().trim();
    const filtered = !t
      ? msgs
      : msgs.filter((m) =>
          String(m.content || "")
            .toLowerCase()
            .includes(t),
        );
    if (!filtered.length) {
      list.innerHTML = '<div class="text-muted">No messages</div>';
      return;
    }
    list.innerHTML = "";
    filtered.forEach((m) => {
      const div = document.createElement("div");
      const roleColor =
        m.role === "user"
          ? "#93c5fd"
          : m.role === "assistant"
            ? "#a78bfa"
            : "#f59e0b";
      div.style.cssText = "padding:8px 0;border-bottom:1px solid #1f2937;";
      div.innerHTML = `<div class="flex items-center justify-between"><span style="color:${roleColor};font-weight:600;">${m.role}</span><span class="text-xs text-muted">${formatDateTime(m.timestamp)}</span></div><div class="text-sm" style="white-space:pre-wrap;">${(m.content || "").replace(/</g, "&lt;")}</div>`;
      list.appendChild(div);
    });
  };
  overlay
    .querySelector("#msg-search")
    ?.addEventListener("input", (e) => render(e.target.value));
  render();
}

async function openUserRemindersModal(userId) {
  const { res, data } = await fetchJsonSafe(`/users/${userId}/reminders`);
  if (!res.ok) {
    showToast(`Load reminders failed: ${data.detail || res.status}`, "error");
    return;
  }
  const rems = data.reminders || [];
  const overlay = openContentModal(
    `Reminders • ${getUserDisplayName(userId)}`,
    `
    <input id="rem-search" type="text" placeholder="Search reminders..." style="margin-bottom:8px;" />
    <div id="rem-list"></div>
  `,
  );
  const list = overlay.querySelector("#rem-list");
  const render = (term = "") => {
    const t = term.toLowerCase().trim();
    const filtered = !t
      ? rems
      : rems.filter((r) => JSON.stringify(r).toLowerCase().includes(t));
    if (!filtered.length) {
      list.innerHTML = '<div class="text-muted">No reminders</div>';
      return;
    }
    list.innerHTML = "";
    filtered.forEach((r) => {
      const payload = parseMaybeJson(r.payload, {});
      const label = payload.text || r.text || r.kind || "Reminder";
      const when = r.next_run_at || r.due_at || "-";
      const div = document.createElement("div");
      div.className = "card";
      div.style.marginBottom = "8px";
      div.innerHTML = `<div class="flex items-center justify-between"><strong>${label}</strong><span class="badge-status ${r.enabled ? "badge-active" : "badge-inactive"}">${r.enabled ? "enabled" : "disabled"}</span></div><div class="text-xs text-muted mt-8">Due: ${formatDateTime(when)}</div>`;
      list.appendChild(div);
    });
  };
  overlay
    .querySelector("#rem-search")
    ?.addEventListener("input", (e) => render(e.target.value));
  render();
}

async function openUserImagesModal(userId) {
  const { res, data } = await fetchJsonSafe(
    `/users/${userId}/images?limit=200`,
  );
  if (!res.ok) {
    showToast(`Load images failed: ${data.detail || res.status}`, "error");
    return;
  }
  const imgs = data.images || [];
  const overlay = openContentModal(
    `Images • ${getUserDisplayName(userId)}`,
    `
    <input id="img-search" type="text" placeholder="Search image captions..." style="margin-bottom:8px;" />
    <div id="img-list" class="image-grid"></div>
  `,
  );
  const list = overlay.querySelector("#img-list");
  const render = (term = "") => {
    const t = term.toLowerCase().trim();
    const filtered = !t
      ? imgs
      : imgs.filter((i) => JSON.stringify(i).toLowerCase().includes(t));
    if (!filtered.length) {
      list.innerHTML = '<div class="text-muted">No images</div>';
      return;
    }
    list.innerHTML = "";
    filtered.forEach((item) => {
      const card = document.createElement("div");
      card.className = "card";
      const imgSrc = item.preview_url || "";
      card.innerHTML = `
        ${imgSrc ? `<img src="${imgSrc}" style="width:100%;height:160px;object-fit:cover;border-radius:4px;margin-bottom:4px;cursor:pointer;" onclick="window.open('${imgSrc}','_blank')" />` : ""}
        <div class="text-xs">${item.caption || ""}</div>
        <div class="text-xs text-muted">${formatDateTime(item.uploaded_at)}</div>
      `;
      list.appendChild(card);
    });
  };
  overlay
    .querySelector("#img-search")
    ?.addEventListener("input", (e) => render(e.target.value));
  render();
}

// Backward-compatible entrypoint used by older inline handlers.
async function loadDrawerImages(userId) {
  await openUserImagesModal(userId);
}

function closeDrawer() {
  document.getElementById("user-drawer-overlay")?.remove();
  document.getElementById("user-drawer")?.remove();
}

async function deleteUser(userId) {
  const ok = await confirmDangerous(
    "Delete User",
    `Are you sure you want to delete user ${userId} and all related data? This cannot be undone.`,
  );
  if (!ok) return;
  try {
    const res = await fetch(`/users/${userId}`, { method: "DELETE" });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* non-JSON error body */
    }
    if (!res.ok) {
      showToast(
        `Delete failed: ${data.detail || res.statusText || res.status}`,
        "error",
      );
      return;
    }
    selectedUserIds.delete(userId);
    if (selectedUserId === userId) selectedUserId = null;
    closeDrawer();
    showToast(`Deleted user ${userId}`, "warning");
    loadUsers();
    loadUserNames();
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, "error");
  }
}

async function deleteSelectedUsers() {
  const ids = Array.from(selectedUserIds);
  if (ids.length === 0) {
    showToast("No users selected", "warning");
    return;
  }
  const ok = await confirmDangerous(
    "Delete Users",
    `Delete ${ids.length} selected user(s)? This cannot be undone.`,
  );
  if (!ok) return;
  try {
    const res = await fetch("/users/delete_many", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_ids: ids }),
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* non-JSON error body */
    }
    if (!res.ok) {
      showToast(
        `Delete failed: ${data.detail || res.statusText || res.status}`,
        "error",
      );
      return;
    }
    selectedUserIds = new Set();
    showToast(`Deleted ${data.count} users`, "warning");
    loadUsers();
    loadUserNames();
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, "error");
  }
}

async function exportSingleUser(userId) {
  const res = await fetch(`/export/user/${userId}?limit=500`);
  const data = await res.json();
  if (!res.ok) {
    showToast(`Export failed: ${data.detail}`, "error");
    return;
  }
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `user_${userId}_export.json`;
  a.click();
  URL.revokeObjectURL(url);
  showToast("Export downloaded", "success");
}

// ============================================================
// MODERATION (merged with Crisis Alerts)
// ============================================================
const selectedModerationEvents = new Set();
const selectedCrisisEvents = new Set();

function updateSelectionCount(elementId, count) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = `${count} selected`;
}

function syncCheckboxes(containerSelector, selectedSet) {
  document.querySelectorAll(containerSelector).forEach((el) => {
    const eventId = parseInt(el.dataset.eventId || "", 10);
    if (!Number.isFinite(eventId)) return;
    el.checked = selectedSet.has(eventId);
  });
}

function toggleModerationSelection(eventId, checked, kind = "moderation") {
  const selectedSet =
    kind === "crisis" ? selectedCrisisEvents : selectedModerationEvents;
  if (checked) selectedSet.add(eventId);
  else selectedSet.delete(eventId);
  const counterId =
    kind === "crisis" ? "crisis-selection-count" : "moderation-selection-count";
  updateSelectionCount(counterId, selectedSet.size);
}

function selectAllVisible(kind = "moderation") {
  const selector =
    kind === "crisis"
      ? ".crisis-select-checkbox[data-event-id]"
      : ".moderation-select-checkbox[data-event-id]";
  const selectedSet =
    kind === "crisis" ? selectedCrisisEvents : selectedModerationEvents;
  document.querySelectorAll(selector).forEach((el) => {
    const eventId = parseInt(el.dataset.eventId || "", 10);
    if (!Number.isFinite(eventId)) return;
    selectedSet.add(eventId);
    el.checked = true;
  });
  updateSelectionCount(
    kind === "crisis" ? "crisis-selection-count" : "moderation-selection-count",
    selectedSet.size,
  );
}

function clearSelections(kind = "moderation") {
  const selectedSet =
    kind === "crisis" ? selectedCrisisEvents : selectedModerationEvents;
  selectedSet.clear();
  syncCheckboxes(
    kind === "crisis"
      ? ".crisis-select-checkbox[data-event-id]"
      : ".moderation-select-checkbox[data-event-id]",
    selectedSet,
  );
  updateSelectionCount(
    kind === "crisis" ? "crisis-selection-count" : "moderation-selection-count",
    0,
  );
}

async function bulkModerationAction(kind = "moderation", action = "resolve") {
  const selectedSet =
    kind === "crisis" ? selectedCrisisEvents : selectedModerationEvents;
  const eventIds = Array.from(selectedSet);
  if (!eventIds.length) {
    showToast("No events selected", "warning");
    return;
  }

  let notes = "";
  if (action === "resolve") {
    const response = await showModal(
      "Resolve Selected",
      `Resolve ${eventIds.length} selected moderation event(s)?`,
      {
        input: true,
        inputPlaceholder: "Resolution notes (optional)",
        confirmText: "Resolve Selected",
      },
    );
    if (response === null) return;
    notes = response || "";
  } else {
    const confirmed = await confirmDangerous(
      "Remove Selected",
      `Delete ${eventIds.length} selected moderation event(s)? This cannot be undone.`,
    );
    if (!confirmed) return;
  }

  const endpoint =
    action === "resolve"
      ? "/moderation/events/bulk-resolve"
      : "/moderation/events/bulk-delete";
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_ids: eventIds, notes }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showToast(
      `${action === "resolve" ? "Resolve" : "Delete"} failed: ${data.detail || res.status}`,
      "error",
    );
    return;
  }
  showToast(
    action === "resolve"
      ? `Resolved ${data.count || eventIds.length} event(s)`
      : `Removed ${data.count || eventIds.length} event(s)`,
    action === "resolve" ? "success" : "warning",
  );
  clearSelections(kind);
  await loadModeration();
  await loadCrisisAlerts();
}

async function loadModeration() {
  const mode =
    document.getElementById("moderation-filter-resolved")?.value || "open";
  const limit =
    parseInt(document.getElementById("moderation-limit")?.value || "100", 10) ||
    100;
  const q = new URLSearchParams();
  q.set("limit", String(limit));
  if (mode === "open") q.set("resolved", "false");
  if (mode === "resolved") q.set("resolved", "true");
  if (mode === "critical") {
    q.set("resolved", "false");
  }

  try {
    const res = await fetch(`/moderation/events?${q.toString()}`);
    const data = await res.json();
    const container = document.getElementById("moderation-results");
    if (!container) return;
    const events = data.events || data || [];

    if (!Array.isArray(events) || events.length === 0) {
      container.innerHTML =
        '<div class="empty-state"><div class="empty-text">No moderation events found</div></div>';
      updateModerationBadge(0);
      updateSelectionCount("moderation-selection-count", selectedModerationEvents.size);
      return;
    }

    let filtered = events;
    if (mode === "critical") {
      filtered = events.filter((e) => (e.severity || 0) >= 4);
    }

    updateModerationBadge(events.filter((e) => !e.resolved).length);

    container.innerHTML = `<div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr>
          <th><input type="checkbox" onclick="this.checked ? selectAllVisible('moderation') : clearSelections('moderation')" /></th><th>ID</th><th>User</th><th>Type</th><th>Severity</th><th>Time</th><th>Status</th><th>Actions</th>
        </tr></thead>
        <tbody id="moderation-tbody"></tbody>
      </table>
    </div>`;

    const tbody = document.getElementById("moderation-tbody");
    filtered.forEach((evt) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><input type="checkbox" class="moderation-select-checkbox" data-event-id="${evt.id}" onchange="toggleModerationSelection(${evt.id}, this.checked, 'moderation')" /></td>
        <td>${evt.id}</td>
        <td>${getUserDisplayName(evt.user_id)}</td>
        <td>${evt.event_type || "-"}</td>
        <td>${renderSeverity(evt.severity)}</td>
        <td class="text-xs">${formatDateTime(evt.timestamp)}</td>
        <td>${evt.resolved ? renderStatusBadge("resolved") : renderStatusBadge("open")}</td>
        <td>
          <button class="xs secondary" onclick="viewModerationContext(${evt.id})">Context</button>
          ${!evt.resolved ? `<button class="xs" onclick="resolveEventInline(${evt.id})">Resolve</button>` : `<span class="text-xs text-muted">${evt.resolved_by || ""}</span>`}
        </td>
      `;
      tbody.appendChild(tr);
    });
    syncCheckboxes(".moderation-select-checkbox[data-event-id]", selectedModerationEvents);
    updateSelectionCount("moderation-selection-count", selectedModerationEvents.size);
  } catch (e) {
    console.error("loadModeration:", e);
  }
}

async function viewModerationContext(eventId) {
  const { res, data } = await fetchJsonSafe(
    `/moderation/events/${eventId}/context`,
  );
  if (!res.ok) {
    showToast(`Context load failed: ${data.detail || res.status}`, "error");
    return;
  }
  const evt = data.event || {};
  const details = evt.details_obj || {};
  const before = (data.context?.before || []).slice().reverse();
  const after = data.context?.after || [];
  const trigger = data.trigger_message || details.message || "";

  const renderMsg = (m) => `
    <div style="padding:8px 0;border-bottom:1px solid #1f2937;">
      <div class="flex items-center justify-between">
        <span style="font-weight:600;color:${m.role === "user" ? "#93c5fd" : "#a78bfa"}">${escapeHtml(m.role || "-")}</span>
        <span class="text-xs text-muted">${formatDateTime(m.timestamp)}</span>
      </div>
      <div class="text-sm" style="white-space:pre-wrap;">${escapeHtml(m.content || "")}</div>
    </div>
  `;

  const html = `
    <div class="card mb-8">
      <div class="text-xs text-muted">Event #${evt.id} • ${escapeHtml(evt.event_type || "-")} • Severity ${evt.severity ?? "-"}</div>
      <div class="text-xs text-muted">User: ${escapeHtml(getUserDisplayName(evt.user_id))}</div>
      ${evt.personality ? `<div class="text-xs text-muted">Personality: ${escapeHtml(evt.personality)}</div>` : ""}
      <div class="text-xs text-muted">Time: ${formatDateTime(evt.timestamp)}</div>
      ${trigger ? `<div class="mt-8"><strong>Trigger:</strong><div class="text-sm" style="white-space:pre-wrap;">${escapeHtml(trigger)}</div></div>` : ""}
    </div>
    <h4>Conversation Context</h4>
    <div class="text-xs text-muted">Before</div>
    <div>${before.length ? before.map(renderMsg).join("") : '<div class="text-muted">No prior messages</div>'}</div>
    <div class="text-xs text-muted mt-8">After</div>
    <div>${after.length ? after.map(renderMsg).join("") : '<div class="text-muted">No subsequent messages</div>'}</div>
  `;
  openContentModal(`Moderation Context • #${eventId}`, html);
}

async function resolveEventInline(eventId) {
  const notes = await showModal(
    "Resolve Event",
    `Resolve moderation event #${eventId}?`,
    {
      input: true,
      inputPlaceholder: "Resolution notes (optional)",
      confirmText: "Resolve",
      danger: false,
    },
  );
  if (notes === null) return;
  const res = await fetch(`/moderation/events/${eventId}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: notes || "" }),
  });
  if (!res.ok) {
    const d = await res.json();
    showToast(`Resolve failed: ${d.detail || res.status}`, "error");
    return;
  }
  showToast("Event resolved", "success");
  loadModeration();
  loadCrisisAlerts();
}

function updateModerationBadge(count) {
  const badge = document.querySelector('[data-tab="moderation"] .badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count;
    badge.classList.add("visible");
  } else {
    badge.classList.remove("visible");
  }
}

async function loadCrisisAlerts() {
  const limit =
    parseInt(document.getElementById("crisis-limit")?.value || "100", 10) ||
    100;
  try {
    const res = await fetch(`/crisis/active?limit=${limit}`);
    const data = await res.json();
    const container = document.getElementById("crisis-results");
    if (!container) return;
    const alerts = data.alerts || data.events || data || [];

    if (!Array.isArray(alerts) || alerts.length === 0) {
      container.innerHTML =
        '<div class="empty-state"><div class="empty-text">No active crisis alerts</div></div>';
      updateSelectionCount("crisis-selection-count", selectedCrisisEvents.size);
      return;
    }

    container.innerHTML = "";
    alerts.forEach((alert) => {
      const card = document.createElement("div");
      card.className = "card";
      card.style.marginBottom = "8px";
      card.style.borderLeft = `3px solid ${alert.severity >= 4 ? "#ef4444" : "#f59e0b"}`;
      card.innerHTML = `
        <div class="flex items-center justify-between">
          <div>
            <label class="flex items-center gap-8">
              <input type="checkbox" class="crisis-select-checkbox" data-event-id="${alert.id}" onchange="toggleModerationSelection(${alert.id}, this.checked, 'crisis')" />
              <span style="font-weight:600;">${getUserDisplayName(alert.user_id)}</span>
            </label>
            <span class="text-xs text-muted" style="margin-left:8px;">#${alert.id}</span>
          </div>
          ${renderSeverity(alert.severity)}
        </div>
        <div class="text-sm mt-8">${alert.event_type || "-"}</div>
        ${alert.details_obj?.message ? `<div class="text-xs mt-8" style="white-space:pre-wrap;">${escapeHtml(String(alert.details_obj.message).slice(0, 260))}</div>` : ""}
        <div class="text-xs text-muted mt-8">${formatDateTime(alert.timestamp)}</div>
        <div class="flex gap-8 mt-8">
          <button class="xs secondary" onclick="viewModerationContext(${alert.id})">Context</button>
          ${!alert.resolved ? `<button class="xs" onclick="resolveEventInline(${alert.id})">Resolve</button>` : ""}
        </div>
      `;
      container.appendChild(card);
    });
    syncCheckboxes(".crisis-select-checkbox[data-event-id]", selectedCrisisEvents);
    updateSelectionCount("crisis-selection-count", selectedCrisisEvents.size);
  } catch (e) {
    console.error("loadCrisisAlerts:", e);
  }
}

// ============================================================
// ANALYTICS
// ============================================================
async function loadSystemMetrics() {
  try {
    const res = await fetch("/metrics/system");
    if (!res.ok) return;
    const data = await res.json();
    const el = document.getElementById("system-metrics");
    if (!el) return;
    el.innerHTML = "";

    // CPU Bar
    if (typeof data.cpu_percent === "number") {
      const cpuColor =
        data.cpu_percent > 80
          ? "red"
          : data.cpu_percent > 50
            ? "amber"
            : "green";
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label">CPU</div>
          <div class="bar-track"><div class="bar-fill ${cpuColor}" style="width:${Math.min(100, data.cpu_percent)}%"></div></div>
          <div class="bar-value">${data.cpu_percent}%</div>
        </div>`;
    }

    // Memory
    if (data.memory && typeof data.memory.percent === "number") {
      const memColor =
        data.memory.percent > 80
          ? "red"
          : data.memory.percent > 60
            ? "amber"
            : "green";
      const memUsedGB = (data.memory.used / 1073741824).toFixed(1);
      const memTotalGB = (data.memory.total / 1073741824).toFixed(1);
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label">Memory</div>
          <div class="bar-track"><div class="bar-fill ${memColor}" style="width:${data.memory.percent}%"></div></div>
          <div class="bar-value">${memUsedGB}/${memTotalGB} GB</div>
        </div>`;
    }

    // Disk
    if (data.disk && typeof data.disk.total === "number") {
      const diskPct = ((data.disk.used / data.disk.total) * 100).toFixed(0);
      const diskColor = diskPct > 90 ? "red" : diskPct > 70 ? "amber" : "green";
      const diskFreeGB = (data.disk.free / 1073741824).toFixed(1);
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label">Disk</div>
          <div class="bar-track"><div class="bar-fill ${diskColor}" style="width:${diskPct}%"></div></div>
          <div class="bar-value">${diskFreeGB} GB free</div>
        </div>`;
    }

    // Platform info
    el.innerHTML += `<div class="text-xs text-muted mt-8">${data.platform || ""} | Python ${data.python_version || ""}</div>`;
  } catch (e) {
    console.error("loadSystemMetrics:", e);
  }
}

async function loadAppMetrics() {
  const hrs =
    parseInt(document.getElementById("window-hours")?.value || "24", 10) || 24;
  try {
    const res = await fetch(`/metrics/app?hours=${hrs}`);
    if (!res.ok) return;
    const data = await res.json();
    const el = document.getElementById("app-metrics");
    if (!el) return;

    el.innerHTML = "";
    const metrics = [
      { label: "Messages Total", value: data.messages_total, color: "blue" },
      {
        label: `Messages (${hrs}h)`,
        value: data.messages_24h,
        color: "purple",
      },
      { label: "Reminders Total", value: data.reminders_total, color: "amber" },
      {
        label: "Due Next Hour",
        value: data.reminders_due_next_hour,
        color: "amber",
      },
      {
        label: "Open Moderation",
        value: data.moderation_open,
        color: data.moderation_open > 0 ? "red" : "green",
      },
    ];

    const maxVal = Math.max(
      ...metrics.map((m) => (typeof m.value === "number" ? m.value : 0)),
      1,
    );
    metrics.forEach((m) => {
      const width =
        typeof m.value === "number"
          ? Math.min(100, (m.value / maxVal) * 100)
          : 0;
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label">${m.label}</div>
          <div class="bar-track"><div class="bar-fill ${m.color}" style="width:${width}%"></div></div>
          <div class="bar-value">${m.value ?? "n/a"}</div>
        </div>`;
    });

    loadTimeseries(hrs);
  } catch (e) {
    console.error("loadAppMetrics:", e);
  }
}

async function loadTimeseries(hours = 48) {
  try {
    const res = await fetch(`/metrics/timeseries?hours=${hours}`);
    if (!res.ok) return;
    const data = await res.json();
    const el = document.getElementById("app-metrics-chart");
    if (!el) return;

    el.innerHTML = "";
    const msg = (data.messages || []).slice(-24);
    const rem = (data.reminders || []).slice(-24);
    const maxMsg = Math.max(...msg.map((m) => m.count || 0), 1);
    const maxRem = Math.max(...rem.map((r) => r.count || 0), 1);

    el.innerHTML += "<h4>Messages (24h)</h4>";
    msg.forEach((m) => {
      const w = Math.min(100, (m.count / maxMsg) * 100);
      const hr = m.bucket ? m.bucket.split(" ")[1] || m.bucket : "";
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label text-xs">${hr}</div>
          <div class="bar-track"><div class="bar-fill purple" style="width:${w}%"></div></div>
          <div class="bar-value text-xs">${m.count}</div>
        </div>`;
    });

    el.innerHTML += '<h4 class="mt-12">Reminders (24h)</h4>';
    rem.forEach((r) => {
      const w = Math.min(100, (r.count / maxRem) * 100);
      const hr = r.bucket ? r.bucket.split(" ")[1] || r.bucket : "";
      el.innerHTML += `
        <div class="metric-bar">
          <div class="bar-label text-xs">${hr}</div>
          <div class="bar-track"><div class="bar-fill amber" style="width:${w}%"></div></div>
          <div class="bar-value text-xs">${r.count}</div>
        </div>`;
    });
  } catch (e) {
    console.error("loadTimeseries:", e);
  }
}

async function loadLatencyLive() {
  const el = document.getElementById("latency-live");
  if (!el) return;
  const limit =
    parseInt(document.getElementById("latency-limit")?.value || "20", 10) || 20;
  try {
    const res = await fetch(`/metrics/latency_live?limit=${limit}`);
    if (!res.ok) {
      el.textContent = "Failed to load";
      return;
    }
    const data = await res.json();
    const rows = data.rows || [];
    const summary = data.summary || {};
    const fmt = (v) =>
      v === null || v === undefined ? "-" : `${Number(v).toFixed(1)}ms`;

    el.innerHTML = "";
    // Summary bar
    el.innerHTML += `<div class="flex gap-8 flex-wrap mb-8 text-xs">
      <span>Samples: <strong>${summary.count ?? 0}</strong></span>
      <span>Avg Total: <strong>${fmt(summary.avg_total_ms)}</strong></span>
      <span>Avg Queue: <strong>${fmt(summary.avg_queue_ms)}</strong></span>
      <span>Avg RAG: <strong>${fmt(summary.avg_rag_ms)}</strong></span>
      <span>Avg Memory: <strong>${fmt(summary.avg_memory_ms)}</strong></span>
      <span>Avg LLM: <strong>${fmt(summary.avg_llm_ms)}</strong></span>
      <span>Avg Persist: <strong>${fmt(summary.avg_persist_ms)}</strong></span>
      <span>Avg Send: <strong>${fmt(summary.avg_send_ms)}</strong></span>
      <span>Avg E2E: <strong>${fmt(summary.avg_e2e_ms)}</strong></span>
      <span class="text-success">OK: ${summary.ok_count ?? 0}</span>
      <span class="text-error">ERR: ${summary.error_count ?? 0}</span>
    </div>`;

    if (rows.length === 0) {
      el.innerHTML +=
        '<div class="text-muted text-sm">No timing samples yet.</div>';
      return;
    }

    let tableHTML = `<table class="data-table"><thead><tr>
      <th>Time</th><th>User</th><th>Total</th><th>Queue</th><th>RAG</th><th>Memory</th><th>LLM</th><th>Persist</th><th>Send</th><th>E2E</th><th>Status</th>
    </tr></thead><tbody>`;
    rows.forEach((r) => {
      const ts = r.created_at
        ? new Date(r.created_at).toLocaleTimeString()
        : "-";
      const statusColor = r.status === "ok" ? "text-success" : "text-error";
      const memoryLabel = r.memory_mode
        ? `${fmt(r.memory_ms)} (${r.memory_mode})`
        : fmt(r.memory_ms);
      tableHTML += `<tr>
        <td class="text-xs">${ts}</td>
        <td>${getUserDisplayName(r.user_id)}</td>
        <td>${fmt(r.total_ms)}</td>
        <td>${fmt(r.queue_ms)}</td>
        <td>${fmt(r.rag_ms)}</td>
        <td>${memoryLabel}</td>
        <td>${fmt(r.llm_ms)}</td>
        <td>${fmt(r.persist_ms)}</td>
        <td>${fmt(r.send_ms)}</td>
        <td>${fmt(r.e2e_ms)}</td>
        <td class="${statusColor}">${r.status || "-"}</td>
      </tr>`;
      if (r.error)
        tableHTML += `<tr><td colspan="11" class="text-error text-xs">${String(r.error).slice(0, 200)}</td></tr>`;
    });
    tableHTML += "</tbody></table>";
    el.innerHTML += tableHTML;
  } catch (e) {
    console.error("loadLatencyLive:", e);
  }
}

// ---- User Analytics Subtab ----
async function loadUserAnalytics() {
  const el = document.getElementById("user-analytics-content");
  if (!el) return;
  el.innerHTML = '<div class="text-muted">Loading user analytics...</div>';

  try {
    const selected =
      document.getElementById("user-analytics-user")?.value || "";
    const url = selected
      ? `/metrics/user_analytics?user_id=${encodeURIComponent(selected)}`
      : "/metrics/user_analytics";
    const res = await fetch(url);
    if (!res.ok) throw new Error("Failed to load user analytics");
    const data = await res.json();

    el.innerHTML = "";

    if (data.mode === "single_user") {
      const user = data.user || {};
      el.innerHTML += `<div class="grid-auto mb-8" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;">
        <div class="card stat-card"><div class="stat-value">${escapeHtml(user.user_name || "-")}</div><div class="stat-label">User</div></div>
        <div class="card stat-card"><div class="stat-value">${data.messages_total ?? 0}</div><div class="stat-label">Messages Total</div></div>
        <div class="card stat-card"><div class="stat-value">${data.messages_24h ?? 0}</div><div class="stat-label">Messages (24h)</div></div>
        <div class="card stat-card"><div class="stat-value">${data.messages_7d ?? 0}</div><div class="stat-label">Messages (7d)</div></div>
        <div class="card stat-card"><div class="stat-value">${data.user_messages ?? 0}</div><div class="stat-label">User Messages</div></div>
        <div class="card stat-card"><div class="stat-value">${data.assistant_messages ?? 0}</div><div class="stat-label">Assistant Messages</div></div>
      </div>`;

      const byDay = data.messages_by_day || [];
      if (byDay.length) {
        el.innerHTML += "<h4>Message Trend (14 days)</h4>";
        const maxByDay = Math.max(...byDay.map((d) => d.count || 0), 1);
        byDay.forEach((d) => {
          const w = Math.min(100, ((d.count || 0) / maxByDay) * 100);
          el.innerHTML += `<div class="metric-bar">
            <div class="bar-label text-xs">${escapeHtml(d.date || "-")}</div>
            <div class="bar-track"><div class="bar-fill blue" style="width:${w}%"></div></div>
            <div class="bar-value text-xs">${d.count || 0}</div>
          </div>`;
        });
      }

      const sentiment = data.sentiment_by_day || [];
      if (sentiment.length) {
        el.innerHTML += '<h4 class="mt-16">Sentiment Trend (14 days)</h4>';
        sentiment.forEach((s) => {
          const valence = Number(s.avg_valence || 0);
          const pct = Math.min(100, Math.max(0, ((valence + 1) / 2) * 100));
          el.innerHTML += `<div class="metric-bar">
            <div class="bar-label text-xs">${escapeHtml(s.date || "-")}</div>
            <div class="bar-track"><div class="bar-fill ${valence < 0 ? "amber" : "green"}" style="width:${pct}%"></div></div>
            <div class="bar-value text-xs">v=${valence.toFixed(2)} (${s.sample_count || 0})</div>
          </div>`;
        });
      }

      const latest = data.latest_messages || [];
      el.innerHTML += '<h4 class="mt-16">Latest Messages</h4>';
      if (!latest.length) {
        el.innerHTML +=
          '<div class="text-muted text-sm">No messages found.</div>';
      } else {
        latest.slice(0, 20).forEach((m) => {
          el.innerHTML += `
            <div class="card mb-8">
              <div class="flex items-center justify-between">
                <span style="font-weight:600;color:${m.role === "user" ? "#93c5fd" : "#a78bfa"}">${escapeHtml(m.role || "-")}</span>
                <span class="text-xs text-muted">${formatDateTime(m.timestamp)}</span>
              </div>
              <div class="text-sm mt-8" style="white-space:pre-wrap;">${escapeHtml((m.content || "").slice(0, 3000))}</div>
            </div>`;
        });
      }
      return;
    }

    // Overview stats
    el.innerHTML += `<div class="grid-auto mb-8" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;">
      <div class="card stat-card"><div class="stat-value">${data.total_users ?? 0}</div><div class="stat-label">Total Users</div></div>
      <div class="card stat-card"><div class="stat-value">${data.active_24h ?? 0}</div><div class="stat-label">Active (24h)</div></div>
      <div class="card stat-card"><div class="stat-value">${data.active_7d ?? 0}</div><div class="stat-label">Active (7d)</div></div>
      <div class="card stat-card"><div class="stat-value">${data.active_30d ?? 0}</div><div class="stat-label">Active (30d)</div></div>
      <div class="card stat-card"><div class="stat-value">${data.onboarded ?? 0}</div><div class="stat-label">Onboarded</div></div>
      <div class="card stat-card"><div class="stat-value">${data.avg_messages_per_user ?? 0}</div><div class="stat-label">Avg Msgs/User</div></div>
    </div>`;

    // Top users by messages
    if (data.top_users_by_messages && data.top_users_by_messages.length > 0) {
      el.innerHTML += "<h4>Top Users by Message Count</h4>";
      const maxMsgs = Math.max(
        ...data.top_users_by_messages.map((u) => u.message_count),
        1,
      );
      data.top_users_by_messages.forEach((u) => {
        const w = Math.min(100, (u.message_count / maxMsgs) * 100);
        el.innerHTML += `<div class="metric-bar">
          <div class="bar-label">${getUserDisplayName(u.user_id)}</div>
          <div class="bar-track"><div class="bar-fill blue" style="width:${w}%"></div></div>
          <div class="bar-value">${u.message_count}</div>
        </div>`;
      });
    }

    // New users over time
    if (data.new_users_by_day && data.new_users_by_day.length > 0) {
      el.innerHTML += '<h4 class="mt-16">New User Signups (Last 30 Days)</h4>';
      const maxNew = Math.max(...data.new_users_by_day.map((d) => d.count), 1);
      data.new_users_by_day.forEach((d) => {
        const w = Math.min(100, (d.count / maxNew) * 100);
        el.innerHTML += `<div class="metric-bar">
          <div class="bar-label text-xs">${d.date}</div>
          <div class="bar-track"><div class="bar-fill green" style="width:${w}%"></div></div>
          <div class="bar-value text-xs">${d.count}</div>
        </div>`;
      });
    }

    // Retention / engagement
    if (data.retention) {
      el.innerHTML += '<h4 class="mt-16">Engagement Breakdown</h4>';
      const ret = data.retention;
      const categories = [
        {
          label: "Power Users (daily)",
          count: ret.daily_active || 0,
          color: "green",
        },
        {
          label: "Regular (weekly)",
          count: ret.weekly_only || 0,
          color: "blue",
        },
        {
          label: "Occasional (monthly)",
          count: ret.monthly_only || 0,
          color: "amber",
        },
        { label: "Dormant (30d+)", count: ret.dormant || 0, color: "red" },
      ];
      const maxRet = Math.max(...categories.map((c) => c.count), 1);
      categories.forEach((c) => {
        const w = Math.min(100, (c.count / maxRet) * 100);
        el.innerHTML += `<div class="metric-bar">
          <div class="bar-label">${c.label}</div>
          <div class="bar-track"><div class="bar-fill ${c.color}" style="width:${w}%"></div></div>
          <div class="bar-value">${c.count}</div>
        </div>`;
      });
    }

    // Mood distribution
    if (data.mood_distribution && data.mood_distribution.length > 0) {
      el.innerHTML += '<h4 class="mt-16">Mood Distribution (All Users)</h4>';
      const maxMood = Math.max(
        ...data.mood_distribution.map((m) => m.count),
        1,
      );
      data.mood_distribution.forEach((m) => {
        const w = Math.min(100, (m.count / maxMood) * 100);
        el.innerHTML += `<div class="metric-bar">
          <div class="bar-label">${m.mood_label || "unknown"}</div>
          <div class="bar-track"><div class="bar-fill purple" style="width:${w}%"></div></div>
          <div class="bar-value">${m.count}</div>
        </div>`;
      });
    }
  } catch (e) {
    el.innerHTML = `<div class="text-error">${e.message}</div>`;
    console.error("loadUserAnalytics:", e);
  }
}

// ============================================================
// GRAPHS (Chart.js visualizations)
// ============================================================
const _chartInstances = {};
function _destroyChart(id) {
  if (_chartInstances[id]) {
    _chartInstances[id].destroy();
    delete _chartInstances[id];
  }
}
function _createChart(canvasId, config) {
  _destroyChart(canvasId);
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  const inst = new Chart(ctx, config);
  _chartInstances[canvasId] = inst;
  return inst;
}

const _chartColors = {
  blue: "rgba(96,165,250,1)",
  blueFill: "rgba(96,165,250,0.15)",
  green: "rgba(74,222,128,1)",
  greenFill: "rgba(74,222,128,0.15)",
  amber: "rgba(251,191,36,1)",
  amberFill: "rgba(251,191,36,0.15)",
  purple: "rgba(167,139,250,1)",
  purpleFill: "rgba(167,139,250,0.15)",
  red: "rgba(248,113,113,1)",
  redFill: "rgba(248,113,113,0.15)",
  cyan: "rgba(34,211,238,1)",
  cyanFill: "rgba(34,211,238,0.15)",
  pink: "rgba(244,114,182,1)",
  pinkFill: "rgba(244,114,182,0.15)",
};
const _chartDefaults = {
  color: "#94a3b8",
  borderColor: "rgba(148,163,184,0.15)",
  font: { family: "system-ui, sans-serif", size: 11 },
};

async function loadGraphs() {
  const el = document.getElementById("graphs-content");
  if (!el) return;
  el.innerHTML = '<div class="text-muted">Loading graphs...</div>';

  const userId = document.getElementById("graphs-user")?.value || "";
  const daysVal = document.getElementById("graphs-range")?.value || "14";
  let url = `/metrics/graph_data?days=${daysVal}`;
  if (userId) url += `&user_id=${encodeURIComponent(userId)}`;

  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error("Failed to load graph data");
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    el.innerHTML = `
      <div class="graph-grid">
        <div class="graph-card">
          <h4>Messages Per Day</h4>
          <canvas id="chart-msgs-day" height="220"></canvas>
        </div>
        <div class="graph-card">
          <h4>Activity by Hour of Day</h4>
          <canvas id="chart-msgs-hour" height="220"></canvas>
        </div>
        <div class="graph-card">
          <h4>Sentiment Over Time</h4>
          <canvas id="chart-sentiment" height="220"></canvas>
        </div>
        <div class="graph-card">
          <h4>Messages by Day of Week</h4>
          <canvas id="chart-msgs-dow" height="220"></canvas>
        </div>
        <div class="graph-card">
          <h4>Role Distribution</h4>
          <canvas id="chart-roles" height="220"></canvas>
        </div>
        <div class="graph-card">
          <h4>Avg Message Length</h4>
          <canvas id="chart-msg-len" height="220"></canvas>
        </div>
        <div class="graph-card graph-card-wide">
          <h4>Word Cloud</h4>
          <canvas id="chart-wordcloud" height="320"></canvas>
        </div>
      </div>
    `;

    const gridOpts = {
      color: _chartDefaults.borderColor,
    };
    const tickOpts = {
      color: _chartDefaults.color,
      font: _chartDefaults.font,
    };

    // 1) Messages per day — line chart
    const mpd = data.messages_per_day || [];
    _createChart("chart-msgs-day", {
      type: "line",
      data: {
        labels: mpd.map((d) => d.date?.slice(5) || ""),
        datasets: [
          {
            label: "Messages",
            data: mpd.map((d) => d.count),
            borderColor: _chartColors.blue,
            backgroundColor: _chartColors.blueFill,
            fill: true,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: gridOpts, ticks: tickOpts },
          y: {
            grid: gridOpts,
            ticks: { ...tickOpts, beginAtZero: true },
            beginAtZero: true,
          },
        },
      },
    });

    // 2) Messages by hour — bar chart
    const mbh = data.messages_by_hour || [];
    _createChart("chart-msgs-hour", {
      type: "bar",
      data: {
        labels: mbh.map((d) => `${String(d.hour).padStart(2, "0")}:00`),
        datasets: [
          {
            label: "Messages",
            data: mbh.map((d) => d.count),
            backgroundColor: mbh.map((d, i) => {
              // Color gradient from night (purple) -> morning (amber) -> day (blue) -> evening (pink)
              if (d.hour < 6) return _chartColors.purple;
              if (d.hour < 12) return _chartColors.amber;
              if (d.hour < 18) return _chartColors.blue;
              return _chartColors.pink;
            }),
            borderRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: gridOpts, ticks: tickOpts },
          y: {
            grid: gridOpts,
            ticks: { ...tickOpts, beginAtZero: true },
            beginAtZero: true,
          },
        },
      },
    });

    // 3) Sentiment over time — multi-line
    const sot = data.sentiment_over_time || [];
    _createChart("chart-sentiment", {
      type: "line",
      data: {
        labels: sot.map((d) => d.date?.slice(5) || ""),
        datasets: [
          {
            label: "Valence",
            data: sot.map((d) => d.valence),
            borderColor: _chartColors.green,
            backgroundColor: _chartColors.greenFill,
            fill: false,
            tension: 0.3,
            pointRadius: 2,
          },
          {
            label: "Arousal",
            data: sot.map((d) => d.arousal),
            borderColor: _chartColors.amber,
            backgroundColor: _chartColors.amberFill,
            fill: false,
            tension: 0.3,
            pointRadius: 2,
          },
          {
            label: "Dominance",
            data: sot.map((d) => d.dominance),
            borderColor: _chartColors.purple,
            backgroundColor: _chartColors.purpleFill,
            fill: false,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: { color: _chartDefaults.color, font: _chartDefaults.font },
          },
        },
        scales: {
          x: { grid: gridOpts, ticks: tickOpts },
          y: { grid: gridOpts, ticks: tickOpts, min: -1, max: 1 },
        },
      },
    });

    // 4) Messages by day of week — bar
    const dow = data.messages_by_dow || [];
    _createChart("chart-msgs-dow", {
      type: "bar",
      data: {
        labels: dow.map((d) => d.day),
        datasets: [
          {
            label: "Messages",
            data: dow.map((d) => d.count),
            backgroundColor: [
              _chartColors.red,
              _chartColors.blue,
              _chartColors.green,
              _chartColors.amber,
              _chartColors.purple,
              _chartColors.cyan,
              _chartColors.pink,
            ],
            borderRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: gridOpts, ticks: tickOpts },
          y: {
            grid: gridOpts,
            ticks: { ...tickOpts, beginAtZero: true },
            beginAtZero: true,
          },
        },
      },
    });

    // 5) Role distribution — doughnut
    const roles = data.role_distribution || [];
    const roleColors = {
      user: _chartColors.blue,
      assistant: _chartColors.purple,
      system: _chartColors.amber,
    };
    _createChart("chart-roles", {
      type: "doughnut",
      data: {
        labels: roles.map((r) => r.role || "unknown"),
        datasets: [
          {
            data: roles.map((r) => r.count),
            backgroundColor: roles.map(
              (r) => roleColors[r.role] || _chartColors.cyan,
            ),
            borderColor: "#1e293b",
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: _chartDefaults.color,
              font: _chartDefaults.font,
              padding: 12,
            },
          },
        },
      },
    });

    // 6) Average message length — line
    const aml = data.avg_msg_length || [];
    _createChart("chart-msg-len", {
      type: "line",
      data: {
        labels: aml.map((d) => d.date?.slice(5) || ""),
        datasets: [
          {
            label: "User",
            data: aml.map((d) => d.avg_user_len),
            borderColor: _chartColors.blue,
            tension: 0.3,
            pointRadius: 2,
          },
          {
            label: "Assistant",
            data: aml.map((d) => d.avg_bot_len),
            borderColor: _chartColors.purple,
            tension: 0.3,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: { color: _chartDefaults.color, font: _chartDefaults.font },
          },
        },
        scales: {
          x: { grid: gridOpts, ticks: tickOpts },
          y: { grid: gridOpts, ticks: tickOpts, beginAtZero: true },
        },
      },
    });

    // 7) Word cloud
    const wc = data.word_cloud || [];
    if (wc.length && typeof Chart.controllers?.wordCloud !== "undefined") {
      const maxCount = Math.max(...wc.map((w) => w.count), 1);
      _createChart("chart-wordcloud", {
        type: "wordCloud",
        data: {
          labels: wc.map((w) => w.word),
          datasets: [
            {
              data: wc.map((w) => 10 + (w.count / maxCount) * 60),
              color: wc.map(() => {
                const colors = [
                  _chartColors.blue,
                  _chartColors.green,
                  _chartColors.amber,
                  _chartColors.purple,
                  _chartColors.cyan,
                  _chartColors.pink,
                ];
                return colors[Math.floor(Math.random() * colors.length)];
              }),
            },
          ],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
        },
      });
    } else if (wc.length) {
      // Fallback: CSS-based word cloud if Chart.js wordcloud plugin not loaded
      const wcEl = document.getElementById("chart-wordcloud");
      if (wcEl) {
        const maxC = Math.max(...wc.map((w) => w.count), 1);
        const colors = [
          _chartColors.blue,
          _chartColors.green,
          _chartColors.amber,
          _chartColors.purple,
          _chartColors.cyan,
          _chartColors.pink,
        ];
        const parent = wcEl.parentElement;
        wcEl.style.display = "none";
        const cloudDiv = document.createElement("div");
        cloudDiv.className = "css-word-cloud";
        wc.forEach((w, i) => {
          const sz = 12 + (w.count / maxC) * 36;
          const span = document.createElement("span");
          span.textContent = w.word;
          span.title = `${w.word}: ${w.count}`;
          span.style.cssText = `font-size:${sz}px;color:${colors[i % colors.length]};padding:3px 6px;display:inline-block;`;
          cloudDiv.appendChild(span);
        });
        parent.appendChild(cloudDiv);
      }
    }
  } catch (e) {
    el.innerHTML = `<div class="text-error">${e.message}</div>`;
    console.error("loadGraphs:", e);
  }
}

// ============================================================
// PSYCH PROFILE
// ============================================================
async function loadPsychHistory() {
  const psychUser = document.getElementById("psych-user");
  if (!psychUser || !psychUser.value) return;
  try {
    const res = await fetch(`/psych/${psychUser.value}/history?limit=10`);
    const data = await res.json();
    const history = document.getElementById("psych-history");
    if (!history) return;
    history.innerHTML = '<option value="">Latest</option>';
    if (res.ok && data.history) {
      data.history.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.id;
        opt.textContent = `#${item.id} | ${item.created_at} | msgs=${item.messages_analyzed ?? "n/a"}`;
        history.appendChild(opt);
      });
    }
  } catch (e) {
    console.error("loadPsychHistory:", e);
  }
}

async function loadPsych() {
  const psychUser = document.getElementById("psych-user");
  if (!psychUser || !psychUser.value) {
    showToast("Select a user first", "warning");
    return;
  }
  const historySel = document.getElementById("psych-history");
  const pid =
    historySel && historySel.value
      ? `?profile_id=${encodeURIComponent(historySel.value)}`
      : "";

  try {
    const res = await fetch(`/psych/${psychUser.value}${pid}`);
    const data = await res.json();
    const container = document.getElementById("psych-results");
    if (!container) return;

    if (!res.ok) {
      container.innerHTML = `<div class="text-error">${data.detail || "Error loading profile"}</div>`;
      return;
    }

    const row = data.profile || null;
    const profile = row ? parseMaybeJson(row.profile_data, {}) : null;
    if (!profile || Object.keys(profile).length === 0) {
      currentPsychProfile = null;
      currentPsychMeta = null;
      container.innerHTML =
        '<div class="text-muted">No profile found for this user.</div>';
      return;
    }

    currentPsychProfile = profile;
    currentPsychMeta = row;
    container.innerHTML = renderPsychProfileHtml(profile, row);
  } catch (e) {
    console.error("loadPsych:", e);
    document.getElementById("psych-results").innerHTML =
      `<div class="text-error">${e.message}</div>`;
  }
}

// --- Psych profile helpers ---------------------------------------------------

function _pv(metric) {
  // Extract the numeric value from {"value": X, "confidence": Y} or a plain number
  if (metric == null) return null;
  if (typeof metric === "number") return metric;
  if (typeof metric === "object" && "value" in metric)
    return Number(metric.value);
  return null;
}

function _pc(metric) {
  // Extract confidence from {"value": X, "confidence": Y}
  if (metric == null) return null;
  if (typeof metric === "object" && "confidence" in metric)
    return Number(metric.confidence);
  return null;
}

function _pctStr(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v >= 0 && v <= 1) return `${Math.round(v * 100)}%`;
  return v.toFixed(1);
}

function _barPct(v) {
  if (v == null || !Number.isFinite(v)) return 0;
  if (v >= 0 && v <= 1) return Math.round(v * 100);
  if (v >= -1 && v < 0) return Math.round((v + 1) * 50);
  return Math.max(0, Math.min(100, Math.round(v)));
}

function _prettyLabel(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Tooltip definitions: [low description, high description]
const _metricTooltips = {
  // Big Five
  openness: [
    "Prefers routine, concrete thinking, and practical approaches",
    "Highly imaginative, curious, and open to novel ideas and experiences",
  ],
  conscientiousness: [
    "Flexible and spontaneous but may struggle with follow-through",
    "Organized, disciplined, and goal-oriented with strong self-control",
  ],
  extraversion: [
    "Reserved and introspective; recharges through solitude",
    "Energized by social interaction; outgoing and assertive",
  ],
  agreeableness: [
    "Skeptical and competitive; prioritizes self-interest",
    "Cooperative, empathetic, and focused on social harmony",
  ],
  neuroticism: [
    "Emotionally stable, calm under pressure, and resilient",
    "Prone to stress, worry, and emotional volatility",
  ],
  // Mental Health
  depression_likelihood: [
    "Low risk of depressive symptoms; generally positive outlook",
    "Elevated depressive indicators; may include sadness, hopelessness, or low energy",
  ],
  anxiety_likelihood: [
    "Calm disposition; low worry baseline",
    "Elevated anxiety markers; persistent worry, tension, or apprehension",
  ],
  bipolar_indicators: [
    "Stable mood patterns without significant swings",
    "Shows signs of mood cycling between highs and lows",
  ],
  adhd_indicators: [
    "Can sustain focus and organize tasks effectively",
    "Shows patterns of distractibility, impulsivity, or difficulty with sustained attention",
  ],
  ocd_indicators: [
    "Flexible thinking without intrusive repetitive patterns",
    "Signs of repetitive thoughts or compulsive behavioral patterns",
  ],
  ptsd_indicators: [
    "No signs of trauma-related stress responses",
    "Shows markers of trauma response: hypervigilance, avoidance, or re-experiencing",
  ],
  social_anxiety: [
    "Comfortable in social situations; low social apprehension",
    "Significant discomfort in social settings; fear of judgment or scrutiny",
  ],
  eating_disorder_indicators: [
    "Healthy relationship with food and body image",
    "Shows concerning patterns around food, eating, or body image",
  ],
  dissociation_indicators: [
    "Grounded and present; strong sense of continuous self",
    "Signs of detachment from self or reality; may feel disconnected",
  ],
  body_dysmorphia_indicators: [
    "Realistic self-perception; comfortable with appearance",
    "Excessive preoccupation with perceived flaws in appearance",
  ],
  substance_use_risk: [
    "Low risk indicators for substance misuse",
    "Elevated risk factors for problematic substance use",
  ],
  addiction_vulnerability: [
    "Low vulnerability to addictive patterns",
    "Higher vulnerability to addictive behaviors or dependencies",
  ],
  autism_spectrum_indicators: [
    "Neurotypical social and communication patterns",
    "Shows patterns consistent with neurodivergent processing styles",
  ],
  // Dark Triad
  narcissism: [
    "Modest self-view; not focused on status or admiration",
    "Elevated self-importance; seeks admiration and has grandiose tendencies",
  ],
  machiavellianism: [
    "Straightforward and trusting in dealings with others",
    "Strategic and calculating; tends toward manipulation for personal gain",
  ],
  psychopathy: [
    "Empathetic and emotionally connected to others",
    "Reduced empathy; callous tendencies and poor impulse control",
  ],
  // Emotional Intelligence
  self_awareness: [
    "Limited insight into own emotions and their triggers",
    "Deep understanding of own emotional states and patterns",
  ],
  self_regulation: [
    "Struggles to manage emotional responses effectively",
    "Excellent control over emotional reactions and impulses",
  ],
  motivation: [
    "May lack internal drive or struggle with goal pursuit",
    "Highly intrinsically motivated with strong drive toward goals",
  ],
  empathy: [
    "Difficulty understanding or sharing others' feelings",
    "Naturally attuned to others' emotions and perspectives",
  ],
  social_skills: [
    "Challenges in navigating social dynamics effectively",
    "Skilled at building rapport, resolving conflicts, and influencing others",
  ],
  // Cognitive
  vocabulary_complexity: [
    "Uses simpler, everyday language",
    "Employs sophisticated, nuanced vocabulary",
  ],
  logical_coherence: [
    "Thinking may be more associative or scattered",
    "Highly structured, logical reasoning patterns",
  ],
  abstract_thinking: [
    "Prefers concrete, literal thinking",
    "Comfortable with abstract concepts and metaphorical reasoning",
  ],
  creativity_indicators: [
    "Conventional, practical approach to problems",
    "Highly creative, generates novel ideas and connections",
  ],
  // Psychological Traits
  impulsivity: [
    "Thoughtful and deliberate in decision-making",
    "Acts quickly without much forethought; risk of rash decisions",
  ],
  resilience: [
    "May struggle to bounce back from setbacks",
    "Strong ability to recover from adversity and adapt",
  ],
  self_esteem: [
    "Low self-worth; may doubt own value and abilities",
    "Healthy self-regard; confident in own worth and capabilities",
  ],
  perfectionism: [
    "Accepting of imperfection; relaxed standards",
    "High standards that may cause stress when unmet",
  ],
  assertiveness: [
    "Passive; avoids confrontation and may not advocate for self",
    "Confidently expresses needs and stands up for boundaries",
  ],
  optimism: [
    "Tends toward pessimistic or realistic outlook",
    "Generally sees positive outcomes; hopeful about the future",
  ],
  emotional_stability: [
    "Emotions fluctuate significantly; reactive",
    "Even-keeled emotionally; maintains composure under stress",
  ],
  open_mindedness: [
    "Prefers established views; resistant to new ideas",
    "Receptive to diverse perspectives and willing to revise beliefs",
  ],
  // Communication
  verbosity: [
    "Concise communicator; uses few words",
    "Expressive and detailed; tends toward lengthy communication",
  ],
  emotional_expressiveness: [
    "Reserved about sharing feelings openly",
    "Freely and openly expresses emotional states",
  ],
  humor_usage: [
    "Serious tone; rarely uses humor",
    "Frequently uses humor as a communication and coping tool",
  ],
  formality_level: [
    "Casual, informal communication style",
    "Formal, polished communication approach",
  ],
  directness: [
    "Indirect; implies meaning rather than stating it",
    "Blunt and straightforward; says exactly what they mean",
  ],
  // Motivation Drivers
  achievement: [
    "Not primarily motivated by accomplishment or success",
    "Strongly driven by goals, mastery, and measurable progress",
  ],
  affiliation: [
    "Independent; less need for social belonging",
    "Deeply motivated by connection, belonging, and relationships",
  ],
  power: [
    "Comfortable following; not seeking control",
    "Motivated by influence, authority, and impact on others",
  ],
  autonomy: [
    "Comfortable with structure and external direction",
    "Strongly values independence and self-direction",
  ],
  // Cognitive Distortions
  catastrophizing: [
    "Proportionate response to negative events",
    "Tends to assume worst-case scenarios will occur",
  ],
  black_and_white_thinking: [
    "Sees nuance and shades of gray",
    "Tends toward all-or-nothing, polarized thinking",
  ],
  overgeneralization: [
    "Evaluates events individually on their merits",
    "Draws sweeping conclusions from single events",
  ],
  mind_reading: [
    "Takes others' statements at face value",
    "Assumes knowledge of what others think or feel",
  ],
  emotional_reasoning: [
    "Distinguishes feelings from facts",
    "Treats emotions as evidence of truth",
  ],
  should_statements: [
    "Flexible expectations of self and others",
    'Rigid rules about how things "should" be',
  ],
  personalization: [
    "Attributes events to appropriate causes",
    "Takes excessive personal responsibility for external events",
  ],
  filtering: [
    "Balanced view of positive and negative aspects",
    "Focuses on negatives while dismissing positives",
  ],
  // Standalone
  locus_of_control: [
    "Believes outcomes are driven by external forces (luck, fate)",
    "Believes personal actions directly determine outcomes",
  ],
  growth_mindset: [
    "Sees abilities as fixed and unchangeable",
    "Believes abilities can be developed through effort and learning",
  ],
  risk_tolerance: [
    "Risk-averse; prefers safety and certainty",
    "Comfortable with uncertainty; willing to take calculated risks",
  ],
  // Attachment
  security_score: [
    "Insecure attachment; difficulty trusting or relying on others",
    "Secure attachment; comfortable with intimacy and independence",
  ],
  anxiety_dimension: [
    "Relaxed about relationships; low fear of abandonment",
    "Anxious about relationships; fears rejection or abandonment",
  ],
  avoidance_dimension: [
    "Comfortable with closeness and emotional intimacy",
    "Avoids emotional closeness; values self-sufficiency over intimacy",
  ],
  disorganization_level: [
    "Consistent, organized attachment behavior",
    "Contradictory attachment behaviors; may desire and fear closeness",
  ],
  // Time perspective
  past_focus: [
    "Lives in the present; doesn't dwell on the past",
    "Frequently reflects on past experiences and memories",
  ],
  present_focus: [
    "Future-oriented planner; less focused on the moment",
    "Lives in the present; focused on current experiences",
  ],
  future_focus: [
    "Present-oriented; less concerned with future planning",
    "Goal-oriented planner; frequently thinks about future outcomes",
  ],
  // MBTI dimensions
  introversion_extraversion: [
    "Introverted — energized by solitude, reflective inner world",
    "Extraverted — energized by social interaction, action-oriented",
  ],
  sensing_intuition: [
    "Sensing — focuses on concrete facts, present reality, practical details",
    "Intuition — focuses on patterns, possibilities, and abstract meaning",
  ],
  thinking_feeling: [
    "Thinking — decides via logic, consistency, and objective analysis",
    "Feeling — decides via personal values, empathy, and impact on others",
  ],
  judging_perceiving: [
    "Judging — prefers structure, plans, and decisive closure",
    "Perceiving — prefers flexibility, spontaneity, and open options",
  ],
};

// MBTI type reference data
const _mbtiTypes = {
  INTJ: {
    name: "The Architect",
    rarity: "2.1%",
    desc: "Strategic, independent, and determined. Natural planners who see the big picture and devise long-term strategies. Can appear aloof but deeply value competence.",
  },
  INTP: {
    name: "The Logician",
    rarity: "3.3%",
    desc: "Analytical, objective, and inventive. Loves exploring ideas and theories. Values precision in thought and can get lost in abstract problem-solving.",
  },
  ENTJ: {
    name: "The Commander",
    rarity: "1.8%",
    desc: "Bold, strategic, and decisive leader. Naturally takes charge and organizes people toward goals. Direct communicator who values efficiency.",
  },
  ENTP: {
    name: "The Debater",
    rarity: "3.2%",
    desc: "Quick-witted, clever, and intellectually curious. Loves challenging ideas and exploring possibilities. Thrives on novelty and mental sparring.",
  },
  INFJ: {
    name: "The Advocate",
    rarity: "1.5%",
    desc: "Insightful, principled, and compassionate. The rarest type — deeply idealistic with a quiet determination to help others. Strong moral compass.",
  },
  INFP: {
    name: "The Mediator",
    rarity: "4.4%",
    desc: "Idealistic, empathetic, and creative. Guided by inner values and a desire for authenticity. Rich inner emotional world and strong imagination.",
  },
  ENFJ: {
    name: "The Protagonist",
    rarity: "2.5%",
    desc: "Charismatic, empathetic, and inspiring leader. Natural mentor who brings out the best in others. Deeply attuned to people's needs.",
  },
  ENFP: {
    name: "The Campaigner",
    rarity: "8.1%",
    desc: "Enthusiastic, creative, and sociable. Sees life as full of possibilities. Warm, imaginative, and driven by a desire to make a positive impact.",
  },
  ISTJ: {
    name: "The Logistician",
    rarity: "11.6%",
    desc: "Responsible, thorough, and dependable. Values tradition, loyalty, and duty. Methodical worker who follows through on commitments.",
  },
  ISFJ: {
    name: "The Defender",
    rarity: "13.8%",
    desc: "Warm, dedicated, and protective. Quietly caring and attentive to others' needs. Reliable and hardworking with a strong sense of duty.",
  },
  ESTJ: {
    name: "The Executive",
    rarity: "8.7%",
    desc: "Organized, logical, and assertive. Natural administrator who values order, rules, and getting things done efficiently.",
  },
  ESFJ: {
    name: "The Consul",
    rarity: "12.3%",
    desc: "Caring, sociable, and popular. Attentive to others' feelings and needs. Values harmony and goes out of their way to help.",
  },
  ISTP: {
    name: "The Virtuoso",
    rarity: "5.4%",
    desc: "Bold, practical, and experimental. Master of tools and techniques. Calm under pressure with a talent for troubleshooting.",
  },
  ISFP: {
    name: "The Adventurer",
    rarity: "8.8%",
    desc: "Gentle, sensitive, and helpful. Lives in the moment with quiet warmth. Artistic and values personal freedom and authenticity.",
  },
  ESTP: {
    name: "The Entrepreneur",
    rarity: "4.3%",
    desc: "Energetic, perceptive, and action-oriented. Lives in the moment and tackles problems head-on. Bold risk-taker with street smarts.",
  },
  ESFP: {
    name: "The Entertainer",
    rarity: "8.5%",
    desc: "Spontaneous, energetic, and fun-loving. The life of the party who brings joy to others. Lives fully in the present moment.",
  },
};

// Enneagram type reference data
const _enneagramTypes = {
  1: {
    name: "The Reformer",
    aka: "The Perfectionist",
    desc: "Principled, purposeful, and self-controlled. Driven by a desire to be good, ethical, and correct. Fears being corrupt or defective.",
    strengths: "Ethical, reliable, fair, organized, self-disciplined",
    weaknesses:
      "Critical, rigid, perfectionistic, resentful when standards aren't met",
    growth: "Learning to accept imperfection and embrace spontaneity",
  },
  2: {
    name: "The Helper",
    aka: "The Giver",
    desc: "Generous, demonstrative, and people-pleasing. Driven by a need to be loved and needed. Warm and self-sacrificing but can become possessive.",
    strengths:
      "Caring, generous, warm, supportive, intuitive about others' needs",
    weaknesses:
      "People-pleasing, possessive, can neglect own needs, manipulative when unhealthy",
    growth: "Learning to acknowledge own needs and set boundaries",
  },
  3: {
    name: "The Achiever",
    aka: "The Performer",
    desc: "Adaptive, excelling, and driven. Motivated by a need for success and to be admired. Image-conscious and highly efficient but can lose touch with authentic self.",
    strengths: "Ambitious, efficient, adaptable, charming, goal-oriented",
    weaknesses:
      "Overly competitive, image-obsessed, workaholic, can be deceptive",
    growth: "Connecting with authentic feelings beyond performance and status",
  },
  4: {
    name: "The Individualist",
    aka: "The Romantic",
    desc: "Expressive, dramatic, and self-absorbed. Driven by a need to be unique and authentic. Deep emotional life but can become melancholic and envious.",
    strengths: "Creative, emotionally honest, empathetic, authentic, intuitive",
    weaknesses: "Moody, self-absorbed, envious, withdrawn, overly dramatic",
    growth:
      "Balancing emotional depth with equanimity and letting go of what's missing",
  },
  5: {
    name: "The Investigator",
    aka: "The Observer",
    desc: "Perceptive, innovative, and secretive. Driven by a need to understand the world. Intellectual and independent but can become detached and isolated.",
    strengths: "Analytical, objective, perceptive, self-sufficient, visionary",
    weaknesses:
      "Detached, isolated, stingy with time/energy, intellectually arrogant",
    growth:
      "Engaging with the world more fully and sharing knowledge generously",
  },
  6: {
    name: "The Loyalist",
    aka: "The Skeptic",
    desc: "Committed, security-oriented, and anxious. Driven by a need for security and support. Reliable and hardworking but can become fearful and suspicious.",
    strengths:
      "Loyal, responsible, trustworthy, practical, courageous when committed",
    weaknesses: "Anxious, suspicious, indecisive, reactive, catastrophizing",
    growth: "Building inner confidence and trusting their own judgment",
  },
  7: {
    name: "The Enthusiast",
    aka: "The Epicure",
    desc: "Spontaneous, versatile, and scattered. Driven by a need for variety and stimulation. Fun-loving and optimistic but can become unfocused and escapist.",
    strengths:
      "Optimistic, versatile, spontaneous, quick-thinking, adventurous",
    weaknesses:
      "Scattered, impulsive, escapist, superficial, difficulty with commitment",
    growth:
      "Staying present with discomfort rather than seeking the next distraction",
  },
  8: {
    name: "The Challenger",
    aka: "The Protector",
    desc: "Self-confident, decisive, and confrontational. Driven by a need to be strong and in control. Protective and direct but can become domineering.",
    strengths: "Confident, decisive, protective, resourceful, natural leader",
    weaknesses:
      "Domineering, confrontational, excessive, difficulty showing vulnerability",
    growth:
      "Allowing vulnerability and tenderness without seeing it as weakness",
  },
  9: {
    name: "The Peacemaker",
    aka: "The Mediator",
    desc: "Receptive, reassuring, and complacent. Driven by a need for peace and harmony. Easy-going and supportive but can become passive and disengaged.",
    strengths: "Patient, accepting, supportive, empathetic, great mediator",
    weaknesses:
      "Passive, complacent, stubborn through inaction, conflict-avoidant",
    growth: "Engaging with own desires and asserting themselves proactively",
  },
};

const _enneagramInstincts = {
  sp: {
    name: "Self-Preservation",
    desc: "Focused on physical safety, comfort, health, and material security. Prioritizes personal wellbeing and practical needs.",
  },
  sx: {
    name: "Sexual/One-to-One",
    desc: "Focused on intensity, attraction, and deep one-on-one connections. Seeks merger and chemistry with others.",
  },
  so: {
    name: "Social",
    desc: "Focused on belonging, group dynamics, and social contribution. Attuned to community roles and social hierarchies.",
  },
};

const _sectionSummaries = {
  big_five: (profile) => {
    const b = profile.big_five || {};
    const traits = [];
    const pv = (m) =>
      typeof m === "object" && m !== null ? Number(m.value) : Number(m);
    if (pv(b.openness) > 0.6) traits.push("intellectually curious");
    else if (pv(b.openness) < 0.4) traits.push("practically grounded");
    if (pv(b.conscientiousness) > 0.6) traits.push("well-organized");
    else if (pv(b.conscientiousness) < 0.4) traits.push("spontaneous");
    if (pv(b.extraversion) > 0.6) traits.push("socially energized");
    else if (pv(b.extraversion) < 0.4) traits.push("introspective");
    if (pv(b.neuroticism) > 0.6) traits.push("emotionally sensitive");
    else if (pv(b.neuroticism) < 0.4) traits.push("emotionally stable");
    if (!traits.length) return "";
    return `This user appears ${traits.join(", ")}. These core personality dimensions are relatively stable over time and shape how they interact with the world.`;
  },
  mental_health: (profile) => {
    const mh = profile.mental_health_indicators || {};
    const pv = (m) =>
      m == null ? 0 : typeof m === "object" ? Number(m.value) : Number(m);
    const elevated = [];
    if (pv(mh.depression_likelihood) > 0.5) elevated.push("depression");
    if (pv(mh.anxiety_likelihood) > 0.5) elevated.push("anxiety");
    if (pv(mh.adhd_indicators) > 0.5) elevated.push("ADHD");
    if (pv(mh.social_anxiety) > 0.5) elevated.push("social anxiety");
    if (!elevated.length)
      return "No significant mental health risk markers detected. All indicators fall within healthy ranges.";
    return `Elevated indicators detected for: ${elevated.join(", ")}. These are statistical estimates from conversation patterns, not clinical diagnoses. A professional evaluation is recommended for anything concerning.`;
  },
  dark_triad: (profile) => {
    const dt = profile.dark_triad || {};
    const pv = (m) =>
      m == null ? 0 : typeof m === "object" ? Number(m.value) : Number(m);
    const high = [];
    if (pv(dt.narcissism) > 0.5) high.push("narcissistic");
    if (pv(dt.machiavellianism) > 0.5) high.push("Machiavellian");
    if (pv(dt.psychopathy) > 0.5) high.push("psychopathic");
    if (!high.length)
      return "Dark triad scores are within normal ranges, suggesting prosocial interpersonal tendencies.";
    return `Elevated ${high.join(" and ")} traits detected. These may reflect communication style rather than personality disorder. Context and cultural factors should be considered.`;
  },
  emotional_intelligence: (profile) => {
    const ei = profile.emotional_intelligence || {};
    const pv = (m) =>
      m == null ? 0 : typeof m === "object" ? Number(m.value) : Number(m);
    const avg = [
      "self_awareness",
      "self_regulation",
      "motivation",
      "empathy",
      "social_skills",
    ]
      .map((k) => pv(ei[k]))
      .filter((v) => v > 0);
    if (!avg.length) return "";
    const mean = avg.reduce((a, b) => a + b, 0) / avg.length;
    if (mean > 0.7)
      return "Shows strong emotional intelligence across most dimensions. This user is likely effective at understanding and managing both their own and others' emotions.";
    if (mean < 0.4)
      return "Emotional intelligence scores suggest room for growth in self-awareness and interpersonal skills. Mindfulness and active listening exercises may help.";
    return "Moderate emotional intelligence with a mix of strengths and areas for development.";
  },
  cognitive: (profile) => {
    const cog = profile.cognitive_metrics || {};
    const pv = (m) =>
      m == null ? 0 : typeof m === "object" ? Number(m.value) : Number(m);
    const iq = pv(cog.estimated_iq);
    let summary = "";
    if (iq > 120)
      summary =
        "Cognitive indicators suggest above-average intellectual capacity. ";
    else if (iq > 90)
      summary = "Cognitive indicators are in the normal range. ";
    const creative = pv(cog.creativity_indicators);
    if (creative > 0.7)
      summary += "Notably creative and inventive in their thinking patterns.";
    else if (creative > 0.4)
      summary += "Shows a balanced mix of creative and analytical thinking.";
    return summary || "Cognitive metrics are within typical ranges.";
  },
  personality_typing: (profile) => {
    const pt = profile.personality_typing || {};
    const parts = [];
    if (pt.myers_briggs?.type) {
      const mbInfo = _mbtiTypes[pt.myers_briggs.type];
      if (mbInfo) {
        parts.push(
          `Myers-Briggs type ${pt.myers_briggs.type} ("${mbInfo.name}", ~${mbInfo.rarity} of the population). ${mbInfo.desc}`,
        );
      }
    }
    if (pt.enneagram?.primary_type) {
      const enInfo = _enneagramTypes[pt.enneagram.primary_type];
      if (enInfo) {
        parts.push(
          `Enneagram Type ${pt.enneagram.primary_type} — ${enInfo.name} (${enInfo.aka}). Key strengths: ${enInfo.strengths}. Growth areas: ${enInfo.weaknesses}. Path forward: ${enInfo.growth}.`,
        );
      }
    }
    if (!parts.length) return "";
    return (
      parts.join(" ") +
      " Together, these typing systems offer complementary lenses for understanding personality patterns, communication preferences, and growth opportunities."
    );
  },
};

function _getTooltip(key, value) {
  const tips = _metricTooltips[key];
  if (!tips) return "";
  const v = _pv(value);
  if (v == null) return tips[0] + " | " + tips[1];
  const tip = v > 0.5 ? tips[1] : tips[0];
  return tip;
}

function metricBar(label, value, color = "blue", confidence, metricKey) {
  const v = _pv(value);
  if (v === null) return "";
  const pct = _barPct(v);
  const confBadge =
    confidence != null && Number.isFinite(confidence)
      ? `<span class="conf-badge">${Math.round(confidence * 100)}% conf</span>`
      : "";
  const tooltip = metricKey ? _getTooltip(metricKey, value) : "";
  const tipAttr = tooltip
    ? ` title="${escapeHtml(tooltip)}" style="cursor:help;"`
    : "";
  return `<div class="metric-bar"${tipAttr}>
    <div class="bar-label">${escapeHtml(label)}${confBadge}${tooltip ? ' <span class="tooltip-icon">?</span>' : ""}</div>
    <div class="bar-track"><div class="bar-fill ${color}" style="width:${pct}%"></div></div>
    <div class="bar-value">${_pctStr(v)}</div>
  </div>`;
}

function _metricRow(label, metric, color = "blue", metricKey) {
  // Renders a metric that may be {"value":X,"confidence":Y} or a plain number
  const v = _pv(metric);
  const c = _pc(metric);
  return metricBar(label, v, color, c, metricKey);
}

function _sectionCard(title, innerHtml) {
  if (!innerHtml || !innerHtml.trim()) return "";
  return `<div class="psych-section card mb-8">
    <h4 class="psych-section-title">${escapeHtml(title)}</h4>
    ${innerHtml}
  </div>`;
}

function _bulletList(arr) {
  if (!arr || !Array.isArray(arr) || !arr.length) return "";
  return (
    '<ul class="psych-list">' +
    arr.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("") +
    "</ul>"
  );
}

function _textBlock(text) {
  if (!text) return "";
  return `<div class="psych-text">${escapeHtml(String(text))}</div>`;
}

function renderPsychProfileHtml(profile, row) {
  const confidence = Number(row?.confidence_score ?? 0);
  const confidencePct = Math.max(
    0,
    Math.min(100, Math.round(confidence * 100)),
  );
  const msgs = Number(row?.messages_analyzed ?? 0);
  let html = "";

  // --- Header card ---
  html += `<div class="card mb-8">
    <div class="text-xs text-muted">Profile #${row?.id ?? "-"} &bull; Created ${formatDateTime(row?.created_at)} &bull; Updated ${formatDateTime(row?.updated_at)}</div>
    <div class="text-xs text-muted">Messages analyzed: ${msgs}</div>
    ${metricBar("Overall Confidence", confidence, confidencePct < 40 ? "amber" : "green")}
  </div>`;

  // --- Executive Summary ---
  const exec = profile.executive_summary || {};
  if (exec.overview || exec.most_prominent_traits) {
    let sumHtml = "";
    if (exec.overview) {
      sumHtml += `<div class="psych-text psych-overview">${escapeHtml(String(exec.overview))}</div>`;
    }
    if (exec.overall_functioning) {
      sumHtml += `<div class="psych-text" style="margin-top:8px;"><strong>Overall Functioning:</strong> ${escapeHtml(String(exec.overall_functioning))}</div>`;
    }
    if (exec.most_prominent_traits && exec.most_prominent_traits.length) {
      sumHtml +=
        '<div style="margin-top:8px;"><strong>Most Prominent Traits:</strong></div>' +
        _bulletList(exec.most_prominent_traits);
    }
    if (exec.core_strengths && exec.core_strengths.length) {
      sumHtml +=
        '<div style="margin-top:8px;"><strong>Core Strengths:</strong></div>' +
        _bulletList(exec.core_strengths);
    }
    if (exec.core_weaknesses && exec.core_weaknesses.length) {
      sumHtml +=
        '<div style="margin-top:8px;"><strong>Core Weaknesses:</strong></div>' +
        _bulletList(exec.core_weaknesses);
    }
    if (
      exec.therapeutic_recommendations &&
      exec.therapeutic_recommendations.length
    ) {
      sumHtml +=
        '<div style="margin-top:8px;"><strong>Therapeutic Recommendations:</strong></div>' +
        _bulletList(exec.therapeutic_recommendations);
    }
    const neededMsg = exec.estimated_messages_for_95_confidence;
    if (neededMsg && neededMsg > 0) {
      sumHtml += `<div class="text-xs text-muted" style="margin-top:8px;">~${neededMsg} more messages needed for 95% confidence</div>`;
    }
    html += _sectionCard("Executive Summary", sumHtml);
  }

  // --- Big Five ---
  const big5 = profile.big_five || {};
  const big5Traits = [
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
  ];
  let big5Html = "";
  const big5Summary = _sectionSummaries.big_five(profile);
  if (big5Summary)
    big5Html += `<div class="psych-section-summary">${escapeHtml(big5Summary)}</div>`;
  big5Traits.forEach((trait) => {
    const m = big5[trait];
    if (m != null)
      big5Html += _metricRow(
        _prettyLabel(trait),
        m,
        trait === "neuroticism" ? "amber" : "blue",
        trait,
      );
  });
  html += _sectionCard(
    "Big Five Personality",
    big5Html ||
      '<div class="text-muted text-sm">No Big Five data in this profile.</div>',
  );

  // --- Mental Health Indicators ---
  const mh = profile.mental_health_indicators || {};
  let mhHtml = "";
  const mhSummary = _sectionSummaries.mental_health(profile);
  if (mhSummary)
    mhHtml += `<div class="psych-section-summary">${escapeHtml(mhSummary)}</div>`;
  const mhKeys = [
    "depression_likelihood",
    "anxiety_likelihood",
    "bipolar_indicators",
    "adhd_indicators",
    "ocd_indicators",
    "ptsd_indicators",
    "social_anxiety",
    "eating_disorder_indicators",
    "dissociation_indicators",
    "body_dysmorphia_indicators",
    "substance_use_risk",
    "addiction_vulnerability",
    "autism_spectrum_indicators",
  ];
  mhKeys.forEach((key) => {
    const m = mh[key];
    if (m != null) {
      const v = _pv(m);
      const color =
        v != null && v > 0.6 ? "red" : v != null && v > 0.3 ? "amber" : "green";
      mhHtml += _metricRow(_prettyLabel(key), m, color, key);
    }
  });
  if (mhHtml) html += _sectionCard("Mental Health Indicators", mhHtml);

  // --- Personality Typing ---
  const pt = profile.personality_typing || {};
  let ptHtml = "";
  const ptSummary = _sectionSummaries.personality_typing(profile);
  if (ptSummary)
    ptHtml += `<div class="psych-section-summary">${escapeHtml(ptSummary)}</div>`;
  if (pt.myers_briggs) {
    const mb = pt.myers_briggs;
    const mbTypeInfo = _mbtiTypes[mb.type];
    ptHtml += `<div style="margin-bottom:8px;"><strong>Myers-Briggs:</strong> <span style="font-size:18px;font-weight:700;color:#93c5fd;${mbTypeInfo ? "cursor:help;" : ""}" ${mbTypeInfo ? `title="${escapeHtml(mbTypeInfo.desc)}"` : ""}>${escapeHtml(String(mb.type || "—"))}</span>`;
    if (mbTypeInfo)
      ptHtml += ` <span style="font-size:14px;color:#94a3b8;" title="${escapeHtml(mbTypeInfo.desc)}">— ${escapeHtml(mbTypeInfo.name)} <span style="opacity:0.7;">(${escapeHtml(mbTypeInfo.rarity)} of population)</span></span>`;
    if (mb.confidence != null)
      ptHtml += ` <span class="conf-badge">${Math.round(mb.confidence * 100)}% conf</span>`;
    ptHtml += "</div>";
    if (mb.dimensions && typeof mb.dimensions === "object") {
      Object.entries(mb.dimensions).forEach(([dim, data]) => {
        const score = typeof data === "object" ? data.score : data;
        const conf = typeof data === "object" ? data.confidence : null;
        if (score != null) {
          // MBTI dimensions are -1 to 1; normalize to 0-1 for display
          const normalized = (Number(score) + 1) / 2;
          ptHtml += metricBar(_prettyLabel(dim), normalized, "blue", conf, dim);
        }
      });
    }
  }
  if (pt.enneagram) {
    const en = pt.enneagram;
    const enTypeInfo = _enneagramTypes[en.primary_type];
    ptHtml += `<div style="margin-top:12px;"><strong>Enneagram:</strong> Type ${escapeHtml(String(en.primary_type || "?"))}`;
    if (enTypeInfo)
      ptHtml += ` <span style="font-size:14px;color:#94a3b8;">— ${escapeHtml(enTypeInfo.name)} (${escapeHtml(enTypeInfo.aka)})</span>`;
    if (en.wing)
      ptHtml += ` <span style="opacity:0.8;">${escapeHtml(String(en.wing))}</span>`;
    if (en.confidence != null)
      ptHtml += ` <span class="conf-badge">${Math.round(en.confidence * 100)}% conf</span>`;
    ptHtml += "</div>";
    if (enTypeInfo) {
      ptHtml += `<div class="text-xs" style="margin:4px 0 4px 12px;color:#94a3b8;cursor:help;" title="${escapeHtml(enTypeInfo.desc)}">${escapeHtml(enTypeInfo.desc)} <span class="tooltip-icon">?</span></div>`;
      ptHtml += `<div class="text-xs" style="margin:2px 0 2px 12px;"><span style="color:#86efac;">Strengths:</span> <span style="color:#94a3b8;">${escapeHtml(enTypeInfo.strengths)}</span></div>`;
      ptHtml += `<div class="text-xs" style="margin:2px 0 2px 12px;"><span style="color:#fca5a5;">Growth areas:</span> <span style="color:#94a3b8;">${escapeHtml(enTypeInfo.weaknesses)}</span></div>`;
      ptHtml += `<div class="text-xs" style="margin:2px 0 6px 12px;"><span style="color:#93c5fd;">Growth direction:</span> <span style="color:#94a3b8;">${escapeHtml(enTypeInfo.growth)}</span></div>`;
    }
    if (en.instinctual_variant) {
      const ivStr = String(en.instinctual_variant).toLowerCase();
      const instInfo =
        _enneagramInstincts[ivStr] || _enneagramInstincts[ivStr.split("/")[0]];
      if (instInfo) {
        ptHtml += `<div class="text-xs text-muted" title="${escapeHtml(instInfo.desc)}" style="cursor:help;">Instinctual variant: <strong>${escapeHtml(instInfo.name)}</strong> (${escapeHtml(String(en.instinctual_variant))}) <span class="tooltip-icon">?</span></div>`;
      } else {
        ptHtml += `<div class="text-xs text-muted">Instinctual variant: ${escapeHtml(String(en.instinctual_variant))}</div>`;
      }
    }
    if (en.integration_direction) {
      const intType = _enneagramTypes[en.integration_direction];
      const disType = _enneagramTypes[en.disintegration_direction];
      const intTip = `In growth (integration), you move toward the healthy traits of Type ${en.integration_direction}${intType ? " (" + intType.name + ")" : ""}. Under stress (disintegration), you may take on unhealthy traits of Type ${en.disintegration_direction || "?"}${disType ? " (" + disType.name + ")" : ""}.`;
      ptHtml += `<div class="text-xs text-muted" title="${escapeHtml(intTip)}" style="cursor:help;">Integration &rarr; Type ${en.integration_direction}${intType ? " (" + escapeHtml(intType.name) + ")" : ""} &bull; Disintegration &rarr; Type ${en.disintegration_direction || "?"}${disType ? " (" + escapeHtml(disType.name) + ")" : ""} <span class="tooltip-icon">?</span></div>`;
    }
  }
  if (pt.introversion_level != null) {
    ptHtml +=
      '<div style="margin-top:8px;">' +
      _metricRow("Introversion Level", pt.introversion_level, "blue") +
      "</div>";
  }
  if (ptHtml) html += _sectionCard("Personality Typing", ptHtml);

  // --- Dark Triad ---
  const dt = profile.dark_triad || {};
  let dtHtml = "";
  const dtSummary = _sectionSummaries.dark_triad(profile);
  if (dtSummary)
    dtHtml += `<div class="psych-section-summary">${escapeHtml(dtSummary)}</div>`;
  ["narcissism", "machiavellianism", "psychopathy"].forEach((key) => {
    if (dt[key] != null)
      dtHtml += _metricRow(_prettyLabel(key), dt[key], "purple", key);
  });
  if (dtHtml) html += _sectionCard("Dark Triad", dtHtml);

  // --- Emotional Intelligence ---
  const ei = profile.emotional_intelligence || {};
  let eiHtml = "";
  const eiSummary = _sectionSummaries.emotional_intelligence(profile);
  if (eiSummary)
    eiHtml += `<div class="psych-section-summary">${escapeHtml(eiSummary)}</div>`;
  [
    "self_awareness",
    "self_regulation",
    "motivation",
    "empathy",
    "social_skills",
  ].forEach((key) => {
    if (ei[key] != null)
      eiHtml += _metricRow(_prettyLabel(key), ei[key], "blue", key);
  });
  if (eiHtml) html += _sectionCard("Emotional Intelligence", eiHtml);

  // --- Cognitive Metrics ---
  const cog = profile.cognitive_metrics || {};
  let cogHtml = "";
  const cogSummary = _sectionSummaries.cognitive(profile);
  if (cogSummary)
    cogHtml += `<div class="psych-section-summary">${escapeHtml(cogSummary)}</div>`;
  if (cog.estimated_iq != null) {
    const iq = _pv(cog.estimated_iq);
    const iqConf = _pc(cog.estimated_iq);
    const iqConfBadge =
      iqConf != null
        ? ` <span class="conf-badge">${Math.round(iqConf * 100)}% conf</span>`
        : "";
    cogHtml += `<div style="margin-bottom:6px;"><strong>Estimated IQ:</strong> <span style="font-size:16px;font-weight:700;color:#93c5fd;">${iq != null ? Math.round(iq) : "—"}</span>${iqConfBadge}</div>`;
  }
  [
    "vocabulary_complexity",
    "logical_coherence",
    "abstract_thinking",
    "creativity_indicators",
  ].forEach((key) => {
    if (cog[key] != null)
      cogHtml += _metricRow(_prettyLabel(key), cog[key], "blue", key);
  });
  if (cogHtml) html += _sectionCard("Cognitive Metrics", cogHtml);

  // --- Attachment Style ---
  const att = profile.attachment_style || {};
  let attHtml = "";
  if (att.primary_type) {
    attHtml += `<div style="margin-bottom:6px;"><strong>Primary Type:</strong> ${escapeHtml(_prettyLabel(String(att.primary_type)))}`;
    if (att.confidence != null)
      attHtml += ` <span class="conf-badge">${Math.round(att.confidence * 100)}% conf</span>`;
    attHtml += "</div>";
  }
  [
    "security_score",
    "anxiety_dimension",
    "avoidance_dimension",
    "disorganization_level",
  ].forEach((key) => {
    if (att[key] != null)
      attHtml += _metricRow(_prettyLabel(key), att[key], "blue", key);
  });
  if (attHtml) html += _sectionCard("Attachment Style", attHtml);

  // --- Cognitive Distortions ---
  const cd = profile.cognitive_distortions || {};
  let cdHtml = "";
  Object.entries(cd).forEach(([key, m]) => {
    if (m != null) {
      const v = _pv(m);
      const color = v != null && v > 0.5 ? "amber" : "green";
      cdHtml += _metricRow(_prettyLabel(key), m, color, key);
    }
  });
  if (cdHtml) html += _sectionCard("Cognitive Distortions", cdHtml);

  // --- Defense Mechanisms ---
  const dm = profile.defense_mechanisms || {};
  let dmHtml = "";
  if (dm.mature_adaptive && dm.mature_adaptive.length) {
    dmHtml +=
      "<div><strong>Mature / Adaptive:</strong></div>" +
      _bulletList(dm.mature_adaptive);
  }
  if (dm.neurotic_intermediate && dm.neurotic_intermediate.length) {
    dmHtml +=
      '<div style="margin-top:6px;"><strong>Neurotic / Intermediate:</strong></div>' +
      _bulletList(dm.neurotic_intermediate);
  }
  if (dm.immature_maladaptive && dm.immature_maladaptive.length) {
    dmHtml +=
      '<div style="margin-top:6px;"><strong>Immature / Maladaptive:</strong></div>' +
      _bulletList(dm.immature_maladaptive);
  }
  if (dm.primary_mechanisms && dm.primary_mechanisms.length) {
    dmHtml +=
      '<div style="margin-top:6px;"><strong>Primary Mechanisms:</strong></div>' +
      _bulletList(dm.primary_mechanisms);
  }
  if (dmHtml) html += _sectionCard("Defense Mechanisms", dmHtml);

  // --- Motivation Drivers ---
  const md = profile.motivation_drivers || {};
  let mdHtml = "";
  ["achievement", "affiliation", "power", "autonomy"].forEach((key) => {
    if (md[key] != null)
      mdHtml += _metricRow(_prettyLabel(key), md[key], "blue", key);
  });
  if (mdHtml) html += _sectionCard("Motivation Drivers", mdHtml);

  // --- Psychological Traits ---
  const psy = profile.psychological_traits || {};
  let psyHtml = "";
  [
    "impulsivity",
    "resilience",
    "self_esteem",
    "perfectionism",
    "assertiveness",
    "optimism",
    "emotional_stability",
    "open_mindedness",
  ].forEach((key) => {
    if (psy[key] != null)
      psyHtml += _metricRow(_prettyLabel(key), psy[key], "blue", key);
  });
  if (psyHtml) html += _sectionCard("Psychological Traits", psyHtml);

  // --- Communication Patterns ---
  const comm = profile.communication_patterns || {};
  let commHtml = "";
  [
    "verbosity",
    "emotional_expressiveness",
    "humor_usage",
    "formality_level",
    "directness",
  ].forEach((key) => {
    if (comm[key] != null)
      commHtml += _metricRow(_prettyLabel(key), comm[key], "blue", key);
  });
  if (commHtml) html += _sectionCard("Communication Patterns", commHtml);

  // --- Standalone metrics (locus of control, growth mindset, etc.) ---
  const standaloneKeys = [
    ["locus_of_control", "Locus of Control (0=external, 1=internal)"],
    ["growth_mindset", "Growth Mindset (0=fixed, 1=growth)"],
    ["risk_tolerance", "Risk Tolerance (0=averse, 1=seeking)"],
  ];
  let saHtml = "";
  standaloneKeys.forEach(([key, label]) => {
    if (profile[key] != null)
      saHtml += _metricRow(label, profile[key], "blue", key);
  });
  // Style-type fields
  const styleFields = [
    ["learning_style", "Learning Style"],
    ["conflict_resolution_style", "Conflict Resolution"],
    ["decision_making_style", "Decision Making"],
  ];
  styleFields.forEach(([key, label]) => {
    const s = profile[key];
    if (s && s.primary) {
      const confBadge =
        s.confidence != null
          ? ` <span class="conf-badge">${Math.round(s.confidence * 100)}% conf</span>`
          : "";
      saHtml += `<div style="margin:4px 0;"><strong>${escapeHtml(label)}:</strong> ${escapeHtml(_prettyLabel(String(s.primary)))}${confBadge}</div>`;
    }
  });
  // Time perspective
  const tp = profile.time_perspective || {};
  ["past_focus", "present_focus", "future_focus"].forEach((key) => {
    if (tp[key] != null)
      saHtml += _metricRow(_prettyLabel(key), tp[key], "blue", key);
  });
  if (saHtml) html += _sectionCard("Additional Dimensions", saHtml);

  // --- Therapeutic Recommendations ---
  const tr = profile.therapeutic_recommendations || {};
  let trHtml = "";
  if (tr.suggested_treatments && tr.suggested_treatments.length) {
    trHtml +=
      "<div><strong>Suggested Treatments:</strong></div>" +
      _bulletList(tr.suggested_treatments);
  }
  if (tr.therapy_modalities && tr.therapy_modalities.length) {
    trHtml +=
      '<div style="margin-top:6px;"><strong>Therapy Modalities:</strong></div>' +
      _bulletList(tr.therapy_modalities);
  }
  if (tr.handling_strategies && tr.handling_strategies.length) {
    trHtml +=
      '<div style="margin-top:6px;"><strong>Handling Strategies:</strong></div>' +
      _bulletList(tr.handling_strategies);
  }
  if (tr.communication_tips && tr.communication_tips.length) {
    trHtml +=
      '<div style="margin-top:6px;"><strong>Communication Tips:</strong></div>' +
      _bulletList(tr.communication_tips);
  }
  if (tr.sensitive_topics && tr.sensitive_topics.length) {
    trHtml +=
      '<div style="margin-top:6px;"><strong>Sensitive Topics:</strong></div>' +
      _bulletList(tr.sensitive_topics);
  }
  if (tr.strengths_to_leverage && tr.strengths_to_leverage.length) {
    trHtml +=
      '<div style="margin-top:6px;"><strong>Strengths to Leverage:</strong></div>' +
      _bulletList(tr.strengths_to_leverage);
  }
  if (trHtml) html += _sectionCard("Therapeutic Recommendations", trHtml);

  // --- Ideal Partner Profile ---
  const ip = profile.ideal_partner_profile || {};
  let ipHtml = "";
  if (ip.personality_traits && ip.personality_traits.length) {
    ipHtml +=
      "<div><strong>Personality Traits:</strong></div>" +
      _bulletList(ip.personality_traits);
  }
  if (ip.communication_style)
    ipHtml += `<div style="margin-top:6px;"><strong>Communication Style:</strong> ${escapeHtml(String(ip.communication_style))}</div>`;
  if (ip.values_alignment && ip.values_alignment.length) {
    ipHtml +=
      '<div style="margin-top:6px;"><strong>Values Alignment:</strong></div>' +
      _bulletList(ip.values_alignment);
  }
  if (ip.attachment_compatibility)
    ipHtml += `<div style="margin-top:6px;"><strong>Attachment Compatibility:</strong> ${escapeHtml(String(ip.attachment_compatibility))}</div>`;
  if (ip.deal_breakers && ip.deal_breakers.length) {
    ipHtml +=
      '<div style="margin-top:6px;"><strong>Deal Breakers:</strong></div>' +
      _bulletList(ip.deal_breakers);
  }
  if (ipHtml) html += _sectionCard("Ideal Partner Profile", ipHtml);

  // --- Career Recommendations ---
  const cr = profile.career_recommendations || {};
  let crHtml = "";
  if (cr.suitable_roles && cr.suitable_roles.length) {
    crHtml +=
      "<div><strong>Suitable Roles:</strong></div>" +
      _bulletList(cr.suitable_roles);
  }
  if (cr.work_environment)
    crHtml += `<div style="margin-top:6px;"><strong>Work Environment:</strong> ${escapeHtml(String(cr.work_environment))}</div>`;
  if (cr.skills_to_develop && cr.skills_to_develop.length) {
    crHtml +=
      '<div style="margin-top:6px;"><strong>Skills to Develop:</strong></div>' +
      _bulletList(cr.skills_to_develop);
  }
  if (cr.career_values && cr.career_values.length) {
    crHtml +=
      '<div style="margin-top:6px;"><strong>Career Values:</strong></div>' +
      _bulletList(cr.career_values);
  }
  if (crHtml) html += _sectionCard("Career Recommendations", crHtml);

  // --- Important Insights ---
  const ins = profile.important_insights || {};
  let insHtml = "";
  if (ins.user_should_know && ins.user_should_know.length) {
    insHtml +=
      "<div><strong>User Should Know:</strong></div>" +
      _bulletList(ins.user_should_know);
  }
  if (ins.how_to_communicate && ins.how_to_communicate.length) {
    insHtml +=
      '<div style="margin-top:6px;"><strong>How to Communicate:</strong></div>' +
      _bulletList(ins.how_to_communicate);
  }
  if (ins.timing_considerations && ins.timing_considerations.length) {
    insHtml +=
      '<div style="margin-top:6px;"><strong>Timing Considerations:</strong></div>' +
      _bulletList(ins.timing_considerations);
  }
  if (insHtml) html += _sectionCard("Important Insights", insHtml);

  // --- Array sections ---
  const arraySections = [
    ["blindspots", "Blindspots"],
    ["idiosyncrasies", "Idiosyncrasies"],
    ["interests_topics", "Interests & Topics"],
    ["coping_mechanisms", "Coping Mechanisms"],
    ["notable_strengths", "Notable Strengths"],
    ["areas_for_growth", "Areas for Growth"],
  ];
  arraySections.forEach(([key, label]) => {
    const arr = profile[key];
    if (arr && Array.isArray(arr) && arr.length) {
      html += _sectionCard(label, _bulletList(arr));
    }
  });

  return html;
}

async function reanalyzePsych() {
  const psychUser = document.getElementById("psych-user");
  if (!psychUser || !psychUser.value) {
    showToast("Select a user first", "warning");
    return;
  }
  const ok = await confirmDangerous(
    "Force Reanalysis",
    "This will re-run the full psychological analysis pipeline for this user. This may take a moment and consumes LLM resources. Continue?",
  );
  if (!ok) return;
  const progressEl = document.getElementById("psych-reanalyze-progress");
  const statusEl = document.getElementById("psych-reanalyze-status");
  if (progressEl) progressEl.value = 5;
  if (statusEl) statusEl.textContent = "Queued...";

  const { res, data } = await fetchJsonSafe(
    `/psych/${psychUser.value}/reanalyze`,
    { method: "POST" },
  );
  if (!res.ok) {
    if (progressEl) progressEl.value = 0;
    if (statusEl) statusEl.textContent = "";
    showToast(`Reanalysis failed: ${data.detail || res.status}`, "error");
    return;
  }
  showToast("Reanalysis started", "info");
  await pollPsychReanalysis(data.job_id);
  await loadPsychHistory();
  await loadPsych();
}

async function pollPsychReanalysis(jobId) {
  if (!jobId) return;
  const progressEl = document.getElementById("psych-reanalyze-progress");
  const statusEl = document.getElementById("psych-reanalyze-status");
  for (let i = 0; i < 180; i++) {
    const { res, data } = await fetchJsonSafe(
      `/psych/reanalyze/${encodeURIComponent(jobId)}`,
    );
    if (!res.ok) {
      if (statusEl)
        statusEl.textContent = `Status check failed: ${data.detail || res.status}`;
      return;
    }
    const pct = Number(data.progress || 0);
    if (progressEl) progressEl.value = Math.max(0, Math.min(100, pct));
    if (statusEl)
      statusEl.textContent = `${data.status || "running"}: ${data.detail || ""}`;
    if (["ok", "skipped", "error", "failed"].includes(String(data.status))) {
      if (data.status === "ok")
        showToast("Psych profile reanalysis completed", "success");
      else if (data.status === "skipped")
        showToast("Psych profile reanalysis skipped", "warning");
      else showToast("Psych profile reanalysis failed", "error");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  if (statusEl)
    statusEl.textContent = "Timed out waiting for reanalysis completion.";
}

function exportCurrentPsychProfile() {
  if (!currentPsychProfile || !currentPsychMeta) {
    showToast("Load a psych profile first", "warning");
    return;
  }
  const userId = document.getElementById("psych-user")?.value || "unknown";
  const payload = {
    user_id: Number(userId),
    profile_meta: currentPsychMeta,
    profile_data: currentPsychProfile,
    exported_at: new Date().toISOString(),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `psych_profile_user_${userId}_${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);
  showToast("Psych profile export downloaded", "success");
}

// ============================================================
// SYSTEM
// ============================================================
async function loadModuleStatus() {
  try {
    const res = await fetch("/status/modules");
    const data = await res.json();
    const el = document.getElementById("module-status");
    if (!el) return;
    el.innerHTML = "";
    Object.entries(data).forEach(([k, v]) => {
      const ok = v === "ok" || v === "running" || v === "active";
      const div = document.createElement("div");
      div.className = "card";
      div.innerHTML = `<div class="flex items-center gap-8"><span class="status-dot ${ok ? "ok" : v === "unknown" ? "unknown" : "error"}"></span><span style="font-weight:600;">${k}</span></div><div class="text-xs text-muted mt-8">${v}</div>`;
      el.appendChild(div);
    });
  } catch (e) {
    console.error("loadModuleStatus:", e);
  }
}

async function loadScheduler() {
  try {
    const res = await fetch("/status/scheduler");
    if (!res.ok) return;
    const data = await res.json();
    const el = document.getElementById("scheduler-status");
    if (!el) return;
    if (data.mode === "external") {
      const ev = data.evidence || {};
      const lines = [
        "Mode: external runtime (bot process)",
        "",
        "Inferred Activity:",
        `  Last message:     ${ev.last_message ? formatDateTime(ev.last_message) : "none"}`,
        `  Last sentiment:   ${ev.last_sentiment_analysis ? formatDateTime(ev.last_sentiment_analysis) : "none"}`,
        `  Last psych run:   ${ev.last_psych_profile ? formatDateTime(ev.last_psych_profile) : "none"}`,
        `  Active reminders: ${ev.active_reminders ?? 0}`,
        `  Open moderation:  ${ev.open_moderation_events ?? 0}`,
      ];
      el.textContent = lines.join("\n");
      return;
    }
    const jobs = data.jobs || [];
    if (!jobs.length) {
      el.textContent = `Mode: ${data.mode || "local"}\nRunning: ${data.running ? "yes" : "no"}\nNo jobs scheduled in this process.`;
      return;
    }
    const lines = [
      `Mode: ${data.mode || "local"}`,
      `Running: ${data.running ? "yes" : "no"}`,
      "",
      "Jobs:",
      ...jobs.map(
        (j) =>
          `- ${j.id} | next: ${j.next_run ? formatDateTime(j.next_run) : "-"} | ${j.trigger}`,
      ),
    ];
    el.textContent = lines.join("\n");
  } catch (e) {
    console.error("loadScheduler:", e);
  }
}

async function loadTelegram() {
  try {
    const res = await fetch("/status/telegram");
    if (!res.ok) return;
    const data = await res.json();
    const statusEl = document.getElementById("telegram-status");
    const recentEl = document.getElementById("telegram-recent");
    if (statusEl) {
      const lines = [
        `Status: ${data.status || "unknown"}`,
        `Last Message: ${data.last_message_at ? formatDateTime(data.last_message_at) : "never"}`,
      ];
      if (data.error) lines.push(`Error: ${data.error}`);
      statusEl.textContent = lines.join("\n");
    }
    if (!recentEl) return;
    const rows = data.recent_messages || [];
    if (!rows.length) {
      recentEl.innerHTML =
        '<div class="text-muted text-sm">No recent messages.</div>';
      return;
    }
    recentEl.innerHTML = "";
    rows.forEach((r) => {
      const roleColor = r.role === "user" ? "#93c5fd" : "#a78bfa";
      const sentiment = [
        r.emotion_label ? `emotion=${r.emotion_label}` : null,
        r.valence !== null && r.valence !== undefined
          ? `val=${Number(r.valence).toFixed(2)}`
          : null,
        r.arousal !== null && r.arousal !== undefined
          ? `aro=${Number(r.arousal).toFixed(2)}`
          : null,
        r.dominance !== null && r.dominance !== undefined
          ? `dom=${Number(r.dominance).toFixed(2)}`
          : null,
        r.confidence !== null && r.confidence !== undefined
          ? `conf=${Number(r.confidence).toFixed(2)}`
          : null,
      ]
        .filter(Boolean)
        .join(" | ");

      const card = document.createElement("div");
      card.className = "card mb-8";
      card.innerHTML = `
        <div class="flex items-center justify-between">
          <div>
            <span style="font-weight:600;color:${roleColor};">${escapeHtml(r.role || "-")}</span>
            <span class="text-xs text-muted" style="margin-left:8px;">${escapeHtml(r.user_name || getUserDisplayName(r.user_id))}</span>
          </div>
          <span class="text-xs text-muted">${formatDateTime(r.timestamp)}</span>
        </div>
        <div class="text-sm mt-8" style="white-space:pre-wrap;">${escapeHtml((r.content || "").slice(0, 500))}</div>
        <div class="text-xs text-muted mt-8">${escapeHtml(sentiment || "No sentiment ratings")}</div>
      `;
      recentEl.appendChild(card);
    });
  } catch (e) {
    console.error("loadTelegram:", e);
  }
}

async function loadModels() {
  try {
    const [currentRes, availRes] = await Promise.all([
      fetch("/models"),
      fetch("/models/ollama"),
    ]);
    const current = await currentRes.json();
    const available = await availRes.json();
    const names = (available.models || []).map((m) => m.name);

    function hydrateModelSelect(elId, selected) {
      const el = document.getElementById(elId);
      if (!el) return;
      el.innerHTML = "";
      if (selected && !names.includes(selected)) {
        const opt = document.createElement("option");
        opt.value = selected;
        opt.textContent = selected + " (current)";
        el.appendChild(opt);
      }
      names.forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        el.appendChild(opt);
      });
      el.value = selected || "";
    }

    hydrateModelSelect("model-chat", current.chat_model || "");
    hydrateModelSelect("model-embed", current.embed_model || "");
    hydrateModelSelect("model-vision", current.vision_model || "");

    hydrateModelSelect("model-planner", current.planner_model || "");

    const el = document.getElementById("model-results");
    if (el)
      el.textContent = JSON.stringify(
        { current, available_count: names.length },
        null,
        2,
      );
  } catch (e) {
    console.error("loadModels:", e);
  }
}

async function saveModels() {
  const body = {
    chat_model: document.getElementById("model-chat")?.value || undefined,
    embed_model: document.getElementById("model-embed")?.value || undefined,
    vision_model: document.getElementById("model-vision")?.value || undefined,
  };
  try {
    const res = await fetch("/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    const el = document.getElementById("model-results");
    if (el) el.textContent = JSON.stringify(data, null, 2);
    if (res.ok) {
      showToast("Model configuration saved! Restart bot to apply.", "success");
      loadModels(); // refresh dropdowns to confirm saved values
    } else showToast(`Failed: ${data.detail || res.status}`, "error");
  } catch (e) {
    showToast(`Error: ${e.message}`, "error");
  }
}

async function savePlannerModel() {
  const val = (document.getElementById("model-planner")?.value || "").trim();
  const resultEl = document.getElementById("planner-model-result");
  if (!val) {
    if (resultEl) resultEl.textContent = "Enter a model name first.";
    return;
  }
  try {
    const res = await fetch("/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ planner_model: val }),
    });
    const data = await res.json();
    if (resultEl) resultEl.textContent = res.ok ? `Saved: ${data.planner_model}` : `Error: ${data.detail || res.status}`;
    if (res.ok) showToast("Planner model saved! Restart bot to apply.", "success");
    else showToast(`Failed: ${data.detail || res.status}`, "error");
  } catch (e) {
    if (resultEl) resultEl.textContent = `Error: ${e.message}`;
    showToast(`Error: ${e.message}`, "error");
  }
}

let _shadowRows = [];

function _fmtSentiment(summary) {
  if (!summary || typeof summary !== "object") return "(none)";
  // Keys that actually live in heuristic_summary / llm_summary
  const keys = [
    "primary_intent", "sentiment_priority", "emotion_label",
    "crisis_risk", "scheduled_event", "timing_question_ok",
    "allow_reminder_action", "allow_media_action",
    "needs_rag", "needs_live_search_now", "needs_live_search_followup",
    "clarification_required", "clarification_text",
    "search_query", "search_reason",
  ];
  return keys.map((k) => {
    const v = summary[k];
    return `${k}: ${v !== undefined && v !== null ? v : "—"}`;
  }).join("\n");
}

async function loadPlannerShadow() {
  const limit = parseInt(document.getElementById("shadow-limit")?.value || "50", 10) || 50;
  const tbody = document.getElementById("shadow-tbody");
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="padding:8px;color:#94a3b8;">Loading…</td></tr>';
  try {
    const res = await fetch(`/planner/shadow?limit=${limit}`);
    if (!res.ok) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="7">Error ${res.status}</td></tr>`;
      return;
    }
    const data = await res.json();
    _shadowRows = data.rows || [];
    if (!_shadowRows.length) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="padding:8px;color:#94a3b8;">No shadow records yet — shadow mode needs llm_turn_planner_shadow=true and at least one processed message.</td></tr>';
      return;
    }
    if (tbody) {
      tbody.innerHTML = _shadowRows.map((r, i) => {
        const mismatches = (r.mismatch_fields || []).join(", ") || "none";
        const hMs = r.heuristic_latency_ms != null ? `${r.heuristic_latency_ms}ms` : "—";
        const lMs = r.llm_latency_ms != null ? `${r.llm_latency_ms}ms` : "—";
        const ts = r.created_at ? new Date(r.created_at).toLocaleString() : "—";
        const msg = (r.user_text || "").slice(0, 80).replace(/</g, "&lt;");
        const mismatchColor = (r.mismatch_fields || []).length > 0 ? "#f59e0b" : "#10b981";
        return `<tr data-idx="${i}" style="cursor:pointer;border-bottom:1px solid #1f2937;">
          <td style="padding:6px 8px;">${ts}</td>
          <td style="padding:6px 8px;">${r.user_id || "—"}</td>
          <td style="padding:6px 8px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${msg}</td>
          <td style="padding:6px 8px;">${hMs}</td>
          <td style="padding:6px 8px;">${lMs}</td>
          <td style="padding:6px 8px;color:${mismatchColor}">${mismatches}</td>
          <td style="padding:6px 8px;">${r.planner_source || "—"}</td>
        </tr>`;
      }).join("");
      tbody.querySelectorAll("tr[data-idx]").forEach((tr) => {
        tr.addEventListener("click", () => {
          // Clear previous selection
          tbody.querySelectorAll("tr[data-idx]").forEach((r) => r.style.background = "");
          tr.style.background = "#1e3a5f";
          const row = _shadowRows[parseInt(tr.dataset.idx, 10)];
          if (!row) return;
          const detail = document.getElementById("shadow-detail");
          const hEl = document.getElementById("shadow-heuristic-detail");
          const lEl = document.getElementById("shadow-llm-detail");
          const modelLabel = document.getElementById("shadow-llm-model-label");
          if (hEl) hEl.textContent = _fmtSentiment(row.heuristic_summary);
          if (lEl) lEl.textContent = _fmtSentiment(row.llm_summary);
          if (modelLabel) modelLabel.textContent = row.llm_model || "—";
          if (detail) detail.style.display = "";
          detail && detail.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      });
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="7">Error: ${e.message}</td></tr>`;
  }
}

// LLM Defaults management
const _llmParams = [
  "temperature",
  "top_p",
  "top_k",
  "repeat_penalty",
  "num_ctx",
  "num_predict",
];

async function loadLLMDefaults() {
  try {
    const res = await fetch("/admin/llm-defaults");
    const data = await res.json();
    _llmParams.forEach((p) => {
      const stdEl = document.getElementById(`llm-std-${p}`);
      const dbEl = document.getElementById(`llm-db-${p}`);
      if (stdEl) stdEl.value = data.standard?.[p] ?? "";
      if (dbEl) dbEl.value = data.downbad?.[p] ?? "";
    });
  } catch (e) {
    console.error("loadLLMDefaults:", e);
  }
}

async function saveLLMDefaults() {
  const standard = {};
  const downbad = {};
  _llmParams.forEach((p) => {
    const sv = document.getElementById(`llm-std-${p}`)?.value;
    const dv = document.getElementById(`llm-db-${p}`)?.value;
    if (sv !== "" && sv != null) standard[p] = parseFloat(sv);
    if (dv !== "" && dv != null) downbad[p] = parseFloat(dv);
  });
  try {
    const res = await fetch("/admin/llm-defaults", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ standard, downbad }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(
        "LLM defaults saved! Takes effect on next user message.",
        "success",
      );
      const st = document.getElementById("llm-defaults-status");
      if (st) st.textContent = "Saved " + new Date().toLocaleTimeString();
    } else {
      showToast(`Failed: ${data.detail || res.status}`, "error");
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, "error");
  }
}

function pullModel() {
  const nameEl = document.getElementById("model-pull-name");
  const name = (nameEl?.value || "").trim();
  if (!name) {
    showToast("Enter a model name/tag to pull", "warning");
    return;
  }
  const progress = document.getElementById("model-pull-progress");
  const status = document.getElementById("model-pull-status");
  if (progress) progress.value = 0;
  if (status) status.textContent = "Starting pull...";
  const src = new EventSource(
    `/models/pull/stream?model=${encodeURIComponent(name)}`,
  );
  src.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (typeof msg.progress === "number" && progress)
        progress.value = msg.progress;
      if (status) status.textContent = msg.message || msg.status || "";
      if (msg.status === "completed") {
        src.close();
        showToast(`Model ${name} pulled successfully`, "success");
        loadModels();
      } else if (msg.status === "error") {
        src.close();
        showToast(`Pull failed: ${msg.message}`, "error");
      }
    } catch (err) {
      if (status) status.textContent = evt.data;
    }
  };
  src.onerror = () => {
    src.close();
  };
}

// ============================================================
// TOOLS
// ============================================================
async function loadRemindersForSelectedUser() {
  const sel = document.getElementById("reminder-user-select");
  const listEl = document.getElementById("reminder-list");
  const idInput = document.getElementById("reminder-id-input");
  if (!sel || !sel.value) {
    if (listEl) listEl.textContent = "Select a user first.";
    return;
  }
  const uid = sel.value;
  try {
    const res = await fetch(`/users/${uid}/reminders`);
    const data = await res.json();
    if (!res.ok) {
      if (listEl) listEl.textContent = `Error: ${data.detail || res.status}`;
      return;
    }
    const reminders = data.reminders || [];
    if (idInput) {
      idInput.innerHTML = "";
      reminders.forEach((r) => {
        const opt = document.createElement("option");
        const payload = parseMaybeJson(r.metadata || r.payload, {});
        const label = payload.text || r.text || r.kind || "Reminder";
        opt.value = r.id;
        opt.textContent = `${label} | due ${r.next_run_at} | ${r.enabled ? "enabled" : "disabled"}`;
        idInput.appendChild(opt);
      });
    }
    if (listEl) {
      if (!reminders.length) {
        listEl.textContent = "No reminders for this user.";
      } else {
        const lines = reminders.map((r, idx) => {
          const payload = parseMaybeJson(r.metadata || r.payload, {});
          const label =
            payload.text || r.text || r.kind || `Reminder ${idx + 1}`;
          const mode =
            payload.mode ||
            (r.cadence_cron ? "recurring_exact" : "one_off_exact");
          const lastDelivered = r.last_delivered_at || "never";
          return `${idx + 1}. ${label}\n   id=${r.id} | due=${r.next_run_at || r.due_at || "-"} | cadence=${r.cadence_cron || "once"} | mode=${mode} | enabled=${r.enabled ? "yes" : "no"} | last_sent=${lastDelivered}`;
        });
        listEl.textContent = lines.join("\n\n");
      }
    }
  } catch (e) {
    if (listEl) listEl.textContent = `Error: ${e.message}`;
  }
}

async function reminderAction(url, body = {}) {
  const resultEl = document.getElementById("reminder-action-result");
  if (resultEl) resultEl.textContent = "";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (resultEl)
      resultEl.textContent = res.ok
        ? JSON.stringify(data)
        : `Error: ${data.detail || res.status}`;
    if (res.ok) showToast("Reminder action completed", "success");
  } catch (e) {
    if (resultEl) resultEl.textContent = `Error: ${e.message}`;
  }
}

async function createReminderFromForm() {
  const sel = document.getElementById("reminder-user-select");
  const textInput = document.getElementById("reminder-text-input");
  if (!sel?.value) {
    showToast("Select a user", "warning");
    return;
  }
  if (!textInput?.value) {
    showToast("Reminder text is required", "warning");
    return;
  }
  const timeInput = document.getElementById("reminder-time-input");
  const modeInput = document.getElementById("reminder-mode-input");
  const cronInput = document.getElementById("reminder-cron-input");
  const fuzzInput = document.getElementById("reminder-fuzz-minutes");
  const enabledInput = document.getElementById("reminder-enabled-input");
  const dt = timeInput?.value
    ? new Date(timeInput.value).toISOString()
    : new Date().toISOString();
  const mode = modeInput?.value || "one_off_exact";
  const metadata = { text: textInput.value, mode };
  if (fuzzInput?.value) metadata.fuzz_minutes = parseInt(fuzzInput.value, 10);
  if (mode.includes("fuzzy")) metadata.fuzzy = true;
  const payload = {
    user_id: String(sel.value),
    text: textInput.value,
    next_run_at: dt,
    cadence_cron: cronInput?.value || null,
    enabled: enabledInput?.checked ?? true,
    metadata,
  };
  const res = await fetch("/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  const resultEl = document.getElementById("reminder-action-result");
  if (resultEl)
    resultEl.textContent = res.ok
      ? JSON.stringify(data, null, 2)
      : `Error: ${data.detail || res.status}`;
  if (res.ok) {
    showToast("Reminder created", "success");
    await loadRemindersForSelectedUser();
  }
}

async function clearAllReminders() {
  const dangerToggle = document.getElementById("danger-toggle");
  if (!dangerToggle?.checked) {
    showToast("Enable dangerous tools first", "warning");
    return;
  }
  const ok = await confirmDangerous(
    "Clear All Reminders",
    "Delete ALL reminders for ALL users? This cannot be undone.",
  );
  if (!ok) return;
  const res = await fetch("/reminders/clear_all", { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showToast(`Error: ${data.detail || res.status}`, "error");
    return;
  }
  showToast(`Cleared ${data.deleted || 0} reminder(s)`, "warning");
  loadRemindersForSelectedUser();
}

async function memorySearch() {
  const q = document.getElementById("memory-query")?.value;
  if (!q) {
    showToast("Query required", "warning");
    return;
  }
  const params = new URLSearchParams();
  params.set("q", q);
  const user = document.getElementById("memory-user-select")?.value;
  const since = document.getElementById("memory-since")?.value;
  const until = document.getElementById("memory-until")?.value;
  const limit = document.getElementById("memory-limit")?.value;
  const role = document.getElementById("memory-role-filter")?.value;
  if (user) params.set("user_id", user);
  if (role) params.set("role", role);
  if (since) params.set("since", since);
  if (until) params.set("until", until);
  if (limit) params.set("limit", limit);
  const res = await fetch(`/memory/search?${params.toString()}`);
  const data = await res.json();
  const el = document.getElementById("memory-results");
  if (el)
    el.textContent = res.ok
      ? JSON.stringify(data, null, 2)
      : `Error: ${data.detail || res.status}`;
}

async function exportUserData() {
  const userId = document.getElementById("export-user-select")?.value;
  if (!userId) {
    showToast("Select a user", "warning");
    return;
  }
  const params = new URLSearchParams();
  const since = document.getElementById("export-since")?.value;
  const until = document.getElementById("export-until")?.value;
  const limit = document.getElementById("export-limit")?.value;
  if (since) params.set("since", since);
  if (until) params.set("until", until);
  if (limit) params.set("limit", limit);
  const res = await fetch(`/export/user/${userId}?${params.toString()}`);
  const data = await res.json();
  const el = document.getElementById("export-results");
  if (el)
    el.textContent = res.ok
      ? JSON.stringify(data, null, 2)
      : `Error: ${data.detail || res.status}`;
}

// ============================================================
// FEEDBACK
// ============================================================
async function loadFeedback() {
  const status = document.getElementById("feedback-status")?.value;
  const limit = document.getElementById("feedback-limit")?.value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (limit) params.set("limit", limit);

  try {
    const res = await fetch(`/feedback?${params.toString()}`);
    const data = await res.json();
    const container = document.getElementById("feedback-results");
    if (!container) return;

    const items = data.feedback || data || [];
    if (!Array.isArray(items) || items.length === 0) {
      container.innerHTML =
        '<div class="empty-state"><div class="empty-text">No feedback found</div></div>';
      return;
    }

    // Status counts
    const counts = {
      new: 0,
      reviewing: 0,
      triage: 0,
      resolved: 0,
      wont_fix: 0,
    };
    items.forEach((f) => {
      if (counts[f.status] !== undefined) counts[f.status]++;
    });
    container.innerHTML = `<div class="flex gap-8 flex-wrap mb-8 text-xs">
      ${Object.entries(counts)
        .filter(([, v]) => v > 0)
        .map(([k, v]) => `<span>${renderStatusBadge(k)} ${v}</span>`)
        .join("")}
    </div>`;

    // Cards
    items.forEach((f) => {
      const card = document.createElement("div");
      card.className = "card";
      card.style.marginBottom = "8px";
      card.innerHTML = `
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-8">
            <span style="font-weight:600;">#${f.id}</span>
            ${renderStatusBadge(f.status)}
            <span class="badge-status ${f.feedback_type === "bug" ? "badge-open" : "badge-new"}">${f.feedback_type || "feedback"}</span>
          </div>
          <span class="text-xs text-muted">${formatDateTime(f.created_at)}</span>
        </div>
        <div class="text-sm mt-8">${getUserDisplayName(f.user_id)}: ${f.content || ""}</div>
        ${f.admin_notes ? `<div class="text-xs text-muted mt-8" style="font-style:italic;">Admin: ${f.admin_notes}</div>` : ""}
        <div class="flex gap-8 mt-8">
          <select class="feedback-status-sel" data-id="${f.id}" style="width:auto;padding:3px 8px;font-size:11px;">
            <option value="">Status...</option>
            <option value="new">New</option>
            <option value="triage">Triage</option>
            <option value="reviewing">Reviewing</option>
            <option value="resolved">Resolved</option>
            <option value="wont_fix">Won't Fix</option>
          </select>
          <button class="xs secondary" onclick="updateFeedbackInline(${f.id})">Update</button>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (e) {
    console.error("loadFeedback:", e);
  }
}

async function updateFeedbackInline(feedbackId) {
  const sel = document.querySelector(
    `.feedback-status-sel[data-id="${feedbackId}"]`,
  );
  const newStatus = sel?.value;
  const notes = await showModal(
    "Update Feedback",
    `Update feedback #${feedbackId}`,
    {
      input: true,
      inputPlaceholder: "Admin notes (optional)",
      confirmText: "Update",
    },
  );
  if (notes === null) return;
  const body = {};
  if (notes) body.admin_notes = notes;
  if (newStatus) body.status = newStatus;
  const res = await fetch(`/feedback/${feedbackId}/update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    showToast("Feedback updated", "success");
    loadFeedback();
  } else {
    const d = await res.json();
    showToast(`Failed: ${d.detail || res.status}`, "error");
  }
}

// ============================================================
// ENHANCED LLM CONSOLE
// ============================================================
function addConsoleMessage(role, content, toolExecutions = []) {
  const history = document.getElementById("console-chat-history");
  if (!history) return;
  const msgDiv = document.createElement("div");
  msgDiv.className = "console-msg";

  const roleLabel = document.createElement("div");
  roleLabel.className = `role-label ${role}`;
  roleLabel.textContent =
    { user: "You:", assistant: "Assistant:", system: "System:" }[role] || role;

  const contentDiv = document.createElement("div");
  contentDiv.className = "content";
  // Basic markdown-ish rendering for assistant messages
  if (role === "assistant") {
    contentDiv.innerHTML = basicMarkdown(content);
  } else {
    contentDiv.textContent = content;
  }

  msgDiv.appendChild(roleLabel);
  msgDiv.appendChild(contentDiv);

  if (toolExecutions && toolExecutions.length > 0) {
    const toolsDiv = document.createElement("div");
    toolsDiv.className = "tool-badges";
    toolExecutions.forEach((tool) => {
      const badge = document.createElement("span");
      badge.className = `tool-badge ${tool.success ? "success" : "failure"}`;
      badge.textContent = `${tool.success ? "\u2713" : "\u2717"} ${tool.tool}`;
      toolsDiv.appendChild(badge);
    });
    msgDiv.appendChild(toolsDiv);
  }

  history.appendChild(msgDiv);
  history.scrollTop = history.scrollHeight;
}

function basicMarkdown(text) {
  // Very basic markdown: code blocks, inline code, bold
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(
      /```(\w*)\n?([\s\S]*?)```/g,
      '<pre style="background:#1e293b;padding:8px;border-radius:4px;margin:6px 0;font-size:12px;">$2</pre>',
    )
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

async function sendConsoleMessage() {
  const input = document.getElementById("console-input");
  const message = input?.value.trim();
  if (!message || consoleProcessing) return;
  consoleProcessing = true;
  const sendBtn = document.getElementById("btn-console-send");
  const stopBtn = document.getElementById("btn-console-stop");
  if (sendBtn) sendBtn.disabled = true;
  if (stopBtn) stopBtn.disabled = false;
  addConsoleMessage("user", message);
  input.value = "";
  try {
    const body = {
      message,
      session_id: consoleSessionId,
      tools_enabled:
        document.getElementById("console-tools-enabled")?.checked ?? true,
      allow_external_files:
        document.getElementById("console-external-files")?.checked ?? false,
      max_iterations: 5,
    };
    const res = await fetch("/highrisk/llm_console_enhanced", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      addConsoleMessage("system", `Error: ${data.detail || res.statusText}`);
      return;
    }
    consoleSessionId = data.session_id;
    const info = document.getElementById("console-session-info");
    if (info)
      info.textContent = `Session: ${data.session_id} | Messages: ${data.conversation_length}`;
    addConsoleMessage("assistant", data.response, data.tool_executions);
    if (data.rolled_back_edits && data.rolled_back_edits.length > 0) {
      addConsoleMessage(
        "system",
        `Auto-rolled back ${data.rolled_back_edits.length} expired edit(s)`,
      );
    }
  } catch (err) {
    addConsoleMessage("system", `Error: ${err.message}`);
  } finally {
    consoleProcessing = false;
    if (sendBtn) sendBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;
  }
}

async function clearConsoleHistory() {
  const ok = await confirmDangerous(
    "Clear Console",
    "Clear the conversation history?",
  );
  if (!ok) return;
  try {
    await fetch(
      `/highrisk/llm_console_clear?session_id=${consoleSessionId || ""}`,
      { method: "POST" },
    );
    const history = document.getElementById("console-chat-history");
    if (history)
      history.innerHTML =
        '<div class="text-muted" style="font-style:italic;">Console cleared. Type a message to begin...</div>';
    consoleSessionId = null;
    const info = document.getElementById("console-session-info");
    if (info) info.textContent = "";
  } catch (e) {
    showToast(`Failed to clear: ${e.message}`, "error");
  }
}

async function viewConsoleSessions() {
  try {
    const res = await fetch("/highrisk/llm_console_sessions");
    const data = await res.json();
    const sessions = data.sessions || [];
    if (sessions.length === 0) {
      showToast("No active sessions", "info");
      return;
    }
    const msg = sessions
      .map(
        (s) =>
          `${s.session_id}: ${s.message_count} messages\n  "${s.last_message}"`,
      )
      .join("\n\n");
    await alertModal("Active Sessions", msg.replace(/\n/g, "<br>"));
  } catch (e) {
    showToast(`Failed: ${e.message}`, "error");
  }
}

// ============================================================
// MISC / DB Stats (absorbed into dashboard)
// ============================================================
async function loadDBStats() {
  try {
    const res = await fetch("/stats/db");
    const data = await res.json();
    const el = document.getElementById("db-stats-content");
    if (!el) return;
    el.innerHTML = "";
    const entries = [
      ["Users", data.users, "blue"],
      ["Messages", data.messages, "purple"],
      ["Reminders (enabled)", data.reminders_enabled, "amber"],
      ["Sentiments", data.sentiments, "green"],
      ["Open Crises", data.crises_open, data.crises_open > 0 ? "red" : "green"],
      ["Image Uploads", data.images, "blue"],
    ];
    entries.forEach(([label, val, color]) => {
      el.innerHTML += `<div class="metric-bar">
        <div class="bar-label">${label}</div>
        <div class="bar-value" style="min-width:auto;">${val ?? "error"}</div>
      </div>`;
    });
  } catch (e) {
    console.error("loadDBStats:", e);
  }
}

// ============================================================
// USER NAMES LOADER (populates all user selectors)
// ============================================================
async function loadUserNames() {
  try {
    const res = await fetch("/users/names");
    const data = await res.json();
    if (!res.ok) return;
    const users = data.users || [];
    userNameMap = {};
    users.forEach((u) => {
      userNameMap[String(u.id)] = u.name;
    });

    const targets = [
      { id: "psych-user", blank: false },
      { id: "reminder-user-select", blank: false },
      {
        blank: true,
        blankText: "Admin Preview (use admin-linked owner)",
      },
      { id: "user-analytics-user", blank: true, blankText: "All Users" },
      { id: "graphs-user", blank: true, blankText: "All Users" },
      { id: "memory-user-select", blank: true, blankText: "All Users" },
      { id: "export-user-select", blank: false },
    ];
    targets.forEach((t) => {
      const sel = document.getElementById(t.id);
      if (!sel) return;
      const prev = sel.value;
      sel.innerHTML = t.blank
        ? `<option value="">${t.blankText || "All"}</option>`
        : "";
      users.forEach((u) => {
        const opt = document.createElement("option");
        opt.value = u.id;
        opt.textContent = u.name;
        sel.appendChild(opt);
      });
      if (
        prev &&
        Array.from(sel.options).some((o) => String(o.value) === String(prev))
      ) {
        sel.value = prev;
      }
    });

    if (users.length > 0 && !document.getElementById("psych-user")?.value) {
      const el = document.getElementById("psych-user");
      if (el) el.value = users[0].id;
    }
    ["export-user-select", "reminder-user-select"].forEach(
      (id) => {
        const el = document.getElementById(id);
        if (el && !el.value) el.value = String(users[0]?.id || "");
      },
    );
    await loadPsychHistory();
  } catch (e) {
    console.error("loadUserNames:", e);
  }
}

// ============================================================
// GLOBAL EVENT WIRING
// ============================================================
function wireEvents() {
  // Tab switching
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  window.addEventListener("hashchange", () => {
    const hash = window.location.hash.slice(1);
    if (hash) switchTab(hash);
  });

  // Sub-tab switching
  document.querySelectorAll(".subtab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const container = btn.closest(".tab-content");
      switchSubtab(container, btn.dataset.subtab);
      // Lazy load subtab data
      if (btn.dataset.subtab === "user-analytics") loadUserAnalytics();
      if (btn.dataset.subtab === "graphs") loadGraphs();
    });
  });

  // Dashboard
  document
    .getElementById("btn-refresh-status")
    ?.addEventListener("click", () => {
      loadStatus();
      loadModuleStatus();
      loadDBStats();
    });
  document
    .getElementById("btn-restart")
    ?.addEventListener("click", async () => {
      if (!document.getElementById("danger-toggle")?.checked) {
        showToast("Enable dangerous tools first", "warning");
        return;
      }
      const ok = await confirmDangerous(
        "Restart Bot",
        "Request a bot restart?",
      );
      if (!ok) return;
      const res = await fetch("/actions/restart", { method: "POST" });
      if (res.ok) showToast("Restart requested", "success");
      else showToast("Failed to restart", "error");
    });
  document
    .getElementById("btn-disable")
    ?.addEventListener("click", async () => {
      if (!document.getElementById("danger-toggle")?.checked) {
        showToast("Enable dangerous tools first", "warning");
        return;
      }
      const ok = await confirmDangerous(
        "Disable Bot",
        "Disable bot interactions?",
      );
      if (!ok) return;
      const res = await fetch("/actions/disable_bot", { method: "POST" });
      showToast(
        res.ok ? "Disable requested" : "Failed",
        res.ok ? "warning" : "error",
      );
    });
  document.getElementById("btn-enable")?.addEventListener("click", async () => {
    const res = await fetch("/actions/enable_bot", { method: "POST" });
    showToast(
      res.ok ? "Enable requested" : "Failed",
      res.ok ? "success" : "error",
    );
  });
  document
    .getElementById("btn-shutdown-admin")
    ?.addEventListener("click", async () => {
      const ok = await confirmDangerous(
        "Stop Admin",
        "Stop the admin server? You will lose access to this page.",
      );
      if (!ok) return;
      await fetch("/actions/shutdown_admin", { method: "POST" });
      showToast("Admin shutting down...", "warning");
    });
  document
    .getElementById("btn-broadcast")
    ?.addEventListener("click", async () => {
      if (!document.getElementById("danger-toggle")?.checked) {
        showToast("Enable dangerous tools first", "warning");
        return;
      }
      const text = document.getElementById("broadcast-text")?.value;
      const dry = document.getElementById("broadcast-dryrun")?.checked;
      if (!text) {
        showToast("Enter a message", "warning");
        return;
      }
      const ok = await confirmDangerous(
        "Broadcast",
        `${dry ? "[DRY RUN] " : ""}Send message to all users?`,
      );
      if (!ok) return;
      const res = await fetch("/actions/broadcast", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, dry_run: dry }),
      });
      const data = await res.json();
      showToast(`Broadcast: ${data.status} (${data.targets} targets)`, "info");
    });

  // Feed controls
  document.getElementById("btn-feed-pause")?.addEventListener("click", () => {
    feedPaused = !feedPaused;
    const btn = document.getElementById("btn-feed-pause");
    if (btn) btn.textContent = feedPaused ? "Resume Feed" : "Pause Feed";
    if (!feedPaused && feedBuffer.length > 0) {
      const feedEl = document.getElementById("live-feed");
      feedBuffer.forEach((msg) => {
        const div = document.createElement("div");
        div.textContent = msg;
        feedEl.appendChild(div);
      });
      feedEl.scrollTop = feedEl.scrollHeight;
      feedBuffer = [];
    }
  });

  // Users
  document.getElementById("btn-users-next")?.addEventListener("click", () => {
    usersOffset += usersLimit;
    loadUsers();
  });
  document.getElementById("btn-users-prev")?.addEventListener("click", () => {
    usersOffset = Math.max(0, usersOffset - usersLimit);
    loadUsers();
  });
  document
    .getElementById("btn-users-delete-selected")
    ?.addEventListener("click", deleteSelectedUsers);
  document
    .getElementById("btn-users-select-all")
    ?.addEventListener("click", () => {
      const boxes = document.querySelectorAll(".user-select-box");
      const shouldCheck = Array.from(boxes).some((b) => !b.checked);
      boxes.forEach((box) => {
        box.checked = shouldCheck;
        const uid = parseInt(box.dataset.userId, 10);
        if (shouldCheck) selectedUserIds.add(uid);
        else selectedUserIds.delete(uid);
      });
    });
  document.getElementById("users-search")?.addEventListener("input", (e) => {
    usersSearchTerm = e.target.value;
    usersOffset = 0;
    loadUsers();
  });

  // Moderation
  document
    .getElementById("btn-moderation-load")
    ?.addEventListener("click", loadModeration);

  // Analytics
  document
    .getElementById("btn-latency-refresh")
    ?.addEventListener("click", loadLatencyLive);
  document
    .getElementById("window-hours")
    ?.addEventListener("change", () => loadAppMetrics());
  document
    .getElementById("btn-user-analytics-refresh")
    ?.addEventListener("click", loadUserAnalytics);
  document
    .getElementById("user-analytics-user")
    ?.addEventListener("change", loadUserAnalytics);
  document
    .getElementById("btn-graphs-refresh")
    ?.addEventListener("click", loadGraphs);
  document
    .getElementById("graphs-user")
    ?.addEventListener("change", loadGraphs);
  document
    .getElementById("graphs-range")
    ?.addEventListener("change", loadGraphs);

  // Psych
  document
    .getElementById("btn-psych-load")
    ?.addEventListener("click", loadPsych);
  document
    .getElementById("btn-psych-reanalyze")
    ?.addEventListener("click", reanalyzePsych);
  document
    .getElementById("btn-psych-export")
    ?.addEventListener("click", exportCurrentPsychProfile);
  document
    .getElementById("psych-user")
    ?.addEventListener("change", async () => {
      await loadPsychHistory();
      await loadPsych();
    });
  document
    .getElementById("psych-history")
    ?.addEventListener("change", loadPsych);

  // Crisis (inside moderation)
  document
    .getElementById("btn-crisis-refresh")
    ?.addEventListener("click", loadCrisisAlerts);
  document
    .getElementById("btn-crisis-select-all")
    ?.addEventListener("click", () => selectAllVisible("crisis"));
  document
    .getElementById("btn-crisis-select-none")
    ?.addEventListener("click", () => clearSelections("crisis"));
  document
    .getElementById("btn-crisis-resolve-selected")
    ?.addEventListener("click", () => bulkModerationAction("crisis", "resolve"));
  document
    .getElementById("btn-crisis-delete-selected")
    ?.addEventListener("click", () => bulkModerationAction("crisis", "delete"));
  document
    .getElementById("btn-moderation-select-all")
    ?.addEventListener("click", () => selectAllVisible("moderation"));
  document
    .getElementById("btn-moderation-select-none")
    ?.addEventListener("click", () => clearSelections("moderation"));
  document
    .getElementById("btn-moderation-resolve-selected")
    ?.addEventListener("click", () =>
      bulkModerationAction("moderation", "resolve"),
    );
  document
    .getElementById("btn-moderation-delete-selected")
    ?.addEventListener("click", () =>
      bulkModerationAction("moderation", "delete"),
    );

  // System
  document
    .getElementById("btn-model-save")
    ?.addEventListener("click", saveModels);
  document
    .getElementById("btn-model-pull")
    ?.addEventListener("click", pullModel);
  document
    .getElementById("btn-planner-model-save")
    ?.addEventListener("click", savePlannerModel);
  document
    .getElementById("btn-shadow-refresh")
    ?.addEventListener("click", loadPlannerShadow);
  // Models are loaded on page init; no need for click/focus reloads
  // (those would overwrite user's in-progress selection)

  // LLM Defaults
  document
    .getElementById("btn-llm-defaults-save")
    ?.addEventListener("click", saveLLMDefaults);

  // Tools
  document
    .getElementById("btn-reminder-load-user")
    ?.addEventListener("click", loadRemindersForSelectedUser);
  document
    .getElementById("btn-reminder-clear-all")
    ?.addEventListener("click", clearAllReminders);
  document
    .getElementById("reminder-user-select")
    ?.addEventListener("change", loadRemindersForSelectedUser);
  document
    .getElementById("btn-reminder-create")
    ?.addEventListener("click", createReminderFromForm);
  document
    .getElementById("btn-reminder-enable")
    ?.addEventListener("click", () => {
      const id = document.getElementById("reminder-id-input")?.value;
      if (!id) {
        showToast("Select a reminder", "warning");
        return;
      }
      reminderAction(`/reminders/${id}/enable`);
    });
  document
    .getElementById("btn-reminder-disable")
    ?.addEventListener("click", () => {
      const id = document.getElementById("reminder-id-input")?.value;
      if (!id) {
        showToast("Select a reminder", "warning");
        return;
      }
      reminderAction(`/reminders/${id}/disable`);
    });
  document
    .getElementById("btn-reminder-delete")
    ?.addEventListener("click", () => {
      const id = document.getElementById("reminder-id-input")?.value;
      if (!id) {
        showToast("Select a reminder", "warning");
        return;
      }
      reminderAction(`/reminders/${id}/delete`);
    });
  document
    .getElementById("btn-reminder-update")
    ?.addEventListener("click", () => {
      const id = document.getElementById("reminder-id-input")?.value;
      if (!id) {
        showToast("Select a reminder", "warning");
        return;
      }
      const body = {};
      const text = document.getElementById("reminder-text-input")?.value;
      const time = document.getElementById("reminder-time-input")?.value;
      const cron = document.getElementById("reminder-cron-input")?.value;
      const mode = document.getElementById("reminder-mode-input")?.value;
      const fuzz = document.getElementById("reminder-fuzz-minutes")?.value;
      const enabled = document.getElementById(
        "reminder-enabled-input",
      )?.checked;
      if (text) body.text = text;
      if (time) body.next_run_at = new Date(time).toISOString();
      if (cron) body.cadence_cron = cron;
      body.metadata = {
        mode: mode || "one_off_exact",
        fuzz_minutes: fuzz ? parseInt(fuzz, 10) : undefined,
      };
      body.enabled = enabled;
      reminderAction(`/reminders/${id}/update`, body);
    });
  document
    .getElementById("btn-memory-search")
    ?.addEventListener("click", memorySearch);
  document
    .getElementById("btn-export-user")
    ?.addEventListener("click", exportUserData);

  // Feedback
  document
    .getElementById("btn-feedback-load")
    ?.addEventListener("click", loadFeedback);

  // Console
  document
    .getElementById("btn-console-send")
    ?.addEventListener("click", sendConsoleMessage);
  document.getElementById("console-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      sendConsoleMessage();
    }
  });
  document
    .getElementById("btn-console-clear")
    ?.addEventListener("click", clearConsoleHistory);
  document
    .getElementById("btn-console-sessions")
    ?.addEventListener("click", viewConsoleSessions);

  // Trust form
  document
    .getElementById("trust-form")
    ?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const token = document.getElementById("trust-token")?.value;
      if (!token) {
        showToast("Enter trust token", "warning");
        return;
      }
      const res = await fetch(`/auth/trust?token=${encodeURIComponent(token)}`);
      if (res.ok) {
        showToast("Device trusted! Reload the page.", "success");
      } else {
        showToast("Invalid token", "error");
      }
    });

  // Periodic refresh
  setInterval(() => {
    loadStatus();
    loadModuleStatus();
    loadScheduler();
    loadTelegram();
    const analyticsTab = document.getElementById("tab-analytics");
    if (analyticsTab && analyticsTab.classList.contains("active"))
      loadLatencyLive();
  }, 15000);
}

// ============================================================
// INIT
// ============================================================
document.addEventListener("DOMContentLoaded", () => {
  wireEvents();

  // Initial hash routing
  const hash = window.location.hash.slice(1);

  // Dashboard-critical data only — everything else lazy-loads per tab
  loadStatus();
  loadModuleStatus();
  loadScheduler();
  loadTelegram();
  loadDBStats();
  startFeed();

  // If a specific tab was bookmarked, switch to it (triggers lazy-load)
  if (hash && document.getElementById(`tab-${hash}`)) {
    switchTab(hash);
  } else {
    _tabLoaded.add("dashboard");
  }

  // Set danger toggle from server config
  const dangerToggle = document.getElementById("danger-toggle");
  if (dangerToggle && typeof DANGEROUS_ENABLED !== "undefined") {
    dangerToggle.checked = DANGEROUS_ENABLED;
  }
});
