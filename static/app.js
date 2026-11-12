const $ = (id) => document.getElementById(id);
const API_BASE = "/api/v1";
const STORAGE_KEY = "context-bridge-workspace-v4";
const token = document.querySelector('meta[name="api-token"]').content;

let conversations = { left: null, right: null };
let pendingTransfer = null;
let selectionSnapshot = null;
let selectedIds = new Set();
let busy = false;
let ready = false;


function setStatus(text, kind = "") {
  $("status").textContent = text;
  $("status").className = `status ${kind}`;
}


function log(text) {
  $("activity-log").textContent +=
    `${new Date().toLocaleTimeString()} ${text}\n`;
}


async function api(path, method = "GET", data = undefined) {
  log(`${method} ${API_BASE}${path}`);

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: data === undefined ? undefined : JSON.stringify(data),
  });

  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("The server returned a non-JSON response.");
  }

  if (!response.ok) {
    throw new Error(result.error || `HTTP ${response.status}`);
  }

  return result;
}


function updateControls(value) {
  busy = value;

  document.querySelectorAll(
    "button, textarea, select, input"
  ).forEach((element) => {
    element.disabled = value || !ready;
  });

  $("prepare-button").disabled =
    value || !ready ||
    !conversations.left?.messages.some((m) => m.role === "user");

  $("commit-button").disabled =
    value || !ready ||
    !pendingTransfer || pendingTransfer.status !== "prepared";

  $("download-button").disabled =
    value || !ready || !pendingTransfer;

  $("selection-build").disabled =
    value || !ready || !selectionSnapshot || selectedIds.size === 0;
}


function saveWorkspace() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    left: conversations.left?.id || null,
    right: conversations.right?.id || null,
  }));
}


function showTransfer() {
  $("handoff-json").textContent = pendingTransfer
    ? JSON.stringify(pendingTransfer, null, 2)
    : "No handoff yet.";
}


function invalidatePreparation(reason) {
  pendingTransfer = null;
  showTransfer();
  $("transfer-state").textContent = reason;
}


function renderConversation(side) {
  const container = $(`${side}-messages`);
  const history = conversations[side]?.messages || [];
  container.replaceChildren();

  if (!history.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = side === "left"
      ? "Discuss your project naturally. Select the relevant turns when preparing a handoff."
      : "This assistant starts independently. Context arrives only after an approved import.";
    container.appendChild(empty);
    return;
  }

  for (const item of history) {
    const wrapper = document.createElement("div");
    wrapper.className = `message ${item.role}`;

    const role = document.createElement("div");
    role.className = "message-role";
    role.textContent = item.role === "user"
      ? "YOU / WORKFLOW"
      : `MODEL ${side === "left" ? "A" : "B"}`;

    const content = document.createElement("div");
    content.className = "message-content";
    content.textContent = item.content;

    const evidence = document.createElement("span");
    evidence.className = "message-id";
    evidence.textContent = `Evidence ID: ${item.id}`;

    wrapper.append(role, content, evidence);
    container.appendChild(wrapper);
  }

  container.scrollTop = container.scrollHeight;
}


async function ensureConversation(side) {
  if (!conversations[side]) {
    conversations[side] = await api("/conversations", "POST", {
      role: side === "left" ? "source" : "destination",
    });
    saveWorkspace();
  }
  return conversations[side];
}


async function sendMessage(side) {
  if (busy || !ready) return;

  const input = $(`${side}-input`);
  const text = input.value.trim();
  if (!text) return;

  updateControls(true);
  setStatus(`Model ${side === "left" ? "A" : "B"} is responding…`, "busy");

  try {
    const conversation = await ensureConversation(side);

    const result = await api(
      `/conversations/${conversation.id}/messages`,
      "POST",
      { message: text }
    );

    conversations[side] = result.conversation;
    input.value = "";
    renderConversation(side);

    if (pendingTransfer?.status === "prepared") {
      invalidatePreparation("Conversation changed — prepare again");
    }

    log(`MODEL: ${side === "left" ? "source" : "destination"} generation.`);

    setStatus(
      result.truncated
        ? "Response reached the output-token limit."
        : "Response received. History retained in server memory.",
      "success"
    );
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    updateControls(false);
    input.focus();
  }
}


for (const side of ["left", "right"]) {
  $(`${side}-form`).addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(side);
  });

  $(`${side}-input`).addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage(side);
    }
  });
}


