const eventId = document.getElementById("event-id-data").dataset.eventId;
const API_BASE_URL = "/api";
const socket = io({ transports: ["websocket", "polling"] });

let lastMessageId = 0;
let displayedMessageKeys = new Set();
let messagesData = [];
let executionsData = [];
let hierarchyData = [];
let reviewsData = [];
let currentEvent = null;
let refreshTimer = null;

const elements = {
  chatMessages: document.getElementById("chat-messages"),
  userInput: document.getElementById("user-input"),
  sendButton: document.getElementById("send-button"),
  eventName: document.getElementById("event-name"),
  eventStatus: document.getElementById("event-status"),
  eventIdDisplay: document.getElementById("event-id-display"),
  eventRound: document.getElementById("event-round"),
  eventSource: document.getElementById("event-source"),
  eventSeverity: document.getElementById("event-severity"),
  eventCreated: document.getElementById("event-created"),
  currentRound: document.getElementById("current-round"),
  taskCount: document.getElementById("task-count"),
  actionCount: document.getElementById("action-count"),
  commandCount: document.getElementById("command-count"),
  messageCount: document.getElementById("message-count"),
  connectionStatus: document.getElementById("connection-status"),
  executionIndicator: document.getElementById("execution-indicator"),
  executionCount: document.querySelector(".execution-count"),
  executionCountDisplay: document.getElementById("execution-count-display"),
  executionPanel: document.getElementById("execution-panel"),
  executionList: document.getElementById("execution-list"),
  executionEmpty: document.getElementById("execution-empty"),
  executionModal: document.getElementById("execution-modal"),
  executionId: document.getElementById("execution-id"),
  executionCommand: document.getElementById("execution-command"),
  executionTime: document.getElementById("execution-time"),
  executionResult: document.getElementById("execution-result"),
  contextContent: document.getElementById("context-content"),
  contextToggle: document.getElementById("context-toggle"),
  eventDetailsModal: document.getElementById("event-details-modal"),
  eventMessageDetail: document.getElementById("event-message-detail"),
  eventContextDetail: document.getElementById("event-context-detail"),
  eventSummaryList: document.getElementById("event-summary-list"),
  eventTreeModal: document.getElementById("event-tree-modal"),
  eventTreeContainer: document.getElementById("event-tree-container"),
  roleHistoryModal: document.getElementById("role-history-modal"),
  roleHistoryTitle: document.getElementById("role-history-title"),
  roleHistoryList: document.getElementById("role-history-list"),
  messageSourceModal: document.getElementById("message-source-modal"),
  messageSourceContent: document.getElementById("message-source-content"),
};

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  initializeSocket();
  refreshAll();
  refreshTimer = window.setInterval(refreshAll, 3000);
});

function bindEvents() {
  elements.sendButton?.addEventListener("click", submitUserMessage);
  elements.userInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitUserMessage();
    }
  });

  document.getElementById("event-details-btn")?.addEventListener("click", () => {
    openModal(elements.eventDetailsModal);
  });
  document.getElementById("event-tree-btn")?.addEventListener("click", () => {
    openModal(elements.eventTreeModal);
  });
  document.getElementById("settings-btn")?.addEventListener("click", () => {
    refreshAll();
    showToast("已手动刷新", "success");
  });
  document.getElementById("mode-switch")?.addEventListener("click", toggleDrivingMode);
  document.getElementById("close-execution-detail")?.addEventListener("click", closeAllModals);
  document.querySelector(".execution-panel-close")?.addEventListener("click", toggleExecutionPanel);
  elements.executionIndicator?.addEventListener("click", toggleExecutionPanel);

  document.querySelectorAll(".cyber-modal-close").forEach((button) => {
    button.addEventListener("click", closeAllModals);
  });
  document.querySelectorAll(".role-item").forEach((item) => {
    item.addEventListener("click", () => showRoleHistory(item.dataset.role));
  });
}

function initializeSocket() {
  socket.on("connect", () => {
    updateConnectionStatus("connected", "已连接");
    socket.emit("join", { event_id: eventId });
  });
  socket.on("disconnect", () => {
    updateConnectionStatus("disconnected", "已断开");
  });
  socket.on("status", (payload) => {
    if (payload.status === "joined") {
      updateConnectionStatus("connected", "已加入");
    }
    if (payload.event_status && currentEvent) {
      currentEvent.event_status = payload.event_status;
      currentEvent.current_round = payload.event_round ?? currentEvent.current_round;
      displayEventDetails(currentEvent);
    }
  });
  socket.on("new_message", (message) => {
    addMessage(message, true);
    fetchEventStats();
    fetchHierarchy();
    fetchExecutions();
    fetchSummaries();
  });
  socket.on("error", (payload) => {
    showToast(payload.message || "WebSocket 出错", "error");
  });
}

