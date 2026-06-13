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
let messageRenderQueue = [];
let isRenderingMessage = false;
let hasHydratedInitialMessages = false;

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
  executionCountDisplay: document.getElementById("execution-count-display"),
  executionListModal: document.getElementById("execution-list-modal"),
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

  document.getElementById("event-overview-btn")?.addEventListener("click", () => {
    openModal(elements.eventDetailsModal);
  });
  document.getElementById("task-tree-btn")?.addEventListener("click", () => {
    openModal(elements.eventTreeModal);
  });
  document.getElementById("execution-log-btn")?.addEventListener("click", () => {
    openModal(elements.executionListModal);
  });
  document.getElementById("settings-btn")?.addEventListener("click", () => {
    refreshAll();
    showToast("Refreshed manually", "success");
  });
  document.getElementById("close-execution-detail")?.addEventListener("click", closeAllModals);

  document.querySelectorAll(".cyber-modal-close").forEach((button) => {
    button.addEventListener("click", closeAllModals);
  });
  document.querySelectorAll(".role-item").forEach((item) => {
    item.addEventListener("click", () => showRoleHistory(item.dataset.role));
  });
  elements.chatMessages?.addEventListener("click", (event) => {
    const header = event.target.closest(".collapsible-header");
    if (!header) return;
    const block = header.closest(".collapsible-result");
    const content = block?.querySelector(".collapsible-content");
    const icon = block?.querySelector(".collapse-icon");
    if (!content || !icon) return;
    content.classList.toggle("collapsed");
    block.classList.toggle("expanded", !content.classList.contains("collapsed"));
    icon.classList.toggle("collapsed", content.classList.contains("collapsed"));
  });
}

