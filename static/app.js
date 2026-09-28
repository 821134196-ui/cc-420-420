/* 楼宇门禁卡系统前端逻辑（无框架，原生 JS） */
"use strict";

const state = { me: null, data: null, page: "people", reveal: false, pendingRule: null };

const STATUS = {
  available: ["可用", "blue", "s-available"],
  issued:    ["待激活", "amber", "s-issued"],
  lost:      ["已挂失", "red", "s-lost"],
  damaged:   ["损坏待回收", "amber", "s-pending"],
  frozen:    ["已冻结", "amber", "s-pending"],
  expired:   ["临时卡到期", "violet", "s-expired"],
  revoked:   ["已回收", "gray", "s-revoked"],
  recycled:  ["已入库", "gray", "s-revoked"],
};
const PERSON_STATUS = { active: ["在岗", "green"], left: ["已离场", "gray"] };

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = "toast", 3200);
}

async function api(path, body) {
  const opt = {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User-Id": $("#role-select").value },
    body: JSON.stringify(body || {}),
  };
  const res = await fetch(path, opt);
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.error || "请求失败"), { data });
  return data;
}

async function refresh() {
  const res = await fetch(`/api/state?reveal=${state.reveal ? 1 : 0}`, {
    headers: { "X-User-Id": $("#role-select").value },
  });
  const data = await res.json();
  state.me = data.me;
  state.data = data;
  render();
}

/* ---------------- 渲染 ---------------- */

function tag(status) {
  const [label, cls] = STATUS[status] || [status, "gray"];
  return `<span class="tag ${cls}">${label}</span>`;
}

function render() {
  renderDevice();
  renderPeople();
  renderCards();
  renderMatrix();
  renderGroupOptions();
  renderIssueOptions();
  renderLostPage();
  renderAudit();
  applyRole();
}

function renderDevice() {
  const d = state.data.device;
  $("#device-dot").className = "dot " + (d.online ? "on" : "off");
  $("#device-text").textContent = d.online ? "设备在线" : "设备离线";
  $("#device-pending").textContent = d.pending ? `待补传 ${d.pending} 笔` : "";
  $("#btn-device-sync").disabled = d.online && d.pending === 0;
}

