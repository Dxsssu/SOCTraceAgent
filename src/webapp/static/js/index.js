const API_BASE_URL = "/api";

document.addEventListener("DOMContentLoaded", () => {
  fetchEvents();
  document.getElementById("refresh-events")?.addEventListener("click", fetchEvents);
  document.getElementById("event-form")?.addEventListener("submit", submitEventForm);
});

async function submitEventForm(event) {
  event.preventDefault();
  const submitButton = document.querySelector('#event-form button[type="submit"]');
  const originalText = submitButton.textContent;

  const payload = {
    event_name: document.getElementById("event-name").value.trim(),
    message: document.getElementById("event-message").value.trim(),
    context: document.getElementById("event-context").value.trim(),
    severity: document.getElementById("event-severity").value,
    source: document.getElementById("event-source").value.trim(),
  };

  if (!payload.message) {
    showToast("事件描述不能为空", "error");
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "创建中...";
  try {
    const response = await fetch(`${API_BASE_URL}/event/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || "创建失败");
    }

    showToast("事件创建成功", "success");
    document.getElementById("event-form").reset();
    document.getElementById("event-source").value = "web_manual";
    document.getElementById("event-severity").value = "medium";
    await fetchEvents();
    setTimeout(() => {
      window.location.href = `/warroom/${data.data.event_id}`;
    }, 600);
  } catch (error) {
    console.error(error);
    showToast(error.message || "创建失败", "error");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = originalText;
  }
}

async function fetchEvents() {
  const eventsContainer = document.getElementById("events-container");
  if (!eventsContainer) return;
  eventsContainer.innerHTML = `
    <div class="text-center py-5">
      <div class="spinner-border text-primary" role="status"></div>
      <p class="mt-2">加载事件列表...</p>
    </div>
  `;

  try {
    const response = await fetch(`${API_BASE_URL}/event/list`);
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || "加载失败");
    }

    if (!data.data.length) {
      eventsContainer.innerHTML = `
        <div class="text-center py-5">
          <p class="text-muted">暂无安全事件</p>
        </div>
      `;
      return;
    }

    eventsContainer.innerHTML = `<div class="list-group">${data.data.map(renderEventCard).join("")}</div>`;
  } catch (error) {
    console.error(error);
    eventsContainer.innerHTML = `
      <div class="alert alert-danger" role="alert">
        加载失败: ${escapeHtml(error.message || "未知错误")}
      </div>
    `;
  }
}

function renderEventCard(event) {
  const createdAt = formatDateTime(event.created_at);
  const severityBadge = getSeverityBadge(event.severity);
  const statusBadge = getStatusBadge(event.event_status);
  return `
    <a href="/warroom/${escapeHtml(event.event_id)}" class="list-group-item list-group-item-action event-card severity-${escapeHtml(event.severity)}">
      <div class="d-flex w-100 justify-content-between">
        <h5 class="mb-1">${escapeHtml(event.event_name || "未命名事件")}</h5>
        <small>${escapeHtml(createdAt)}</small>
      </div>
      <p class="mb-1">${escapeHtml(event.message || "")}</p>
      <div class="d-flex justify-content-between align-items-center">
        <div>
          <span class="badge rounded-pill ${severityBadge.className}">${severityBadge.text}</span>
          <span class="badge rounded-pill ${statusBadge.className}">${statusBadge.text}</span>
        </div>
        <small>来源: ${escapeHtml(event.source || "-")}</small>
      </div>
    </a>
  `;
}

function getSeverityBadge(severity) {
  const mapping = {
    low: { className: "bg-success", text: "低" },
    medium: { className: "bg-warning text-dark", text: "中" },
    high: { className: "bg-danger", text: "高" },
    critical: { className: "bg-dark", text: "严重" },
  };
  return mapping[severity] || { className: "bg-secondary", text: severity || "未知" };
}

function getStatusBadge(status) {
  const mapping = {
    pending: { className: "bg-warning text-dark", text: "待规划" },
    planned: { className: "bg-info text-dark", text: "已规划" },
    executing: { className: "bg-primary", text: "执行中" },
    reviewing: { className: "bg-secondary", text: "复盘中" },
    replanning: { className: "bg-info", text: "重规划" },
    completed: { className: "bg-success", text: "已完成" },
    failed: { className: "bg-danger", text: "失败" },
  };
  return mapping[status] || { className: "bg-secondary", text: status || "未知" };
}

function showToast(message, type = "info") {
  const toastContainer = document.getElementById("toast-container");
  const toast = document.createElement("div");
  const theme =
    type === "success" ? "success" : type === "error" ? "danger" : "primary";
  toast.className = `toast align-items-center text-white bg-${theme}`;
  toast.setAttribute("role", "alert");
  toast.setAttribute("aria-live", "assertive");
  toast.setAttribute("aria-atomic", "true");
  toast.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${escapeHtml(message)}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>
  `;
  toastContainer.appendChild(toast);
  const instance = new bootstrap.Toast(toast, { delay: 2400 });
  instance.show();
  toast.addEventListener("hidden.bs.toast", () => toast.remove());
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
