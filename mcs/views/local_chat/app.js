"use strict";
const $ = id => document.getElementById(id);
const state = {patients: [], patientCursor: null, project: null, sections: [["", "タイムライン"]], cursor: null, query: "", panel: "", panelPage: 0, generation: null, mainRequest: 0, detailRequest: 0, navRequest: 0, patientRequest: 0, contextRequest: 0, sideRequest: 0, sideProject: null, listOpener: null, detailOpener: null, onSelect: null, onSearch: null};
const subjectLabels = {patient: "本人", family: "家族", other: "他者", unspecified: "対象未確認"};
const fmt = (ts, options = {}) => ts == null ? "時刻未取得" : new Date(ts * 1000).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", ...options});
// A date without the year when it falls in the current JST year: 11/18, otherwise 2025/11/18.
const jstYear = ms => new Date(ms + 9 * 3600e3).getUTCFullYear();
const shortDate = ts => fmt(ts, jstYear(ts * 1000) === jstYear(Date.now()) ? {month: "numeric", day: "numeric"} : {year: "numeric", month: "numeric", day: "numeric"});
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
    for (const id of ["older", "timeline", "timeline-status"]) $(id).hidden = true;
    $("panel-view").hidden = false; $("panel-view").replaceChildren(...nodes);
    shell.openList(false);
  },
  openList(open = true) {
    // The phone sheet is modal: the page behind is inert, focus moves into the sheet and back to the opener.
    const wasOpen = document.body.classList.contains("list-open");
    if (open === wasOpen) return;
    if (open) state.listOpener = document.activeElement;
    document.body.classList.toggle("list-open", open);
    $("patients-toggle").setAttribute("aria-expanded", String(open));
    $("workspace").inert = open;
    if (open) $("patient-search").focus();
    else if ($("sidebar").contains(document.activeElement) || !document.activeElement || document.activeElement === document.body) restoreFocus(state.listOpener, $("patients-toggle"));
  },
};
function restoreFocus(opener, fallback) {(opener?.isConnected && !opener.hidden ? opener : fallback)?.focus({preventScroll: true});}
// The element that scrolls the main column: the column itself, or the page on phones where the page scrolls.
function scroller() {
  const panel = $("timeline-panel"), style = globalThis.getComputedStyle?.(panel);
  return !style || /auto|scroll/.test(style.overflowY) ? panel : document.scrollingElement || document.documentElement;
}
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
    const label = el("span", null, "patient-label"), top = el("span", null, "patient-top");
    top.append(el("span", patient.patient_name || `患者 ${patient.project_id}`, "patient-title"));
    if (patient.is_archived) top.append(chip("アーカイブ", "muted"));
    label.append(top, el("small", [`ID ${patient.project_id}`, patient.latest_at != null && `最終投稿 ${shortDate(patient.latest_at)}`].filter(Boolean).join(" · ")));
    const count = el("span", patient.message_count, "count"); count.title = `保存済み ${patient.message_count} 件`;
    button.append(label, count);
    button.onclick = () => selectPatient(patient);
    $("patients").append(button);
  }
  if (!state.patients.length) $("patients").append(el("p", "該当する患者はいません", "empty"));
  $("patient-count").textContent = `${state.patients.length}${state.patientCursor ? "+" : ""}`;
  $("more-patients").hidden = !state.patientCursor;
}
async function loadPatients(more = false) {
  const request = ++state.patientRequest;
  if (!state.project) loadSideStatus();
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
function closeDetail() {
  ++state.detailRequest;
  // Focus inside the closing detail goes back to whatever opened it.
  const hadFocus = $("detail").contains(document.activeElement);
  $("detail").classList.remove("open"); $("close-detail").hidden = true; $("detail-content").replaceChildren(el("p", "投稿の「詳細・返信」から記録を確認できます。", "empty"));
  if (hadFocus) restoreFocus(state.detailOpener, $("timeline-panel"));
}
function selectPatient(patient) {
  shell.openList(false);
  state.project = patient.project_id; state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true;
  const meta = [`ID ${patient.project_id}`, `保存済み ${patient.message_count} 件`, patient.latest_at != null && `最終投稿 ${shortDate(patient.latest_at)}`];
  shell.heading(patient.patient_name || `患者 ${patient.project_id}`, meta.filter(Boolean).join(" · "), [...(patient.is_archived ? [chip("アーカイブ", "muted")] : []), ...fetchChips(patient)]);
  state.panelPage = 0; closeDetail(); patientList(); loadPatientContext(patient.project_id); loadSideStatus(); loadMain();
  state.onSelect?.(patient.project_id);
}
function fetchChips(patient) {
  // Only an incomplete fetch is called out; a complete fetch with the history end on record shows nothing.
  const labels = {pending: "未取得", incomplete: "一部未取得", partial: "一部取得", failed: "取得失敗"};
  const chips = [];
  if (patient.fetch_state !== "complete") chips.push(chip(labels[patient.fetch_state] || "取得状況を要確認", "warn"));
  if (patient.history_floor !== -1) chips.push(chip("履歴全体の取得は未確認", "warn"));
  for (const item of chips) item.title = `取得状態: ${item.textContent}`;
  return chips;
}
async function loadPatientContext(project) {
  const request = ++state.contextRequest, panel = $("patient-context");
  panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を読込中…", "context-empty"));
  try {
    const data = await api("context", {project});
    if (request !== state.contextRequest || project !== state.project) return;
    const groups = data.sections.filter(group => group.items.length);
    panel.replaceChildren(el("h2", "患者コンテキスト"));
    if (!groups.length) {panel.append(el("p", "表示できる患者コンテキストはまだ保存されていません。", "context-empty")); return;}
    const preview = el("div", null, "context-preview");
    for (const group of groups.slice(0, 4)) {
      const item = group.items[0], entry = el("div", null, "context-brief"), label = el("div", null, "context-label");
      const text = `${subjectLabels[item.subject] || "対象未確認"}: ${item.text || item.evidence || ""}`, line = el("span", text, "context-text");
      line.title = text; label.append(el("b", group.label), el("small", item.posted_at_ts == null ? "日付未取得" : shortDate(item.posted_at_ts), "context-date"));
      entry.append(label, line);
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
    if (request === state.contextRequest && project === state.project) panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を取得できませんでした。「更新」で読み直してください。", "context-empty"));
  }
}
function post(row, detailed = false) {
  const article = el("article", null, "post");
  const name = row.sender_name || "投稿者未取得";
  article.append(el("span", Array.from(name)[0], "avatar"));
  const content = el("div", null, "post-content"), heading = el("div", null, "post-heading");
  heading.append(el("span", name, "sender"));
  if (row.profession || row.organization) heading.append(el("span", [row.profession, row.organization].filter(Boolean).join(" · "), "post-role"));
  heading.append(el("time", fmt(row.posted_at_ts, {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"}), "timestamp"));
  content.append(heading, el("p", row.body_state === "deleted" ? "削除された投稿" : row.body_text || "本文未取得", "post-body"));
  if (row.summary_lines?.length) {const summary = el("details", null, "post-summary"); summary.append(el("summary", "記録の要約")); for (const line of row.summary_lines) summary.append(el("p", line)); content.append(summary);}
  // One footer row: the actions on the post, then its record states as chips (shown in the detail too).
  const footer = el("div", null, "post-footer");
  if (!detailed) {
    const button = el("button", `詳細・返信${row.stored_replies ? ` ${row.stored_replies}件` : ""}`, "reply-link");
    button.onclick = () => openDetail(row);
    footer.append(button);
    const files = (row.attachments || []).reduce((sum, item) => sum + item.count, 0);
    if (files) footer.append(chip(`添付 ${files}件`, "muted"));
    if (row.parent_id) footer.append(chip("返信の検索結果", "info"));
  }
  if (row.stamp_line) footer.append(chip(row.stamp_line, stampIncomplete.test(row.stamp_line) ? "warn" : "muted"));
  if (!row.body_complete && row.body_state !== "deleted") footer.append(chip("本文は一部未取得です", "warn"));
  if (row.reply_count > row.stored_replies) footer.append(chip("未取得の返信があります", "warn"));
  if (footer.children.length) content.append(footer);
  article.append(content); return article;
}
// Every non-complete wording of stamp_format.thread_stamp_line / actor_line (not actor names such as 氏名不明).
const stampIncomplete = /^スタンプ 未取得|再取得失敗|押した人 (?:取得失敗|古い情報)|取得状態不明/;
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
    const panel = scroller(), height = panel.scrollHeight;
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
  if (!$("detail").contains(document.activeElement)) state.detailOpener = document.activeElement;
  $("detail").classList.add("open"); $("close-detail").hidden = false; $("detail-content").replaceChildren(el("p", "詳細を読込中…", "empty"));
  $("close-detail").focus({preventScroll: true});
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
function showView(key) {state.panel = key; state.panelPage = 0; closeDetail(); loadMain();}
function viewTabs() {
  const strip = tabs(state.sections, state.panel, showView), hadFocus = $("view-tabs").contains(document.activeElement);
  $("view-tabs").replaceChildren(strip);
  // The chosen tab keeps keyboard focus across the rebuild and, on narrow screens, scrolls into view.
  const selected = strip.children[state.sections.findIndex(([key]) => key === state.panel)];
  selected?.scrollIntoView({block: "nearest", inline: "nearest"});
  if (hadFocus) selected?.focus();
}
function loadMain() {
  const timeline = !state.panel;
  viewTabs();
  for (const id of ["older", "timeline", "timeline-status"]) if (!timeline) $(id).hidden = true;
  $("panel-view").hidden = timeline;
  if (timeline) {$("timeline").hidden = false; loadTimeline();}
  else loadPanel();
}
function renderPanel(view, onPage) {
  const box = el("div", null, "panel");
  box.append(el("h2", view.title || ""));
  for (const line of view.head || []) box.append(el("p", line, "panel-head"));
  if (view.body != null) for (const line of view.body.split("\n")) box.append(el("p", line, line.startsWith("■") ? "panel-group" : "panel-line"));
  let group;
  for (const item of view.items || []) {
    if (item.group && item.group !== group) {group = item.group; box.append(el("h3", group, "panel-group-head"));}
    box.append(panelItem(item));
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
function panelItem(item) {
  const row = el("div", null, "panel-item");
  if (item.quotes?.length) for (const quote of item.quotes) row.append(el("blockquote", quote));
  else row.append(el("p", item.text || ""));
  if (!item.source && !item.message_id) return row;
  const meta = el("div", null, "panel-item-meta");
  if (item.source) meta.append(el("small", item.source));
  if (item.message_id) meta.append(openPostButton(item.message_id));
  row.append(meta);
  return row;
}
function openPostButton(message) {
  // A navigation has its own token: the open detail (and its buttons) stays live until the post has
  // loaded and openDetail replaces it. A close, another patient or a newer navigation drops the answer.
  const open = el("button", "投稿を開く", "text-button"), project = state.project;
  open.onclick = async () => {
    const nav = ++state.navRequest, detail = state.detailRequest;
    const current = () => nav === state.navRequest && detail === state.detailRequest && project === state.project;
    try {const source = await api("evidence", {project, message}); if (current()) openDetail(source.message);}
    catch (error) {if (current()) fail(error);}
  };
  return open;
}
// いまの状況: open signals and reply checks beside the timeline, only while the wide right column shows.
const sideViews = [["signals", "未解消のシグナル"], ["replies", "連絡・返答の確認"]], sideLimit = 5;
const wideRail = globalThis.matchMedia?.("(min-width: 1200px)");
function loadSideStatus() {
  const request = ++state.sideRequest, project = state.project, rail = $("side-status");
  if (!project || (wideRail && !wideRail.matches)) {state.sideProject = null; rail.replaceChildren(); return Promise.resolve();}
  state.sideProject = project;
  const cards = sideViews.map(([key, title]) => sideCard(key, title, request, project));
  rail.replaceChildren(el("h2", "いまの状況"), ...cards.map(([box]) => box));
  return Promise.all(cards.map(([, loaded]) => loaded));
}
function sideCard(key, title, request, project) {
  const all = el("button", "すべて見る", "text-button"), body = el("div", null, "side-body");
  all.onclick = () => showView(key);
  body.append(el("p", "読込中…", "side-note"));
  const current = () => request === state.sideRequest && project === state.project;
  const loaded = api("panel", {project, section: key}).then(data => {
    if (!current()) return;
    const view = data.panel, items = view.items || [];
    // Each item keeps its kind (the signal type or reply group) as a small heading.
    let group;
    body.replaceChildren();
    for (const item of items.slice(0, sideLimit)) {
      if (item.group && item.group !== group) {group = item.group; body.append(el("h4", group, "side-group"));}
      body.append(panelItem(item));
    }
    if (!items.length) body.append(el("p", view.empty || "表示できる記録はありません。", "side-note"));
    if (items.length > sideLimit || view.more || view.pages > 1) body.append(el("p", "続きは「すべて見る」で表示します。", "side-note"));
    for (const note of view.notes || []) body.append(el("p", note, "state-note"));
  }, () => {if (current()) body.replaceChildren(el("p", "取得できませんでした。「すべて見る」か「更新」で読み直してください。", "state-note"));});
  return [card(title, [body], {actions: [all]}), loaded];
}
wideRail?.addEventListener?.("change", () => {if (wideRail.matches && state.project && state.sideProject !== state.project) loadSideStatus();});
async function loadPanel() {
  if (!state.project) return;
  const request = ++state.mainRequest, project = state.project, section = state.panel;
  $("error").hidden = true;
  $("panel-view").replaceChildren(el("p", "読込中…", "empty"));
  try {
    const data = await api("panel", {project, section, page: state.panelPage || null});
    if (request !== state.mainRequest) return;
    $("panel-view").replaceChildren(renderPanel(data.panel, page => {state.panelPage = page; loadPanel();}));
    scroller().scrollTop = 0;
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
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (document.body.classList.contains("list-open")) shell.openList(false);
  else if ($("detail").classList.contains("open")) closeDetail();
});
$("message-form").onsubmit = event => {event.preventDefault(); state.query = $("message-search").value.trim(); $("clear-search").hidden = !state.query; state.panel = ""; closeDetail(); loadMain();};
$("clear-search").onclick = () => {state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true; closeDetail(); loadMain();};
$("older").onclick = () => loadTimeline(true);
$("close-detail").onclick = closeDetail;
$("refresh").onclick = () => location.reload();
viewTabs();
loadSections();
loadPatients();