function initializeSocket() {
  socket.on("connect", () => {
    updateConnectionStatus("connected", "Connected");
    socket.emit("join", { event_id: eventId });
  });
  socket.on("disconnect", () => {
    updateConnectionStatus("disconnected", "Disconnected");
  });
  socket.on("status", (payload) => {
    if (payload.status === "joined") {
      updateConnectionStatus("connected", "Joined");
    }
    if (payload.event_status && currentEvent) {
      currentEvent.event_status = payload.event_status;
      currentEvent.current_round = payload.event_round ?? currentEvent.current_round;
      displayEventDetails(currentEvent);
    }
  });
  socket.on("new_message", (message) => {
    lastMessageId = Math.min(lastMessageId, Number(message.id || lastMessageId || 0));
    fetchEventMessages().then(() => {
      fetchEventStats();
      fetchHierarchy();
      fetchExecutions();
      fetchSummaries();
    });
  });
  socket.on("error", (payload) => {
    showToast(payload.message || "WebSocket error", "error");
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
    const incomingMessages = [];
    for (const message of data.data) {
      if (displayedMessageKeys.has(getMessageKey(message))) {
        lastMessageId = Math.max(lastMessageId, Number(message.id || 0));
        continue;
      }
      incomingMessages.push(message);
    }
    if (!incomingMessages.length) {
      if (messagesData.length || lastMessageId > 0) {
        hasHydratedInitialMessages = true;
      }
      return;
    }
    if (!hasHydratedInitialMessages && !messagesData.length) {
      renderMessageBatch(incomingMessages);
      hasHydratedInitialMessages = true;
      return;
    }
    for (const message of incomingMessages) {
      enqueueMessage(message, false);
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
      throw new Error(data.message || "Failed to send message");
    }
    showToast("Message added to the event stream", "success");
  } catch (error) {
    showToast(error.message || "Failed to send message", "error");
  }
}

function displayEventDetails(event) {
  elements.eventName.textContent = event.event_name || "Untitled event";
  elements.eventIdDisplay.textContent = `ID: ${event.event_id}`;
  elements.eventRound.textContent = `Round: ${event.current_round}`;
  elements.eventSource.textContent = `Source: ${event.source || "-"}`;
  elements.eventSeverity.textContent = `Severity: ${getSeverityText(event.severity)}`;
  elements.eventCreated.textContent = `Created: ${formatDateTime(event.created_at)}`;
  elements.currentRound.textContent = event.current_round;

  const statusClass = getStatusClass(event.event_status);
  elements.eventStatus.className = `event-status ${statusClass}`;
  elements.eventStatus.querySelector(".status-text").textContent = getStatusText(event.event_status);

  elements.eventMessageDetail.textContent = event.message || "";
  elements.eventContextDetail.textContent = JSON.stringify(event.context || {}, null, 2);
}

function renderSummaries() {
  if (!reviewsData.length) {
    elements.eventSummaryList.innerHTML = `<div class="system-notification"><p>No RoundReview entries yet.</p></div>`;
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
            <div class="markdown-content role-history-markdown">${renderMarkdown(review.summary_text || "")}</div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderHierarchy() {
  if (!hierarchyData.length) {
    elements.eventTreeContainer.innerHTML = `<p>No traceback task tree available yet.</p>`;
    return;
  }
  const latestRound = [...hierarchyData]
    .sort((left, right) => Number(left.round_id || 0) - Number(right.round_id || 0))
    .at(-1);
  const latestTree = latestRound?.tree;
  if (!latestTree?.root_nodes?.length) {
    elements.eventTreeContainer.innerHTML = `
      <div class="event-tree-round">
        <div class="event-tree-round-title">Latest Task Tree · Round ${escapeHtml(latestRound?.round_id || "--")}</div>
        <p>No task tree is available for the current round.</p>
      </div>
    `;
    return;
  }
  elements.eventTreeContainer.innerHTML = `
    <div class="event-tree-round">
      <div class="event-tree-round-title">Latest Task Tree · Round ${escapeHtml(latestRound.round_id)}</div>
      <div class="event-tree-summary">
        <span>Root directions: ${escapeHtml(latestTree.root_nodes.length)}</span>
        <span>Execution records: ${escapeHtml(latestRound.executions.length)}</span>
        <span>Reviews：${escapeHtml(latestRound.reviews.length)}</span>
      </div>
      <div class="ttt-tree modal-ttt-tree">${renderTTTTree(latestTree.root_nodes || [])}</div>
    </div>
  `;
}

function enqueueMessage(message, fromSocket) {
  const messageKey = getMessageKey(message);
  if (displayedMessageKeys.has(messageKey)) {
    return false;
  }
  displayedMessageKeys.add(messageKey);
  messageRenderQueue.push({ message, fromSocket });
  processMessageQueue();
  return true;
}

function processMessageQueue() {
  if (isRenderingMessage || !messageRenderQueue.length) {
    return;
  }
  isRenderingMessage = true;
  const { message, fromSocket } = messageRenderQueue.shift();
  messagesData.push(message);
  const messageNode = createMessageNode(message);
  const sender = getSenderName(message);
  elements.chatMessages.appendChild(messageNode);
  scrollToBottom();
  if (fromSocket) {
    showToast(`${sender}: new message received`, "info");
  }
  window.setTimeout(() => {
    isRenderingMessage = false;
    processMessageQueue();
  }, getMessageRenderDelay(message.message_type));
  return true;
}

function renderMessageBatch(messages) {
  const fragment = document.createDocumentFragment();
  for (const message of messages) {
    displayedMessageKeys.add(getMessageKey(message));
    messagesData.push(message);
    fragment.appendChild(createMessageNode(message));
    lastMessageId = Math.max(lastMessageId, Number(message.id || 0));
  }
  elements.chatMessages.appendChild(fragment);
  scrollToBottom();
}

function createMessageNode(message) {
  const messageNode = document.createElement("div");
  const sender = getSenderName(message);
  const roleClass = getMessageClass(message.message_from);
  const payload = normalizePayload(message);
  const summary = formatMessageContent(message.message_type, payload);
  messageNode.className = `message ${roleClass}`;
  messageNode.dataset.messageType = message.message_type || "";
  messageNode.innerHTML = `
    <div class="message-header">
      <span class="message-sender">${escapeHtml(sender)}</span>
      <div class="message-time-container">
        <span class="message-time">${escapeHtml(formatDateTime(message.created_at))}</span>
        <span class="message-source-btn" title="View source">{"{}"}</span>
      </div>
    </div>
    <div class="message-content">${summary}</div>
  `;
  messageNode.querySelector(".message-source-btn").addEventListener("click", () => {
    showMessageSourceModal(message);
  });
  return messageNode;
}

function getMessageKey(message) {
  return message.message_id || `${message.id}-${message.created_at}`;
}

function normalizePayload(message) {
  const payload = message.message_content ?? message.payload ?? {};
  return typeof payload === "object" && payload !== null ? payload : { text: String(payload) };
}

function formatMessageContent(messageType, payload) {
  if (messageType === "planner_analysis_completed") {
    return renderPlannerAnalysis(payload);
  }
  if (messageType === "ttt_initialized") {
    return renderTTTMessage("TTT initialized", payload.ttt, "Planner produced the initial task tree.");
  }
  if (messageType === "ttt_updated") {
    return renderTTTMessage("TTT updated", payload.ttt, "Planner adjusted the next-round task tree based on the review.");
  }
  if (messageType === "overall_assessment_created") {
    return renderOverallAssessmentCard(payload);
  }
  if (messageType === "leaf_claimed") {
    return renderInfoCard("TTT node claimed", [
      payload.node_title || payload.node_id || "A pending leaf node has been claimed",
    ]);
  }
  if (messageType === "tool_selected") {
    return renderInfoCard("Execution tool selected", [
      `Tool: ${payload.tool_name || "Not selected"}`,
      payload.node_id ? `Node: ${payload.node_id}` : "",
    ].filter(Boolean));
  }
  if (messageType === "execution_started") {
    return renderInfoCard(
      "Execution started",
      [
        payload.tool_name ? `Calling ${payload.tool_name}` : "Running tool",
        payload.node_id ? `Node: ${payload.node_id}` : "",
      ].filter(Boolean),
      { loading: true },
    );
  }
  if (messageType === "execution_completed" || messageType === "execution_failed") {
    return renderExecutionResultCard(payload, messageType === "execution_completed");
  }
  if (messageType === "round_review_started") {
    return renderInfoCard("Review started", [
      "Reviewer is summarizing the current execution results",
      payload.execution_count !== undefined ? `Execution count: ${payload.execution_count}` : "",
    ].filter(Boolean));
  }
  if (messageType === "round_review_created") {
    return renderReviewCard(payload);
  }
  if (messageType === "handoff_to_planner") {
    return renderInfoCard("Handed back to Planner", ["This round review is complete. Waiting for the next TTT update."]);
  }
  if (messageType === "handoff_to_reviewer") {
    return renderInfoCard("Handed back to Reviewer", ["Execution finished. Waiting for Reviewer to summarize the results."]);
  }
  if (messageType === "user_message") {
    return `<p>${escapeHtml(payload.text || "")}</p>`;
  }
  if (messageType === "system_info") {
    return renderSystemInfoMessage(payload);
  }
  if (payload.text) {
    return `<p>${escapeHtml(payload.text)}</p>`;
  }
  if (payload.response_text) {
    return `<p>${escapeHtml(payload.response_text)}</p>`;
  }
  return `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
}

function renderSystemInfoMessage(payload) {
  const text = payload.text || "";
  if (text === "planner_start_initial_planning") {
    return renderInfoCard("Alert received, starting initial analysis", [
      payload.event_name ? `Event: ${payload.event_name}` : "Planner is interpreting the new alert.",
    ]);
  }
  if (text === "planner_start_procedural_memory_lookup") {
    return renderInfoCard("Searching long-term memory", [
      "Planner is searching procedural memory for a reusable investigation workflow.",
    ]);
  }
  if (text === "planner_procedural_memory_lookup_completed") {
    const selected = payload.selected_procedural_memory;
    const lines = selected
      ? [
          `Matched: ${selected.title || selected.document_id}`,
          selected.summary || "",
        ].filter(Boolean)
      : ["No suitable procedural memory was found. Planner will build the TTT directly from the alert analysis."];
    return renderInfoCard("Long-term memory search completed", lines, {
      collapsibleJson: selected || { matched: false },
      collapsibleTitle: selected ? "View matched memory details" : "View search result",
    });
  }
  if (text === "planner_start_ttt_initialization") {
    return renderInfoCard("Initializing TTT from the retrieved context", [
      "Planner is turning the alert analysis and procedural memory into a task tree.",
    ]);
  }
  if (text === "planner_start_ttt_replanning") {
    return renderInfoCard("Updating TTT from the review", [
      payload.review_round ? `Replanning from the Round ${payload.review_round} summary.` : "Planner is updating the task tree.",
    ]);
  }
  return `<p>${escapeHtml(text || "System message")}</p>`;
}

function renderPlannerAnalysis(payload) {
  const analysis = payload.analysis || "";
  return `
    <div class="dialog-card">
      <div class="dialog-title">Initial analysis</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(analysis || "Planner completed the initial analysis.")}</div>
    </div>
  `;
}

function renderTTTMessage(title, ttt, summaryText) {
  return `
    <div class="dialog-card">
      <div class="dialog-title">${escapeHtml(title)}</div>
      <p class="dialog-summary">${escapeHtml(summaryText)}</p>
      ${ttt ? `<div class="ttt-tree">${renderTTTTree(ttt.root_nodes || [])}</div>` : ""}
    </div>
  `;
}

function renderExecutionResultCard(payload, success) {
  const result = payload.result || {};
  const resultText = JSON.stringify(result, null, 2);
  return `
    <div class="dialog-card">
      <div class="dialog-title">${success ? "Execution result" : "Execution failed"}</div>
      ${renderKeyValueGrid([
        ["Node", payload.node_title || payload.node_id || "-"],
        ["Tool", payload.tool_name || "-"],
        ["Status", success ? "Success" : "Failed"],
      ])}
      ${payload.error_message ? `<p class="dialog-error">${escapeHtml(payload.error_message)}</p>` : ""}
      <div class="collapsible-result">
        <div class="collapsible-header">
          <span class="collapse-icon collapsed">▸</span>
          <span>View execution output</span>
        </div>
        <div class="collapsible-content collapsed">
          <pre>${escapeHtml(resultText)}</pre>
        </div>
      </div>
    </div>
  `;
}

function renderReviewCard(payload) {
  return `
    <div class="dialog-card">
      <div class="dialog-title">Review result</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(payload.summary_text || "Reviewer completed the round summary.")}</div>
    </div>
  `;
}

function renderOverallAssessmentCard(payload) {
  return `
    <div class="dialog-card">
      <div class="dialog-title">Overall assessment</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(payload.summary_text || "Planner completed the overall assessment.")}</div>
    </div>
  `;
}

function renderInfoCard(title, lines, options = {}) {
  return `
    <div class="dialog-card dialog-card-compact">
      <div class="dialog-title ${options.loading ? "dialog-title-loading" : ""}">
        ${options.loading ? `<span class="loading-spinner" aria-hidden="true"></span>` : ""}
        <span>${escapeHtml(title)}</span>
      </div>
      <div class="dialog-lines">
        ${lines.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
      </div>
      ${options.collapsibleJson ? `
        <div class="collapsible-result">
          <div class="collapsible-header">
            <span class="collapse-icon collapsed">▸</span>
            <span>${escapeHtml(options.collapsibleTitle || "View details")}</span>
          </div>
          <div class="collapsible-content collapsed">
            <pre>${escapeHtml(JSON.stringify(options.collapsibleJson, null, 2))}</pre>
          </div>
        </div>
      ` : ""}
    </div>
  `;
}

function getMessageRenderDelay(messageType) {
  const delays = {
    planner_analysis_completed: 420,
    ttt_initialized: 420,
    ttt_updated: 420,
    round_review_created: 380,
    execution_completed: 320,
    execution_failed: 320,
  };
  return delays[messageType] || 220;
}

function renderBulletSection(title, items) {
  const safeItems = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!safeItems.length) return "";
  return `
    <div class="dialog-section">
      <div class="dialog-section-title">${escapeHtml(title)}</div>
      <ul class="dialog-bullets">
        ${safeItems.map((item) => `<li>${escapeHtml(formatEntity(item))}</li>`).join("")}
      </ul>
    </div>
  `;
}

function renderKeyValueGrid(items) {
  const rows = items.filter(([, value]) => value && String(value).trim());
  if (!rows.length) return "";
  return `
    <div class="dialog-grid">
      ${rows.map(([label, value]) => `
        <div class="dialog-grid-item">
          <span class="dialog-grid-label">${escapeHtml(label)}</span>
          <span class="dialog-grid-value">${escapeHtml(String(value))}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function renderTTTTree(nodes) {
  if (!Array.isArray(nodes) || !nodes.length) return "<p>No TTT available yet.</p>";
  return `
    <ul class="ttt-tree-list">
      ${nodes.map((node) => renderTTTNode(node)).join("")}
    </ul>
  `;
}

function renderTTTNode(node) {
  const children = Array.isArray(node.children) ? node.children : [];
  const isLeaf = children.length === 0;
  return `
    <li class="ttt-tree-node">
      <div class="ttt-tree-node-line">
        <span class="ttt-node-id">${escapeHtml(node.node_id || "")}</span>
        <span class="ttt-node-title">${escapeHtml(node.title || "")}</span>
        ${isLeaf ? `<span class="ttt-node-status status-${escapeHtml(node.status || "todo")}">${escapeHtml(node.status || "todo")}</span>` : ""}
      </div>
      ${children.length ? `<ul class="ttt-tree-list child-list">${children.map((child) => renderTTTNode(child)).join("")}</ul>` : ""}
    </li>
  `;
}

function formatEntity(value) {
  if (typeof value !== "string") return String(value || "");
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return trimmed;
  return trimmed
    .replaceAll("'", '"')
    .replace(/^\{/, "")
    .replace(/\}$/, "")
    .replace(/"type":\s*"([^"]+)"/, "type=$1")
    .replace(/"value":\s*"([^"]+)"/, " value=$1")
    .replace(/"role":\s*"([^"]+)"/, " role=$1")
    .replaceAll(",", "");
}

function getMessageClass(role) {
  const mapping = {
    _planner: "message-captain",
    _executor: "message-operator",
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
    user: "User",
    system: "System",
  };
  return mapping[message.message_from] || message.message_from;
}

function showRoleHistory(role) {
  const roleMessages = messagesData.filter((message) => message.message_from === role);
  const roleLabel = getSenderName({ message_from: role });
  elements.roleHistoryModal.dataset.role = role;
  elements.roleHistoryTitle.textContent = `${roleLabel} History`;
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
    : `<div class="system-notification"><p>No messages for this role yet.</p></div>`;
  openModal(elements.roleHistoryModal);
}

function updateExecutionIndicator() {
  const count = executionsData.length;
  elements.executionCountDisplay.textContent = count;
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
          <div class="execution-item-desc">${escapeHtml(execution.node_title || "Untitled")}</div>
          <div class="execution-item-action">
            <button class="execution-item-btn">View</button>
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
  elements.executionResult.textContent = JSON.stringify(execution.result || {}, null, 2);
  elements.contextContent.innerHTML = `
    <div class="context-item"><span class="label">Node</span><pre>${escapeHtml(execution.node_title || "-")}</pre></div>
    <div class="context-item"><span class="label">Status</span><pre>${escapeHtml(execution.execution_status || "-")}</pre></div>
    <div class="context-item"><span class="label">Error</span><pre>${escapeHtml(execution.error_message || "-")}</pre></div>
  `;
  openModal(elements.executionModal);
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
  showToast("Message source copied", "success");
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
    low: "Low",
    medium: "Medium",
    high: "High",
    critical: "Critical",
    unknown: "Unknown",
  };
  return mapping[severity] || severity || "Unknown";
}

function getStatusText(status) {
  const mapping = {
    pending: "Pending planning",
    planned: "Planned",
    executing: "Executing",
    reviewing: "Reviewing",
    replanning: "Replanning",
    completed: "Completed",
    failed: "Failed",
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
    return "<ul><li>None</li></ul>";
  }
  return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function renderMarkdown(value) {
  const source = String(value || "").trim();
  if (!source) return "<p></p>";

  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let paragraph = [];
  let listItems = [];
  let inCodeBlock = false;
  let codeBlockLines = [];
  let tableHeader = null;
  let tableRows = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };

  const flushCodeBlock = () => {
    if (!inCodeBlock) return;
    html.push(`<pre><code>${escapeHtml(codeBlockLines.join("\n"))}</code></pre>`);
    inCodeBlock = false;
    codeBlockLines = [];
  };

  const flushTable = () => {
    if (!tableHeader || !tableRows.length) {
      tableHeader = null;
      tableRows = [];
      return;
    }
    html.push(`
      <table>
        <thead>
          <tr>${tableHeader.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${tableRows
            .map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join("")}</tr>`)
            .join("")}
        </tbody>
      </table>
    `);
    tableHeader = null;
    tableRows = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      flushParagraph();
      flushList();
      flushTable();
      if (inCodeBlock) {
        flushCodeBlock();
      } else {
        inCodeBlock = true;
        codeBlockLines = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeBlockLines.push(line);
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      flushList();
      flushTable();
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      flushTable();
      const level = headingMatch[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
      continue;
    }

    const blockquoteMatch = trimmed.match(/^>\s?(.*)$/);
    if (blockquoteMatch) {
      flushParagraph();
      flushList();
      flushTable();
      html.push(`<blockquote>${renderInlineMarkdown(blockquoteMatch[1])}</blockquote>`);
      continue;
    }

    const listMatch = trimmed.match(/^[-*]\s+(.+)$/);
    if (listMatch) {
      flushParagraph();
      flushTable();
      listItems.push(listMatch[1]);
      continue;
    }

    const orderedListMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (orderedListMatch) {
      flushParagraph();
      flushTable();
      listItems.push(orderedListMatch[1]);
      continue;
    }

    if (looksLikeMarkdownTableRow(trimmed)) {
      flushParagraph();
      flushList();
      const cells = splitMarkdownTableRow(trimmed);
      if (!tableHeader) {
        tableHeader = cells;
        tableRows = [];
        continue;
      }
      if (isMarkdownTableSeparator(trimmed)) {
        continue;
      }
      tableRows.push(cells);
      continue;
    }

    flushList();
    flushTable();
    paragraph.push(trimmed);
  }

  flushParagraph();
  flushList();
  flushTable();
  flushCodeBlock();

  return html.join("");
}

function looksLikeMarkdownTableRow(line) {
  return line.includes("|") && splitMarkdownTableRow(line).length >= 2;
}

function splitMarkdownTableRow(line) {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isMarkdownTableSeparator(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderInlineMarkdown(value) {
  let html = escapeHtml(String(value || ""));
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return html;
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
