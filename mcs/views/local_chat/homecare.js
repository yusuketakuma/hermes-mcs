"use strict";
// Home-care core screens (staff, patient register, visit day board, MCS links). Shares $, el, api, fail, state and the UI kit with app.js.
// Occupation modules add their own sections; the core only reads their declared qualifications, roles and visit kinds from api/hc/meta.
const hc = {enabled: false, routes: false, csrf: "", actor: null, resident: false, user: null, meta: {qualifications: {}, roles: {}, visit_kinds: {}}, staff: [], patients: [], patient: null, day: null, mode: "mcs", request: 0, view: 0, linkRequest: 0,
  // Occupation modules register their own register section and visit-sheet card here (see pharmacy.js).
  modules: {}};
const VISIT_STATUS = {planned: "予定", done: "実施", cancelled: "中止"};
const CONTACTS = {patient: "本人", keyperson: "キーパーソン", facility: "施設", other: "その他"};
const PATIENT_STATUS = {active: "利用中", suspended: "休止", ended: "終了"};
const visitForm = () => [["visit_date", "訪問日（必須）", "date"], ["start_time", "開始時刻", "time"],
  ["kind", "種類", "select", hc.meta.visit_kinds], ["staff_id", "訪問する職員", "staff"],
  ["status", "状態", "select", VISIT_STATUS], ["note", "記録・申し送り", "textarea"]];
// The patient form is grouped by meaning; the flat spec is what is read back and sent.
const PATIENT_GROUPS = [
  ["本人", [["name", "氏名（必須）"], ["kana", "フリガナ"], ["birth_date", "生年月日", "date"]]],
  ["連絡先", [["phone", "本人電話", "tel"], ["preferred_contact", "優先連絡先", "select", CONTACTS],
    ["contact_phone", "優先連絡先の電話（施設・その他）", "tel"], ["keyperson_name", "キーパーソン氏名"], ["keyperson_relation", "続柄"],
    ["keyperson_phone", "キーパーソン電話", "tel"], ["contact_note", "連絡しやすい時間など"]]],
  ["訪問先", [["postal_code", "郵便番号"], ["address", "住所"], ["residence", "施設・部屋"],
    ["access_note", "訪問先へのアクセス", "textarea"], ["caution_note", "注意事項", "textarea"]]],
  ["利用状態", [["status", "利用状態", "select", PATIENT_STATUS]]]];
const PATIENT_FORM = PATIENT_GROUPS.flatMap(([, fields]) => fields);
const STAFF_FORM = [
  ["name", "氏名（必須）"], ["kana", "フリガナ"], ["qualifications", "資格（1つ以上）", "qualifications"],
  ["tailscale_login", "Tailscale のログイン名"], ["admin", "管理者（職員の登録・変更ができる）", "checkbox", false],
  ["active", "在籍", "checkbox", true]];
const HC_ERRORS = {version_conflict: "ほかの人が先に更新しました。入力は残しています。最新を読み直してから、もう一度保存してください。",
  already_linked: "この患者か MCS の患者は、すでに別の相手と対応付けられています。", not_linked: "対応付けが見つかりません。読み直してください。",
  already_assigned: "この職員はすでにこの役割の担当です。", admin_required: "この操作は管理者だけができます。",
  not_qualified: "この役割に必要な資格を持つ在籍中の職員を選んでください。", role_not_allowed: "この役割は今は使えません。", still_assigned: "担当を持っています。先に担当を交代するか外してください。", team_changed: "ほかの人が先に担当を変えました。最新のケアチームを読み直しました。",
  visit_not_qualified: "この種類の訪問を「実施」にできるのは、必要な資格（薬局なら薬剤師）を持つ職員だけです。記録する人と訪問した職員を確かめてください。",
  actor_required: "操作者を選んでください。", invalid_fields: "入力を確認してください。", not_found: "記録が見つかりません。",
  project_not_allowed: "この MCS の患者は閲覧範囲の外です。",
  route_unavailable: "訪問ルートは設定されていません。", route_failed: "地図サービスからルートを受け取れませんでした。時間をおいてやり直してください。",
  too_many_stops: "1日の訪問が多すぎてルートを作れません。職員ごとに分けてください。"};
async function hcPost(op, body) {
  const reply = await fetch(`api/hc/${op}`, {method: "POST", cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer",
    headers: {"Content-Type": "application/json", "X-MCS-CSRF": hc.csrf}, body: JSON.stringify({...body, actor: hc.actor})});
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) {const error = new Error(HC_ERRORS[data.error] || "保存できませんでした。"); error.fields = data.fields || []; error.code = data.error; throw error;}
  return data;
}