async function refreshAll() {
  await Promise.all([
    fetchEventDetails(),
    fetchEventMessages(),
    fetchEventStats(),
    fetchExecutions(),
    fetchHierarchy(),
    fetchSummaries(),
    fetchDrivingMode(),
  ]);
}

async function fetchEventDetails() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}`);
    const data = await response.json();
    if (data.status === "success") {
      currentEvent = data.data;
      displayEventDetails(data.data);
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchEventMessages() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}/messages?after_rowid=${lastMessageId}`);
    const data = await response.json();
    if (data.status !== "success") return;
    for (const message of data.data) {
      addMessage(message, false);
      lastMessageId = Math.max(lastMessageId, Number(message.id || 0));
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchEventStats() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}/stats`);
    const data = await response.json();
    if (data.status === "success") {
      elements.taskCount.textContent = data.data.task_count;
      elements.actionCount.textContent = data.data.action_count;
      elements.commandCount.textContent = data.data.command_count;
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchExecutions() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}/executions`);
    const data = await response.json();
    if (data.status === "success") {
      executionsData = data.data;
      updateExecutionIndicator();
      updateExecutionList();
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchHierarchy() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}/hierarchy`);
    const data = await response.json();
    if (data.status === "success") {
      hierarchyData = data.data;
      renderHierarchy();
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchSummaries() {
  try {
    const response = await fetch(`${API_BASE_URL}/event/${eventId}/summaries`);
    const data = await response.json();
    if (data.status === "success") {
      reviewsData = data.data;
      renderSummaries();
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchDrivingMode() {
  try {
    const response = await fetch(`${API_BASE_URL}/state/driving-mode`);
    const data = await response.json();
    if (data.status === "success") {
      const mode = data.data.mode;
      const switchNode = document.getElementById("mode-switch");
      const label = switchNode.querySelector(".switch-label");
      switchNode.classList.toggle("auto-mode", mode === "auto");
      switchNode.classList.toggle("manual-mode", mode === "manual");
      label.textContent = mode === "auto" ? "轮询模式" : "手动刷新";
    }
  } catch (error) {
    console.error(error);
  }
}

async function toggleDrivingMode() {
  const switchNode = document.getElementById("mode-switch");
  const newMode = switchNode.classList.contains("auto-mode") ? "manual" : "auto";
  try {
    const response = await fetch(`${API_BASE_URL}/state/driving-mode`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: newMode }),
    });
    const data = await response.json();
    if (data.status === "success") {
      fetchDrivingMode();
      showToast(`模式已切换为 ${newMode === "auto" ? "轮询" : "手动"}`, "success");
    }
  } catch (error) {
    console.error(error);
  }
}

async function submitUserMessage() {
  const text = elements.userInput.value.trim();
  if (!text) {
    return;
  }
  elements.userInput.value = "";
  try {
    const response = await fetch(`${API_BASE_URL}/event/send_message/${eventId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await response.json();
    if (data.status !== "success") {
      throw new Error(data.message || "发送失败");
    }
    showToast("消息已写入事件流", "success");
  } catch (error) {
    showToast(error.message || "发送失败", "error");
  }
}

function displayEventDetails(event) {
  elements.eventName.textContent = event.event_name || "未命名事件";
  elements.eventIdDisplay.textContent = `ID: ${event.event_id}`;
  elements.eventRound.textContent = `轮次: ${event.current_round}`;
  elements.eventSource.textContent = `来源: ${event.source || "-"}`;
  elements.eventSeverity.textContent = `严重程度: ${getSeverityText(event.severity)}`;
  elements.eventCreated.textContent = `创建时间: ${formatDateTime(event.created_at)}`;
  elements.currentRound.textContent = event.current_round;

  const statusClass = getStatusClass(event.event_status);
  elements.eventStatus.className = `event-status ${statusClass}`;
  elements.eventStatus.querySelector(".status-text").textContent = getStatusText(event.event_status);

  elements.eventMessageDetail.textContent = event.message || "";
  elements.eventContextDetail.textContent = JSON.stringify(event.context || {}, null, 2);
}

