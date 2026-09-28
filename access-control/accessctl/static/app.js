/* 楼宇门禁卡系统前端逻辑（无框架，原生 JS） */
"use strict";

const STATE = {
  operator: null,
  operators: [],
  people: [],
  cards: [],
  groups: [],
  devices: [],
  buildings: [],
  drafts: [],
  cardFilter: "",
  revealAll: false,
  currentDraft: null,
  selectedGroupFloors: [],
};

const STATUS_CN = {
  in_stock: "在库", active: "可用", lost: "挂失", damaged: "损坏",
  frozen: "待回收", recycled: "已回收", expired: "已失效",
};
const EVENT_CN = {
  card_issued: "发卡激活", card_lost: "卡片遗失", card_damaged: "卡片损坏",
  card_replaced: "补卡换发", card_borrowed: "借用转交", card_frozen: "离场冻结",
  card_recycled: "卡片回收", temp_expired: "临时卡到期",
  permission_changed: "权限变更", permission_violation: "权限越界",
  device_offline: "设备离线", device_online: "设备恢复",
};
const ACTION_CN = {
  issue: "发卡激活", lost: "挂失", damaged: "损坏", replaced: "被替换",
  freeze: "离场冻结", recycle: "回收", expire: "到期失效", loan: "借用转交",
};
const ALL_FLOORS = [1, 2, 3, 4, 5, 6];

/* -------------------------------------------------------------- 工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (STATE.operator) headers["X-Operator-Id"] = STATE.operator.id;
  const res = await fetch(path, {
    headers,
    method: opts.method || "GET",
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `请求失败 (${res.status})`);
    err.code = data.code;
    err.status = res.status;
    throw err;
  }
  return data;
}

function formData(form) {
  const out = {};
  new FormData(form).forEach((v, k) => { out[k] = v === "" ? null : v; });
  return out;
}

function confirmModal(title, detailRows, confirmText = "确认", danger = false) {
  return new Promise(resolve => {
    const modal = $("#modal");
    const rows = detailRows.map(([k, v]) =>
      `<p><span>${esc(k)}</span><b>${esc(v)}</b></p>`).join("");
    $("#modalCard").innerHTML = `
      <h3>${esc(title)}</h3>
      <div class="modal-detail">${rows}</div>
      <div class="modal-actions">
        <button class="btn ghost" id="mCancel">取消</button>
        <button class="btn ${danger ? "danger" : "primary"}" id="mOk">${esc(confirmText)}</button>
      </div>`;
    modal.classList.remove("hidden");
    const close = val => { modal.classList.add("hidden"); resolve(val); };
    $("#mCancel").onclick = () => close(false);
    $("#mOk").onclick = () => close(true);
    modal.onclick = e => { if (e.target === modal) close(false); };
  });
}

/* -------------------------------------------------------------- 初始化 */

async function init() {
  const meta = await api("/api/meta");
  STATE.operators = meta.operators;
  const sel = $("#operatorSelect");
  sel.innerHTML = meta.operators.map(o =>
    `<option value="${o.id}">${o.id} · ${esc(o.name)}（${roleName(o.role)}）</option>`
  ).join("");
  STATE.operator = meta.operators[0];
  sel.value = STATE.operator.id;
  sel.onchange = () => {
    STATE.operator = meta.operators.find(o => o.id === Number(sel.value));
    renderRoleBadge();
    applyRolePermissions();
    refreshActiveView();
  };
  renderRoleBadge();

  $$(".tab").forEach(tab => tab.onclick = () => {
    $$(".tab").forEach(t => t.classList.remove("active"));
    $$(".view").forEach(v => v.classList.remove("active"));
    tab.classList.add("active");
    $(`#view-${tab.dataset.view}`).classList.add("active");
    refreshActiveView();
  });

  bindGlobalActions();
  bindIssueForm();
  bindGroupForm();
  bindLostForms();
  bindDeviceForms();
  applyRolePermissions();
  refreshActiveView();
  setInterval(() => {
    const active = $(".tab.active").dataset.view;
    if (["overview", "issue", "devices", "lost", "cards"].includes(active)) {
      refreshActiveView();
    }
  }, 12000);
}