// ---------- Labels and dates ----------
const qualificationLabel = code => hc.meta.qualifications[code] || code;
const kindLabel = kind => hc.meta.visit_kinds[kind] || kind;
const roleLabel = role => hc.meta.roles[role]?.label || role;
const visitChip = visit => chip(VISIT_STATUS[visit.status] || visit.status, visit.status in VISIT_STATUS ? visit.status : "");
const WEEKDAYS = "日月火水木金土";
const dayOf = date => new Date(`${date}T00:00:00Z`);
// The office works in Japan time; dates are calendar days, so they are computed in UTC to avoid the browser's zone.
const siteToday = () => new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
const shiftDay = (date, days) => {const day = dayOf(date); day.setUTCDate(day.getUTCDate() + days); return day.toISOString().slice(0, 10);};
// Short dates name the year only when it is not this year (in Japan time), so older records are not ambiguous.
const dayLabel = date => {
  const day = dayOf(date), year = day.getUTCFullYear() === Number(siteToday().slice(0, 4)) ? "" : `${day.getUTCFullYear()}/`;
  return `${year}${day.getUTCMonth() + 1}/${day.getUTCDate()}(${WEEKDAYS[day.getUTCDay()]})`;
};
const longDay = date => {const day = dayOf(date); return `${day.getUTCFullYear()}年${day.getUTCMonth() + 1}月${day.getUTCDate()}日(${WEEKDAYS[day.getUTCDay()]})`;};
const visitWhen = visit => `${dayLabel(visit.visit_date)}${visit.start_time ? " " + visit.start_time : ""}`;
function age(birth) {
  const now = dayOf(siteToday()), born = dayOf(birth);
  if (Number.isNaN(born.getTime())) return null;
  const years = now.getUTCFullYear() - born.getUTCFullYear();
  return now.getUTCMonth() * 100 + now.getUTCDate() < born.getUTCMonth() * 100 + born.getUTCDate() ? years - 1 : years;
}
// Only a value that is one plain number becomes a tel: link; free text (内線, two numbers, notes) stays text as entered.
function telLink(phone) {
  const digits = phone.replace(/\D/g, "");
  const plain = /^\+?[\d\-()]+$/.test(phone.trim()) || /^\+[\d\-() ]+$/.test(phone.trim());
  if (!plain || digits.length < 3 || digits.length > 15) return el("span", phone);
  const link = el("a", phone, "tel"); link.href = `tel:${phone.trim().startsWith("+") ? "+" : ""}${digits}`;
  return link;
}
// A label followed by its number, for the contact list on the visit sheet; a missing number reads 未登録.
function withPhone(text, phone) {
  const node = el("span", null, "hc-contact");
  node.append(el("span", text), phone ? telLink(phone) : el("span", "電話未登録", "hc-quiet"));
  return node;
}
const personText = (name, relation) => name ? `${name}${relation ? `（${relation}）` : ""}` : "";

// ---------- Forms ----------
function field([key, label, type, option], value, form) {
  if (type === "qualifications") {
    const set = el("fieldset", null, "hc-field hc-checks"), held = value || [];
    set.name = key; set.append(el("legend", label));
    for (const code of [...new Set([...Object.keys(hc.meta.qualifications), ...held])]) {
      const item = el("label"), box = el("input"); box.type = "checkbox"; box.name = "qualification"; box.value = code; box.checked = held.includes(code);
      item.append(box, el("span", qualificationLabel(code))); set.append(item);
    }
    return set;
  }
  const wrap = el("label", null, "hc-field"), id = `hc-${form}-${key}`;
  wrap.append(el("span", label));
  let input;
  if (type === "staff") {
    input = el("select"); input.append(el("option", "未定")); input.firstChild.value = "";
    for (const person of hc.staff.filter(s => s.active || s.staff_id === value)) {const node = el("option", person.name); node.value = String(person.staff_id); input.append(node);}
    input.value = value == null ? "" : String(value);
  } else if (type === "select") {
    input = el("select");
    for (const [choice, text] of Object.entries(option)) {const node = el("option", text); node.value = choice; input.append(node);}
    if (value != null) input.value = String(value);
  } else if (type === "textarea") {input = el("textarea"); input.value = value || "";}
  else if (type === "checkbox") {input = el("input"); input.type = "checkbox"; input.checked = value == null ? option : Boolean(value); wrap.classList.add("hc-check");}
  else {input = el("input"); input.type = type || "text"; input.value = value || ""; input.autocomplete = "off";}
  input.id = id; input.name = key; wrap.append(input);
  return wrap;
}
function formValues(form, spec) {
  const values = {};
  for (const [key, , type] of spec) {
    if (type === "qualifications") {values[key] = [...form.querySelectorAll('[name="qualification"]')].filter(box => box.checked).map(box => box.value); continue;}
    const input = form.querySelector(`[name="${key}"]`);
    values[key] = type === "checkbox" ? input.checked : type === "staff" ? (input.value ? Number(input.value) : null) : input.value;
  }
  return values;
}
function recordForm(kind, spec, record, onSaved, {fixed = {}, groups = null} = {}) {
  const form = el("form", null, groups ? "hc-form hc-form-grouped" : "hc-form"), message = el("p", null, "state-note");
  if (groups) for (const [title, fields] of groups) {
    const set = el("fieldset", null, "hc-group"), grid = el("div", null, "hc-group-fields");
    grid.append(...fields.map(item => field(item, record?.[item[0]], kind))); set.append(el("legend", title), grid);
    form.append(set);
  }
  else for (const item of spec) form.append(field(item, record?.[item[0]], kind));
  const save = el("button", record ? "保存" : "登録", "button button-primary");
  form.append(save, message);
  let current = record;
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true; message.textContent = "";
    for (const node of form.querySelectorAll(".invalid")) node.classList.remove("invalid");
    let data;
    try {
      const values = {...formValues(form, spec), ...(current ? {} : fixed)};
      data = current ? await hcPost(`${kind}.update`, {id: current[`${kind}_id`], values, expected_version: current.version})
                     : await hcPost(`${kind}.create`, {values});
    } catch (error) {
      message.textContent = error.message;
      for (const name of error.fields || []) form.querySelector(`[name="${name}"]`)?.closest(".hc-field")?.classList.add("invalid");
      save.disabled = false;
      return;
    }
    current = data.record;
    await afterSave(save, message, () => onSaved(data.record));
  };
  return form;
}
async function afterSave(save, message, refresh) {
  // From here the form edits the stored record, so a retry after a failed refresh never creates it twice.
  save.textContent = "保存"; save.disabled = false;
  try {await refresh();} catch {message.textContent = "保存しました。画面を読み直せませんでした。もう一度保存しても二重には登録されません。";}
}
async function refreshAfterWrite(reload, saved = true) {
  // A change that was saved stays saved; a failed re-read says so instead of looking like a failed change.
  try {await reload();} catch (error) {fail(saved ? {message: "保存しました。画面を読み直せませんでした。更新して確認してください。"} : error);}
}