function renderPeople() {
  const ps = state.data.people;
  const active = ps.filter(p => p.status === "active").length;
  $("#people-kpi").innerHTML = `
    <div class="kpi"><div class="num">${state.data.tenants.length}</div><div class="lbl">租户</div></div>
    <div class="kpi"><div class="num blue">${ps.length}</div><div class="lbl">人员总数</div></div>
    <div class="kpi"><div class="num blue">${active}</div><div class="lbl">在岗</div></div>
    <div class="kpi"><div class="num gray">${ps.length - active}</div><div class="lbl">已离场</div></div>`;
  $("#people-table").innerHTML = `<table>
    <thead><tr><th>姓名</th><th>所属租户</th><th>门牌</th><th>楼层</th><th>证件号</th><th>电话</th><th>状态</th></tr></thead>
    <tbody>${ps.map(p => {
      const [l, c] = PERSON_STATUS[p.status] || [p.status, "gray"];
      return `<tr>
        <td><b>${esc(p.name)}</b></td>
        <td>${esc(p.tenant_name || "—")}</td>
        <td class="mono">${esc(p.unit_no || "—")}</td>
        <td>${p.floor ?? "—"}F</td>
        <td class="mono muted">${esc(p.id_no)}</td>
        <td class="mono muted">${esc(p.phone)}</td>
        <td><span class="tag ${c}">${l}</span>${p.left_at ? `<div class="muted" style="font-size:11px">${esc(p.left_at)}</div>` : ""}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function renderCards() {
  let cards = state.data.cards;
  const filter = $("#card-filter").value;
  const counts = {};
  cards.forEach(c => counts[c.status] = (counts[c.status] || 0) + 1);
  $("#cards-kpi").innerHTML = `
    <div class="kpi"><div class="num blue">${counts.available || 0}</div><div class="lbl">可用</div></div>
    <div class="kpi"><div class="num" style="color:var(--amber)">${(counts.issued || 0) + (counts.damaged || 0) + (counts.frozen || 0)}</div><div class="lbl">待激活/待回收</div></div>
    <div class="kpi"><div class="num red">${counts.lost || 0}</div><div class="lbl">挂失</div></div>
    <div class="kpi"><div class="num" style="color:var(--violet)">${counts.expired || 0}</div><div class="lbl">到期失效</div></div>
    <div class="kpi"><div class="num gray">${counts.revoked || 0}</div><div class="lbl">已回收</div></div>`;
  if (filter) cards = cards.filter(c => c.status === filter);
  $("#card-grid").innerHTML = cards.map(c => {
    const [, , cls] = STATUS[c.status] || ["", "", ""];
    const t = c.card_type === "temp" ? '<span class="tag violet">临时</span>' : "";
    const replaced = c.replaced_by_no
      ? `<div>↳ 补卡自 <span class="mono">${esc(c.replaced_by_no)}</span></div>` : "";
    const dev = c.device_delivered ? "" : '<span class="tag amber">设备待补传</span>';
    return `<div class="access-card ${cls}">
      <div class="ac-head"><span class="ac-no mono">${esc(c.card_no)}</span>${tag(c.status)}</div>
      <div class="ac-person">${esc(c.person_name || "未绑定")} ${t} ${dev}</div>
      <div class="ac-meta">
        <div>权限组：${esc(c.group_name || "未绑定")}</div>
        <div>发于 ${esc(c.counter || "")} · ${esc((c.issued_at || c.created_at || "").slice(0, 10))}</div>
        ${c.expires_at ? `<div>到期：${esc(c.expires_at)}</div>` : ""}
        ${replaced}
      </div>
    </div>`;
  }).join("") || '<p class="muted">没有符合条件的卡片。</p>';
}

function renderMatrix() {
  const m = state.data.matrix;
  const head = `<tr><th>权限组 ＼ 楼层</th>${m.floors.map(f => `<th>${f}F</th>`).join("")}</tr>`;
  const rows = m.groups.map(g => `<tr>
    <th style="text-align:left;white-space:nowrap">${esc(g.name)}</th>
    ${g.cells.map(c => {
      if (c.state === "none") return '<td class="none" title="无权限">—</td>';
      const r = c.rule;
      const title = `${r.start_time}-${r.end_time} · ${r.valid_from} ~ ${r.valid_to}`;
      const icon = c.state === "allow" ? "✓" : c.state === "time" ? "时" : "日";
      return `<td class="${c.state}" title="${esc(title)}">${icon}<span class="cell-time">${esc(r.start_time.slice(0,5))}</span></td>`;
    }).join("")}
  </tr>`).join("");
  $("#floor-matrix").innerHTML = `<table class="matrix">${head}${rows}</table>`;
}

function groupOptions() {
  return state.data.groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
}
function personOptions() {
  return state.data.people.filter(p => p.status === "active")
    .map(p => `<option value="${p.id}">${esc(p.name)}（${esc(p.unit_no || "无门牌")}）</option>`).join("");
}
function cardOptions(filterFn) {
  return state.data.cards.filter(filterFn || (() => true))
    .map(c => `<option value="${c.id}">${esc(c.card_no)} · ${esc(c.person_name || "未绑定")} · ${(STATUS[c.status] || [c.status])[0]}</option>`).join("");
}

function renderGroupOptions() {
  $("#rule-group").innerHTML = groupOptions();
  const today = new Date().toISOString().slice(0, 10);
  if (!$("#rule-from").value) { $("#rule-from").value = today; $("#rule-to").value = today; }
}

function renderIssueOptions() {
  $("#issue-person").innerHTML = personOptions();
  $("#issue-group").innerHTML = groupOptions();
  const activeCards = c => ["available", "issued"].includes(c.status);
  $("#sim-card").innerHTML = cardOptions(() => true);
  $("#recycle-card").innerHTML = cardOptions(c => ["available", "issued", "lost", "damaged"].includes(c.status));
}

function renderLostPage() {
  const cards = state.data.cards;
  const lost = cards.filter(c => c.status === "lost").length;
  const damaged = cards.filter(c => c.status === "damaged").length;
  $("#lost-kpi").innerHTML = `
    <div class="kpi"><div class="num red">${lost}</div><div class="lbl">挂失中</div></div>
    <div class="kpi"><div class="num amber">${damaged}</div><div class="lbl">损坏待回收</div></div>
    <div class="kpi"><div class="num blue">${cards.filter(c => c.status === "available").length}</div><div class="lbl">当前可用</div></div>`;
  $("#lost-card").innerHTML = cardOptions(c => ["available", "issued"].includes(c.status));
  $("#reissue-card").innerHTML = cardOptions(c => c.status === "lost");
  $("#damage-card").innerHTML = cardOptions(c => ["available", "issued"].includes(c.status));
  $("#transfer-card").innerHTML = cardOptions(c => ["available", "issued"].includes(c.status));
  $("#transfer-person").innerHTML = personOptions();
}

function renderAudit() {
  const digest = state.data.event_digest;
  $("#event-filter").innerHTML =
    `<option value="">全部事件</option>` +
    Object.entries(digest).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  renderEventList();
  const recs = state.data.recycles;
  $("#audit-recycles").innerHTML = recs.length ? `<table>
    <thead><tr><th>卡号</th><th>持有人</th><th>回收前状态</th><th>原因</th><th>经办柜台</th><th>回收时间</th></tr></thead>
    <tbody>${recs.map(r => `<tr>
      <td class="mono">${esc(r.card_no)}</td><td>${esc(r.person_name || "—")}</td>
      <td>${tag(r.card_status)}</td><td>${esc(r.reason)}</td>
      <td>${esc(r.counter)}</td><td class="muted">${esc(r.recycled_at)}</td></tr>`).join("")}
    </tbody></table>` : '<p class="muted">暂无回收记录。</p>';
}

function renderEventList() {
  const filter = $("#event-filter").value;
  const digest = state.data.event_digest;
  let events = state.data.events;
  if (filter) events = events.filter(e => e.event_type === filter);
  $("#event-list").innerHTML = events.slice(0, 150).map(e => `
    <div class="event-row">
      <div class="event-type"><span class="tag blue">${esc(digest[e.event_type] || e.event_type)}</span></div>
      <div style="flex:1">
        ${e.card_no ? `卡号 <span class="mono">${esc(maskInEvent(e.card_no))}</span> · ` : ""}${esc(e.detail)}
      </div>
      <div class="muted" style="white-space:nowrap">${esc(e.actor)} · ${esc(e.created_at)}</div>
    </div>`).join("") || '<p class="muted">暂无事件。</p>';
}

// 事件里的卡号同样按遮挡开关显示（后端明细含明文，这里非 reveal 时遮挡）
function maskInEvent(no) {
  if (state.reveal) return no;
  return no.length > 5 ? no.slice(0, 3) + "*".repeat(no.length - 5) + no.slice(-2) : no;
}

/* 岗位权限：柜员不能改权限组/切换设备离线；审计员只读 */
function applyRole() {
  const role = state.me.role;
  const NAV_IDS = new Set(["role-select", "reveal-check", "card-filter", "event-filter"]);
  // 先全部恢复可用，再按岗位收紧（避免从审计员切回后控件仍被禁用）
  $$("input,select,button").forEach(el => { el.disabled = false; });

  if (role === "clerk") {
    ["btn-device-off", "btn-save-rule", "rule-group", "rule-floor", "rule-start",
     "rule-end", "rule-from", "rule-to"].forEach(id => { const el = $("#" + id); if (el) el.disabled = true; });
  }
  if (role === "auditor") {
    $$("input,select,button").forEach(el => {
      if (!NAV_IDS.has(el.id) && el.type !== "checkbox") el.disabled = true;
    });
    NAV_IDS.forEach(id => { const el = $("#" + id); if (el) el.disabled = false; });
    $("#reveal-check").disabled = false;
  }
  renderDevice(); // 重新叠加设备状态导致的禁用
}

/* ---------------- 交互 ---------------- */

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  $$(".tab").forEach(t => t.classList.toggle("active", t === tab));
  state.page = tab.dataset.page;
  $$(".page").forEach(p => p.classList.toggle("active", p.id === `page-${state.page}`));
});