function updateSelectionCount() {
  if (!selectionSnapshot) return;

  const users = selectionSnapshot.messages.filter((m) => m.role === "user");
  let replyCount = 0;

  selectionSnapshot.messages.forEach((item, index, history) => {
    if (
      item.role === "user" &&
      selectedIds.has(item.id) &&
      history[index + 1]?.role === "assistant"
    ) {
      replyCount += 1;
    }
  });

  $("selection-count").textContent =
    `${selectedIds.size} of ${users.length} user messages selected` +
    (selectionSnapshot.mode === "messages"
      ? ` · ${replyCount} paired replies will also be included`
      : " · assistant replies are excluded");

  $("selection-all").checked =
    users.length > 0 && selectedIds.size === users.length;

  $("selection-all").indeterminate =
    selectedIds.size > 0 && selectedIds.size < users.length;

  $("selection-build").disabled =
    busy || !ready || selectedIds.size === 0;
}


function renderSelectionRows() {
  const body = $("selection-body");
  body.replaceChildren();

  const users = selectionSnapshot.messages.filter((m) => m.role === "user");

  users.forEach((item, index) => {
    const row = document.createElement("tr");
    row.className = "border-t border-gray-200";

    const checkboxCell = document.createElement("td");
    checkboxCell.className = "px-5 py-4 align-top";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "size-4 accent-gray-800";
    checkbox.checked = selectedIds.has(item.id);
    checkbox.dataset.messageId = item.id;
    checkbox.setAttribute("aria-label", `Include user message ${index + 1}`);

    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedIds.add(item.id);
      else selectedIds.delete(item.id);
      updateSelectionCount();
    });

    checkboxCell.appendChild(checkbox);

    const idCell = document.createElement("td");
    idCell.className = "px-4 py-4 align-top font-mono text-xs text-gray-500";
    idCell.textContent = item.id;

    const textCell = document.createElement("td");
    textCell.className =
      "px-5 py-4 align-top text-sm leading-6 whitespace-pre-wrap wrap-anywhere";
    textCell.textContent = item.content;

    row.append(checkboxCell, idCell, textCell);
    body.appendChild(row);
  });

  updateSelectionCount();
}


$("prepare-button").addEventListener("click", async () => {
  if (busy || !ready || !conversations.left) return;

  updateControls(true);
  setStatus("Loading source messages for selection…", "busy");

  try {
    const source = await api(
      `/conversations/${conversations.left.id}`
    );

    conversations.left = source;
    renderConversation("left");

    if (!source.messages.some((m) => m.role === "user")) {
      throw new Error("There are no user messages to select.");
    }

    invalidatePreparation("Select messages to prepare a new handoff");

    selectionSnapshot = {
      ...source,
      mode: $("transfer-mode").value,
    };

    selectedIds = new Set();

    $("selection-policy").textContent =
      selectionSnapshot.mode === "messages"
        ? "Raw mode"
        : "Summary mode";

    renderSelectionRows();
    $("selection-modal").showModal();

    setStatus("Select the messages to include in the handoff.");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    updateControls(false);
  }
});


$("selection-all").addEventListener("change", (event) => {
  const users = selectionSnapshot.messages.filter((m) => m.role === "user");

  selectedIds = event.target.checked
    ? new Set(users.map((m) => m.id))
    : new Set();

  $("selection-body").querySelectorAll(
    'input[type="checkbox"]'
  ).forEach((checkbox) => {
    checkbox.checked = selectedIds.has(checkbox.dataset.messageId);
  });

  updateSelectionCount();
});


$("selection-cancel").addEventListener("click", () => {
  $("selection-modal").close();
});


$("selection-modal").addEventListener("close", () => {
  selectionSnapshot = null;
  selectedIds.clear();
  $("selection-body").replaceChildren();
  $("selection-count").textContent = "";
  updateControls(busy);
});


$("selection-modal").addEventListener("cancel", (event) => {
  if (busy) event.preventDefault();
});


$("selection-build").addEventListener("click", async () => {
  if (busy || !selectionSnapshot || selectedIds.size === 0) return;

  const snapshot = selectionSnapshot;
  const ids = [...selectedIds];

  updateControls(true);
  $("selection-modal").close();

  setStatus(
    snapshot.mode === "summary"
      ? "Summarizing only the selected user messages…"
      : "Packaging selected user messages and their paired replies…",
    "busy"
  );

  try {
    const destination = await ensureConversation("right");

    pendingTransfer = await api("/transfers/prepare", "POST", {
      source_id: snapshot.id,
      destination_id: destination.id,
      source_revision: snapshot.revision,
      selected_user_message_ids: ids,
      mode: snapshot.mode,
    });

    showTransfer();
    $("transfer-state").textContent = "Prepared — not imported";

    log(
      snapshot.mode === "summary"
        ? "MODEL + TOOL: selected-user summary generated and validated."
        : "TOOL ONLY: selected turns packaged; skipped turns and replies excluded."
    );

    const selection = pendingTransfer.package.selection;

    setStatus(
      `Prepared from ${selection.selected_user_message_count} selected user messages. ` +
      "Review the package and approve the destination import.",
      "success"
    );
  } catch (error) {
    $("transfer-state").textContent = "Preparation failed";
    setStatus(error.message, "error");
  } finally {
    updateControls(false);
  }
});