// ---------- Shared layout: expandable record tables and "＋ …" forms ----------
function rowTable(headers) {
  const table = el("table", null, "rows hc-table"), head = el("tr"), body = el("tbody");
  for (const text of headers) head.append(el("th", text));
  const thead = el("thead"); thead.append(head); table.append(thead, body);
  return {table, body};
}
const emptyRow = (text, span) => {const row = el("tr", null, "hc-empty"), cell = el("td", text); cell.colSpan = span; row.append(cell); return row;};
// One record per row; opening it shows its edit form on the row below, built when first opened.
function expandRow(cells, makeForm, span) {
  const row = el("tr", null, "hc-open"), formRow = el("tr", null, "hc-form-row"), holder = el("td");
  holder.colSpan = span; formRow.append(holder); formRow.hidden = true;
  cells.forEach((value, index) => {
    const cell = el("td", null, index === 0 ? "hc-key" : null);
    const content = index === 0 ? el("button", null, "hc-toggle") : cell;
    if (value instanceof Node) content.append(value); else content.textContent = value == null || value === "" ? "—" : String(value);
    if (index === 0) {content.type = "button"; content.setAttribute("aria-expanded", "false"); cell.append(content);}
    if (value === "" || value == null) cell.classList.add("hc-none");
    row.append(cell);
  });
  const toggle = row.querySelector(".hc-toggle");
  row.onclick = event => {
    if (event.target.closest("a")) return;
    const open = formRow.hidden;
    if (open && !holder.firstChild) holder.append(makeForm());
    formRow.hidden = !open; row.classList.toggle("open", open); toggle.setAttribute("aria-expanded", String(open));
  };
  const pair = document.createDocumentFragment(); pair.append(row, formRow);
  return pair;
}
const clipped = text => text ? el("span", text.split("\n")[0], "hc-clip") : "";
// A "＋ …" button and its new-record form. The form is built on first open; collapsing only hides it, so a draft
// survives; a successful save (close) or 「取り消す」 discards it.
function adder(label, makeForm, open = false) {
  const area = el("div", null, "hc-adder"), button = el("button", label, "button"); button.type = "button";
  const show = visible => {
    if (visible && !area.firstChild) {
      const discard = el("button", "取り消す", "text-button"); discard.type = "button"; discard.onclick = () => close();
      area.append(makeForm(), discard);
    }
    area.hidden = !visible; button.setAttribute("aria-expanded", String(visible));
  };
  const close = () => {area.replaceChildren(); show(false);};
  button.onclick = () => show(area.hidden);
  show(open);
  return {button, area, close};
}
// A register section: a wide card whose list redraws itself after a write; "＋ …" opens a new-record form above the list.
function registerSection(title, fill, add = null) {
  const body = el("div", null, "hc-section-body");
  let addForm = null;
  const reload = async () => {await fill(body, reload); addForm?.close();};
  if (add) addForm = adder(add.label, () => add.form(reload));
  fill(body, reload);
  return card(title, addForm ? [addForm.area, body] : [body], {wide: true, actions: addForm ? [addForm.button] : []});
}
function historyPages(list, first, render, fetchPage, label, current) {
  // Older entries load a page at a time below the ones shown; the place they go is kept apart from the form below.
  const more = el("div", null, "hc-more"); let shown = 0;
  const append = page => {
    for (const item of page.items) list.append(render(item));
    shown += page.items.length; more.replaceChildren();
    if (!page.more) return;
    const button = el("button", label, "load-more"); button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      let failed = null;
      const next = await fetchPage(shown).catch(error => {failed = error; return null;});
      if (!current() || !more.isConnected) return;
      if (next) {append(next); return;}
      // A repeated failure replaces the note rather than stacking another one.
      button.disabled = false; more.querySelector(".state-note")?.remove();
      more.append(el("p", "以前の記録を読み込めませんでした。", "state-note")); fail(failed);
    };
    more.append(button);
  };
  append(first);
  return more;
}