$("#role-select").addEventListener("change", refresh);
$("#card-filter").addEventListener("change", renderCards);
$("#event-filter").addEventListener("change", renderEventList);
$("#reveal-check").addEventListener("change", async (e) => {
  if (e.target.checked && !["admin", "auditor"].includes(state.me.role)) {
    e.target.checked = false;
    toast("仅管理员/审计员可查看明文号码", true);
    return;
  }
  state.reveal = e.target.checked;
  await refresh();
});

/* 设备 */
$("#btn-device-off").addEventListener("click", async () => {
  try { await api("/api/device/offline", {}); toast("设备已置为离线，新发卡将排队补传"); await refresh(); }
  catch (e) { toast(e.message, true); }
});
$("#btn-device-sync").addEventListener("click", async () => {
  try {
    // 设备离线时先尝试恢复在线（仅管理员有权限），再执行补传
    if (!state.data.device.online) {
      try { await api("/api/device/online", {}); }
      catch (e) { return toast("设备恢复需要管理员权限；请联系管理员上线后补传", true); }
    }
    const r = await api("/api/device/sync", {});
    toast(r.synced ? `设备恢复，补传 ${r.synced} 笔授权` : "设备在线，无待补传");
    await refresh();
  } catch (e) { toast(e.message, true); }
});

