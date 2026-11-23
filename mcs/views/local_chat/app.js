"use strict";
const $ = id => document.getElementById(id);
const state = {patients: [], patientCursor: null, project: null, sections: [["", "タイムライン"]], cursor: null, query: "", panel: "", panelPage: 0, generation: null, mainRequest: 0, detailRequest: 0, navRequest: 0, patientRequest: 0, contextRequest: 0, sideRequest: 0, sideProject: null, countRequest: 0, countBox: null, openPatient: null, listOpener: null, detailOpener: null, onSelect: null, onSearch: null};
const subjectLabels = {patient: "本人", family: "家族", other: "他者", unspecified: "対象未確認"};
const fmt = (ts, options = {}) => ts == null ? "時刻未取得" : new Date(ts * 1000).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo", ...options});
// A date without the year when it falls in the current JST year: 11/18, otherwise 2025/11/18.
const jstYear = ms => new Date(ms + 9 * 3600e3).getUTCFullYear();
// Times carry the year only when it is not the current JST year.
const postTime = ts => fmt(ts, {...(ts != null && jstYear(ts * 1000) !== jstYear(Date.now()) ? {year: "numeric"} : {}), month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
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
  // Browser errors (offline, a stopped server, a non-JSON reply) are worded for the reader, not passed through.
  let reply, data;
  try {reply = await fetch(`api/${kind}?${params}`, {cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer"});}
  catch {throw new Error("ローカルのサーバーに接続できませんでした。サーバーが動いているか確かめて「更新」してください。");}
  try {data = await reply.json();}
  catch {throw new Error("サーバーの応答を読み取れませんでした。「更新」で読み直してください。");}
  if (!reply.ok) throw new Error(errorTexts[data.error] || "データを表示できません。公開スナップショットと閲覧範囲を確認してください。");
  if (!data.snapshot) return data;
  if (state.generation && data.snapshot.generation_id !== state.generation) throw new Error("スナップショットが更新されました。「更新」で読み直してください。");
  state.generation = data.snapshot.generation_id; state.snapshotAt = data.snapshot.generated_at;
  if (Array.isArray(data.warnings)) state.snapshotWarnings = data.warnings;
  snapshotNote();
  return data;
}
const errorTexts = {
  cursor_scope_or_generation_changed: "スナップショットが更新されました。「更新」で読み直してください。",
  bad_search_filter: "検索の条件を読み取れませんでした。語句を変えて検索してください。",
  search_cursor_invalid: "検索の続きの位置が使えなくなりました。もう一度検索してください。",
  search_query_empty: "検索する語句を入れてください。資料: 分類: 開始: 終了: は絞り込みの指定なので、それだけでは検索できません。",
  config_unreadable: "設定を読み取れないため表示できません。",
};
const warningLabels = {
  history_completeness_unverified: "MCS の履歴をすべて取得できたかは確認できていません。",
  snapshot_may_lag_live_state: "MCS の現在の状態より遅れている場合があります。",
};
function ageText(seconds) {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  return minutes < 1 ? "1分以内" : minutes < 60 ? `${minutes}分前` : minutes < 48 * 60 ? `${Math.floor(minutes / 60)}時間前` : `${Math.floor(minutes / 1440)}日前`;
}
// How old the data is: in the heading, and as a line under the posts with the snapshot's own warnings.
function snapshotNote() {
  if (state.snapshotAt == null) return;
  const at = fmt(state.snapshotAt, {year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"}), age = ageText(Date.now() / 1000 - state.snapshotAt);
  $("snapshot-time").textContent = `スナップショット ${at} JST · ${age}`;
  const warnings = (state.snapshotWarnings || []).map(code => warningLabels[code] || `スナップショットの注意事項があります（${code}）。`);
  $("snapshot-note").textContent = [`表示は ${at} JST（${age}）時点のスナップショットです。`, ...warnings].join("");
}
globalThis.setInterval?.(snapshotNote, 60000);
// The room's own heading (same names carry facility and ID; group rooms their group name).
const patientTitle = patient => patient.heading || patient.patient_name || `患者 ${patient.project_id}`;
// Facility and group names, unless the heading already carries them.
const placeNames = patient => [patient.station_name, patient.group_name].filter(name => name && !patientTitle(patient).includes(name));
function signalBadge(signals) {
  // Open signals on the list row: a partial count is a lower bound and a failed one is unknown, never 0.
  if (!signals) return null;
  const {count, state: status} = signals;
  if (status === "confirmed") {
    const badge = count ? chip(`シグナル ${count}`, "warn") : null;
    if (badge) badge.title = `未解消のシグナル ${count} 件`;
    return badge;
  }
  if (status !== "partial" || count == null) {const badge = chip("シグナル 不明", "muted"); badge.title = "未解消のシグナルを取得できません"; return badge;}
  const unreadable = signals.reason === "unreadable";
  const badge = count ? chip(`シグナル ${count}+`, "warn") : chip(unreadable ? "シグナル 一部不明" : "シグナル 判定中", "muted");
  badge.title = `${unreadable ? "一部のシグナルの状態を読み取れません" : "判定は終わっていません"}。判定済みの範囲で未解消のシグナル ${count} 件`;
  return badge;
}
function patientList() {
  let selected = null;
  $("patients").replaceChildren();
  // A remembered patient that is not on the loaded pages still shows, first, so the selection is visible.
  const open = state.openPatient, rows = open && !$("patient-search").value.trim() && !state.patients.some(row => row.project_id === open.project_id) ? [open, ...state.patients] : state.patients;
  for (const patient of rows) {
    const button = el("button", null, "patient" + (patient.project_id === state.project ? " selected" : ""));
    button.setAttribute("aria-current", patient.project_id === state.project ? "page" : "false");
    const label = el("span", null, "patient-label"), top = el("span", null, "patient-top");
    top.append(el("span", patientTitle(patient), "patient-title"));
    if (patient.is_archived) top.append(chip("アーカイブ", "muted"));
    const badge = signalBadge(patient.open_signals);
    label.append(top, el("small", [`ID ${patient.project_id}`, ...placeNames(patient), patient.latest_at != null && `最終投稿 ${shortDate(patient.latest_at)}`].filter(Boolean).join(" · ")));
    // The disease and the open-signal badge share a line, so the name keeps the full width.
    if (patient.disease || badge) {const sub = el("span", null, "patient-sub"); if (badge) sub.append(badge); if (patient.disease) sub.append(el("small", patient.disease, "patient-disease")); label.append(sub);}
    const count = el("span", patient.message_count, "count"); count.title = `保存済み ${patient.message_count} 件`;
    button.append(label, count);
    button.onclick = () => selectPatient(patient);
    $("patients").append(button);
    if (patient.project_id === state.project) selected = button;
  }
  // The list is ordered by latest activity, so the open patient may move: keep its row in view.
  selected?.scrollIntoView({block: "nearest"});
  if (!rows.length) $("patients").append(el("p", "該当する患者はいません", "empty"));
  $("patient-count").textContent = `${state.patients.length}${state.patientCursor ? "+" : ""}`;
  $("more-patients").hidden = !state.patientCursor;
}
// The open patient survives 「更新」 (a page reload) through the address's #project=N, not browser storage.
function rememberedId() {const match = /^#project=(\d+)$/.exec(globalThis.location?.hash || ""); return match ? Number(match[1]) : null;}
async function rememberedPatient() {
  const id = rememberedId();
  if (id == null) return null;
  return state.patients.find(row => row.project_id === id)
    || (await api("patients", {q: String(id)}).catch(() => ({items: []}))).items.find(row => row.project_id === id) || null;
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
    if (!state.project && state.patients.length) {
      const chosen = await rememberedPatient() || state.patients[0];
      if (request === state.patientRequest && !state.project) selectPatient(chosen);
    }
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
  state.project = patient.project_id; state.openPatient = patient; state.query = ""; $("message-search").value = ""; $("clear-search").hidden = true;
  const meta = [`ID ${patient.project_id}`, ...placeNames(patient), patient.disease, `保存済み ${patient.message_count} 件`,
    patient.latest_at != null && `最終投稿 ${shortDate(patient.latest_at)}`, patient.last_complete_fetch != null && `最終取得 ${postTime(patient.last_complete_fetch)}`];
  state.countBox = el("span", null, "count-chips");
  shell.heading(patientTitle(patient), meta.filter(Boolean).join(" · "), [...(patient.is_archived ? [chip("アーカイブ", "muted")] : []), ...fetchChips(patient), state.countBox, ...mcsRoomLink(patient.mcs_url)]);
  globalThis.history?.replaceState?.(null, "", `#project=${patient.project_id}`);
  state.panelPage = 0; closeDetail(); patientList(); loadPatientContext(patient.project_id); loadSideStatus(); loadCounts(); loadMain();
  state.onSelect?.(patient.project_id);
}
function fetchChips(patient) {
  // Only an incomplete fetch is called out; a complete fetch with the history end on record shows nothing.
  const labels = {pending: "未取得", incomplete: "一部未取得", partial: "一部取得", failed: "取得失敗", forbidden: "閲覧権限なし"};
  const imports = {capturing: ["初回取込中", "info"], held: ["初回取込 保留", "warn"], unknown: ["初回取込 状態不明", "warn"]};
  const chips = [];
  if (patient.fetch_state !== "complete") {
    const label = labels[patient.fetch_state] || "取得状況を要確認", reason = patient.fetch_reason_label;
    chips.push(chip(reason && reason !== label ? `${label}（${reason}）` : label, "warn"));
  }
  if (patient.history_floor !== -1) chips.push(chip("履歴全体の取得は未確認", "warn"));
  for (const item of chips) item.title = `取得状態: ${item.textContent}${patient.last_complete_fetch != null ? ` · 最終取得 ${postTime(patient.last_complete_fetch)}` : " · 完了した取得の記録なし"}`;
  if (imports[patient.initial_import]) chips.push(chip(...imports[patient.initial_import]));
  return chips;
}
// The MCS room, as a plain external link: https only, nothing from the local machine.
function externalLink(url, text, cls) {
  if (!/^https:\/\//.test(url || "")) return null;
  const link = el("a", text, cls);
  link.href = url; link.target = "_blank"; link.rel = "noreferrer noopener";
  return link;
}
const mcsRoomLink = url => {const link = externalLink(url, "MCSで開く ↗", "chip chip-link"); return link ? [link] : [];};
async function loadCounts() {
  // Heading counts for the open patient: open signals and reply checks, each a way into its tab.
  const request = ++state.countRequest, project = state.project, box = state.countBox;
  const current = () => request === state.countRequest && project === state.project && box.isConnected;
  try {
    const data = await api("counts", {project});
    if (current()) box.replaceChildren(...countChips(data));
  } catch {
    if (current()) box.replaceChildren(countChip("未解消シグナル 取得できません", "muted", "signals"), countChip("返答確認 取得できません", "muted", "replies"));
  }
}
function countChip(text, tone, view) {
  const button = el("button", text, `chip chip-${tone}`);
  button.type = "button"; button.onclick = () => showView(view);
  return button;
}
function countChips({open_signals: signals = {}, replies = {}}) {
  let signal;
  if (signals.state === "confirmed" && signals.count != null) signal = countChip(`未解消シグナル ${signals.count}`, signals.count ? "warn" : "muted", "signals");
  else if (signals.state === "partial" && signals.count != null) {
    // Partial is never 0件: either still being judged, or partly unreadable.
    const unreadable = signals.reason === "unreadable";
    signal = countChip(signals.count ? `未解消シグナル ${signals.count}以上（${unreadable ? "一部読み取れません" : "判定中"}）` : unreadable ? "未解消シグナル 一部読み取れません" : "未解消シグナル 判定中", signals.count ? "warn" : "muted", "signals");
    signal.title = `${unreadable ? "一部のシグナルの状態を読み取れません" : "判定が終わっていない投稿があります"}。判定済みの範囲で ${signals.count} 件`;
  } else signal = countChip("未解消シグナル 取得できません", "muted", "signals");
  let reply;
  const open = [replies.not_found && `返答が見つからない ${replies.not_found}`, replies.unknown && `確認できない ${replies.unknown}`].filter(Boolean);
  if (replies.state === "observed" && replies.requests != null) reply = countChip(`返答確認 依頼 ${replies.requests}${open.length ? `（${open.join("・")}）` : ""}`, open.length ? "warn" : "muted", "replies");
  else if (replies.state === "partial" && replies.requests != null) {
    reply = countChip(`返答確認 判定中（依頼 ${replies.requests}以上${open.length ? `・${open.join("・")}` : ""}）`, open.length ? "warn" : "muted", "replies");
    reply.title = "解析が終わっていない投稿があります。数は解析済みの範囲です。";
  } else reply = countChip("返答確認 取得できません", "muted", "replies");
  return [signal, reply];
}
async function loadPatientContext(project) {
  const request = ++state.contextRequest, panel = $("patient-context");
  panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を読込中…", "context-empty"));
  try {
    const data = await api("context", {project});
    if (request !== state.contextRequest || project !== state.project) return;
    const groups = data.sections.filter(group => group.items.length);
    panel.replaceChildren(el("h2", "患者コンテキスト"));
    const preview = el("div", null, "context-preview"), details = el("details", null, "context-records"), sections = new Map();
    if (!groups.length) preview.append(el("p", "表示できる患者コンテキストはまだ保存されていません。", "context-empty"));
    for (const group of groups.slice(0, 4)) {
      // Each item opens the full record below (keyboard and touch, not only the tooltip).
      const item = group.items[0], entry = el("button", null, "context-brief"), label = el("span", null, "context-label");
      entry.type = "button";
      entry.onclick = () => {details.open = true; const target = sections.get(group); target.scrollIntoView({block: "nearest"}); target.focus({preventScroll: true});};
      const text = `${subjectLabels[item.subject] || "対象未確認"}: ${item.text || item.evidence || ""}`, line = el("span", text, "context-text");
      line.title = text; label.append(el("b", group.label), el("small", item.posted_at_ts == null ? "日付未取得" : shortDate(item.posted_at_ts), "context-date"));
      entry.append(label, line);
      if (item.scope_note) entry.append(el("small", item.scope_note, "context-scope"));
      preview.append(entry);
    }
    const total = groups.reduce((count, group) => count + group.items.length, 0);
    details.append(el("summary", total ? `背景・根拠をすべて表示 · ${total}件` : "連携サマリーと取得状況を表示"));
    // The expansion starts with the MCS 連携サマリー, the context summary and how complete the reading is.
    const overview = el("section", null, "context-overview"); overview.tabIndex = -1;
    overview.append(karteCard(data.karte_summary));
    if (data.summary?.length) {const summary = el("div", null, "context-summary"); summary.append(el("h3", "まとめ"), ...data.summary.map(item => el("p", item.text))); overview.append(summary);}
    if (data.status?.length) {const status = el("div", null, "context-status"); status.append(el("h3", "取得・解析の状況"), ...data.status.map(line => el("p", line))); overview.append(status);}
    details.append(overview);
    for (const group of groups) {
      const section = el("section"); section.tabIndex = -1; sections.set(group, section); section.append(el("h3", group.label));
      for (const item of group.items) {
        const record = contextSection({patient_context: [item]});
        record.firstElementChild.textContent = fmt(item.posted_at_ts, {year:"numeric", month:"numeric", day:"numeric"});
        section.append(record);
      }
      details.append(section);
    }
    // One line saying how complete the reading is; it opens the overview above for the full text.
    const line = [data.karte_summary?.line, data.as_of == null ? "反映時点は記録されていません" : null, ...(data.status || [])].filter(Boolean).join(" · ");
    const completeness = el("button", line, "context-completeness");
    completeness.type = "button"; completeness.title = line;
    completeness.onclick = () => {details.open = true; overview.scrollIntoView({block: "nearest"}); overview.focus({preventScroll: true});};
    panel.append(preview, details, completeness);
  } catch (error) {
    if (request === state.contextRequest && project === state.project) panel.replaceChildren(el("h2", "患者コンテキスト"), el("p", "背景情報を取得できませんでした。「更新」で読み直してください。", "context-empty"));
  }
}
const karteStates = {not_fetched: "未取得", failed: "取得失敗", empty: "空（MCS に記載がありません）", present: "あり"};
function karteCard(karte) {
  // MCS 患者連携サマリー: the whole comment when present, otherwise exactly which of 未取得 / 取得失敗 / 空 it is.
  if (!karte) return card("連携サマリー（MCS）", [el("p", "連携サマリーの状態を取得できませんでした。", "state-note")]);
  const box = card("連携サマリー（MCS）", [], {actions: [chip(karteStates[karte.state] || "状態不明", ["present", "empty"].includes(karte.state) ? "muted" : "warn")]});
  const absent = {not_fetched: "MCS の連携サマリーはまだ取得していません。", failed: "MCS の連携サマリーを取得できませんでした。", empty: "MCS の連携サマリーは空でした。"};
  if (karte.comment) box.append(el("p", karte.comment, "karte-comment"));
  else if (!(karte.state === "failed" && karte.failed_at != null)) box.append(el("p", absent[karte.state] || "連携サマリーの状態を確認できません。", "state-note"));
  const meta = [karte.updated_at && `MCS の更新 ${karte.updated_at}`, karte.updater_profession && `更新した職種 ${karte.updater_profession}`].filter(Boolean);
  if (meta.length) box.append(el("p", meta.join(" · "), "karte-meta"));
  if (karte.failed_at != null) box.append(el("p", karte.state === "failed" ? `最新の取得に失敗しました（${postTime(karte.failed_at)}）。一度も取得できていません。`
    : `最新の取得に失敗しました（${postTime(karte.failed_at)}）。前回取得した内容を表示しています。`, "state-note"));
  return box;
}
function post(row, detailed = false) {
  const article = el("article", null, "post");
  const name = row.sender_name || "投稿者未取得";
  article.append(el("span", Array.from(name)[0], "avatar"));
  const content = el("div", null, "post-content"), heading = el("div", null, "post-heading");
  heading.append(el("span", row.sender_self ? `${name}（自分）` : name, "sender"));
  if (row.profession || row.organization) heading.append(el("span", [row.profession, row.organization].filter(Boolean).join(" · "), "post-role"));
  heading.append(el("time", postTime(row.posted_at_ts), "timestamp"));
  // Urgency stays on the first line of the post; its reasons are in the detail.
  const marks = [...(row.sender_own_station && !row.sender_self ? [chip("自事業所", "muted")] : []), ...urgencyChips(row.urgency)];
  if (marks.length) {const box = el("span", null, "post-marks"); box.append(...marks); heading.append(box);}
  content.append(heading, postBody(row));
  if (detailed && row.urgency) content.append(...urgencyDetail(row.urgency));
  if (row.summary_lines?.length) {const summary = el("details", null, "post-summary"); summary.append(el("summary", "記録の要約")); for (const line of row.summary_lines) summary.append(el("p", line)); content.append(summary);}
  // One footer row: the actions on the post, then its record states as chips (shown in the detail too).
  const footer = el("div", null, "post-footer");
  if (!detailed) {
    const button = el("button", `詳細・返信${row.stored_replies ? ` ${row.stored_replies}件` : ""}`, "reply-link");
    button.onclick = () => openDetail(row);
    footer.append(button);
    const files = attachmentChip(row.attachments);
    if (files) footer.append(files);
    if (row.parent_id) footer.append(chip("返信の検索結果", "info"));
    const latest = lastReplyChip(row.last_reply);
    if (latest) footer.append(latest);
  }
  if (row.summary_state_label) footer.append(chip(row.summary_state_label, row.summary_state === "failed" ? "warn" : "muted"));
  footer.append(...flagChips(row.flags), ...observedChanges(row));
  if (row.stamp_line) footer.append(chip(row.stamp_line, stampIncomplete.test(row.stamp_line) ? "warn" : "muted"));
  if (row.stamps?.self) footer.append(chip(`自分のスタンプ ${row.stamps.self}`, "info"));
  if (!row.body_complete && row.body_state !== "deleted") footer.append(chip("本文は一部未取得です", "warn"));
  if (row.reply_count > row.stored_replies) footer.append(chip("未取得の返信があります", "warn"));
  const viewed = physicianChip(row.physician_viewed, detailed);
  if (viewed) footer.append(viewed);
  if (footer.children.length) content.append(footer);
  if (detailed && row.stamps) content.append(...stampDetail(row.stamps));
  article.append(content); return article;
}
function urgencyChips(urgency) {
  if (!urgency) return [];
  const chips = [];
  if (urgency.label) {
    const mark = chip(`${urgency.label}${urgency.qc_note || ""}`, "urgent");
    mark.title = urgency.reasons?.length ? `根拠: ${urgency.reasons.join(" / ")}` : "根拠は投稿の詳細にあります";
    chips.push(mark);
  }
  if (urgency.review_label) chips.push(chip(urgency.review_label, "warn"));
  return chips;
}
function urgencyDetail(urgency) {
  const lines = [];
  if (urgency.reasons?.length) lines.push(el("p", `緊急度の根拠: ${urgency.reasons.map(reason => `「${reason}」`).join(" ")}`, "post-note"));
  else if (urgency.level) lines.push(el("p", "緊急度の根拠の記載は記録されていません。", "post-note"));
  return lines;
}
function lastReplyChip(reply) {
  if (!reply) return null;
  const who = [reply.sender_name ? `${reply.sender_name}${reply.self ? "（自分）" : ""}` : "投稿者未取得", reply.profession].filter(Boolean).join("・");
  const text = `最新返信 ${reply.posted_at_ts == null ? "時刻未取得" : postTime(reply.posted_at_ts)} ${who}`;
  return chip(reply.complete ? text : `${text}（未取得の返信あり）`, reply.complete ? "muted" : "warn");
}
function flagChips(flags) {
  // Only what was observed as set is a chip; 未取得 states are listed in the detail (記録の状態).
  if (!flags) return [];
  return [["mention_self", "@自分宛て", "info"], ["pinned", "ピン留め", "muted"], ["bookmarked", "ブックマーク", "muted"]]
    .filter(([key]) => flags[key]?.value === true).map(([, text, tone]) => chip(text, tone));
}
const flagStates = {not_fetched: "未取得", failed: "取得失敗", invalid: "読み取れません", self_unknown: "自分のアカウントを特定できません"};
function flagLines(flags) {
  // 記録の状態: each of @自分宛て / ピン留め / ブックマーク, including the ones not fetched.
  return [["mention_self", "@自分宛て"], ["pinned", "ピン留め"], ["bookmarked", "ブックマーク"]].map(([key, label]) => {
    const flag = flags[key] || {};
    const text = flag.state === "observed" && typeof flag.value === "boolean" ? (flag.value ? "あり" : "なし") : flagStates[flag.state] || "状態不明";
    return el("p", `${label}: ${text}`, flag.state === "observed" ? null : "state-note");
  });
}
function observedChanges(row) {
  // Times are when the collector saw the change, not when the author made it; earlier text is not kept.
  const chips = [];
  if (row.change_observed_at != null) chips.push(chip(`変更を検知（取得側の観測 ${postTime(row.change_observed_at)}）`, "info"));
  if (row.deleted_observed_at != null) chips.push(chip(`削除を観測（取得側の観測 ${postTime(row.deleted_observed_at)}）`, "muted"));
  for (const item of chips) item.title = "MCS の投稿者が変更・削除した時刻ではなく、取得側がそれを見つけた時刻です。以前の本文は保存していません。";
  return chips;
}
const actorStates = {stale: "古い情報", failed: "取得失敗", not_fetched: "未取得"};
function stampDetail(stamps) {
  // Who pressed which stamp, and whether that list is complete, current, failed or not fetched.
  const lines = [];
  const actors = stamps.actors || {};
  if (stamps.state === "observed" && stamps.counts?.length) {
    if (actors.state === "complete" || actors.state === "stale") {
      const names = actors.items.map(actor => `${actor.emoji} ${actor.name || "氏名不明"}${actor.profession ? `（${actor.profession}）` : ""}${actor.self ? "（自分）" : ""}`);
      lines.push(el("p", `押した人${actors.state === "stale" ? "（古い情報）" : ""}: ${names.join(" / ") || "氏名の記録なし"}${actors.observed_at != null ? ` · ${postTime(actors.observed_at)} 時点` : ""}`, "post-note"));
    } else lines.push(el("p", `押した人: ${actorStates[actors.state] || "状態不明"}`, "post-note state-note"));
  }
  if (stamps.state === "invalid") lines.push(el("p", "スタンプの記録を読み取れませんでした。", "post-note state-note"));
  return lines;
}
// Every non-complete wording of stamp_format.thread_stamp_line / actor_line (not actor names such as 氏名不明).
const stampIncomplete = /^スタンプ 未取得|再取得失敗|押した人 (?:取得失敗|古い情報)|取得状態不明/;
function postBody(row) {
  // A complete post with no text (a file-only post) has no body; only an incomplete one is 未取得.
  if (row.body_state === "deleted") return el("p", "削除された投稿", "post-body post-body-missing");
  if (row.body_text) return el("p", row.body_text, "post-body");
  return el("p", row.body_state === "full" ? "本文なし" : "本文未取得", "post-body post-body-missing");
}
function attachmentChip(groups = []) {
  const count = state => groups.filter(item => state.includes(item.state)).reduce((sum, item) => sum + item.count, 0);
  const total = count(groups.map(item => item.state)), failed = count(["failed"]), pending = count(["pending"]);
  if (!total) return null;
  const missing = [failed && `取得失敗 ${failed}`, pending && `未取得 ${pending}`].filter(Boolean);
  return chip(`添付 ${total}件${missing.length ? `（${missing.join("・")}）` : ""}`, missing.length ? "warn" : "muted");
}
const viewedReasons = {care_team_not_current: "ケアチーム情報が最新ではありません", reactors_not_current: "スタンプを押した人の情報が最新ではありません", care_team_profession_unknown: "職種の分からない担当者がいます"};
function physicianChip(viewed, detailed) {
  // 医師の「見ました」は観測できたかどうかだけを示す（未観測は未読ではない）。
  if (!viewed) return null;
  if (viewed.state === "known") return viewed.physicians ? chip(`医師 見ました未観測 ${viewed.not_observed}/${viewed.physicians}`, viewed.not_observed ? "warn" : "muted") : null;
  const reason = viewedReasons[viewed.reason];
  return chip(`医師 見ました 確認できません${detailed && reason ? `（${reason}）` : ""}`, "muted");
}
function chronological(rows) {return rows.slice().sort((a,b) => (a.posted_at_ts ?? -Infinity) - (b.posted_at_ts ?? -Infinity) || a.message_id - b.message_id);}
async function loadTimeline(more = false) {
  if (!state.project) return;
  const request = ++state.mainRequest;
  $("error").hidden = true; $("older").disabled = true;
  if (!more) {state.cursor = null; $("timeline").replaceChildren(); $("timeline-status").textContent = "投稿を読込中…"; $("timeline-status").hidden = false; $("older").hidden = true;}
  try {
    const data = await api("timeline", {project: state.project, cursor: more ? state.cursor : null});
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
    else {$("timeline").replaceChildren(fragment); panel.scrollTop = panel.scrollHeight;}
    $("older").hidden = !state.cursor;
    const empty = !$("timeline").children.length;
    $("timeline-status").hidden = !empty;
    $("timeline-status").textContent = empty ? "保存済みの投稿はありません。" : "";
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
    if (source.message.flags) flags.append(...flagLines(source.message.flags));
    else for (const text of source.message.metadata_flags || []) flags.append(el("p", text));
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
    const appendFiles = items => {for (const file of items) files.append(attachmentEntry(file));};
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
const fileStates = {downloaded: "取得済み", pending: "未取得", failed: "取得失敗", pruned: "原本保持期間終了"};
const ocrStates = {complete: "作成済み", empty: "文字なし（読み取れる文字がありませんでした）", unprocessed: "未作成", failed: "作成に失敗", unsupported: "対応していない形式のため作成していません", unavailable: "原本がないため作成できません", rejected_by_policy: "表の読み取りが不確かなため除外しました"};
const fileErrors = {download_failed: "ダウンロードに失敗", download_too_large: "サイズの上限を超えています", download_empty: "空のファイルでした", disk_full: "保存領域が不足しています", url_not_allowed: "許可されていない取得先です", deadline_exceeded: "時間内に終わりませんでした", network_error: "通信エラー", http_error: "MCS サーバーのエラー", session_expired: "ログインの期限切れ", no_token: "ログインしていません", forbidden: "閲覧が許可されていません", response_too_large: "応答が大きすぎます", db_write_failed: "保存に失敗"};
function fileSize(bytes) {return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : bytes >= 1024 ? `${Math.round(bytes / 1024)} KB` : `${bytes} B`;}
function attachmentEntry(file) {
  // Every recorded attachment fact: file state and reason, size, OCR state, OCR pages and what was found in them.
  const entry = el("details"), ocr = file.ocr || {}, stored = ["downloaded", "pruned"].includes(file.state);
  entry.append(el("summary", file.name || `添付 ${file.attachment_id}`));
  entry.open = !(ocr.pages || []).length;  // only long OCR text starts folded
  const states = el("div", null, "file-states");
  states.append(chip(`ファイル: ${fileStates[file.state] || "状態を確認できません"}`, ["failed", "pending"].includes(file.state) ? "warn" : "muted"),
    chip(`文字記録: ${ocrStates[ocr.state] || "状態を確認できません"}`, ["failed", "rejected_by_policy", "unprocessed"].includes(ocr.state) ? "warn" : "muted"));
  if (stored && file.bytes > 0) states.append(chip(fileSize(file.bytes), "muted"));
  entry.append(states);
  if (file.state !== "downloaded" && file.error && file.error !== "not_recorded") entry.append(el("p", `理由: ${fileErrors[file.error] || `エラー（${file.error}）`}`, "state-note"));
  if (ocr.state === "rejected_by_policy" && /^https:\/\//.test(ocr.original_project_url || "")) {
    const link = el("a", "MCS で原本を確認する", "text-button");
    link.href = ocr.original_project_url; link.target = "_blank"; link.rel = "noreferrer noopener";
    entry.append(link);
  }
  if (ocr.state === "complete" && ocr.original_available === false) entry.append(el("p", "原本ファイルは保持期間を過ぎています。文字記録だけが残っています。", "state-note"));
  for (const page of ocr.pages || []) {
    entry.append(el("h4", `${page.page}ページ`, "ocr-page"));
    entry.append(el("p", page.text == null ? "このページの文字は記録されていません" : page.text || "（文字なし）", page.text ? "ocr-text" : page.text == null ? "state-note" : "ocr-empty"));
  }
  const medical = ocr.medical || {};
  const headings = [...new Set((medical.sections || []).map(item => item.heading).filter(Boolean))];
  if (headings.length) entry.append(el("p", `文書の見出し: ${headings.join("・")}`, "ocr-found"));
  for (const item of medical.pump_candidates || []) {
    const line = el("p", `${item.page}ページ ${item.label}: ${item.raw_value || "値なし"}`, "ocr-found");
    if (item.status === "needs_review") line.append(" ", chip("要確認", "warn"));
    entry.append(line);
  }
  return entry;
}
async function loadSections() {
  try {
    const data = await api("sections");
    state.sections = [["", "タイムライン"], ...data.items.map(item => [item.key, item.label]), ["registered", "MCS登録データ"]];
    viewTabs();
  } catch (error) {fail(error);}
}
function showView(key) {state.panel = key; state.panelPage = 0; closeDetail(); loadMain();}
function viewTabs() {
  const strip = tabs(state.sections, state.panel, showView), hadFocus = $("view-tabs").contains(document.activeElement);
  $("view-tabs").replaceChildren(strip);
  // The row scrolls sideways; a vertical wheel scrolls it too, and fades mark the sides with hidden tabs.
  strip.onscroll = tabFades;
  strip.onwheel = event => {
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX) || strip.scrollWidth <= strip.clientWidth) return;
    strip.scrollLeft += event.deltaY; event.preventDefault();
  };
  // The chosen tab is scrolled into the row (never the page) and keeps keyboard focus across the rebuild.
  const selected = strip.children[state.sections.findIndex(([key]) => key === state.panel)];
  if (selected) revealTab(strip, selected);
  tabFades();
  if (hadFocus) selected?.focus({preventScroll: true});
}
const tabFadeWidth = 40;
function revealTab(strip, tab) {
  const start = tab.offsetLeft - tabFadeWidth, end = tab.offsetLeft + tab.offsetWidth + tabFadeWidth - strip.clientWidth;
  if (strip.scrollLeft > start) strip.scrollLeft = Math.max(0, start);
  else if (strip.scrollLeft < end) strip.scrollLeft = end;
}
function tabFades() {
  const box = $("view-tabs"), strip = box.firstElementChild;
  const hidden = strip ? strip.scrollWidth - strip.clientWidth : 0;
  box.classList.toggle("fade-start", hidden > 1 && strip.scrollLeft > 1);
  box.classList.toggle("fade-end", hidden > 1 && strip.scrollLeft < hidden - 1);
}
// Width changes (window, rail, returning to the MCS screens) change which tabs are hidden.
if (globalThis.ResizeObserver) new ResizeObserver(tabFades).observe($("view-tabs"));
function loadMain() {
  // A chosen tab wins; without one, a post search shows its results instead of the timeline.
  const timeline = !state.panel && !state.query;
  viewTabs();
  for (const id of ["older", "timeline", "timeline-status"]) if (!timeline) $(id).hidden = true;
  $("panel-view").hidden = timeline;
  if (timeline) {$("timeline").hidden = false; loadTimeline();}
  else if (state.panel === "registered") loadRegistered();
  else if (state.panel) loadPanel();
  else loadSearch();
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
async function loadSearch(more = false) {
  // Stored posts and attachment OCR text, with what was not searched; 「続きを検索」 pages on.
  // A search with no hit says only that nothing was found in what was searched, never that no record exists.
  if (!state.project) return;
  const request = ++state.mainRequest, project = state.project, query = state.query, view = $("panel-view");
  $("error").hidden = true;
  if (!more) {state.search = null; view.replaceChildren(el("p", "検索中…", "empty"));}
  const button = state.search?.more;
  if (button) button.disabled = true;
  try {
    const [data, senders] = await Promise.all([api("sources", {project, q: query, cursor: more ? state.search.cursor : null}), more ? null : senderMatches(project, query)]);
    if (request !== state.mainRequest) return;
    if (!more) {
      const box = el("div", null, "panel search-results"), list = el("div", null, "search-list"), empty = el("p", null, "empty"), notes = el("div"), next = el("button", "続きを検索", "load-more");
      next.onclick = () => loadSearch(true);
      box.append(el("h2", data.title || `「${query}」の検索結果`), ...(data.head || []).map(line => el("p", line, "panel-head")), list, empty, next, notes);
      if (senders) box.append(senders);
      state.search = {list, empty, notes, more: next};
      view.replaceChildren(box); scroller().scrollTop = 0;
    }
    const {list, empty, notes, more: next} = state.search;
    for (const item of data.items) list.append(searchItem(item));
    empty.textContent = list.children.length ? "" : data.next_cursor ? "ここまでに検索した範囲では見つかりませんでした。「続きを検索」で残りを検索できます。"
      : `${data.empty || "検索した範囲では見つかりませんでした。"}${data.state === "searched" ? "見つからないことは、記録がないことを意味しません。" : ""}`;
    empty.hidden = !empty.textContent;
    notes.replaceChildren(...(data.notes || []).map(note => el("p", note, "state-note")));
    if (data.state === "searched" && data.range_complete === false && !data.next_cursor) notes.append(el("p", "検索した範囲は記録の一部です。", "state-note"));
    state.search.cursor = data.next_cursor; next.hidden = !data.next_cursor;
  } catch (error) {
    if (request !== state.mainRequest) return;
    if (!more) view.replaceChildren(el("p", "検索できませんでした。", "empty"));
    fail(error);
  } finally {if (button && request === state.mainRequest) button.disabled = false;}
}
// The same width/case folding as the server's search, without spaces.
const foldText = text => String(text || "").normalize("NFKC").toLowerCase().replace(/\s+/g, "");
async function senderMatches(project, query) {
  // Sources search covers text only: posts whose sender name contains every search word come from the post search.
  const terms = query.split(/\s+/).map(foldText).filter(Boolean);
  if (!terms.length) return null;
  let data;
  try {data = await api("search", {project, q: query});}
  catch {return el("p", "投稿者名での検索はできませんでした。", "state-note");}
  const rows = data.items.filter(row => terms.every(term => foldText(row.sender_name).includes(term)));
  if (!rows.length) return null;
  const box = el("section", null, "sender-matches");
  box.append(el("h3", "投稿者名が一致する投稿"), ...rows.map(row => post(row)));
  if (data.next_cursor) box.append(el("p", "表示は一致した投稿の一部です。", "state-note"));
  return box;
}
function searchItem(item) {
  const row = el("div", null, "panel-item search-item"), meta = el("div", null, "panel-item-meta");
  meta.append(chip(item.source === "ocr" ? `添付OCR${item.page != null ? ` ${item.page}ページ` : ""}` : "本文", item.source === "ocr" ? "info" : "muted"));
  if (item.message_id != null) meta.append(openPostButton(item.message_id));
  row.append(el("p", item.text), meta);
  return row;
}
async function loadRegistered() {
  if (!state.project) return;
  const request = ++state.mainRequest, project = state.project;
  $("error").hidden = true;
  $("panel-view").replaceChildren(el("p", "読込中…", "empty"));
  try {
    const data = await api("registered", {project});
    if (request !== state.mainRequest) return;
    $("panel-view").replaceChildren(renderRegistered(data));
    scroller().scrollTop = 0;
  } catch (error) {if (request === state.mainRequest) {$("panel-view").replaceChildren(el("p", "MCS登録データを取得できませんでした。", "empty")); fail(error);}}
}
const registeredTones = {complete: "muted", empty: "muted", unknown: "warn", failed: "warn", stale: "warn"};
function renderRegistered(data) {
  // MCS registered data is its own record, apart from chat: every dataset says whether it was obtained.
  const box = el("div", null, "panel registered");
  box.append(el("h2", "MCS登録データ"), el("p", "MCS の患者情報に登録されたデータです。チャットの投稿とは別の記録です。", "panel-head"));
  if (data.collection_enabled === false) box.append(el("p", "登録データの収集は設定で無効になっています。表示は以前に取得した記録だけです。", "state-note"));
  else if (data.collection_enabled !== true) box.append(el("p", "登録データの収集の設定を読み取れません。表示は取得済みの記録だけです。", "state-note"));
  if (data.state === "unavailable") {box.append(el("p", "このスナップショットは古い形式のため、登録データを読み取れません。", "state-note")); return box;}
  if (data.state === "not_linked" || !data.datasets?.length) {
    box.append(el("p", "この患者の登録データは取得していません（MCS の医療記録との対応付けを確認できないか、グループのルームです）。", "state-note"));
    return box;
  }
  const cards = el("div", null, "cards");
  for (const dataset of data.datasets) cards.append(registeredCard(dataset));
  box.append(cards);
  return box;
}
const referenceParts = {scalar: "", max: "最高", min: "最低", left: "左", right: "右"};
function referenceText(reference = {}) {
  const parts = [];
  for (const [component, label] of Object.entries(referenceParts)) {
    const upper = reference[`upper_reference_limit_${component}`], lower = reference[`lower_reference_limit_${component}`];
    if (upper == null && lower == null) continue;
    parts.push(`${label}${lower ?? ""}〜${upper ?? ""}`);
  }
  return parts.join(" ");
}
const registeredReasons = {
  schema_error: "応答の形式を読み取れませんでした", http_error: "MCS がエラーを返しました", forbidden: "閲覧権限がありません",
  session_expired: "ログインの有効期限が切れていました", network_error: "通信エラー", deadline_exceeded: "時間内に終わりませんでした",
  capacity_limit: "取得件数の上限に達しました", scope_changed: "取得範囲が変わりました", fetch_error: "取得に失敗しました",
  snapshot_missing: "一部のデータを取得できませんでした", page_limit: "ページ数の上限で取得を止めました",
  association_unverified: "MCS の医療記録との対応付けを確認できません", cached_session_required: "ログイン情報がないため取得していません",
  definition_mapping_changed: "検査項目の定義が変わりました", run_lock_busy: "別の取得処理が実行中でした", restore_in_progress: "復元処理の実行中でした",
};
const registeredDate = value => typeof value === "number" ? postTime(value) : value || "日付未記録";
function registeredCard(dataset) {
  const actions = [chip(dataset.state_label || "未確認", registeredTones[dataset.state] || "warn"), ...(dataset.historical ? [chip("過去の記録", "muted")] : [])];
  const box = card(dataset.label, [], {wide: true, actions});
  const meta = [dataset.last_complete_at != null && `最終取得 ${postTime(dataset.last_complete_at)}`,
    dataset.attempted_at != null && dataset.attempted_at !== dataset.last_complete_at && `最終試行 ${postTime(dataset.attempted_at)}`,
    dataset.reason && `理由: ${registeredReasons[dataset.reason] || "理由を判別できません"}`].filter(Boolean);
  if (meta.length) box.append(el("p", meta.join(" · "), "panel-head"));
  if (dataset.item) box.append(el("p", [dataset.item.name || "項目名未記録", dataset.item.unit && `単位 ${dataset.item.unit}`, referenceText(dataset.item.reference) && `基準 ${referenceText(dataset.item.reference)}`].filter(Boolean).join(" · ")));
  const rows = dataset.rows || [];
  const columns = {
    medication_periods: [["開始", row => row.begin_date || "未記録"], ["終了", row => row.end_date || "未記録"], ["薬剤", row => (row.medicines || []).filter(Boolean).join("、") || "薬剤名未記録"]],
    observation_items: [["項目", row => row.name || "未記録"], ["単位", row => row.unit || ""], ["基準", row => referenceText(row.reference)]],
    observation_values: [["日時", row => registeredDate(row.issued_at)], ...["scalar", "max", "min", "left", "right"]
      .filter(key => rows.some(row => row[key] != null)).map(key => [referenceParts[key] || "値", row => row[key] ?? ""])],
  }[dataset.dataset];
  if (rows.length && columns) {
    const table = el("table", null, "rows"), head = el("tr"), body = el("tbody"), thead = el("thead");
    head.append(...columns.map(([title]) => el("th", title)));
    thead.append(head);
    for (const row of rows) {const tr = el("tr"); tr.append(...columns.map(([, value]) => el("td", String(value(row))))); body.append(tr);}
    table.append(thead, body);
    const wrap = el("div", null, "table-wrap"); wrap.append(table); box.append(wrap);
  } else if (!rows.length && dataset.state !== "empty") box.append(el("p", `行を表示できません（${dataset.state_label || "状態不明"}）。`, "state-note"));
  if (dataset.rows_total > rows.length) box.append(el("p", `他${dataset.rows_total - rows.length}件（先頭 ${rows.length} 件を表示）`, "panel-head"));
  return box;
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