// ---------- Screens ----------
// Each screen takes a turn when it starts; a screen whose answer comes back after another one started draws nothing,
// and a save made on a screen refreshes or moves on only while that screen is still the one shown.
function hcScreen() {const turn = ++hc.view; return () => turn === hc.view;}
function hcMain(...nodes) {const page = el("div", null, "hc-page"); page.append(...nodes); shell.main(page);}
function markPatient(patientId) {
  for (const button of $("patients").querySelectorAll(".patient")) {
    const picked = button.dataset.patient === String(patientId);
    button.classList.toggle("selected", picked); button.setAttribute("aria-current", picked ? "page" : "false");
  }
}
async function loadStaff() {
  hc.staff = (await api("hc/staff")).items;
  const select = $("hc-actor"); select.replaceChildren(el("option", "操作者を選択"));
  select.firstChild.value = "";
  for (const person of hc.staff.filter(s => s.active)) {const option = el("option", person.name); option.value = String(person.staff_id); select.append(option);}
  if (hc.actor && hc.staff.some(s => s.staff_id === hc.actor && s.active)) select.value = String(hc.actor); else hc.actor = null;
  if (hc.resident) {
    // On the tailnet the server knows who is writing from the Tailscale login; the choice is shown, not made.
    hc.actor = hc.user?.staff_id ?? null; select.value = hc.actor == null ? "" : String(hc.actor); select.disabled = true;
  }
}
function showStaff() {
  const current = hcScreen(); hc.patient = null; closeDetail(); markPatient(null);
  shell.heading("職員", "操作者と担当薬剤師の名簿");
  const refresh = async () => {await loadStaff(); if (current()) showStaff();};
  const firstRun = !hc.staff.length;
  const add = adder("＋ 職員を追加", () => recordForm("staff", STAFF_FORM, null, async saved => {
    if (hc.resident && !hc.user) {location.reload(); return;}
    if (!hc.actor) hc.actor = saved.staff_id;
    await refresh();
  }), firstRun);
  const {table, body} = rowTable(["氏名", "資格", "管理者", "Tailscale のログイン名", "在籍"]);
  for (const person of hc.staff) {
    const name = el("span", person.name); if (person.kana) name.append(el("small", person.kana));
    const held = el("span", null, "chips"); held.append(...person.qualifications.map(code => chip(qualificationLabel(code), "info")));
    body.append(expandRow([name, held, person.admin ? chip("管理者", "warn") : "", person.tailscale_login,
      person.active ? chip("在籍", "done") : chip("退職", "muted")], () => recordForm("staff", STAFF_FORM, person, refresh), 5));
  }
  const intro = firstRun ? [el("p", hc.resident ? `最初に自分を職員として登録してください。今の Tailscale のログイン（${hc.login}）がこの人に結び付き、管理者になります。` : "最初に自分を職員として登録してください。登録した人がそのまま操作者と管理者になります。", "state-note")] : [];
  hcMain(card(`職員 ${hc.staff.length}人`, [...intro, add.area, ...(firstRun ? [] : [table])], {wide: true, actions: [add.button]}));
}
async function loadHcPatients() {
  const request = ++hc.request;
  const data = await api("hc/patients", {q: $("patient-search").value.trim()});
  if (request !== hc.request) return;
  hc.patients = data.items;
  $("patients").replaceChildren();
  for (const patient of hc.patients) {
    const picked = patient.patient_id === hc.patient;
    const button = el("button", null, "patient hc-patient" + (picked ? " selected" : ""));
    button.setAttribute("aria-current", picked ? "page" : "false");
    const label = el("span", null, "patient-label"), notes = el("span", null, "chips hc-side-chips");
    label.append(patient.name);
    if (patient.kana) label.append(el("small", patient.kana));
    if (patient.next_visit) notes.append(chip(`次 ${dayLabel(patient.next_visit.visit_date)}`, "planned"));
    if (patient.project_id) notes.append(chip("MCS あり", "info"));
    if (patient.status !== "active" && PATIENT_STATUS[patient.status]) notes.append(chip(PATIENT_STATUS[patient.status], "muted"));
    label.append(notes);
    button.append(label);
    button.dataset.patient = String(patient.patient_id);
    button.onclick = () => showHcPatient(patient.patient_id);
    $("patients").append(button);
  }
  const add = el("button", "＋ 患者を登録", "sidebar-more"); add.onclick = () => showHcPatient(null);
  $("patients").append(add);
  $("patient-count").textContent = String(hc.patients.length);
  $("more-patients").hidden = true;
}
// A patient opens on the visit sheet (訪問時の情報); 台帳を編集 is the second tab. A new patient opens on the form.
function showHcPatient(patientId, tab = "sheet") {
  const current = hcScreen(); hc.patient = patientId; closeDetail(); markPatient(patientId);
  if (patientId == null) return newPatient(current);
  // Until this patient's record arrives the page shows only its name from the list, never the previous patient's record.
  const name = hc.patients.find(row => row.patient_id === patientId)?.name || "患者";
  shell.heading(name, "患者台帳"); hcMain(el("p", "読込中…", "empty"));
  return drawPatient(patientId, tab, current).catch(() => {
    if (!current()) return;
    shell.heading(name, "患者台帳", [], [patientTabs(patientId, tab)]);
    hcMain(el("p", tab === "sheet" ? "訪問時の情報を読み込めませんでした。「更新」で読み直してください。" : "台帳を読み込めませんでした。「更新」で読み直してください。", "empty state-note"));
  });
}
const patientTabs = (patientId, tab) => tabs([["sheet", "訪問時の情報"], ["edit", "台帳を編集"]], tab, key => {if (key !== tab) showHcPatient(patientId, key);});
function patientHeading(patient, tab) {
  const years = patient.birth_date ? age(patient.birth_date) : null;
  const meta = [patient.kana || "フリガナ未登録", patient.birth_date && `${patient.birth_date}生${years == null ? "" : `（${years}歳）`}`].filter(Boolean).join(" · ");
  const chips = patient.status !== "active" && PATIENT_STATUS[patient.status] ? [chip(PATIENT_STATUS[patient.status], "muted")] : [];
  shell.heading(patient.name, meta, chips, [patientTabs(patient.patient_id, tab)]);
}
// Draws one patient within the screen turn `current`; a failed read throws so the caller decides how to say so.
async function drawPatient(patientId, tab, current) {
  const data = await api(tab === "sheet" ? "hc/sheet" : "hc/patient", {patient: patientId});
  if (!current()) return;
  patientHeading(data.patient, tab);
  hcMain(tab === "sheet" ? sheetView(data, current) : registerView(patientId, data, current));
}
function newPatient(current) {
  shell.heading("新しい患者", "患者台帳");
  hcMain(card("患者の登録", [recordForm("patient", PATIENT_FORM, null, async saved => {
    if (!current()) return;
    hc.patient = saved.patient_id; await loadHcPatients();
    if (current()) await drawPatient(saved.patient_id, "edit", current);
  }, {groups: PATIENT_GROUPS})], {wide: true}));
}

