const bridge = window.AstrBotPluginPage;
const PAGE_KEY = "pages.records";
const GLOBAL_SESSION_ID = "global";

const dom = {
  refresh: document.getElementById("refresh"),
  sessionFilter: document.getElementById("session-filter"),
  keyword: document.getElementById("keyword"),
  sortBy: document.getElementById("sort-by"),
  sortDesc: document.getElementById("sort-desc"),
  status: document.getElementById("status"),
  overview: document.getElementById("overview"),
  body: document.getElementById("records-body"),
  empty: document.getElementById("empty"),
  prev: document.getElementById("prev"),
  next: document.getElementById("next"),
  pagerLabel: document.getElementById("pager-label"),
  editor: document.getElementById("editor"),
  editorForm: document.getElementById("editor-form"),
  editorSubject: document.getElementById("editor-subject"),
  editorFavour: document.getElementById("editor-favour"),
  editorRelationship: document.getElementById("editor-relationship"),
  editorImpression: document.getElementById("editor-impression"),
  editorAllSessions: document.getElementById("editor-all-sessions"),
  editorError: document.getElementById("editor-error"),
  editorCancel: document.getElementById("editor-cancel"),
};

const state = {
  page: 1,
  pageSize: 20,
  totalPages: 1,
  sessionId: GLOBAL_SESSION_ID,
  sessionName: "",
  editingRow: null,
};

function t(key, fallback) {
  return bridge.t(`${PAGE_KEY}.${key}`, fallback);
}

function setStatus(message, isError = false) {
  dom.status.textContent = message;
  dom.status.classList.toggle("error", isError);
}

function applyI18n() {
  document.title = t("title", "好感度管理台");
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n, element.textContent);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder, element.placeholder);
  });
}

function renderOverview(overview) {
  if (!overview) {
    dom.overview.replaceChildren();
    return;
  }
  const cards = [
    { label: t("statTotal", "记录总数"), value: overview.total },
    { label: t("statAverage", "平均好感度"), value: overview.average_favour },
    { label: t("statMax", "最高好感度"), value: overview.max_favour },
    { label: t("statMin", "最低好感度"), value: overview.min_favour },
    {
      label: t("statTodayNet", "今日净变化"),
      value: overview.today_net_change > 0 ? `+${overview.today_net_change}` : overview.today_net_change,
      className: overview.today_net_change > 0 ? "positive" : overview.today_net_change < 0 ? "negative" : "",
    },
  ];
  dom.overview.replaceChildren(
    ...cards.map((card) => {
      const container = document.createElement("div");
      container.className = "overview-card";
      const label = document.createElement("div");
      label.className = "overview-label";
      label.textContent = card.label;
      const value = document.createElement("div");
      value.className = `overview-value delta ${card.className || ""}`.trim();
      value.textContent = card.value;
      container.append(label, value);
      return container;
    }),
  );
}

function renderRows(rows) {
  dom.body.replaceChildren(
    ...rows.map((row) => {
      const tr = document.createElement("tr");

      const userCell = document.createElement("td");
      const wrapper = document.createElement("div");
      wrapper.className = "user-cell";
      const avatar = document.createElement("img");
      avatar.src = `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(row.user_id)}&s=100`;
      avatar.alt = "";
      const userId = document.createElement("span");
      userId.className = "user-id";
      userId.textContent = row.user_id;
      wrapper.append(avatar, userId);
      userCell.append(wrapper);

      const favourCell = document.createElement("td");
      favourCell.className = "num";
      favourCell.textContent = row.favour;

      const relationshipCell = document.createElement("td");
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = row.relationship || t("none", "无");
      relationshipCell.append(chip);

      const todayCell = document.createElement("td");
      todayCell.className = "num";
      const delta = document.createElement("span");
      const change = Number(row.today_change) || 0;
      delta.className = `delta ${change > 0 ? "positive" : change < 0 ? "negative" : ""}`.trim();
      delta.textContent = change > 0 ? `+${change}` : change;
      todayCell.append(delta);

      const impressionCell = document.createElement("td");
      if (row.impression) {
        impressionCell.textContent = row.impression;
      } else {
        impressionCell.className = "muted";
        impressionCell.textContent = t("none", "无");
      }

      const updatedCell = document.createElement("td");
      updatedCell.textContent = row.updated_at || "-";

      const actionsCell = document.createElement("td");
      actionsCell.className = "actions";
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.className = "btn link";
      editButton.textContent = t("edit", "编辑");
      editButton.addEventListener("click", () => openEditor(row));
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "btn link danger";
      deleteButton.textContent = t("delete", "删除");
      deleteButton.addEventListener("click", () => deleteRow(row));
      actionsCell.append(editButton, deleteButton);

      tr.append(
        userCell,
        favourCell,
        relationshipCell,
        todayCell,
        impressionCell,
        updatedCell,
        actionsCell,
      );
      return tr;
    }),
  );
  dom.empty.hidden = rows.length > 0;
}