$("transfer-mode").addEventListener("change", () => {
  if (pendingTransfer?.status === "prepared") {
    invalidatePreparation("Mode changed — prepare again");
    updateControls(false);
  }
});


$("commit-button").addEventListener("click", async () => {
  if (busy || !ready || !pendingTransfer) return;

  updateControls(true);
  let imported = false;

  try {
    setStatus("Importing the approved handoff…", "busy");

    pendingTransfer = await api(
      `/transfers/${pendingTransfer.id}/commit`,
      "POST",
      {
        approved: true,
        payload_sha256: pendingTransfer.payload_sha256,
      }
    );

    imported = true;
    showTransfer();
    $("transfer-state").textContent = "Imported";

    log("TOOL ONLY: exact package approved and imported; no model call.");

    const destinationId =
      pendingTransfer.package.destination.conversation_id;

    conversations.right = await api(
      `/conversations/${destinationId}`
    );

    if (!pendingTransfer.already_imported) {
      setStatus("Import succeeded. Model B is continuing…", "busy");

      const result = await api(
        `/conversations/${destinationId}/continue`,
        "POST",
        {}
      );

      conversations.right = result.conversation;
      renderConversation("right");

      log("MODEL: destination continuation after import.");

      setStatus(
        result.truncated
          ? "Import succeeded. Continuation reached the output-token limit."
          : "Selected context imported and Model B continued the project.",
        "success"
      );
    } else {
      renderConversation("right");
      setStatus("Already imported. No duplicate continuation.", "success");
    }
  } catch (error) {
    $("transfer-state").textContent = imported
      ? "Imported — continuation failed"
      : "Import failed";

    setStatus(
      (imported
        ? "Import succeeded, but continuation failed. You can send a message on the right. "
        : "") + error.message,
      "error"
    );
  } finally {
    updateControls(false);
  }
});


$("download-button").addEventListener("click", () => {
  if (!pendingTransfer) return;

  const blob = new Blob(
    [JSON.stringify(pendingTransfer.package, null, 2)],
    { type: "application/json" }
  );

  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = url;
  link.download = "context-handoff.json";
  link.click();

  setTimeout(() => URL.revokeObjectURL(url), 1000);
});


$("clear-button").addEventListener("click", async () => {
  if (busy || !ready) return;

  if (!window.confirm(
    "Clear both conversations and their related handoffs from application memory?"
  )) return;

  updateControls(true);

  try {
    await api("/sessions/clear", "POST", {
      conversation_ids: [
        conversations.left?.id,
        conversations.right?.id,
      ].filter(Boolean),
    });

    conversations = { left: null, right: null };
    pendingTransfer = null;

    $("left-input").value = "";
    $("right-input").value = "";
    localStorage.removeItem(STORAGE_KEY);

    renderConversation("left");
    renderConversation("right");
    showTransfer();

    $("transfer-state").textContent = "No handoff prepared";
    $("activity-log").textContent = "";

    setStatus("Workspace cleared from application memory.", "success");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    updateControls(false);
  }
});


async function initialize() {
  updateControls(true);
  renderConversation("left");
  renderConversation("right");

  try {
    const config = await api("/config");
    const label = (spec) => `${spec.provider} · ${spec.model}`;

    $("left-model").textContent = label(config.models.source);
    $("right-model").textContent = label(config.models.destination);
    $("extractor-info").textContent =
      `Summary extractor: ${label(config.models.extractor)}`;

    let saved = {};

    try {
      saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch {
      localStorage.removeItem(STORAGE_KEY);
    }

    for (const side of ["left", "right"]) {
      if (typeof saved?.[side] === "string") {
        try {
          conversations[side] = await api(
            `/conversations/${saved[side]}`
          );
        } catch {
          log(`${side} conversation unavailable or expired.`);
        }
      }
      renderConversation(side);
    }

    saveWorkspace();
    ready = true;

    setStatus(
      "API ready. Prepare handoff opens the message-selection window.",
      "success"
    );
  } catch (error) {
    setStatus(error.message + " Reload after fixing the server.", "error");
  } finally {
    updateControls(false);
  }
}


initialize();