// ---------- 訪問時の情報 ----------
function contactPairs(patient) {
  const preferred = patient.preferred_contact;
  const keyperson = patient.keyperson_name || patient.keyperson_phone
    ? withPhone(personText(patient.keyperson_name, patient.keyperson_relation) || "キーパーソン（氏名未登録）", patient.keyperson_phone) : null;
  // The preferred contact is the entry it names, never another entry's number: 施設・その他 alone use contact_phone.
  const reach = preferred === "patient" ? withPhone("本人", patient.phone)
    : preferred === "keyperson" ? keyperson || el("span", "キーパーソン 未登録", "hc-quiet")
    : CONTACTS[preferred] ? withPhone(CONTACTS[preferred], patient.contact_phone) : null;
  const place = [patient.postal_code && `〒${patient.postal_code}`, patient.address].filter(Boolean).join(" ");
  return [["住所", [place, patient.residence].filter(Boolean).join("\n")], ["優先連絡先", reach],
    ["本人電話", preferred !== "patient" && patient.phone ? telLink(patient.phone) : null],
    ["キーパーソン", preferred !== "keyperson" ? keyperson : null],
    ["連絡しやすい時間", patient.contact_note], ["アクセス", patient.access_note]];
}
function visitItem(visit, big = false) {
  const item = el("div", null, big ? "sheet-visit sheet-visit-next" : "sheet-visit"), head = el("div", null, "sheet-visit-head");
  head.append(el("span", big ? longDay(visit.visit_date) : dayLabel(visit.visit_date), "sheet-visit-date"));
  if (visit.start_time) head.append(el("span", visit.start_time, "sheet-visit-time"));
  head.append(visitChip(visit));
  item.append(head, el("p", [kindLabel(visit.kind), visit.staff_name || "職員未定"].join(" · "), "hc-quiet"));
  if (visit.note) item.append(el("p", visit.note, "hc-note"));
  return item;
}
function sheetView(data, current) {
  const patient = data.patient, grid = el("div", null, "cards hc-sheet");
  if (patient.caution_note) grid.append(card("注意事項", [el("p", patient.caution_note, "hc-caution")], {wide: true, tone: "warn"}));
  const contact = contactPairs(patient);
  grid.append(card("連絡と訪問先", [contact.some(([, value]) => value) ? kv(contact) : el("p", "連絡先・訪問先は台帳に登録されていません。", "hc-quiet")]));
  const visits = [el("h4", "次の予定")];
  data.next_visits.forEach((visit, index) => visits.push(visitItem(visit, index === 0)));
  if (!data.next_visits.length) visits.push(el("p", "登録されている予定はありません。", "hc-quiet"));
  visits.push(el("h4", "最近の実施"));
  for (const visit of data.recent_visits) visits.push(visitItem(visit));
  if (!data.recent_visits.length) visits.push(el("p", "実施の記録はまだありません。", "hc-quiet"));
  grid.append(card("訪問", visits));
  grid.append(card("ケアチーム", [data.team.length ? kv(data.team.map(member => [roleLabel(member.role), member.staff_name])) : el("p", "担当者はまだ登録されていません。", "hc-quiet")]));
  for (const [name, part] of Object.entries(data.modules)) if (hc.modules[name]?.sheet) grid.append(hc.modules[name].sheet(part));
  grid.append(mcsCard(data.mcs, current));
  return grid;
}
function mcsCard(link, current) {
  if (!link.project_id) return card("MCS のやりとり", [el("p", "MCS の患者と対応付けていないか、閲覧範囲の外です。", "hc-quiet")], {wide: true});
  const open = el("button", "タイムラインを開く", "button"); open.type = "button"; open.onclick = () => openMcs(link.project_id).catch(fail);
  const brief = el("div", null, "sheet-brief"); brief.append(el("p", "MCS の記録を読み込んでいます。", "state-note"));
  sheetBrief(brief, link, current);
  return card(`MCS のやりとり · ${link.patient_name}`, [brief], {wide: true, actions: [open]});
}
async function sheetBrief(box, link, current) {
  // The MCS views as of the published snapshot; 前回から変わったこと counts from the last visit that took place.
  const data = await api("brief", {project: link.project_id, since: link.since}).catch(() => null);
  if (!current() || !box.isConnected) return;
  if (!data) {box.replaceChildren(el("p", "MCS の記録を読み込めませんでした。タイムラインから確認してください。", "state-note")); return;}
  const grid = el("div", null, "brief-grid");
  box.replaceChildren(el("p", data.since ? "「前回から変わったこと」は最後に実施した訪問からの変化です。" : "実施済みの訪問がないため、「前回から変わったこと」は直近7日の変化です。", "hc-quiet"), grid);
  // Posts open from the timeline: the sheet does not carry the chat's selected patient.
  const unlinked = view => ({...view, items: view.items.map(item => ({...item, message_id: null}))});
  for (const section of data.sections) {
    // Later pages come from the same snapshot route, counted from the same last visit.
    const show = (target, view) => target.replaceChildren(renderPanel(unlinked(view), async page => {
      const next = await api("panel", {project: link.project_id, section: section.key, page, since: data.since}).catch(error => {if (current()) fail(error); return null;});
      if (next && current() && target.isConnected) show(target, {...next.panel, key: section.key});
    }));
    const target = el("div", null, "brief-item"); show(target, section); grid.append(target);
  }
}