function renderSummaries() {
  if (!reviewsData.length) {
    elements.eventSummaryList.innerHTML = `<div class="system-notification"><p>当前还没有 RoundReview。</p></div>`;
    return;
  }
  elements.eventSummaryList.innerHTML = reviewsData
    .map((review) => {
      return `
        <div class="role-history-item">
          <div class="role-history-header">
            <span class="role-history-type">Round ${escapeHtml(review.round_id)}</span>
            <span class="role-history-time">${escapeHtml(formatDateTime(review.created_at))}</span>
          </div>
          <div class="role-history-content">
            <strong>Findings</strong>
            ${renderBulletList(review.findings)}
            <strong>Gaps</strong>
            ${renderBulletList(review.gaps)}
            <strong>Recommendations</strong>
            ${renderBulletList(review.recommendations)}
          </div>
        </div>
      `;
    })
    .join("");
}

function renderHierarchy() {
  if (!hierarchyData.length) {
    elements.eventTreeContainer.innerHTML = `<p>暂无层级数据</p>`;
    return;
  }
  elements.eventTreeContainer.innerHTML = hierarchyData
    .map((round) => {
      const treeTitle = round.tree?.root_nodes?.[0]?.title || "TTT not ready";
      return `
        <div class="event-tree-round">
          <div class="event-tree-round-title">
            Round ${escapeHtml(round.round_id)}
          </div>
          <ul>
            <li>TTT: ${escapeHtml(treeTitle)}</li>
            <li>Executions: ${escapeHtml(round.executions.length)}</li>
            <li>Reviews: ${escapeHtml(round.reviews.length)}</li>
          </ul>
        </div>
      `;
    })
    .join("");
}

function addMessage(message, fromSocket) {
  const messageKey = message.message_id || `${message.id}-${message.created_at}`;
  if (displayedMessageKeys.has(messageKey)) {
    return false;
  }
  displayedMessageKeys.add(messageKey);
  messagesData.push(message);

  const messageNode = document.createElement("div");
  const sender = getSenderName(message);
  const roleClass = getMessageClass(message.message_from);
  const payload = normalizePayload(message);
  const summary = formatMessageContent(message.message_type, payload);
  messageNode.className = `message ${roleClass}`;
  messageNode.innerHTML = `
    <div class="message-header">
      <span class="message-sender">${escapeHtml(sender)}</span>
      <div class="message-time-container">
        <span class="message-time">${escapeHtml(formatDateTime(message.created_at))}</span>
        <span class="message-source-btn" title="查看源码">{"{}"}</span>
      </div>
    </div>
    <div class="message-content">${summary}</div>
  `;
  messageNode.querySelector(".message-source-btn").addEventListener("click", () => {
    showMessageSourceModal(message);
  });
  elements.chatMessages.appendChild(messageNode);
  scrollToBottom();
  if (fromSocket) {
    showToast(`${sender}: 新消息到达`, "info");
  }
  return true;
}

function normalizePayload(message) {
  const payload = message.message_content ?? message.payload ?? {};
  return typeof payload === "object" && payload !== null ? payload : { text: String(payload) };
}