/* 两步发卡 */
$("#issue-type").addEventListener("change", (e) => {
  $("#expire-field").style.display = e.target.value === "temp" ? "" : "none";
});
$("#btn-issue-prepare").addEventListener("click", async () => {
  const body = {
    counter: $("#issue-counter").value,
    card_no: $("#issue-no").value.trim(),
    person_id: Number($("#issue-person").value),
    group_id: Number($("#issue-group").value),
    card_type: $("#issue-type").value,
    expires_at: $("#issue-expire").value || null,
  };
  if (!body.card_no) return toast("请填写卡号", true);
  if (body.card_type === "temp" && !body.expires_at) return toast("临时卡必须指定到期时间", true);
  try {
    const r = await api("/api/issue/prepare", body);
    state.issueToken = r.token;
    const p = r.preview;
    $("#issue-preview").style.display = "";
    $("#issue-preview").innerHTML = `
      <div class="confirm-box">
        <div class="flex-between"><b>第二步 · 请核对此发卡单</b>
          <span class="tag ${p.device_online ? "green" : "amber"}">${p.device_online ? "设备在线，即时写入" : "设备离线，将排队补传"}</span></div>
        <dl style="margin-top:10px">
          <dt>卡号</dt><dd class="mono">${esc(p.card_no)}</dd>
          <dt>持卡人员</dt><dd>${esc(p.person.name)}（门牌 ${esc(p.person.unit_no || "—")}）</dd>
          <dt>权限组</dt><dd>${esc(p.group)}（楼层：${p.floors.map(f => f + "F").join("、") || "暂无楼层规则"}）</dd>
          <dt>卡片类型</dt><dd>${p.card_type === "temp" ? "临时卡" : "正式卡"}${p.expires_at ? `，到期 ${esc(p.expires_at.replace("T", " "))}` : ""}</dd>
          <dt>发卡柜台</dt><dd>${esc(p.counter)}</dd>
        </dl>
        <div style="font-size:12px;color:var(--slate-500);margin-bottom:10px">
          卡片绑定人员与权限组后将立即激活；若卡号正被另一柜台发放，本笔将失败。</div>
        <button class="btn primary" id="btn-issue-confirm">确认发放并激活</button>
        <button class="btn" id="btn-issue-cancel">返回修改</button>
      </div>`;
    $("#step-1").classList.remove("active");
    $("#step-2").classList.add("active");
    $("#btn-issue-confirm").onclick = async () => {
      try {
        const r = await api("/api/issue/confirm", { token: state.issueToken });
        toast(`发卡成功（卡号 ${p.card_no}），设备${r.device.delivered ? "已写入" : "离线排队中"}`);
        resetIssueForm();
        await refresh();
      } catch (e) { toast(e.message, true); }
    };
    $("#btn-issue-cancel").onclick = resetIssueForm;
  } catch (e) { toast(e.message, true); }
});
function resetIssueForm() {
  $("#issue-preview").style.display = "none";
  $("#step-1").classList.add("active");
  $("#step-2").classList.remove("active");
  $("#issue-no").value = "";
}