// ---------- 台帳を編集 ----------
function registerView(patientId, data, current) {
  // Saving the basic form refreshes only the heading and the list, so open visit or prescription forms keep their input.
  const basic = card("基本情報", [recordForm("patient", PATIENT_FORM, data.patient, async saved => {
    if (!current()) return;
    await loadHcPatients();
    if (current()) patientHeading(saved, "edit");
  }, {groups: PATIENT_GROUPS})], {wide: true});
  // Team and link actions redraw only their own sections, so unsaved edits in the record form stay.
  const teamBody = el("div"), linkBody = el("div");
  const refreshTeam = async () => {
    const fresh = await api("hc/patient", {patient: patientId});
    if (current()) careTeam(teamBody, patientId, fresh.team, refreshTeam, current);
  };
  const refreshLink = async () => {
    const fresh = await api("hc/patient", {patient: patientId});
    if (current()) mcsLink(linkBody, patientId, fresh, refreshLink, current);
  };
  careTeam(teamBody, patientId, data.team, refreshTeam, current); mcsLink(linkBody, patientId, data, refreshLink, current);
  // A saved visit can change the patient's next visit, so the list's 「次 …」 chip is re-read too (it redraws only the list).
  const withList = reload => async () => {await reload(); if (current()) await loadHcPatients();};
  const visits = registerSection("訪問", (box, reload) => patientVisits(box, patientId, withList(reload), current),
    {label: "＋ 訪問を予定する", form: reload => recordForm("visit", visitForm(), null, withList(reload), {fixed: {patient_id: patientId}})});
  const enabled = name => hc.meta.modules.some(module => module.name === name);
  const parts = Object.entries(hc.modules).filter(([name, view]) => view.patient && enabled(name)).map(([, view]) => view.patient(patientId, current));
  const sections = [basic, card("ケアチーム（この事業所の担当）", [teamBody], {wide: true}), visits, ...parts, card("MCS の患者", [linkBody], {wide: true})];
  const jumps = el("nav", null, "hc-jumps"); jumps.setAttribute("aria-label", "台帳の節");
  for (const section of sections) {
    const go = el("button", section.querySelector("h3").textContent.replace(/（.*）$/, ""), "chip"); go.type = "button";
    go.onclick = () => section.scrollIntoView({block: "start", behavior: "smooth"});
    jumps.append(go);
  }
  const page = el("div", null, "hc-register"); page.append(jumps, ...sections);
  return page;
}
async function patientVisits(box, patientId, reload, current) {
  const data = await api("hc/visits", {patient: patientId}).catch(error => {if (box.childElementCount) throw error; return null;});
  if (!current()) return;
  if (!data) {box.replaceChildren(el("p", "訪問の記録を読み込めませんでした。", "state-note")); return;}
  const {table, body} = rowTable(["訪問日", "種類", "状態", "職員", "記録・申し送り"]);
  const visitRow = visit => expandRow([visitWhen(visit), kindLabel(visit.kind), visitChip(visit), visit.staff_name || "未定", clipped(visit.note)],
    () => recordForm("visit", visitForm(), visit, reload), 5);
  if (!data.items.length) body.append(emptyRow("訪問の記録はまだありません。", 5));
  box.replaceChildren(table, historyPages(body, data, visitRow, offset => api("hc/visits", {patient: patientId, offset}), "以前の訪問を表示", current));
}
function mcsLink(link, patientId, data, refresh, current) {
  // The pressed button stays disabled while its write is in flight, so a double tap sends once.
  const relink = async (op, project, button) => {
    if (!Number.isInteger(project) || project <= 0) return;
    button.disabled = true;
    try {await hcPost(op, {patient: patientId, project});} catch (error) {fail(error); button.disabled = false; return;}
    await refreshAfterWrite(async () => {if (!current()) return; await loadHcPatients(); await refresh();});
    button.disabled = false;
  };
  link.replaceChildren();
  if (data.mcs.project_id) {
    link.append(el("p", `${data.mcs.patient_name || "閲覧範囲外の患者"}（MCS ID ${data.mcs.project_id}）と対応付けています。`));
    const tools = el("div", null, "hc-inline");
    if (data.mcs.patient_name) {const open = el("button", "MCS のタイムラインを開く", "button"); open.onclick = () => openMcs(data.mcs.project_id).catch(fail); tools.append(open);}
    const undo = el("button", "対応付けを解除", "text-button");
    undo.onclick = () => relink("unlink", data.mcs.project_id, undo);
    tools.append(undo); link.append(tools);
  } else {
    link.append(el("p", "まだ MCS の患者と対応付けていません。氏名が同じでも自動では対応付けません。", "hc-quiet"));
    for (const candidate of data.candidates) {
      const pick = el("button", `${candidate.patient_name}（MCS ID ${candidate.project_id}）と対応付ける`, "text-button");
      pick.onclick = () => relink("link", candidate.project_id, pick);
      link.append(pick);
    }
    const manual = el("form", null, "hc-inline"), input = el("input"); input.placeholder = "MCS の患者ID"; input.inputMode = "numeric";
    input.setAttribute("aria-label", "MCS の患者ID");
    const submit = el("button", "この ID と対応付ける", "button");
    manual.append(input, submit);
    manual.onsubmit = event => {event.preventDefault(); relink("link", Number(input.value.trim()), submit);};
    link.append(manual);
  }
}
function careTeam(box, patientId, members, reload, current) {
  const refresh = () => refreshAfterWrite(async () => {if (current()) await reload();});
  box.replaceChildren();
  const list = el("div", null, "hc-team");
  for (const member of members) {
    const row = el("div", null, "hc-team-row"), stop = el("button", "担当を外す", "text-button");
    row.append(el("span", roleLabel(member.role), "hc-quiet"), el("b", member.staff_name), stop);
    stop.onclick = async () => {
      stop.disabled = true;
      try {await hcPost("team.end", {assignment: member.assignment_id});} catch (error) {fail(error); stop.disabled = false; return;}
      await refresh(); stop.disabled = false;
    };
    list.append(row);
  }
  if (!members.length) list.append(el("p", "担当者はまだ登録されていません。", "hc-quiet"));
  box.append(list);
  const roles = Object.entries(hc.meta.roles);
  if (!roles.length) return;
  const form = el("form", null, "hc-inline"), role = el("select"), person = el("select");
  role.setAttribute("aria-label", "担当の役割"); person.setAttribute("aria-label", "担当にする職員");
  for (const [code, spec] of roles) {const option = el("option", spec.label); option.value = code; role.append(option);}
  const assign = el("button", "担当にする", "button");
  const people = () => {
    const needs = hc.meta.roles[role.value].requires;
    person.replaceChildren(...hc.staff.filter(s => s.active && s.qualifications.some(q => needs.includes(q))).map(s => {const option = el("option", s.name); option.value = String(s.staff_id); return option;}));
    // With nobody qualified for the role there is no one to assign.
    if (!person.options.length) {const none = el("option", "資格のある在籍職員がいません"); none.value = ""; person.append(none);}
    assign.disabled = !person.value;
  };
  role.onchange = people; people();
  form.append(role, person, assign);
  form.onsubmit = async event => {
    event.preventDefault();
    if (!person.value || assign.disabled) return;
    assign.disabled = true;
    try {const assignments = members.filter(m => m.role === role.value).map(m => m.assignment_id); await hcPost("team.assign", {patient: patientId, staff: Number(person.value), role: role.value, assignments});}
    catch (error) {fail(error); assign.disabled = false; if (error.code === "team_changed") await refreshAfterWrite(async () => {if (current()) await reload();}, false); return;}
    await refresh(); assign.disabled = !person.value;
  };
  box.append(form);
}