function roleName(role) {
  return { admin: "管理员", clerk: "发卡柜台", guard: "门岗保安", auditor: "审计员" }[role] || role;
}
function renderRoleBadge() {
  $("#roleBadge").textContent = roleName(STATE.operator.role);
}
function applyRolePermissions() {
  const r = STATE.operator.role;
  const canIssue = r === "admin" || r === "clerk";
  const canRecycle = r === "admin" || r === "clerk";
  const canGuard = r === "admin" || r === "guard";
  const adminOnly = r === "admin";
  // 发卡 / 回收 / 借用 / 补卡
  $$("#view-issue .btn.primary, #view-issue .btn.warn, #view-issue .btn.ghost, #issueForm button, #recycleForm button, #borrowForm button")
    .forEach(b => b.disabled = !canIssue && !(b.id === "btnDepart"));
  $("#btnDepart").disabled = !canRecycle;
  // 挂失：柜台/门岗均可；损坏、补卡：柜台/管理员
  $("#lostForm button").disabled = !(canIssue || canGuard);
  $("#damagedForm button").disabled = !canIssue;
  $("#replaceForm button").disabled = !canIssue;
  $("#restoreForm button").disabled = !canIssue;
  // 设备
  $$("#view-devices .btn").forEach(b => {
    if (b.closest("#accessForm")) b.disabled = !(canIssue || canGuard);
  });
  // 权限组修改仅管理员
  $$("#groupForm button").forEach(b => b.disabled = !adminOnly);
  $$("#changesTable .btn").forEach(b => b.disabled = !adminOnly);
}

async function loadCore() {
  const [people, cards, groups, devices, buildings] = await Promise.all([
    api("/api/people"), api("/api/cards"), api("/api/groups"),
    api("/api/devices"), api("/api/buildings"),
  ]);
  STATE.people = people; STATE.cards = cards; STATE.groups = groups;
  STATE.devices = devices; STATE.buildings = buildings;
}

