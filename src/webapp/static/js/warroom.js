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
  document.getElementById("close-execution-detail")?.addEventListener("click", closeAllModals);
  document.querySelector(".execution-panel-close")?.addEventListener("click", toggleExecutionPanel);
  elements.executionIndicator?.addEventListener("click", toggleExecutionPanel);

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
    lastMessageId = Math.min(lastMessageId, Number(message.id || lastMessageId || 0));
    fetchEventMessages().then(() => {
      fetchEventStats();
      fetchHierarchy();
      fetchExecutions();
      fetchSummaries();
    });
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
            <div class="markdown-content role-history-markdown">${renderMarkdown(review.summary_text || "")}</div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderHierarchy() {
  if (!hierarchyData.length) {
    elements.eventTreeContainer.innerHTML = `<p>暂无溯源任务树</p>`;
    return;
  }
  const latestRound = [...hierarchyData]
    .sort((left, right) => Number(left.round_id || 0) - Number(right.round_id || 0))
    .at(-1);
  const latestTree = latestRound?.tree;
  if (!latestTree?.root_nodes?.length) {
    elements.eventTreeContainer.innerHTML = `
      <div class="event-tree-round">
        <div class="event-tree-round-title">最新 Round ${escapeHtml(latestRound?.round_id || "--")}</div>
        <p>当前轮次还没有可展示的溯源任务树。</p>
      </div>
    `;
    return;
  }
  elements.eventTreeContainer.innerHTML = `
    <div class="event-tree-round">
      <div class="event-tree-round-title">最新溯源任务树 · Round ${escapeHtml(latestRound.round_id)}</div>
      <div class="event-tree-summary">
        <span>根方向数：${escapeHtml(latestTree.root_nodes.length)}</span>
        <span>Executions：${escapeHtml(latestRound.executions.length)}</span>
        <span>Reviews：${escapeHtml(latestRound.reviews.length)}</span>
      </div>
      <div class="ttt-tree modal-ttt-tree">${renderTTTTree(latestTree.root_nodes || [])}</div>
    </div>
  `;
}

function enqueueMessage(message, fromSocket) {
  const messageKey = message.message_id || `${message.id}-${message.created_at}`;
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
  window.setTimeout(() => {
    isRenderingMessage = false;
    processMessageQueue();
  }, getMessageRenderDelay(message.message_type));
  return true;
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
    return renderTTTMessage("初始化 TTT 完成", payload.ttt, "Planner 已给出初始任务树。");
  }
  if (messageType === "ttt_updated") {
    return renderTTTMessage("TTT 更新完成", payload.ttt, "Planner 已根据 review 调整了下一轮任务树。");
  }
  if (messageType === "overall_assessment_created") {
    return renderOverallAssessmentCard(payload);
  }
  if (messageType === "leaf_claimed") {
    return renderInfoCard("选中 TTT 节点", [
      payload.node_title || payload.node_id || "已领取一个待执行叶子节点",
    ]);
  }
  if (messageType === "tool_selected") {
    return renderInfoCard("选择执行工具", [
      `工具：${payload.tool_name || "未选择"}`,
      payload.node_id ? `节点：${payload.node_id}` : "",
    ].filter(Boolean));
  }
  if (messageType === "execution_started") {
    return renderInfoCard(
      "开始执行",
      [
        payload.tool_name ? `正在调用 ${payload.tool_name}` : "正在执行工具",
        payload.node_id ? `节点：${payload.node_id}` : "",
      ].filter(Boolean),
      { loading: true },
    );
  }
  if (messageType === "execution_completed" || messageType === "execution_failed") {
    return renderExecutionResultCard(payload, messageType === "execution_completed");
  }
  if (messageType === "round_review_started") {
    return renderInfoCard("开始 Review", [
      `Reviewer 正在基于当前执行结果进行总结`,
      payload.execution_count !== undefined ? `执行记录数：${payload.execution_count}` : "",
    ].filter(Boolean));
  }
  if (messageType === "round_review_created") {
    return renderReviewCard(payload);
  }
  if (messageType === "handoff_to_planner") {
    return renderInfoCard("交回 Planner", ["本轮 review 已完成，等待更新 TTT。"]);
  }
  if (messageType === "handoff_to_reviewer") {
    return renderInfoCard("交回 Reviewer", ["本次执行已完成，等待 Reviewer 总结。"]);
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
    return renderInfoCard("接收到告警，开始初始分析", [
      payload.event_name ? `事件：${payload.event_name}` : "Planner 正在理解这条新告警。",
    ]);
  }
  if (text === "planner_start_procedural_memory_lookup") {
    return renderInfoCard("开始检索 Long-term Memory", [
      "Planner 正在检索 procedural memory，寻找可复用的调查 workflow。",
    ]);
  }
  if (text === "planner_procedural_memory_lookup_completed") {
    const selected = payload.selected_procedural_memory;
    const lines = selected
      ? [
          `检索到：${selected.title || selected.document_id}`,
          selected.summary || "",
        ].filter(Boolean)
      : ["未检索到合适的 procedural memory，将直接基于告警分析构建 TTT。"];
    return renderInfoCard("Long-term Memory 检索完成", lines, {
      collapsibleJson: selected || { matched: false },
      collapsibleTitle: selected ? "查看检索命中详情" : "查看检索结果",
    });
  }
  if (text === "planner_start_ttt_initialization") {
    return renderInfoCard("根据检索结果，开始初始化 TTT", [
      "Planner 正在把告警分析与 procedural memory 转成任务树。",
    ]);
  }
  if (text === "planner_start_ttt_replanning") {
    return renderInfoCard("根据 Review 结果，开始调整 TTT", [
      payload.review_round ? `基于 Round ${payload.review_round} 的总结进行重规划。` : "Planner 正在更新任务树。",
    ]);
  }
  return `<p>${escapeHtml(text || "系统消息")}</p>`;
}

function renderPlannerAnalysis(payload) {
  const analysis = payload.analysis || "";
  return `
    <div class="dialog-card">
      <div class="dialog-title">初始分析结果</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(analysis || "Planner 已完成初始分析。")}</div>
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
      <div class="dialog-title">${success ? "执行结果" : "执行失败"}</div>
      ${renderKeyValueGrid([
        ["节点", payload.node_title || payload.node_id || "-"],
        ["工具", payload.tool_name || "-"],
        ["状态", success ? "成功" : "失败"],
      ])}
      ${payload.error_message ? `<p class="dialog-error">${escapeHtml(payload.error_message)}</p>` : ""}
      <div class="collapsible-result">
        <div class="collapsible-header">
          <span class="collapse-icon collapsed">▸</span>
          <span>查看执行结果</span>
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
      <div class="dialog-title">Review 结果</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(payload.summary_text || "Reviewer 已完成本轮总结。")}</div>
    </div>
  `;
}

function renderOverallAssessmentCard(payload) {
  return `
    <div class="dialog-card">
      <div class="dialog-title">整体研判结论</div>
      <div class="dialog-markdown markdown-content">${renderMarkdown(payload.summary_text || "Planner 已完成整体研判总结。")}</div>
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
            <span>${escapeHtml(options.collapsibleTitle || "查看详情")}</span>
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
  if (!Array.isArray(nodes) || !nodes.length) return "<p>暂无 TTT</p>";
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