function formatMessageContent(messageType, payload) {
  if (messageType === "user_message") {
    return `<p>${escapeHtml(payload.text || "")}</p>`;
  }
  if (payload.text) {
    return `<p>${escapeHtml(payload.text)}</p>`;
  }
  if (payload.response_text) {
    return `<p>${escapeHtml(payload.response_text)}</p>`;
  }
  return `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
}

function getMessageClass(role) {
  const mapping = {
    _planner: "message-captain",
    _executor: "message-executor",
    _reviewer: "message-expert",
    user: "message-user",
    system: "message-system",
  };
  return mapping[role] || "message-system";
}

function getSenderName(message) {
  const mapping = {
    _planner: "Planner",
    _executor: "Executor",
    _reviewer: "Reviewer",
    user: "用户",
    system: "系统",
  };
  return mapping[message.message_from] || message.message_from;
}

function showRoleHistory(role) {
  const roleMessages = messagesData.filter((message) => message.message_from === role);
  const roleLabel = getSenderName({ message_from: role });
  elements.roleHistoryModal.dataset.role = role;
  elements.roleHistoryTitle.textContent = `${roleLabel} 历史`;
  elements.roleHistoryList.innerHTML = roleMessages.length
    ? roleMessages
        .map((message) => {
          return `
            <div class="role-history-item">
              <div class="role-history-header">
                <span class="role-history-type">${escapeHtml(message.message_type)}</span>
                <span class="role-history-time">${escapeHtml(formatDateTime(message.created_at))}</span>
              </div>
              <div class="role-history-content"><pre>${escapeHtml(JSON.stringify(normalizePayload(message), null, 2))}</pre></div>
            </div>
          `;
        })
        .join("")
    : `<div class="system-notification"><p>当前还没有该角色的消息。</p></div>`;
  openModal(elements.roleHistoryModal);
}

function updateExecutionIndicator() {
  const count = executionsData.length;
  elements.executionCount.textContent = count;
  elements.executionCountDisplay.textContent = count;
  elements.executionIndicator.classList.toggle("has-waiting", count > 0);
}

function updateExecutionList() {
  if (!executionsData.length) {
    elements.executionEmpty.style.display = "flex";
    elements.executionList.style.display = "none";
    return;
  }
  elements.executionEmpty.style.display = "none";
  elements.executionList.style.display = "flex";
  elements.executionList.innerHTML = executionsData
    .map((execution) => {
      return `
        <div class="execution-item" data-execution-id="${escapeHtml(execution.execution_id)}">
          <div class="execution-item-header">
            <span class="execution-item-id">${escapeHtml(shortId(execution.execution_id))}</span>
            <span class="execution-item-time">${escapeHtml(formatDateTime(execution.created_at))}</span>
          </div>
          <div class="execution-item-command">${escapeHtml(execution.tool_name || execution.node_id)}</div>
          <div class="execution-item-desc">${escapeHtml(execution.node_title || "无标题")}</div>
          <div class="execution-item-action">
            <button class="execution-item-btn">查看</button>
          </div>
        </div>
      `;
    })
    .join("");
  elements.executionList.querySelectorAll(".execution-item").forEach((item) => {
    item.addEventListener("click", () => {
      const executionId = item.dataset.executionId;
      const execution = executionsData.find((candidate) => candidate.execution_id === executionId);
      if (execution) {
        showExecutionModal(execution);
      }
    });
  });
}

function showExecutionModal(execution) {
  elements.executionId.textContent = execution.execution_id;
  elements.executionCommand.textContent = execution.tool_name || execution.node_id;
  elements.executionTime.textContent = formatDateTime(execution.created_at);
  elements.executionResult.value = JSON.stringify(execution.result || {}, null, 2);
  elements.contextContent.innerHTML = `
    <div class="context-item"><span class="label">节点</span><pre>${escapeHtml(execution.node_title || "-")}</pre></div>
    <div class="context-item"><span class="label">状态</span><pre>${escapeHtml(execution.execution_status || "-")}</pre></div>
    <div class="context-item"><span class="label">错误</span><pre>${escapeHtml(execution.error_message || "-")}</pre></div>
  `;
  openModal(elements.executionModal);
}

function toggleExecutionPanel() {
  elements.executionPanel.classList.toggle("active");
}

function toggleExecutionContext() {
  elements.contextContent.classList.toggle("collapsed");
  elements.contextToggle.className = elements.contextContent.classList.contains("collapsed")
    ? "fas fa-chevron-right"
    : "fas fa-chevron-down";
}

function showMessageSourceModal(message) {
  elements.messageSourceContent.textContent = JSON.stringify(message, null, 2);
  openModal(elements.messageSourceModal);
}

function copyMessageSource() {
  navigator.clipboard.writeText(elements.messageSourceContent.textContent || "");
  showToast("已复制消息源码", "success");
}

function openModal(element) {
  element.style.display = "flex";
}

function closeAllModals() {
  document.querySelectorAll(".cyber-modal").forEach((modal) => {
    modal.style.display = "none";
  });
}

function updateConnectionStatus(status, text) {
  elements.connectionStatus.className = status ? `status-${status}` : "";
  elements.connectionStatus.textContent = text;
}

function scrollToBottom() {
  elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
}

function getSeverityText(severity) {
  const mapping = {
    low: "低",
    medium: "中",
    high: "高",
    critical: "严重",
    unknown: "未知",
  };
  return mapping[severity] || severity || "未知";
}

function getStatusText(status) {
  const mapping = {
    pending: "待规划",
    planned: "已规划",
    executing: "执行中",
    reviewing: "复盘中",
    replanning: "重规划",
    completed: "已完成",
    failed: "失败",
  };
  return mapping[status] || status;
}

function getStatusClass(status) {
  const mapping = {
    pending: "pending",
    planned: "processing",
    executing: "processing",
    reviewing: "processing",
    replanning: "processing",
    completed: "completed",
    failed: "failed",
  };
  return mapping[status] || "pending";
}

function renderBulletList(items) {
  if (!items || !items.length) {
    return "<ul><li>无</li></ul>";
  }
  return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function shortId(value) {
  return value ? String(value).slice(0, 8) : "-";
}

function showToast(message, type = "info") {
  const toastContainer = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type === "error" ? "error" : type === "success" ? "success" : "warning"}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  window.setTimeout(() => toast.remove(), 2600);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

window.copyMessageSource = copyMessageSource;
window.toggleExecutionContext = toggleExecutionContext;