async function refreshActiveView() {
  const view = $(".tab.active").dataset.view;
  try {
    if (view === "overview") await renderOverview();
    else {
      await loadCore();
      if (view === "people") renderPeople();
      if (view === "cards") renderCards();
      if (view === "groups") await renderGroups();
      if (view === "issue") await renderIssue();
      if (view === "lost") await renderLost();
      if (view === "devices") await renderDevices();
      if (view === "audit") await renderAudit();
      applyRolePermissions();
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

/* -------------------------------------------------------------- 总览 */

async function renderOverview() {
  const [ov, groups, events] = await Promise.all([
    api("/api/overview"), api("/api/groups"), api("/api/events?limit=8"),
  ]);
  const cards = ov.cards || {};
  const kpis = [
    ["在册人员", ov.people_total, ""],
    ["可用卡", cards.active || 0, "ok"],
    ["在库卡", cards.in_stock || 0, ""],
    ["挂失卡", cards.lost || 0, "danger"],
    ["待回收", cards.frozen || 0, "warn"],
    ["离线设备", ov.offline_devices, ov.offline_devices ? "danger" : "ok"],
    ["待补传指令", ov.pending_outbox, ov.pending_outbox ? "warn" : "ok"],
    ["待确认发卡单", ov.pending_drafts, ov.pending_drafts ? "warn" : ""],
  ];
  $("#kpiGrid").innerHTML = kpis.map(([l, v, cls]) =>
    `<div class="kpi ${cls}"><div class="k-label">${l}</div>
     <div class="k-value">${v}</div></div>`).join("");

  $("#overviewMatrix").innerHTML = matrixHtml(groups);
  $("#overviewEvents").innerHTML = events.length
    ? events.map(evItemHtml).join("")
    : `<p class="muted" style="padding:8px 0">暂无事件</p>`;
}

/* -------------------------------------------------------------- 楼层矩阵 */

function matrixHtml(groups) {
  const floors = ALL_FLOORS;
  return `<table class="matrix">
    <thead><tr><th>权限组</th>${floors.map(f => `<th>${f}F</th>`).join("")}
      <th>时段</th><th>有效期</th></tr></thead>
    <tbody>${groups.map(g => `
      <tr class="matrix-row-selectable" data-group="${g.id}">
        <td><div class="g-name">${esc(g.name)}</div>
            <div class="g-sub">${esc(g.description || "")}</div></td>
        ${floors.map(f => `<td><span class="cell ${g.floors.includes(f) ? "allowed" : "denied"}">
          ${g.floors.includes(f) ? "✓" : "·"}</span></td>`).join("")}
        <td class="g-sub">${esc(g.time_start)}–${esc(g.time_end)}</td>
        <td class="g-sub">${g.valid_from || "长期"} ~ ${g.valid_to || "长期"}</td>
      </tr>`).join("")}
    </tbody></table>`;
}

/* -------------------------------------------------------------- 人员 */

function renderPeople() {
  $("#peopleTable tbody").innerHTML = STATE.people.map(p => `
    <tr>
      <td>${esc(p.person_no)}</td>
      <td><b>${esc(p.name)}</b></td>
      <td>${esc(p.tenant_name || "—")}</td>
      <td>${esc(p.building_name || "")} ${esc(p.room_no || "—")}</td>
      <td>${esc(p.position || "—")}</td>
      <td class="muted">${esc(p.phone || "—")}</td>
      <td>${p.status === "active"
        ? '<span class="badge active">在岗</span>'
        : '<span class="badge frozen">已离场</span>'}</td>
    </tr>`).join("");
  $("#departPerson").innerHTML = STATE.people
    .filter(p => p.status === "active")
    .map(p => `<option value="${p.id}">${esc(p.name)}（${esc(p.person_no)}）</option>`)
    .join("");
}

/* -------------------------------------------------------------- 卡片库存 */

function cardNoDisplay(card) {
  return STATE.revealAll ? card.card_no : card.card_no_masked;
}

function keycardHtml(c) {
  const rel = c.old_card_no
    ? `<span class="kc-rel">换自 ${esc(STATE.revealAll ? c.old_card_no : c.old_card_no_masked)}</span>`
    : c.new_card_no
      ? `<span class="kc-rel">已换发 → ${esc(STATE.revealAll ? c.new_card_no : c.new_card_no_masked)}</span>`
      : `<span class="kc-rel">${c.card_type === "temporary" ? "临时卡" : "正式卡"}</span>`;
  return `<div class="keycard ${c.status}">
    <div class="kc-type">${esc(c.building_name || "云升大厦")} · 批次 ${esc(c.batch_no || "—")}</div>
    <div class="kc-no">
      <span data-no="${esc(c.card_no)}" data-masked="${esc(c.card_no_masked)}">${esc(cardNoDisplay(c))}</span>
      <button class="eye-btn" data-reveal="${esc(c.card_no)}" title="揭示/遮挡">👁</button>
    </div>
    <div class="kc-person">${esc(c.person_name || "未绑定人员")}</div>
    <div class="kc-meta">${esc(c.group_name || "无权限组")} · ${esc(c.room_no || "无门牌")}</div>
    ${c.expires_at ? `<div class="kc-meta">到期：${esc(c.expires_at)}</div>` : ""}
    <div class="kc-foot">
      ${rel}
      <span class="badge ${c.status}">${STATUS_CN[c.status]}</span>
    </div>
  </div>`;
}

function renderCards() {
  const list = STATE.cardFilter
    ? STATE.cards.filter(c => c.status === STATE.cardFilter)
    : STATE.cards;
  $("#cardGrid").innerHTML = list.length
    ? list.map(keycardHtml).join("")
    : `<p class="muted" style="padding:18px">该状态下暂无卡片</p>`;
}

/* -------------------------------------------------------------- 权限组 */

async function renderGroups() {
  $("#groupMatrix").innerHTML = matrixHtml(STATE.groups);
  $$("#groupMatrix .matrix-row-selectable").forEach(tr => tr.onclick = () => {
    const g = STATE.groups.find(x => x.id === Number(tr.dataset.group));
    $("#changeGroup").value = g.id;
    syncGroupForm(g);
    $("#groupForm").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  const sel = $("#changeGroup");
  if (!sel.options.length) {
    sel.innerHTML = STATE.groups.map(g =>
      `<option value="${g.id}">${esc(g.name)}</option>`).join("");
    sel.onchange = () =>
      syncGroupForm(STATE.groups.find(g => g.id === Number(sel.value)));
    syncGroupForm(STATE.groups[0]);
  }
  await renderChanges();
}

function syncGroupForm(g) {
  if (!g) return;
  const form = $("#groupForm");
  form.time_start.value = g.time_start;
  form.time_end.value = g.time_end;
  $("#floorPick").innerHTML = ALL_FLOORS.map(f =>
    `<button type="button" class="floor-chip ${g.floors.includes(f) ? "on" : ""}"
      data-floor="${f}">${f}F</button>`).join("");
  STATE.selectedGroupFloors = [...g.floors];
  $$("#floorPick .floor-chip").forEach(chip => chip.onclick = () => {
    const f = Number(chip.dataset.floor);
    if (chip.classList.contains("on")) {
      chip.classList.remove("on");
      STATE.selectedGroupFloors = STATE.selectedGroupFloors.filter(x => x !== f);
    } else {
      chip.classList.add("on");
      STATE.selectedGroupFloors.push(f);
    }
  });
}

async function renderChanges() {
  const changes = await api("/api/changes");
  const ops = Object.fromEntries(STATE.operators.map(o => [o.id, o.name]));
  const fmt = j => {
    const x = typeof j === "string" ? JSON.parse(j) : j;
    return `楼层[${(x.floors || []).join(",") || "—"}] · ${x.time_start}–${x.time_end}`;
  };
  $("#changesTable tbody").innerHTML = changes.map(c => `
    <tr>
      <td>${c.id}</td>
      <td>${esc(STATE.groups.find(g => g.id === c.group_id)?.name || c.group_id)}</td>
      <td class="muted">${esc(fmt(c.before))}</td>
      <td><b>${esc(fmt(c.after))}</b></td>
      <td>${esc(c.reason || "—")}</td>
      <td>${esc(ops[c.operator_id] || c.operator_id)}</td>
      <td><span class="badge ${c.status}">${({ pending: "待二次确认", confirmed: "已生效", rejected: "已驳回" })[c.status]}</span></td>
      <td>${c.status === "pending"
        ? `<button class="btn primary mini" data-confirm-change="${c.id}">确认生效</button>
           <button class="btn danger-ghost mini" data-reject-change="${c.id}">驳回</button>`
        : "—"}</td>
    </tr>`).join("");
  $$("[data-confirm-change]").forEach(b => b.onclick = async () => {
    const c = changes.find(x => x.id === Number(b.dataset.confirmChange));
    const ok = await confirmModal("二次确认：权限变更", [
      ["权限组", STATE.groups.find(g => g.id === c.group_id)?.name],
      ["变更内容", `楼层[${c.after.floors.join(",")}] · ${c.after.time_start}–${c.after.time_end}`],
      ["变更原因", c.reason || "—"],
    ], "确认生效（第二次确认）");
    if (!ok) return;
    try {
      await api("/api/groups/change/confirm", {
        method: "POST", body: { change_id: c.id, confirm: true },
      });
      toast("权限变更已二次确认并生效", "success");
      await loadCore(); renderGroups();
    } catch (e) { toast(e.message, "error"); }
  });
  $$("[data-reject-change]").forEach(b => b.onclick = async () => {
    try {
      await api("/api/groups/change/confirm", {
        method: "POST",
        body: { change_id: Number(b.dataset.rejectChange), confirm: false },
      });
      toast("已驳回该变更");
      renderChanges();
    } catch (e) { toast(e.message, "error"); }
  });
}

function bindGroupForm() {
  $("#groupForm").onsubmit = async e => {
    e.preventDefault();
    const d = formData(e.target);
    if (!STATE.selectedGroupFloors.length) {
      toast("至少勾选一个授权楼层", "error"); return;
    }
    try {
      const r = await api("/api/groups/change", {
        method: "POST",
        body: {
          group_id: Number(d.group_id),
          floors: STATE.selectedGroupFloors,
          time_start: d.time_start, time_end: d.time_end,
          reason: d.reason,
        },
      });
      toast(`变更单 #${r.id} 已提交，请在下方进行二次确认`, "success");
      e.target.reason.value = "";
      renderChanges();
    } catch (err) { toast(err.message, "error"); }
  };
}

/* -------------------------------------------------------------- 发卡回收 */

function fillIssueSelectors() {
  const inStock = STATE.cards.filter(c => c.status === "in_stock");
  $("#issueCard").innerHTML = inStock.map(c =>
    `<option value="${esc(c.card_no)}">${esc(c.card_no)}（${c.card_type === "temporary" ? "临时" : "正式"}）</option>`
  ).join("") || `<option value="">（无在库卡）</option>`;
  $("#issuePerson").innerHTML = STATE.people.filter(p => p.status === "active")
    .map(p => `<option value="${p.id}">${esc(p.name)}（${esc(p.person_no)}）</option>`).join("");
  $("#issueGroup").innerHTML = STATE.groups.map(g =>
    `<option value="${g.id}">${esc(g.name)}</option>`).join("");

  $("#recycleCard").innerHTML = STATE.cards
    .filter(c => c.status === "frozen" || c.status === "damaged")
    .map(c => `<option value="${esc(c.card_no)}">${esc(c.card_no)} · ${esc(c.person_name || "")} · ${STATUS_CN[c.status]}</option>`)
    .join("") || `<option value="">（无待回收卡）</option>`;

  const activeCards = STATE.cards.filter(c => c.status === "active");
  $("#borrowCard").innerHTML = activeCards.map(c =>
    `<option value="${esc(c.card_no)}">${esc(c.card_no)} · ${esc(c.person_name || "")}</option>`).join("");
  $("#borrowPerson").innerHTML = STATE.people.filter(p => p.status === "active")
    .map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
}

function bindIssueForm() {
  const form = $("#issueForm");
  $("#issueType").onchange = e => {
    document.body.classList.toggle("show-temp", e.target.value === "temporary");
  };
  form.onsubmit = async e => {
    e.preventDefault();
    const d = formData(form);
    if (!d.card_no) { toast("没有可发放的在库卡片", "error"); return; }
    try {
      const draft = await api("/api/issues/draft", {
        method: "POST",
        body: {
          card_no: d.card_no, person_id: Number(d.person_id),
          group_id: Number(d.group_id), card_type: d.card_type,
          valid_from: d.valid_from, valid_to: d.valid_to,
          expires_at: d.expires_at ? d.expires_at.replace("T", " ") + ":00" : null,
          note: d.note,
        },
      });
      STATE.currentDraft = draft;
      showDraftReview(draft);
    } catch (err) {
      if (err.code === "draft_conflict" || err.code === "card_unavailable") {
        toast(err.message, "warn");
      } else toast(err.message, "error");
    }
  };
  $("#draftReview").onclick = async e => {
    if (!STATE.currentDraft) return;
    const id = STATE.currentDraft.id;
    try {
      if (e.target.id === "draftConfirm") {
        await api("/api/issues/confirm", { method: "POST", body: { draft_id: id } });
        toast("第二步确认完成：卡片已绑定人员与权限组并激活", "success");
        $("#draftReview").classList.add("hidden");
        STATE.currentDraft = null;
        setStep(1);
        form.reset();
        document.body.classList.remove("show-temp");
        await loadCore(); await renderIssue();
      } else if (e.target.id === "draftCancel") {
        await api("/api/issues/cancel", { method: "POST", body: { draft_id: id } });
        toast("已撤销发卡单，卡号释放");
        $("#draftReview").classList.add("hidden");
        STATE.currentDraft = null;
        setStep(1);
        await loadCore(); await renderIssue();
      }
    } catch (err) { toast(err.message, "error"); }
  };

  $("#recycleForm").onsubmit = async e => {
    e.preventDefault();
    const card_no = e.target.card_no.value;
    if (!card_no) return;
    const ok = await confirmModal("离场卡片回收登记", [
      ["卡号", card_no],
      ["操作结果", "卡片终态记为「已回收」"],
    ], "确认回收", true);
    if (!ok) return;
    try {
      await api("/api/cards/recycle", { method: "POST", body: { card_no } });
      toast("卡片已回收登记", "success");
      await loadCore(); renderIssue();
    } catch (err) { toast(err.message, "error"); }
  };

  $("#borrowForm").onsubmit = async e => {
    e.preventDefault();
    const d = formData(e.target);
    try {
      await api("/api/cards/borrow", {
        method: "POST",
        body: { card_no: d.card_no, to_person_id: Number(d.to_person_id), note: d.note },
      });
      toast("已登记借用 / 转交，权限仍随卡", "success");
      e.target.note.value = "";
      await loadCore(); renderIssue();
    } catch (err) { toast(err.message, "error"); }
  };
}

function setStep(n) {
  $$(".stepper .step").forEach(s => {
    const k = Number(s.dataset.step);
    s.classList.toggle("active", k === n);
    s.classList.toggle("done", k < n);
  });
}

function showDraftReview(d) {
  setStep(2);
  const person = STATE.people.find(p => p.id === d.person_id);
  const group = STATE.groups.find(g => g.id === d.group_id);
  $("#draftReview").classList.remove("hidden");
  $("#draftReview").innerHTML = `
    <h3>第二步：核对发卡单 ${esc(d.draft_no)}</h3>
    <dl>
      <dt>卡号</dt><dd>${esc(d.card_no)}</dd>
      <dt>持卡人</dt><dd>${esc(person?.name)}（${esc(person?.person_no)}）</dd>
      <dt>权限组</dt><dd>${esc(group?.name)} · 楼层[${(group?.floors || []).join(",")}]</dd>
      <dt>卡类型</dt><dd>${d.card_type === "temporary" ? "临时卡" : "正式卡"}${d.expires_at ? "，到期 " + esc(d.expires_at) : ""}</dd>
      <dt>有效期</dt><dd>${esc(d.valid_from || "长期")} ~ ${esc(d.valid_to || "长期")}</dd>
      <dt>建单柜台</dt><dd>${esc(d.operator_name)}</dd>
    </dl>
    <div class="review-actions">
      <button class="btn ghost" id="draftCancel">撤销（释放卡号）</button>
      <button class="btn primary" id="draftConfirm">信息无误，确认激活</button>
    </div>`;
}

async function renderIssue() {
  fillIssueSelectors();
  const [drafts, loans, lifecycle] = await Promise.all([
    api("/api/drafts"),
    api("/api/loans?active_only=true"),
    api("/api/lifecycle?limit=30"),
  ]);
  STATE.drafts = drafts;
  const pending = drafts.filter(d => d.status === "pending");
  $("#pendingDrafts").innerHTML = pending.length
    ? pending.map(d => `
      <div class="draft-item">
        <div class="di-main">
          <b>${esc(d.card_no)}</b> → ${esc(d.person_name)} · ${esc(d.group_name)}
          <div class="di-sub">单号 ${esc(d.draft_no)} · ${esc(d.operator_name)} 建单于 ${esc(d.created_at)}</div>
        </div>
        <span class="badge pending">待激活</span>
      </div>`).join("")
    : `<p class="muted" style="padding:4px 0 10px">暂无待确认发卡单</p>`;

  $("#loanList").innerHTML = loans.length ? loans.map(l => `
    <div class="loan-item">
      <b>${esc(l.card_no_masked)}</b>：${esc(l.from_name)} → ${esc(l.to_name)}
      <span class="muted">${esc(l.note || "")} · ${esc(l.borrowed_at)}</span>
      <span class="badge active">借用中</span>
    </div>`).join("") : `<p class="muted" style="padding:4px 0">暂无借用记录</p>`;

  $("#lifecycleTable tbody").innerHTML = lifecycle.map(r => `
    <tr>
      <td class="muted">${esc(r.created_at)}</td>
      <td><b>${esc(r.card_no)}</b></td>
      <td>${esc(ACTION_CN[r.action] || r.action)}</td>
      <td>${esc(r.person_name || "—")}</td>
      <td>${esc(r.operator_name || "系统")}</td>
      <td class="muted">${esc(r.detail || "")}</td>
    </tr>`).join("");
}

/* -------------------------------------------------------------- 挂失处理 */

function bindLostForms() {
  $("#lostForm").onsubmit = async e => {
    e.preventDefault();
    const d = formData(e.target);
    const ok = await confirmModal("卡片挂失", [
      ["卡号", d.card_no],
      ["即时处理", "权限冻结并向设备下发吊销"],
      ["后续", "须用新卡补卡换发，旧卡不可恢复"],
    ], "确认挂失", true);
    if (!ok) return;
    try {
      await api("/api/cards/lost", { method: "POST", body: { card_no: d.card_no, note: d.note } });
      toast("挂失成功，权限已即时冻结", "success");
      await loadCore(); renderLost();
    } catch (err) { toast(err.message, "error"); }
  };
  $("#damagedForm").onsubmit = async e => {
    e.preventDefault();
    const card_no = $("#lostCard").value;
    const ok = await confirmModal("登记卡片损坏", [["卡号", card_no], ["处理", "损坏报废，可补卡换发"]], "确认损坏", true);
    if (!ok) return;
    try {
      await api("/api/cards/damaged", { method: "POST", body: { card_no } });
      toast("已登记损坏", "success");
      await loadCore(); renderLost();
    } catch (err) { toast(err.message, "error"); }
  };
  $("#replaceForm").onsubmit = async e => {
    e.preventDefault();
    const d = formData(e.target);
    const ok = await confirmModal("补卡换发", [
      ["旧卡", d.old_card_no], ["新卡", d.new_card_no],
      ["关系", "新卡保留旧卡关联，旧卡维持注销不可恢复"],
    ], "确认换发");
    if (!ok) return;
    try {
      await api("/api/cards/replace", {
        method: "POST",
        body: { old_card_no: d.old_card_no, new_card_no: d.new_card_no, note: d.note },
      });
      toast("补卡换发成功，旧卡关系已保留", "success");
      await loadCore(); renderLost();
    } catch (err) { toast(err.message, "error"); }
  };
  $("#restoreForm").onsubmit = async e => {
    e.preventDefault();
    const card_no = e.target.card_no.value;
    try {
      await api("/api/cards/restore-attempt", { method: "POST", body: { card_no } });
    } catch (err) {
      $("#restoreResult").innerHTML =
        `<div class="deny-box">⛔ ${esc(err.message)}</div>`;
      toast("系统已拒绝补录恢复", "warn");
    }
  };
}

async function renderLost() {
  const active = STATE.cards.filter(c => c.status === "active");
  const abn = STATE.cards.filter(c => ["lost", "damaged", "frozen"].includes(c.status));
  $("#lostCard").innerHTML = active.map(c =>
    `<option value="${esc(c.card_no)}">${esc(c.card_no)} · ${esc(c.person_name || "")}</option>`).join("");
  $("#damagedCard").value = $("#lostCard").value;
  $("#replaceOld").innerHTML = STATE.cards
    .filter(c => c.status === "lost" || c.status === "damaged")
    .map(c => `<option value="${esc(c.card_no)}">${esc(c.card_no)} · ${STATUS_CN[c.status]} · ${esc(c.person_name || "")}</option>`)
    .join("");
  $("#replaceNew").innerHTML = STATE.cards.filter(c => c.status === "in_stock")
    .map(c => `<option value="${esc(c.card_no)}">${esc(c.card_no)}</option>`).join("");
  $("#restoreCard").innerHTML = STATE.cards.filter(c => c.status === "lost")
    .map(c => `<option value="${esc(c.card_no)}">${esc(c.card_no)}（挂失）</option>`).join("");
  $("#lostGrid").innerHTML = abn.length
    ? abn.map(keycardHtml).join("")
    : `<p class="muted" style="padding:18px">暂无异常卡</p>`;
}

/* -------------------------------------------------------------- 设备 */

function bindDeviceForms() {
  $("#accessForm").onsubmit = async e => {
    e.preventDefault();
    const d = formData(e.target);
    try {
      const r = await api("/api/access/attempt", {
        method: "POST",
        body: { device_id: Number(d.device_id), card_no: d.card_no, floor: Number(d.floor) },
      });
      const dev = STATE.devices.find(x => x.id === Number(d.device_id));
      $("#accessResult").innerHTML = `
        <div class="result-box ${r.result}">
          <b>${r.result === "granted" ? "✔ 放行" : "⛔ 拒绝"}</b>
          <div style="margin-top:4px">
            ${r.reasons.length ? esc(r.reasons.join("；")) : "楼层、时段、日期均在授权范围"}
            ${r.device_status === "offline" ? '<br><b>设备离线：已本地记录，恢复在线后补传</b>' : ""}
          </div>
        </div>`;
      renderDevices();
    } catch (err) { toast(err.message, "error"); }
  };
}

async function renderDevices() {
  const devices = await api("/api/devices");
  STATE.devices = devices;
  $("#deviceList").innerHTML = devices.map(d => `
    <div class="device-item ${d.status}">
      <div>
        <div class="dv-name">${esc(d.name)}</div>
        <div class="dv-loc">${esc(d.location)}</div>
        ${d.pending_count ? `<div class="dv-pending">${d.pending_count} 条指令缓冲待补传</div>` : ""}
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="badge ${d.status}">${d.status === "online" ? "在线" : "离线"}</span>
        <button class="btn mini ${d.status === "online" ? "danger-ghost" : "primary"}"
          data-device="${d.id}" data-online="${d.status === "online" ? 0 : 1}">
          ${d.status === "online" ? "模拟离线" : "恢复在线并补传"}
        </button>
      </div>
    </div>`).join("");
  $$("#deviceList [data-device]").forEach(b => b.onclick = async () => {
    const online = b.dataset.online === "1";
    try {
      const r = await api("/api/devices/status", {
        method: "POST",
        body: { device_id: Number(b.dataset.device), online },
      });
      if (online) toast(`设备恢复在线，已补传缓冲指令`, "success");
      else toast("设备已置为离线，后续指令进入本地缓冲", "warn");
      renderDevices();
    } catch (err) { toast(err.message, "error"); }
  });

  $("#accessDevice").innerHTML = devices.map(d =>
    `<option value="${d.id}">${esc(d.name)}（${d.status === "online" ? "在线" : "离线"}）</option>`)
    .join("");

  const [outbox, accesses] = await Promise.all([
    api("/api/outbox"), api("/api/access-events?limit=30"),
  ]);
  $("#outboxTable tbody").innerHTML = outbox.map(o => `
    <tr>
      <td>${o.id}</td><td>${esc(o.device_name)}</td>
      <td><b>${esc(o.card_no)}</b></td>
      <td>${o.command === "issue" ? "下发授权" : "吊销权限"}</td>
      <td>${o.status === "delivered"
        ? '<span class="badge confirmed">已送达</span>'
        : '<span class="badge pending">待补传</span>'}</td>
      <td class="muted">${esc(o.created_at)}</td>
      <td class="muted">${esc(o.delivered_at || "—")}</td>
    </tr>`).join("");

  $("#accessLog").innerHTML = accesses.map(a => `
    <div class="access-item">
      <span class="badge ${a.result}">${a.result === "granted" ? "放行" : "拒绝"}</span>
      <div class="ac-mid">
        <b>${esc(a.card_no)}</b> 刷 ${a.floor}F
        <span class="sync-tag ${a.synced ? "synced" : "unsynced"}">
          ${a.synced ? "已同步" : "待补传"}</span>
        <div class="ac-reason">${esc(a.reason || "")} · ${esc(a.device_name)} · ${esc(a.event_at)}</div>
      </div>
    </div>`).join("");
}

/* -------------------------------------------------------------- 审计 */

async function renderAudit() {
  const filter = $("#eventTypeFilter");
  if (!filter.options.length) {
    filter.innerHTML = `<option value="">全部类型</option>` +
      Object.entries(EVENT_CN).map(([k, v]) =>
        `<option value="${k}">${v}</option>`).join("");
    filter.onchange = renderAudit;
  }
  const [events, audits, life] = await Promise.all([
    api(`/api/events?limit=100${filter.value ? "&type=" + filter.value : ""}`),
    api("/api/audit?limit=60"),
    api("/api/lifecycle?limit=60"),
  ]);
  $("#eventList").innerHTML = events.map(evItemHtml).join("") ||
    `<p class="muted" style="padding:10px 0">暂无事件</p>`;
  $("#auditList").innerHTML = audits.map(a => `
    <div class="audit-item">
      <div class="au-head">
        <span class="au-time">${esc(a.created_at)}</span>
        <span class="au-op">${esc(a.operator_name || "系统")}</span>
        <span>${esc(a.action)}</span>
      </div>
      <div class="au-detail">${esc(a.entity || "")}${a.entity_id ? " #" + esc(a.entity_id) : ""}
        ${a.detail ? " · " + esc(a.detail) : ""}</div>
    </div>`).join("");
  $("#lifecycleAudit").innerHTML = life.map(l => `
    <div class="audit-item">
      <div class="au-head">
        <span class="au-time">${esc(l.created_at)}</span>
        <span class="au-op">${esc(l.card_no)}</span>
        <span>${esc(ACTION_CN[l.action] || l.action)}</span>
      </div>
      <div class="au-detail">${esc(l.person_name || "")} · ${esc(l.operator_name || "系统")}
        ${l.detail ? " · " + esc(l.detail) : ""}</div>
    </div>`).join("");
}

function evItemHtml(ev) {
  return `<div class="event-item">
    <span class="sev-dot ${ev.severity}"></span>
    <div style="flex:1">
      <div class="ev-title">${esc(EVENT_CN[ev.type] || ev.type)}
        ${ev.card_no ? `· <code>${esc(ev.card_no)}</code>` : ""}
        ${ev.person_name ? "· " + esc(ev.person_name) : ""}</div>
      <div class="ev-detail">${esc(ev.detail || "")}${ev.device_name ? " · " + esc(ev.device_name) : ""}</div>
    </div>
    <span class="ev-time">${esc(ev.created_at)}</span>
  </div>`;
}

/* -------------------------------------------------------------- 全局动作 */

function bindGlobalActions() {
  // 卡号揭示
  document.addEventListener("click", async e => {
    const btn = e.target.closest("[data-reveal]");
    if (!btn) return;
    const no = btn.dataset.reveal;
    const span = btn.previousElementSibling;
    if (span.textContent === no) {
      span.textContent = span.dataset.masked;
    } else {
      span.textContent = no;
    }
  });
  $("#revealAll").onchange = e => {
    STATE.revealAll = e.target.checked;
    if ($(".tab.active").dataset.view === "cards") renderCards();
    if ($(".tab.active").dataset.view === "lost") renderLost();
  };
  $$("#cardStatusFilter .seg-btn").forEach(b => b.onclick = () => {
    $$("#cardStatusFilter .seg-btn").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    STATE.cardFilter = b.dataset.status;
    renderCards();
  });
  $("#btnDepart").onclick = async () => {
    const id = Number($("#departPerson").value);
    const p = STATE.people.find(x => x.id === id);
    if (!p) return;
    const ok = await confirmModal("人员离场处理", [
      ["人员", `${p.name}（${p.person_no}）`],
      ["处理", "名下使用中卡片全部冻结并下发吊销"],
      ["后续", "实体卡交回后在「发卡回收」页登记回收"],
    ], "确认离场冻结", true);
    if (!ok) return;
    try {
      const r = await api("/api/people/depart", {
        method: "POST", body: { person_id: id, note: "柜台办理离场" },
      });
      toast(`已离场，冻结 ${r.frozen_cards.length} 张卡：${r.frozen_cards.join(", ") || "无"}`, "success");
      await loadCore(); renderPeople();
    } catch (err) { toast(err.message, "error"); }
  };
}

init().catch(e => toast("初始化失败：" + e.message, "error"));