function renderSessionFilter(sessions) {
  const options = sessions.map((session) => {
    const option = document.createElement("option");
    option.value = session.session_id;
    option.textContent =
      session.session_id === GLOBAL_SESSION_ID ? t("globalSession", "全局") : session.name;
    return option;
  });
  dom.sessionFilter.replaceChildren(...options);
  if (!sessions.some((session) => session.session_id === state.sessionId)) {
    state.sessionId = sessions[0]?.session_id || GLOBAL_SESSION_ID;
  }
  dom.sessionFilter.value = state.sessionId;
  state.sessionName = dom.sessionFilter.selectedOptions[0]?.textContent || state.sessionId;
}

async function load() {
  dom.refresh.disabled = true;
  setStatus(t("loading", "加载中…"));
  try {
    const data = await bridge.apiGet("records", {
      session_id: state.sessionId,
      keyword: dom.keyword.value.trim(),
      sort_by: dom.sortBy.value,
      desc: dom.sortDesc.value,
      page: state.page,
      page_size: state.pageSize,
    });

    state.page = data.page;
    state.pageSize = data.page_size;
    state.totalPages = data.total_pages;
    state.sessionId = data.session_id;

    renderSessionFilter(data.sessions || []);
    renderOverview(data.overview);
    renderRows(data.rows || []);

    dom.pagerLabel.textContent = `${state.page} / ${state.totalPages} · ${t(
      "matched",
      "匹配",
    )} ${data.total}`;
    dom.prev.disabled = state.page <= 1;
    dom.next.disabled = state.page >= state.totalPages;
    setStatus("");
  } catch (error) {
    setStatus(`${t("loadFailed", "加载失败")}: ${error.message}`, true);
  } finally {
    dom.refresh.disabled = false;
  }
}

function openEditor(row) {
  state.editingRow = row;
  dom.editorSubject.textContent = `${t("session", "会话")}: ${
    state.sessionName
  } · ${t("colUser", "用户")}: ${row.user_id}`;
  dom.editorFavour.value = row.favour;
  dom.editorRelationship.value = row.relationship || "";
  dom.editorImpression.value = row.impression || "";
  dom.editorAllSessions.checked = false;
  dom.editorError.textContent = "";
  dom.editor.showModal();
}

async function submitEditor(event) {
  event.preventDefault();
  const row = state.editingRow;
  if (!row) {
    return;
  }

  const favour = Number.parseInt(dom.editorFavour.value, 10);
  if (!Number.isInteger(favour)) {
    dom.editorError.textContent = t("invalidFavour", "好感度必须是整数");
    return;
  }

  const scope = dom.editorAllSessions.checked ? "all" : "session";
  if (scope === "all" && favour === row.favour) {
    dom.editorError.textContent = t("noChange", "没有任何改动");
    return;
  }

  try {
    await bridge.apiPost("records/update", {
      user_id: row.user_id,
      session_id: state.sessionId,
      scope,
      favour,
      relationship: dom.editorRelationship.value.trim(),
      impression: dom.editorImpression.value.trim(),
    });
    dom.editor.close();
    await load();
    setStatus(t("saved", "已保存"));
  } catch (error) {
    dom.editorError.textContent = error.message;
  }
}

async function deleteRow(row) {
  if (!window.confirm(t("confirmDelete", "确定删除该记录吗？此操作不可撤销。"))) {
    return;
  }
  try {
    await bridge.apiPost("records/delete", {
      user_id: row.user_id,
      session_id: state.sessionId,
    });
    await load();
    setStatus(t("deleted", "已删除"));
  } catch (error) {
    setStatus(`${t("deleteFailed", "删除失败")}: ${error.message}`, true);
  }
}

dom.refresh.addEventListener("click", () => {
  state.page = 1;
  load();
});

dom.sessionFilter.addEventListener("change", () => {
  state.sessionId = dom.sessionFilter.value;
  state.page = 1;
  load();
});

dom.keyword.addEventListener("change", () => {
  state.page = 1;
  load();
});

dom.sortBy.addEventListener("change", () => {
  state.page = 1;
  load();
});

dom.sortDesc.addEventListener("change", () => {
  state.page = 1;
  load();
});

dom.prev.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    load();
  }
});

dom.next.addEventListener("click", () => {
  if (state.page < state.totalPages) {
    state.page += 1;
    load();
  }
});

dom.editorCancel.addEventListener("click", () => dom.editor.close());
dom.editorForm.addEventListener("submit", submitEditor);

await bridge.ready();
applyI18n();
bridge.onContext(applyI18n);
await load();
