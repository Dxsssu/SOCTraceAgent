const API_BASE_URL = "/api";
let deleteModalInstance = null;
let pendingDeleteEvent = null;

document.addEventListener("DOMContentLoaded", () => {
  fetchEvents();
  document.getElementById("refresh-events")?.addEventListener("click", fetchEvents);
  document.getElementById("event-form")?.addEventListener("submit", submitEventForm);
  document.getElementById("events-container")?.addEventListener("click", handleEventListClick);
  document.getElementById("confirm-delete-event")?.addEventListener("click", confirmDeleteEvent);
  const deleteModalElement = document.getElementById("deleteEventModal");
  if (deleteModalElement) {
    deleteModalInstance = new bootstrap.Modal(deleteModalElement);
    deleteModalElement.addEventListener("hidden.bs.modal", () => {
      pendingDeleteEvent = null;
      const confirmButton = document.getElementById("confirm-delete-event");
      if (confirmButton) {
        confirmButton.disabled = false;
        confirmButton.textContent = "Delete";
      }
    });
  }
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
    showToast("Event description cannot be empty", "error");
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "Creating...";
  try {
    const response = await fetch(`${API_BASE_URL}/event/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || "Failed to create event");
    }

    showToast("Event created successfully", "success");
    document.getElementById("event-form").reset();
    document.getElementById("event-source").value = "web_manual";
    document.getElementById("event-severity").value = "medium";
    await fetchEvents();
    setTimeout(() => {
      window.location.href = `/warroom/${data.data.event_id}`;
    }, 600);
  } catch (error) {
    console.error(error);
    showToast(error.message || "Failed to create event", "error");
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
      <p class="mt-2">Loading events...</p>
    </div>
  `;

  try {
    const response = await fetch(`${API_BASE_URL}/event/list`);
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || "Failed to load events");
    }

    if (!data.data.length) {
      eventsContainer.innerHTML = `
        <div class="text-center py-5">
          <p class="text-muted">No security events yet</p>
        </div>
      `;
      return;
    }

    eventsContainer.innerHTML = `<div class="list-group">${data.data.map(renderEventCard).join("")}</div>`;
  } catch (error) {
    console.error(error);
    eventsContainer.innerHTML = `
      <div class="alert alert-danger" role="alert">
        Failed to load events: ${escapeHtml(error.message || "Unknown error")}
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
        <div class="d-flex align-items-start flex-grow-1 me-3">
          <h5 class="mb-1 event-card-title">${escapeHtml(event.event_name || "Untitled Event")}</h5>
          <button
            type="button"
            class="btn btn-sm btn-outline-danger event-delete-button"
            data-action="delete-event"
            data-event-id="${escapeHtml(event.event_id)}"
            data-event-name="${escapeHtml(event.event_name || "Untitled Event")}"
            aria-label="Delete event ${escapeHtml(event.event_name || event.event_id)}"
            title="Delete event"
          >
            <i class="bi bi-trash"></i>
          </button>
        </div>
        <small>${escapeHtml(createdAt)}</small>
      </div>
      <p class="mb-1">${escapeHtml(event.message || "")}</p>
      <div class="d-flex justify-content-between align-items-center">
        <div>
          <span class="badge rounded-pill ${severityBadge.className}">${severityBadge.text}</span>
          <span class="badge rounded-pill ${statusBadge.className}">${statusBadge.text}</span>
        </div>
        <small>Source: ${escapeHtml(event.source || "-")}</small>
      </div>
    </a>
  `;
}

function handleEventListClick(event) {
  const deleteButton = event.target.closest('[data-action="delete-event"]');
  if (!deleteButton) return;
  event.preventDefault();
  event.stopPropagation();

  pendingDeleteEvent = {
    eventId: deleteButton.dataset.eventId,
    eventName: deleteButton.dataset.eventName || "Untitled Event",
  };

  const textElement = document.getElementById("delete-event-modal-text");
  if (textElement) {
    textElement.textContent = `Are you sure you want to delete event "${pendingDeleteEvent.eventName}"?`;
  }
  deleteModalInstance?.show();
}

async function confirmDeleteEvent() {
  if (!pendingDeleteEvent?.eventId) return;
  const confirmButton = document.getElementById("confirm-delete-event");
  const originalText = confirmButton?.textContent || "Delete";
  if (confirmButton) {
    confirmButton.disabled = true;
    confirmButton.textContent = "Deleting...";
  }

  try {
    const response = await fetch(`${API_BASE_URL}/event/${encodeURIComponent(pendingDeleteEvent.eventId)}`, {
      method: "DELETE",
    });
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || "Failed to delete event");
    }
    deleteModalInstance?.hide();
    showToast("Event deleted successfully", "success");
    await fetchEvents();
  } catch (error) {
    console.error(error);
    showToast(error.message || "Failed to delete event", "error");
    if (confirmButton) {
      confirmButton.disabled = false;
      confirmButton.textContent = originalText;
    }
  }
}

function getSeverityBadge(severity) {
  const mapping = {
    low: { className: "bg-success", text: "Low" },
    medium: { className: "bg-warning text-dark", text: "Medium" },
    high: { className: "bg-danger", text: "High" },
    critical: { className: "bg-dark", text: "Critical" },
  };
  return mapping[severity] || { className: "bg-secondary", text: severity || "Unknown" };
}

function getStatusBadge(status) {
  const mapping = {
    pending: { className: "bg-warning text-dark", text: "Pending Planning" },
    planned: { className: "bg-info text-dark", text: "Planned" },
    executing: { className: "bg-primary", text: "Executing" },
    reviewing: { className: "bg-secondary", text: "Reviewing" },
    replanning: { className: "bg-info", text: "Replanning" },
    completed: { className: "bg-success", text: "Completed" },
    failed: { className: "bg-danger", text: "Failed" },
  };
  return mapping[status] || { className: "bg-secondary", text: status || "Unknown" };
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
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-US");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
