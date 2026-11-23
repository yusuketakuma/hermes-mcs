"use strict";
const $ = id => document.getElementById(id);
const state = {patients: [], patientCursor: null, project: null, sections: [["", "タイムライン"]], cursor: null, query: "", panel: "", panelPage: 0, generation: null, mainRequest: 0, detailRequest: 0, patientRequest: 0, contextRequest: 0, onSelect: null, onSearch: null};
const subjectLabels = {patient: "本人", family: "家族", other: "他者", unspecified: "対象未確認"};
const fmt = (ts, options = {}) => ts == null ? "時刻未取得" : new Date(ts * 1000).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", ...options});
function el(tag, text, cls) {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;}
// Shared UI kit: every screen builds its chips, cards, key-value lists and tab strips from these.
function chip(text, tone = "") {return el("span", text, tone ? `chip chip-${tone}` : "chip");}
function card(title, nodes = [], {wide = false, tone = "", actions = []} = {}) {
  const box = el("section", null, ["card", wide && "card-wide", tone && `card-${tone}`].filter(Boolean).join(" "));
  const head = el("div", null, "card-head");
  head.append(el("h3", title));
  if (actions.length) {const tools = el("div", null, "card-actions"); tools.append(...actions); head.append(tools);}
  box.append(head, ...nodes);
  return box;
}
function kv(pairs) {
  const list = el("dl", null, "kv");
  for (const [label, value] of pairs) {
    if (value == null || value === "") continue;
    const item = el("div");
    const dd = el("dd");
    if (typeof value === "object") dd.append(value); else dd.textContent = String(value);
    item.append(el("dt", label), dd);
    list.append(item);
  }
  return list;
}
function tabs(items, current, onPick) {
  const strip = el("div", null, "tabs");
  strip.setAttribute("role", "tablist");
  for (const [key, label] of items) {
    const tab = el("button", label, "tab");
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(key === current));
    tab.onclick = () => onPick(key);
    strip.append(tab);
  }
  return strip;
}
// The page frame: mode, one-line heading, the main area and (on phones) the patient sheet.
const shell = {
  setMode(mode) {document.body.dataset.mode = mode;},
  heading(title, meta = "", chips = [], actions = []) {
    $("patient-name").textContent = title; $("patient-meta").textContent = meta;
    $("patient-chips").replaceChildren(...chips); $("heading-actions").replaceChildren(...actions);
  },
  main(...nodes) {
    // A screen that takes the main area ends any timeline load still on its way.
    ++state.mainRequest;
    for (const id of ["coverage", "older", "timeline", "timeline-status"]) $(id).hidden = true;
    $("panel-view").hidden = false; $("panel-view").replaceChildren(...nodes);
    shell.openList(false);
  },
  openList(open = true) {
    document.body.classList.toggle("list-open", open);
    $("patients-toggle").setAttribute("aria-expanded", String(open));
  },
};
function fail(error) {$("error").textContent = error.message || "表示できませんでした。更新して再度お試しください。"; $("error").hidden = false;}
async function api(kind, values = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value != null && value !== "") params.set(key, value);
  const reply = await fetch(`api/${kind}?${params}`, {cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer"});
  const data = await reply.json();
  if (!reply.ok) throw new Error(data.error === "cursor_scope_or_generation_changed" ? "スナップショットが更新されました。「更新」で読み直してください。" : "データを表示できません。公開スナップショットと閲覧範囲を確認してください。");
  if (!data.snapshot) return data;
  if (state.generation && data.snapshot.generation_id !== state.generation) throw new Error("スナップショットが更新されました。「更新」で読み直してください。");
  state.generation = data.snapshot.generation_id;
  $("snapshot-time").textContent = `スナップショット ${fmt(data.snapshot.generated_at)} JST`;
  return data;
}
function patientList() {
  $("patients").replaceChildren();
  for (const patient of state.patients) {
    const button = el("button", null, "patient" + (patient.project_id === state.project ? " selected" : ""));
    button.setAttribute("aria-current", patient.project_id === state.project ? "page" : "false");
    button.append(el("span", "#", "hash"));
    const label = el("span", patient.patient_name || `患者 ${patient.project_id}`, "patient-label");
    label.append(el("small", `ID ${patient.project_id}${patient.is_archived ? " · アーカイブ" : ""}`));
    button.append(label, el("span", patient.message_count, "count"));
    button.onclick = () => selectPatient(patient);
    $("patients").append(button);
  }
  if (!state.patients.length) $("patients").append(el("p", "該当する患者はいません", "empty"));
  $("patient-count").textContent = `${state.patients.length}${state.patientCursor ? "+" : ""}`;
  $("more-patients").hidden = !state.patientCursor;
}
async function loadPatients(more = false) {
  const request = ++state.patientRequest;
  try {
    const data = await api("patients", {q: $("patient-search").value.trim(), cursor: more ? state.patientCursor : null});
    if (request !== state.patientRequest) return;
    state.patients = more ? state.patients.concat(data.items) : data.items;
    state.patientCursor = data.next_cursor;
    patientList();
    if (!state.project && state.patients.length) selectPatient(state.patients[0]);
    if (!state.project && !state.patients.length) {shell.heading("患者タイムライン"); $("timeline-status").textContent = "閲覧範囲に保存済みの患者がいません。";}
  } catch (error) {if (request === state.patientRequest) fail(error);}
}
function closeDetail() {++state.detailRequest; $("detail").classList.remove("open"); $("close-detail").hidden = true; $("detail-content").replaceChildren(el("p", "投稿の「詳細・返信」から記録を確認できます。", "empty"));}
function selectPatient(patient) {
  shell.openList(false);
  state.project = patient.project_id; state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true;
  shell.heading(patient.patient_name || `患者 ${patient.project_id}`, `ID ${patient.project_id} · 保存済み ${patient.message_count} 件`, patient.is_archived ? [chip("アーカイブ", "muted")] : []);
  const fetchLabels = {pending: "未取得", complete: "取得済み", incomplete: "一部未取得", partial: "一部取得", failed: "取得失敗"};
  $("coverage").textContent = `取得状態: ${fetchLabels[patient.fetch_state] || "取得状況を要確認"} · ${patient.history_floor === -1 ? "履歴の終端までの取得記録あり" : "履歴全体の取得は未確認"}`;
  $("coverage").hidden = false;
  state.panelPage = 0; closeDetail(); patientList(); loadPatientContext(patient.project_id); loadMain();
  state.onSelect?.(patient.project_id);
}
async function loadPatientContext(project) {
  const request = ++state.contextRequest, panel = $("patient-context");
  panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を読込中…"));
  try {
    const data = await api("context", {project});
    if (request !== state.contextRequest || project !== state.project) return;
    const groups = data.sections.filter(group => group.items.length);
    panel.replaceChildren(el("h2", "患者コンテキスト"));
    if (!groups.length) {panel.append(el("p", "表示できる患者コンテキストはまだ保存されていません。")); return;}
    const preview = el("div", null, "context-preview");
    for (const group of groups.slice(0, 4)) {
      const item = group.items[0], entry = el("div", null, "context-brief");
      entry.append(el("b", group.label), el("span", `${subjectLabels[item.subject] || "対象未確認"}: ${item.text || item.evidence || ""}`), el("small", fmt(item.posted_at_ts, {year:"numeric", month:"numeric", day:"numeric"})));
      if (item.scope_note) entry.append(el("small", item.scope_note, "context-scope"));
      preview.append(entry);
    }
    const details = el("details", null, "context-records");
    const total = groups.reduce((count, group) => count + group.items.length, 0);
    details.append(el("summary", `背景・根拠をすべて表示 · ${total}件`));
    for (const group of groups) {
      const section = el("section"); section.append(el("h3", group.label));
      for (const item of group.items) {
        const record = contextSection({patient_context: [item]});
        record.firstElementChild.textContent = fmt(item.posted_at_ts, {year:"numeric", month:"numeric", day:"numeric"});
        section.append(record);
      }
      details.append(section);
    }
    panel.append(preview, details);
  } catch (error) {
    if (request === state.contextRequest && project === state.project) panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を取得できませんでした。「更新」で読み直してください。"));
  }
}
function post(row, detailed = false) {
  const article = el("article", null, "post");
  const name = row.sender_name || "投稿者未取得";
  article.append(el("span", Array.from(name)[0], "avatar"));
  const content = el("div", null, "post-content"), heading = el("div", null, "post-heading");
  heading.append(el("span", name, "sender"), el("time", fmt(row.posted_at_ts, {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"}), "timestamp"));
  content.append(heading, el("p", row.body_state === "deleted" ? "削除された投稿" : row.body_text || "本文未取得", "post-body"));
  if (row.profession || row.organization) heading.append(el("span", [row.profession, row.organization].filter(Boolean).join(" · "), "timestamp"));
  if (row.summary_lines?.length) {const summary = el("details", null, "post-summary"); summary.append(el("summary", "記録の要約")); for (const line of row.summary_lines) summary.append(el("p", line)); content.append(summary);}
  if (row.stamp_line) content.append(el("p", row.stamp_line, "state-note"));
  if (!row.body_complete && row.body_state !== "deleted") content.append(el("p", "本文は一部未取得です", "state-note"));
  const footer = el("div", null, "post-footer"), button = el("button", `詳細・返信${row.stored_replies ? ` ${row.stored_replies}件` : ""}`, "reply-link");
  button.onclick = () => openDetail(row);
  footer.append(button);
  const files = (row.attachments || []).reduce((sum, item) => sum + item.count, 0);
  if (files) footer.append(el("span", `添付 ${files}件`, "attachment-pill"));
  if (row.parent_id) footer.append(el("span", "返信の検索結果"));
  if (row.reply_count > row.stored_replies) content.append(el("p", "未取得の返信があります", "state-note"));
  if (!detailed) content.append(footer);
  article.append(content); return article;
}
function chronological(rows) {return rows.slice().sort((a,b) => (a.posted_at_ts ?? -Infinity) - (b.posted_at_ts ?? -Infinity) || a.message_id - b.message_id);}
async function loadTimeline(more = false) {
  if (!state.project) return;
  const request = ++state.mainRequest;
  $("error").hidden = true; $("older").disabled = true;
  if (!more) {state.cursor = null; $("timeline").replaceChildren(); $("timeline-status").textContent = "投稿を読込中…"; $("timeline-status").hidden = false; $("older").hidden = true;}
  try {
    const data = await api(state.query ? "search" : "timeline", {project: state.project, q: state.query, cursor: more ? state.cursor : null});
    if (request !== state.mainRequest) return;
    state.cursor = data.next_cursor;
    const fragment = document.createDocumentFragment(); let lastDay = null;
    for (const row of chronological(data.items)) {
      const day = fmt(row.posted_at_ts, {year:"numeric", month:"long", day:"numeric", weekday:"short"});
      if (day !== lastDay) {fragment.append(el("div", day, "date-divider")); lastDay = day;}
      fragment.append(post(row));
    }
    const panel = $("timeline-panel"), height = panel.scrollHeight;
    if (more) {
      const first = $("timeline").firstElementChild;
      if (first?.className === "date-divider" && first.textContent === lastDay) first.remove();
      $("timeline").prepend(fragment); panel.scrollTop += panel.scrollHeight - height;
    }
    else {$("timeline").replaceChildren(fragment); panel.scrollTop = state.query ? 0 : panel.scrollHeight;}
    $("older").hidden = !state.cursor;
    const empty = !$("timeline").children.length;
    $("timeline-status").hidden = !empty;
    $("timeline-status").textContent = empty ? (state.query ? "一致する保存済みの投稿はありません。" : "保存済みの投稿はありません。") : "";
  } catch (error) {if (request === state.mainRequest) {fail(error); $("timeline-status").hidden = true;}}
  finally {if (request === state.mainRequest) $("older").disabled = false;}
}
function section(title) {const node = el("section", null, "detail-section"); node.append(el("h3", title)); return node;}
function contextSection(row) {
  const context = section("投稿に記録された情報");
  for (const item of row.patient_context || []) {
    context.append(el("p", `対象: ${subjectLabels[item.subject] || "対象未確認"}`, "context-subject"));
    context.append(el("p", item.text || item.evidence || item.category));
    if (item.evidence) context.append(el("blockquote", item.evidence));
    if (item.scope_note) context.append(el("p", item.scope_note, "context-scope"));
    if (item.source_evidence) {
      const source = el("details", null, "context-source");
      source.append(el("summary", "原文全体（分類範囲は未確認）"), el("blockquote", item.source_evidence));
      if (item.record_text) source.append(el("p", "保存された記載（分類範囲は未確認）"), el("p", item.record_text));
      context.append(source);
    }
    for (const detail of item.details || []) {
      context.append(el("p", `${detail.key}: ${detail.value || detail.text || ""}`));
      if (detail.evidence) context.append(el("blockquote", detail.evidence));
    }
  }
  if (!row.patient_context?.length) context.append(el("p", "表示できる構造化記録はまだありません。"));
  return context;
}
async function openDetail(row) {
  const request = ++state.detailRequest, project = state.project;
  const root = row.parent_id && row.parent_available !== false ? row.parent_id : row.message_id;
  $("detail").classList.add("open"); $("close-detail").hidden = false; $("detail-content").replaceChildren(el("p", "詳細を読込中…", "empty"));
  try {
    const [source, replies] = await Promise.all([api("evidence", {project, message: root}), api("thread", {project, message: root})]);
    if (request !== state.detailRequest) return;
    const original = section("元の投稿"); original.append(post(source.message, true));
    if (row.parent_id && row.parent_available === false) original.prepend(el("p", "元の投稿は未取得です。保存済みの返信を表示しています。", "state-note"));
    const flags = section("記録の状態");
    for (const text of source.message.metadata_flags || []) flags.append(el("p", text));
    const context = contextSection(source.message);
    const meds = section("薬剤"), medsBody = el("div");
    // Each post has its own medication view: the original, the selected reply and every listed reply.
    const medsFor = (messageId, label) => {const button = el("button", label, "text-button"); button.onclick = () => {showMeds(medsBody, messageId, request); medsBody.scrollIntoView({block: "nearest"});}; return button;};
    meds.append(medsFor(root, row.message_id === root ? "この投稿の薬剤を表示" : "元の投稿の薬剤を表示"));
    if (row.message_id !== root) meds.append(" ", medsFor(row.message_id, "選択した返信の薬剤を表示"));
    meds.append(medsBody);
    const thread = section("返信"), older = el("button", "以前の返信を表示", "load-more"), list = el("div");
    let cursor = replies.next_cursor;
    const appendReplies = (data, prepend) => {const fragment = document.createDocumentFragment(); for (const reply of chronological(data.items)) {fragment.append(post(reply, true)); const files = el("button", "添付・記録の状態", "text-button"); files.onclick = () => showMessageInfo(reply, request); fragment.append(files, medsFor(reply.message_id, "この返信の薬剤"));} prepend ? list.prepend(fragment) : list.append(fragment);};
    appendReplies(replies, false);
    older.hidden = !cursor;
    older.onclick = async () => {older.disabled = true; try {const page = await api("thread", {project, message: root, cursor}); if (request !== state.detailRequest) return; appendReplies(page, true); cursor = page.next_cursor; older.hidden = !cursor;} catch(error) {if (request === state.detailRequest) fail(error);} finally {older.disabled = false;}};
    thread.append(older, list);
    if (!replies.items.length) thread.append(el("p", "保存済みの返信はありません。"));
    if (source.message.reply_count > source.message.stored_replies) thread.append(el("p", "未取得の返信があります。", "state-note"));
    const info = el("div"); info.id = "message-info";
    $("detail-content").replaceChildren(original, flags, context, meds, thread, info);
    await showMessageInfo(row, request, row.message_id !== root);
  } catch (error) {if (request === state.detailRequest) {$("detail-content").replaceChildren(el("p", "詳細を取得できませんでした。", "empty")); fail(error);}}
}
async function showMessageInfo(row, request, withContext = true) {
  try {
    const [data, source] = await Promise.all([
      api("attachments", {project: state.project, message: row.message_id}),
      api("evidence", {project: state.project, message: row.message_id})
    ]);
    if (request !== state.detailRequest) return;
    const files = section(`添付 · 投稿 ${row.message_id}`);
    const labels = {downloaded:"取得済み", pending:"未取得", failed:"取得失敗", pruned:"原本保持期間終了", unavailable:"利用不可", unprocessed:"未処理", complete:"処理済み", empty:"文字記録なし"};
    function appendFiles(items) {for (const file of items) {const entry = el("details"); entry.append(el("summary", file.name || `添付 ${file.attachment_id}`), el("p", `取得: ${labels[file.state] || "状態を要確認"} · 文字記録: ${labels[file.ocr?.state] || "未取得"}`)); for (const page of file.ocr?.pages || []) entry.append(el("p", page.text || "")); files.append(entry);}}
    appendFiles(data.items);
    let cursor = data.next_cursor;
    const more = el("button", "添付をさらに表示", "text-button"); more.hidden = !cursor;
    more.onclick = async () => {more.disabled = true; try {const page = await api("attachments", {project: row.project_id, message: row.message_id, cursor}); if (request !== state.detailRequest || !files.isConnected) return; appendFiles(page.items); cursor = page.next_cursor; more.hidden = !cursor;} catch(error) {fail(error);} finally {more.disabled = false;}};
    files.append(more);
    if (!data.items.length) files.append(el("p", "保存済みの添付はありません。"));
    if (row.parent_id) {files.append(el("h3", "この返信の状態")); for (const text of row.metadata_flags || []) files.append(el("p", text));}
    const replyMeds = [];
    if (row.parent_id) {
      const meds = section("この返信の薬剤"), body = el("div"), button = el("button", "この返信の薬剤を表示", "text-button");
      button.onclick = () => showMeds(body, row.message_id, request);
      meds.append(button, body); replyMeds.push(meds);
    }
    $("message-info").replaceChildren(files, ...replyMeds, ...(withContext ? [contextSection(source.message)] : []));
  } catch (error) {if (request === state.detailRequest) fail(error);}
}
async function loadSections() {
  try {
    const data = await api("sections");
    state.sections = [["", "タイムライン"], ...data.items.map(item => [item.key, item.label])];
    viewTabs();
  } catch (error) {fail(error);}
}
function viewTabs() {
  $("view-tabs").replaceChildren(tabs(state.sections, state.panel, key => {state.panel = key; state.panelPage = 0; closeDetail(); loadMain();}));
}
function loadMain() {
  const timeline = !state.panel;
  viewTabs();
  for (const id of ["coverage", "older", "timeline", "timeline-status"]) if (!timeline) $(id).hidden = true;
  $("panel-view").hidden = timeline;
  if (timeline) {$("coverage").hidden = !state.project; $("timeline").hidden = false; loadTimeline();}
  else loadPanel();
}
function renderPanel(view, onPage) {
  const box = el("div", null, "panel");
  box.append(el("h2", view.title || ""));
  for (const line of view.head || []) box.append(el("p", line, "panel-head"));
  if (view.body != null) for (const line of view.body.split("\n")) box.append(el("p", line, line.startsWith("■") ? "panel-group" : "panel-line"));
  let group;
  for (const item of view.items || []) {
    if (item.group && item.group !== group) {group = item.group; box.append(el("h3", group));}
    const row = el("div", null, "panel-item");
    if (item.quotes?.length) for (const quote of item.quotes) row.append(el("blockquote", quote));
    else row.append(el("p", item.text || ""));
    if (item.quotes?.length && item.source) row.append(el("small", item.source));
    else if (item.source) row.append(el("small", item.source));
    if (item.message_id) {
      const open = el("button", "投稿を開く", "text-button"), project = state.project;
      open.onclick = async () => {const detail = ++state.detailRequest; try {const source = await api("evidence", {project, message: item.message_id}); if (project === state.project && detail === state.detailRequest) openDetail(source.message);} catch (error) {if (detail === state.detailRequest) fail(error);}};
      row.append(open);
    }
    box.append(row);
  }
  if (!view.items?.length && view.body == null) box.append(el("p", view.empty || "表示できる記録はありません。", "empty"));
  if (view.more) box.append(el("p", `他${view.more}件`, "panel-head"));
  for (const note of view.notes || []) box.append(el("p", note, "state-note"));
  if (onPage && view.pages > 1) {
    const pager = el("div", null, "panel-pager");
    if (view.page > 0) {const back = el("button", "前のページ", "text-button"); back.onclick = () => onPage(view.page - 1); pager.append(back);}
    pager.append(el("span", `${view.page + 1} / ${view.pages}`));
    if (view.page + 1 < view.pages) {const next = el("button", "次のページ", "text-button"); next.onclick = () => onPage(view.page + 1); pager.append(next);}
    box.append(pager);
  }
  return box;
}
async function loadPanel() {
  if (!state.project) return;
  const request = ++state.mainRequest, project = state.project, section = state.panel;
  $("error").hidden = true;
  $("panel-view").replaceChildren(el("p", "読込中…", "empty"));
  try {
    const data = await api("panel", {project, section, page: state.panelPage || null});
    if (request !== state.mainRequest) return;
    $("panel-view").replaceChildren(renderPanel(data.panel, page => {state.panelPage = page; loadPanel();}));
    $("timeline-panel").scrollTop = 0;
  } catch (error) {if (request === state.mainRequest) {$("panel-view").replaceChildren(el("p", "表示できませんでした。", "empty")); fail(error);}}
}
async function showMeds(target, message, request, page = 0) {
  // Only the latest press for this panel may draw, whichever response arrives last.
  const token = target.medsToken = (target.medsToken || 0) + 1;
  const current = () => request === state.detailRequest && token === target.medsToken;
  target.replaceChildren(el("p", "薬剤を読込中…", "empty"));
  try {
    const data = await api("panel", {project: state.project, section: "meds", message, page});
    if (current()) target.replaceChildren(renderPanel(data.panel, next => showMeds(target, message, request, next)));
  } catch (error) {if (current()) {target.replaceChildren(); fail(error);}}
}
$("patient-form").onsubmit = event => {event.preventDefault(); (state.onSearch || loadPatients)();};
$("more-patients").onclick = () => loadPatients(true);
$("patients-toggle").onclick = () => shell.openList(!document.body.classList.contains("list-open"));
$("close-list").onclick = () => shell.openList(false);
$("sidebar").onkeydown = event => {if (event.key === "Escape") shell.openList(false);};
$("message-form").onsubmit = event => {event.preventDefault(); state.query = $("message-search").value.trim(); $("clear-search").hidden = !state.query; state.panel = ""; closeDetail(); loadMain();};
$("clear-search").onclick = () => {state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true; closeDetail(); loadMain();};
$("older").onclick = () => loadTimeline(true);
$("close-detail").onclick = closeDetail;
$("refresh").onclick = () => location.reload();
viewTabs();
loadSections();
loadPatients();