// ---------- 訪問予定 ----------
async function showDay(date) {
  const current = hcScreen(); hc.patient = null; hc.day = date; closeDetail(); markPatient(null);
  shell.heading("訪問予定", longDay(date), date === siteToday() ? [chip("今日", "info")] : []);
  hcMain(dayNav(date), el("p", "読込中…", "empty"));
  const [data, week] = await Promise.all([api("hc/visits", {date}).catch(() => null), api("hc/days", {from: date}).catch(() => null)]);
  if (!current()) return;
  hcMain(dayNav(date), weekStrip(date, week), dayBoard(date, data));
}
function dayNav(date) {
  const bar = el("div", null, "day-nav"), step = (label, target, cls) => {const button = el("button", label, `button ${cls}`); button.type = "button"; button.onclick = () => showDay(target); return button;};
  const input = el("input"); input.type = "date"; input.value = date; input.setAttribute("aria-label", "表示する日");
  input.onchange = () => {if (input.value && input.value !== date) showDay(input.value);};
  bar.append(step("◀ 前日", shiftDay(date, -1), "day-prev"), step("今日", siteToday(), "day-today"), step("翌日 ▶", shiftDay(date, 1), "day-next"), input);
  return bar;
}
function weekStrip(date, week) {
  if (!week) return el("p", "7日分の件数を読み込めませんでした。", "state-note");
  const strip = el("div", null, "week-strip"), today = siteToday();
  strip.setAttribute("aria-label", "7日分の訪問");
  for (const day of week.days) {
    const button = el("button", null, "week-day" + (day.date === today ? " today" : "")); button.type = "button";
    button.setAttribute("aria-current", day.date === date ? "date" : "false");
    // Each count carries its word (予定) and, for the compact phone strip, its first letter (予); zero stays muted.
    const count = (word, n, tone) => {
      const node = el("span", null, n ? tone : "week-zero");
      node.append(el("span", word, "week-long"), el("span", word[0], "week-abbr"), el("b", String(n)));
      return node;
    };
    const counts = el("span", null, "week-counts"), when = dayOf(day.date), compact = el("span", null, "week-compact");
    counts.append(count("予定", day.planned, "week-planned"), count("実施", day.done, "week-done"));
    if (day.cancelled) counts.append(count("中止", day.cancelled, "week-cancelled"));
    compact.append(el("span", `${when.getUTCMonth() + 1}/${when.getUTCDate()}`, "week-md"), el("span", WEEKDAYS[when.getUTCDay()], "week-wd"));
    button.setAttribute("aria-label", `${longDay(day.date)}${day.date === today ? "（今日）" : ""} 予定${day.planned}件 実施${day.done}件${day.cancelled ? ` 中止${day.cancelled}件` : ""}`);
    button.append(el("span", dayLabel(day.date), "week-date"), compact, counts);
    button.onclick = () => {if (day.date !== date) showDay(day.date);};
    strip.append(button);
  }
  return strip;
}
function visitCard(visit) {
  const box = el("button", null, `visit-card visit-${visit.status}`); box.type = "button";
  const body = el("span", null, "visit-body"), top = el("span", null, "visit-top");
  top.append(el("b", visit.patient_name, "visit-name"), el("span", kindLabel(visit.kind), "visit-kind"), visitChip(visit));
  body.append(top, el("span", `担当 ${visit.staff_name || "未定"}`, "visit-line"));
  const place = [visit.address, visit.residence].filter(Boolean).join("　");
  if (place) body.append(el("span", place, "visit-line visit-place"));
  if (visit.caution_note) body.append(el("span", `⚠ ${visit.caution_note.split("\n")[0]}`, "visit-caution"));
  if (visit.note) body.append(el("span", visit.note.split("\n")[0], "visit-note"));
  box.append(el("span", visit.start_time || "時刻未定", visit.start_time ? "visit-time" : "visit-time visit-time-none"), body);
  box.onclick = () => openPatient(visit.patient_id);
  return box;
}
function dayBoard(date, data) {
  const board = el("div", null, "day-board"), list = el("section", null, "day-visits"), route = routeCard(date);
  list.setAttribute("aria-label", "この日の訪問");
  board.append(list, route.card);
  if (!data) {list.append(el("p", "この日の訪問を読み込めませんでした。", "state-note")); return board;}
  // Filtering by staff only narrows what is shown; the counts follow the filter.
  let who = "all";
  const staff = new Map(data.items.map(visit => [visit.staff_id ?? "none", visit.staff_name || "未定"]));
  const draw = () => {
    const shown = data.items.filter(visit => who === "all" || (visit.staff_id ?? "none") === who);
    const bar = el("div", null, "day-filters"), counts = el("span", null, "chips"), people = el("span", null, "chips day-staff");
    // 予定 and 実施 always show their count; 中止 only when there is one.
    for (const status of Object.keys(VISIT_STATUS)) {
      const count = shown.filter(visit => visit.status === status).length;
      if (count || status !== "cancelled") counts.append(chip(`${VISIT_STATUS[status]} ${count}`, status));
    }
    if (staff.size > 1) for (const [key, name] of [["all", "全員"], ...staff]) {
      const pick = el("button", name, "chip chip-filter"); pick.type = "button"; pick.setAttribute("aria-pressed", String(key === who));
      pick.onclick = () => {who = key; route.pick(key); draw();};
      people.append(pick);
    }
    if (data.items.length) bar.append(counts, people);
    const cards = shown.map(visitCard);
    list.replaceChildren(bar, ...(cards.length ? cards : [el("p", data.items.length ? "この職員の訪問はこの日に登録されていません。" : "この日の訪問は登録されていません。", "empty")]));
  };
  draw();
  return board;
}
function routeCard(date) {
  if (!hc.routes) return {card: card("訪問ルート", [el("p", "訪問ルートは設定されていません。設定すると、住所だけを地図サービスへ送って回る順番を作ります。", "hc-quiet")]), pick() {}};
  const form = el("form", null, "hc-inline route-form"), staffPick = field(["staff", "職員", "staff"], null, "route"), result = el("div", null, "route-result"), hint = el("p", null, "hc-quiet");
  hint.hidden = true;
  form.append(staffPick, el("button", "ルートを作る（住所を地図サービスへ送ります）", "button button-primary"));
  form.onsubmit = async event => {
    event.preventDefault(); result.replaceChildren(el("p", "作成中…", "hc-quiet"));
    try {
      const staff = form.querySelector('[name="staff"]').value, plan = await hcPost("route.plan", {date, staff: staff ? Number(staff) : null});
      result.replaceChildren();
      if (plan.stops.length) {
        const stops = el("ol", null, "route-list");
        plan.stops.forEach((stop, index) => {
          const item = el("li"), who = el("span", null, "route-stop");
          who.append(el("b", stop.patient_name));
          if (stop.start_time) who.append(el("small", `予定 ${stop.start_time}`));
          item.append(el("span", String(index + 1), "route-num"), el("time", stop.arrival, "route-time"), who);
          stops.append(item);
        });
        result.append(el("p", "左の時刻は訪問開始の見込みです。", "hc-quiet"), stops);
      }
      if (!plan.stops.length) result.append(el("p", "ルートに入れられる予定の訪問がありません。"));
      if (plan.unscheduled.length) result.append(el("p", `予定の時刻の前後15分に着けない訪問が${plan.unscheduled.length}件あり、ルートに入れていません。時刻か担当を見直してください。`, "state-note"));
      if (plan.unlocated.length) result.append(el("p", `住所から場所を1か所に決められなかった訪問が${plan.unlocated.length}件あり、ルートに入れていません。台帳の住所を番地まで確かめてください。`, "state-note"));
    } catch (error) {result.replaceChildren(el("p", error.message, "state-note"));}
  };
  const select = staffPick.querySelector("select");
  select.options[0].textContent = "全員";  // no staff filter on a route means every visit of the day, not 未定
  // A route belongs to the staff it was built for: any change of staff clears the shown result.
  select.addEventListener("change", () => result.replaceChildren());
  return {card: card("訪問ルート", [form, hint, result]), pick(key) {
    // The day's staff filter carries over: 全員 → every visit, a staff member → that person; 未定 has no route of its own.
    hint.hidden = key !== "none";
    hint.textContent = key === "none" ? "担当未定の訪問だけのルートは作れません。ルートの職員は変えていません。" : "";
    if (key === "none") return;
    const value = key === "all" ? "" : String(key);
    if (![...select.options].some(option => option.value === value) || select.value === value) return;
    select.value = value; result.replaceChildren();
  }};
}