/* 通行仿真 */
$("#btn-sim").addEventListener("click", async () => {
  try {
    const r = await api("/api/access/check", {
      card_id: Number($("#sim-card").value),
      floor: Number($("#sim-floor").value),
      at: $("#sim-at").value || null,
    });
    $("#sim-result").innerHTML =
      `<div class="sim-result ${r.allowed ? "allow" : "deny"}">${r.allowed ? "✓ 放行" : "✕ 拒绝"}：${esc(r.reason)}</div>`;
  } catch (e) { toast(e.message, true); }
});

/* 离场回收 */
$("#btn-recycle").addEventListener("click", async () => {
  if (!confirm("确认回收该卡？权限将立即冻结并记录卡片状态。")) return;
  try {
    const r = await api("/api/card/recycle", {
      card_id: Number($("#recycle-card").value), reason: $("#recycle-reason").value });
    toast(`已回收，回收前状态：${r.old_status}${r.person_left ? "；人员已标记离场" : ""}`);
    await refresh();
  } catch (e) { toast(e.message, true); }
});

/* 挂失 / 补卡 / 损坏 / 转交 */
$("#btn-lost").addEventListener("click", async () => {
  try { await api("/api/card/lost", { card_id: Number($("#lost-card").value) });
    toast("已挂失，权限即时冻结"); await refresh();
  } catch (e) { toast(e.message, true); }
});
$("#btn-reissue").addEventListener("click", async () => {
  const no = $("#reissue-no").value.trim();
  if (!no) return toast("请填写补领新卡号", true);
  try {
    await api("/api/card/reissue", { old_card_id: Number($("#reissue-card").value), new_card_no: no });
    toast(`补卡成功：${no}（旧卡保持挂失，不作恢复）`);
    $("#reissue-no").value = "";
    await refresh();
  } catch (e) { toast(e.message, true); }
});
$("#btn-damage").addEventListener("click", async () => {
  try { await api("/api/card/damage", { card_id: Number($("#damage-card").value) });
    toast("已登记损坏，权限冻结，待回收"); await refresh();
  } catch (e) { toast(e.message, true); }
});
$("#btn-transfer").addEventListener("click", async () => {
  try { await api("/api/card/transfer", {
    card_id: Number($("#transfer-card").value), person_id: Number($("#transfer-person").value) });
    toast("借用转交完成"); await refresh();
  } catch (e) { toast(e.message, true); }
});

/* 权限组规则（二次确认） */
$("#btn-save-rule").addEventListener("click", async () => {
  const body = {
    group_id: Number($("#rule-group").value),
    floor: Number($("#rule-floor").value),
    start_time: $("#rule-start").value, end_time: $("#rule-end").value,
    valid_from: $("#rule-from").value, valid_to: $("#rule-to").value,
    confirm: false,
  };
  await submitRule(body);
});
$("#btn-confirm-rule").addEventListener("click", async () => {
  await submitRule({ ...state.pendingRule, confirm: true });
  $("#rule-confirm").style.display = "none";
});
$("#btn-cancel-rule").addEventListener("click", () => $("#rule-confirm").style.display = "none");

async function submitRule(body) {
  try {
    await api("/api/groups/rules", body);
    toast("权限规则已保存");
    await refresh();
  } catch (e) {
    if (e.data && e.data.needs_confirm) {
      state.pendingRule = body;
      $("#rule-confirm").style.display = "";
      toast(e.message, true);
    } else toast(e.message, true);
  }
}

/* 初始化 */
(async function init() {
  try { await refresh(); }
  catch (e) { toast("初始化失败：" + e.message, true); }
})();
