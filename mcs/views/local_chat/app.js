"use strict";
const $ = id => document.getElementById(id);
const state = {patients: [], patientCursor: null, project: null, cursor: null, query: "", generation: null, mainRequest: 0, detailRequest: 0, patientRequest: 0};
const fmt = (ts, options = {}) => ts == null ? "時刻未取得" : new Date(ts * 1000).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", ...options});
function el(tag, text, cls) {const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node;}
function fail(error) {$("error").textContent = error.message || "表示できませんでした。更新して再度お試しください。"; $("error").hidden = false;}
async function api(kind, values = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value != null && value !== "") params.set(key, value);
  const reply = await fetch(`api/${kind}?${params}`, {cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer"});
  const data = await reply.json();
  if (!reply.ok) throw new Error(data.error === "cursor_scope_or_generation_changed" ? "スナップショットが更新されました。「更新」で読み直してください。" : "データを表示できません。公開スナップショットと閲覧範囲を確認してください。");
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
    if (!state.project && !state.patients.length) $("timeline-status").textContent = "閲覧範囲に保存済みの患者がいません。";
  } catch (error) {if (request === state.patientRequest) fail(error);}
}
function closeDetail() {++state.detailRequest; $("detail").classList.remove("open"); $("close-detail").hidden = true; $("detail-content").replaceChildren(el("p", "投稿の「詳細・返信」から記録を確認できます。", "empty"));}
function selectPatient(patient) {
  state.project = patient.project_id; state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true;
  $("patient-name").textContent = patient.patient_name || `患者 ${patient.project_id}`;
  $("patient-meta").textContent = `患者ID ${patient.project_id} · 保存済み ${patient.message_count} 件${patient.is_archived ? " · アーカイブ" : ""}`;
  const fetchLabels = {pending: "未取得", complete: "取得済み", incomplete: "一部未取得", partial: "一部取得", failed: "取得失敗"};
  $("coverage").textContent = `取得状態: ${fetchLabels[patient.fetch_state] || "取得状況を要確認"} · ${patient.history_floor === -1 ? "履歴の終端までの取得記録あり" : "履歴全体の取得は未確認"}`;
  $("coverage").hidden = false;
  closeDetail(); patientList(); loadTimeline();
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
    if (more) {$("timeline").prepend(fragment); panel.scrollTop += panel.scrollHeight - height;}
    else {$("timeline").replaceChildren(fragment); panel.scrollTop = state.query ? 0 : panel.scrollHeight;}
    $("older").hidden = !state.cursor;
    $("timeline-status").textContent = state.query ? "一致する保存済みの投稿はありません。" : "保存済みの投稿はありません。";
    $("timeline-status").hidden = !!$("timeline").children.length;
  } catch (error) {if (request === state.mainRequest) {fail(error); $("timeline-status").hidden = true;}}
  finally {if (request === state.mainRequest) $("older").disabled = false;}
}
function section(title) {const node = el("section", null, "detail-section"); node.append(el("h3", title)); return node;}
function contextSection(row) {
  const context = section("投稿に記録された情報");
  const subjects = {patient: "本人", family: "家族", other: "他者", unspecified: "対象未確認"};
  for (const item of row.patient_context || []) {
    context.append(el("p", `対象: ${subjects[item.subject] || "対象未確認"}`, "context-subject"));
    context.append(el("p", item.text || item.evidence || item.category));
    if (item.evidence) context.append(el("blockquote", item.evidence));
    for (const detail of item.details || []) {
      context.append(el("p", `${detail.key}: ${detail.value || detail.text || ""}`));
      if (detail.evidence) context.append(el("blockquote", detail.evidence));
    }
  }
  if (!row.patient_context?.length) context.append(el("p", "表示できる構造化記録はまだありません。"));
  return context;
}
async function openDetail(row) {
  const request = ++state.detailRequest, project = state.project, root = row.parent_id || row.message_id;
  $("detail").classList.add("open"); $("close-detail").hidden = false; $("detail-content").replaceChildren(el("p", "詳細を読込中…", "empty"));
  try {
    const [source, replies] = await Promise.all([api("evidence", {project, message: root}), api("thread", {project, message: root})]);
    if (request !== state.detailRequest) return;
    const original = section("元の投稿"); original.append(post(source.message, true));
    const flags = section("記録の状態");
    for (const text of source.message.metadata_flags || []) flags.append(el("p", text));
    const context = contextSection(source.message);
    const thread = section("返信"), older = el("button", "以前の返信を表示", "load-more"), list = el("div");
    let cursor = replies.next_cursor;
    const appendReplies = (data, prepend) => {const fragment = document.createDocumentFragment(); for (const reply of chronological(data.items)) {fragment.append(post(reply, true)); const files = el("button", "添付・記録の状態", "text-button"); files.onclick = () => showMessageInfo(reply, request); fragment.append(files);} prepend ? list.prepend(fragment) : list.append(fragment);};
    appendReplies(replies, false);
    older.hidden = !cursor;
    older.onclick = async () => {older.disabled = true; try {const page = await api("thread", {project, message: root, cursor}); if (request !== state.detailRequest) return; appendReplies(page, true); cursor = page.next_cursor; older.hidden = !cursor;} catch(error) {if (request === state.detailRequest) fail(error);} finally {older.disabled = false;}};
    thread.append(older, list);
    if (!replies.items.length) thread.append(el("p", "保存済みの返信はありません。"));
    if (source.message.reply_count > source.message.stored_replies) thread.append(el("p", "未取得の返信があります。", "state-note"));
    const info = el("div"); info.id = "message-info";
    $("detail-content").replaceChildren(original, flags, context, thread, info);
    await showMessageInfo(row, request);
  } catch (error) {if (request === state.detailRequest) {$("detail-content").replaceChildren(el("p", "詳細を取得できませんでした。", "empty")); fail(error);}}
}
async function showMessageInfo(row, request) {
  try {
    const data = await api("attachments", {project: state.project, message: row.message_id});
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
    $("message-info").replaceChildren(files);
  } catch (error) {if (request === state.detailRequest) fail(error);}
}
$("patient-form").onsubmit = event => {event.preventDefault(); loadPatients();};
$("more-patients").onclick = () => loadPatients(true);
$("message-form").onsubmit = event => {event.preventDefault(); state.query = $("message-search").value.trim(); $("clear-search").hidden = !state.query; closeDetail(); loadTimeline();};
$("clear-search").onclick = () => {state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true; closeDetail(); loadTimeline();};
$("older").onclick = () => loadTimeline(true);
$("close-detail").onclick = closeDetail;
$("refresh").onclick = () => location.reload();
loadPatients();