// ---------- Modes ----------
async function openMcs(projectId) {
  // The way back from a visit sheet always opens the timeline, not whichever view was last chosen.
  $("patient-search").value = ""; state.panel = "";
  await setMode("mcs");
  const patient = state.patients.find(row => row.project_id === projectId)
    || (await api("patients", {q: String(projectId)})).items.find(row => row.project_id === projectId);
  if (patient) selectPatient(patient);
}
// Opening a patient from elsewhere (the day board, the MCS heading) moves to the register with that patient selected.
function openPatient(patientId) {
  setMode("pharmacy");
  return showHcPatient(patientId);
}
// The selected mode is shown by style and exposed to assistive technology.
function markMode(mode) {
  for (const button of document.querySelectorAll("#hc-modes button")) {
    const picked = button.dataset.mode === mode;
    button.classList.toggle("selected", picked); button.setAttribute("aria-pressed", String(picked));
  }
}
function setMode(mode) {
  hc.mode = mode; hc.patient = null; ++state.patientRequest; ++hc.request; hcScreen(); state.project = null; closeDetail();
  markMode(mode);
  shell.setMode(mode);
  if (mode === "mcs") {
    shell.heading("患者を選んでください");
    ++state.contextRequest; ++state.mainRequest; $("patient-context").replaceChildren(); $("older").hidden = true;
    $("timeline").replaceChildren(); $("panel-view").replaceChildren();
    $("timeline-status").textContent = "患者を選ぶと投稿を表示します。";
    $("panel-view").hidden = !state.panel; $("timeline").hidden = false; $("timeline-status").hidden = false;
    return loadPatients();
  }
  const listed = loadHcPatients().catch(fail);
  if (mode === "day") showDay(hc.day || siteToday());
  else if (mode === "staff") showStaff();
  else {
    shell.heading("患者台帳", "一覧から患者を選ぶか、登録してください");
    const add = el("button", "＋ 患者を登録", "button button-primary"); add.type = "button"; add.onclick = () => showHcPatient(null);
    const open = el("button", "患者の一覧を開く", "button hc-phone-only"); open.type = "button"; open.onclick = () => shell.openList(true);
    const hint = el("div", null, "hc-start"); hint.append(el("p", "患者の一覧から選ぶと、訪問時の情報を表示します。", "hc-quiet"), open, add);
    hcMain(hint);
  }
  return listed;
}
async function showLinkedPharmacyPatient(projectId) {
  if (!hc.enabled) return;
  // Only the latest selection draws its chip, even when the same patient is selected twice in a row.
  const request = ++hc.linkRequest;
  try {
    const data = await api("hc/linked", {project: projectId});
    if (request !== hc.linkRequest || !data.patient || state.project !== projectId) return;
    const link = el("button", `台帳: ${data.patient.name}`, "chip chip-info hc-linked"); link.type = "button";
    link.title = "患者台帳で開く"; link.onclick = () => openPatient(data.patient.patient_id);
    $("patient-chips").querySelector(".hc-linked")?.remove();
    $("patient-chips").append(link);
  } catch (error) {if (request === hc.linkRequest) fail(error);}
}
async function initHomecare() {
  try {
    const session = await api("session");
    hc.csrf = session.csrf; hc.enabled = session.homecare; hc.routes = session.routes; hc.resident = session.resident; hc.user = session.user; hc.login = session.login;
    if (!hc.enabled) return;
    // The toolbar appears only once the modules and staff it relies on have loaded.
    hc.meta = await api("hc/meta");
    await loadStaff();
    $("hc-tools").hidden = false; markMode(hc.mode);
    $("hc-actor").onchange = () => {hc.actor = $("hc-actor").value ? Number($("hc-actor").value) : null;};
    for (const button of document.querySelectorAll("#hc-modes button")) button.onclick = () => setMode(button.dataset.mode);
    state.onSelect = showLinkedPharmacyPatient;
    state.onSearch = () => (hc.mode === "mcs" ? loadPatients() : loadHcPatients().catch(fail));
    if (!hc.staff.length) setMode("staff");
  } catch (error) {fail(error);}
}
initHomecare();
