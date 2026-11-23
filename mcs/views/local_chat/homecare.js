"use strict";
// Home-care core screens (staff, patient register, care team, MCS links). Shares $, el, api, fail and state with app.js.
// Occupation modules add their own screens; the core only reads their declared qualifications and roles from api/hc/meta.
const hc = {enabled: false, routes: false, csrf: "", actor: null, resident: false, user: null, meta: {qualifications: {}, roles: {}, visit_kinds: {}}, staff: [], patients: [], patient: null, mode: "mcs", request: 0, view: 0,
  // Occupation modules register their own patient sections and visit-sheet parts here (see pharmacy.js).
  modules: {}};
const VISIT_STATUS = {planned: "予定", done: "実施", cancelled: "中止"};
const visitForm = () => [["visit_date", "訪問日（必須）", "date"], ["start_time", "開始時刻", "time"],
  ["kind", "種類", "select", hc.meta.visit_kinds], ["staff_id", "訪問する職員", "staff"],
  ["status", "状態", "select", VISIT_STATUS], ["note", "記録・申し送り", "textarea"]];
const PATIENT_FORM = [
  ["name", "氏名（必須）"], ["kana", "フリガナ"], ["birth_date", "生年月日", "date"], ["postal_code", "郵便番号"],
  ["address", "住所"], ["residence", "施設・部屋"], ["phone", "本人電話", "tel"],
  ["keyperson_name", "キーパーソン氏名"], ["keyperson_relation", "続柄"], ["keyperson_phone", "キーパーソン電話", "tel"],
  ["preferred_contact", "優先連絡先", "select", {patient: "本人", keyperson: "キーパーソン", facility: "施設", other: "その他"}],
  ["contact_phone", "優先連絡先の電話（施設・その他）", "tel"], ["contact_note", "連絡しやすい時間など"],
  ["access_note", "訪問先へのアクセス", "textarea"], ["caution_note", "注意事項", "textarea"],
  ["status", "利用状態", "select", {active: "利用中", suspended: "休止", ended: "終了"}]];
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
const qualificationLabel = code => hc.meta.qualifications[code] || code;
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
  else if (type === "checkbox") {input = el("input"); input.type = "checkbox"; input.checked = value == null ? option : Boolean(value);}
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
function recordForm(kind, spec, record, onSaved, fixed = {}) {
  const form = el("form", null, "hc-form"), message = el("p", null, "state-note");
  for (const item of spec) form.append(field(item, record?.[item[0]], kind));
  const save = el("button", record ? "保存" : "登録", "button");
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
// Each screen takes a turn when it starts; a screen whose answer comes back after another one started draws nothing.
function hcScreen() {const turn = ++hc.view; return () => turn === hc.view;}
function hcMain(title, ...nodes) {
  // On a phone the patient list folds away once a screen opens; 患者一覧 brings it back.
  document.body.classList.add("picked");
  ++state.mainRequest;
  $("panel-view").hidden = false;
  for (const id of ["coverage", "older", "timeline", "timeline-status"]) $(id).hidden = true;
  const box = el("div", null, "panel"); box.append(el("h2", title), ...nodes);
  $("panel-view").replaceChildren(box);
}
async function showStaff() {
  hcScreen(); closeDetail();
  const list = el("div");
  for (const person of hc.staff) {
    const row = el("details", null, "hc-row");
    row.append(el("summary", `${person.name}${person.active ? "" : "（退職）"} · ${person.qualifications.map(qualificationLabel).join("・")}${person.admin ? " · 管理者" : ""}`));
    row.append(recordForm("staff", STAFF_FORM, person, async () => {await loadStaff(); showStaff();}));
    list.append(row);
  }
  const added = el("details", null, "hc-row"); added.append(el("summary", "職員を追加"), recordForm("staff", STAFF_FORM, null, async saved => {if (hc.resident && !hc.user) {location.reload(); return;} if (!hc.actor) hc.actor = saved.staff_id; await loadStaff(); showStaff();}));
  hcMain("職員", ...(hc.staff.length ? [] : [el("p", hc.resident ? `最初に自分を職員として登録してください。今の Tailscale のログイン（${hc.login}）がこの人に結び付き、管理者になります。` : "最初に自分を職員として登録してください。登録した人がそのまま操作者と管理者になります。", "state-note")]), list, added);
}
async function loadHcPatients() {
  const request = ++hc.request;
  const data = await api("hc/patients", {q: $("patient-search").value.trim()});
  if (request !== hc.request) return;
  hc.patients = data.items;
  $("patients").replaceChildren();
  for (const patient of hc.patients) {
    const button = el("button", null, "patient" + (patient.patient_id === hc.patient ? " selected" : ""));
    const label = el("span", patient.name, "patient-label");
    const notes = [patient.kana, patient.project_id && "MCS あり", {suspended: "休止", ended: "終了"}[patient.status]];
    label.append(el("small", notes.filter(Boolean).join(" · ")));
    button.append(el("span", "●", "hash"), label);
    button.dataset.patient = String(patient.patient_id);
    button.onclick = () => showHcPatient(patient.patient_id);
    $("patients").append(button);
  }
  const add = el("button", "＋ 患者を登録", "sidebar-more"); add.onclick = () => showHcPatient(null);
  $("patients").append(add);
  $("patient-count").textContent = String(hc.patients.length);
  $("more-patients").hidden = true;
}
async function showHcPatient(patientId) {
  const current = hcScreen(); hc.patient = patientId; closeDetail();
  for (const button of $("patients").querySelectorAll(".patient")) button.classList.toggle("selected", button.dataset.patient === String(patientId));
  if (patientId == null) {
    $("patient-name").textContent = "新しい患者"; $("patient-meta").textContent = "薬局の患者台帳";
    hcMain("患者の登録", recordForm("patient", PATIENT_FORM, null, async saved => {await loadHcPatients(); await showHcPatient(saved.patient_id);}));
    return;
  }
  const data = await api("hc/patient", {patient: patientId});
  if (!current()) return;
  const patient = data.patient;
  $("patient-name").textContent = patient.name;
  $("patient-meta").textContent = `薬局の患者台帳 · ${patient.kana || "フリガナ未登録"}`;
  const team = el("section", null, "detail-section"), link = el("section", null, "detail-section");
  // Team and link actions redraw only their own sections, so unsaved edits in the record form stay.
  const refreshTeam = async () => {
    const fresh = await api("hc/patient", {patient: patientId});
    if (hc.patient === patientId) careTeam(team, patientId, fresh.team, refreshTeam);
  };
  const refreshLink = async () => {
    const fresh = await api("hc/patient", {patient: patientId});
    if (hc.patient === patientId) mcsLink(link, patientId, fresh, refreshLink);
  };
  careTeam(team, patientId, data.team, refreshTeam); mcsLink(link, patientId, data, refreshLink);
  const sheet = el("button", "訪問時の情報を開く", "button"); sheet.onclick = () => showSheet(patientId);
  const visitBox = el("section", null, "detail-section"); patientVisits(visitBox, patientId);
  const enabled = name => hc.meta.modules.some(module => module.name === name);
  const parts = Object.entries(hc.modules).filter(([name, view]) => view.patient && enabled(name)).map(([, view]) => {const box = el("section", null, "detail-section"); view.patient(box, patientId); return box;});
  hcMain("基本情報", sheet, recordForm("patient", PATIENT_FORM, patient, async () => {await loadHcPatients(); await showHcPatient(patientId);}), team, visitBox, ...parts, link);
}
const visitLine = visit => `${visit.visit_date}${visit.start_time ? " " + visit.start_time : ""} · ${hc.meta.visit_kinds[visit.kind] || visit.kind} · ${VISIT_STATUS[visit.status]}${visit.staff_name ? " · " + visit.staff_name : ""}`;
async function patientVisits(box, patientId) {
  const reload = () => patientVisits(box, patientId);
  const data = await api("hc/visits", {patient: patientId}).catch(error => {if (box.childElementCount) throw error; fail(error); return null;});
  if (hc.patient !== patientId) return;
  box.replaceChildren(el("h3", "訪問"));
  if (!data) {box.append(el("p", "訪問の記録を読み込めませんでした。", "state-note")); return;}
  const visitRow = visit => {
    const row = el("details", null, "hc-row"); row.append(el("summary", visitLine(visit)));
    if (visit.note) row.append(el("p", visit.note, "hc-note"));
    row.append(recordForm("visit", visitForm(), visit, reload)); return row;
  };
  const list = el("div"); box.append(list);
  if (!data.items.length) list.append(el("p", "訪問の記録はまだありません。"));
  box.append(historyPages(list, data, visitRow, offset => api("hc/visits", {patient: patientId, offset}), "以前の訪問を表示", () => hc.patient === patientId));
  const add = el("details", null, "hc-row"); add.append(el("summary", "訪問を予定する"), recordForm("visit", visitForm(), null, reload, {patient_id: patientId}));
  box.append(add);
}
function historyPages(list, first, render, fetchPage, label, current) {
  // Older entries load a page at a time below the ones shown; the place they go is kept apart from the form below.
  const more = el("div"); let shown = 0;
  const append = page => {
    for (const item of page.items) list.append(render(item));
    shown += page.items.length; more.replaceChildren();
    if (!page.more) return;
    const button = el("button", label, "load-more"); button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      const next = await fetchPage(shown).catch(error => {fail(error); return null;});
      if (!current() || !more.isConnected) return;
      if (next) append(next); else {button.disabled = false; more.append(el("p", "以前の記録を読み込めませんでした。", "state-note"));}
    };
    more.append(button);
  };
  append(first);
  return more;
}
async function showSheet(patientId) {
  const current = hcScreen(); hc.patient = patientId; closeDetail();
  const data = await api("hc/sheet", {patient: patientId});
  if (!current()) return;
  const patient = data.patient, back = el("button", "台帳に戻る", "text-button"); back.onclick = () => showHcPatient(patientId);
  $("patient-name").textContent = patient.name; $("patient-meta").textContent = "訪問時の情報";
  const facts = el("section", null, "detail-section"); facts.append(el("h3", "連絡と訪問先"));
  const contact = {patient: patient.phone, keyperson: `${patient.keyperson_name}（${patient.keyperson_relation}） ${patient.keyperson_phone}`}[patient.preferred_contact] ?? patient.contact_phone;
  for (const [label, value] of [["住所", [patient.address, patient.residence].filter(Boolean).join(" ")], ["優先連絡先", contact], ["連絡のしやすい時間", patient.contact_note],
                                ["訪問先へのアクセス", patient.access_note], ["注意事項", patient.caution_note]]) if (value) facts.append(el("p", `${label}: ${value}`));
  const team = el("section", null, "detail-section"); team.append(el("h3", "ケアチーム"));
  for (const member of data.team) team.append(el("p", `${hc.meta.roles[member.role]?.label || member.role}: ${member.staff_name}`));
  if (!data.team.length) team.append(el("p", "担当者はまだいません。"));
  const visitBox = el("section", null, "detail-section"); visitBox.append(el("h3", "訪問"));
  for (const [label, rows] of [["次の予定", data.next_visits], ["最近の実施", data.recent_visits]]) {
    visitBox.append(el("h4", label));
    for (const visit of rows) {visitBox.append(el("p", visitLine(visit))); if (visit.note) visitBox.append(el("p", visit.note, "hc-note"));}
    if (!rows.length) visitBox.append(el("p", "ありません。"));
  }
  const parts = Object.entries(data.modules).filter(([name]) => hc.modules[name]?.sheet).map(([name, part]) => {const box = el("section", null, "detail-section"); hc.modules[name].sheet(box, part); return box;});
  const mcs = el("section", null, "detail-section"); mcs.append(el("h3", "MCS のやりとり"));
  if (data.mcs.project_id) {
    const open = el("button", `${data.mcs.patient_name} のタイムラインを開く`, "text-button"); open.onclick = () => openMcs(data.mcs.project_id);
    const brief = el("div", null, "sheet-brief"); brief.append(el("p", "MCS の記録を読み込んでいます。", "state-note"));
    mcs.append(open, brief); sheetBrief(brief, patientId, data.mcs);
  } else mcs.append(el("p", "MCS の患者と対応付けていないか、閲覧範囲の外です。"));
  hcMain("訪問時の情報", back, facts, team, visitBox, ...parts, mcs);
}
async function sheetBrief(box, patientId, link) {
  // The MCS views as of the published snapshot; 前回から変わったこと counts from the last visit that took place.
  const data = await api("brief", {project: link.project_id, since: link.since}).catch(() => null);
  if (hc.patient !== patientId || !box.isConnected) return;
  if (!data) {box.replaceChildren(el("p", "MCS の記録を読み込めませんでした。タイムラインから確認してください。", "state-note")); return;}
  box.replaceChildren(el("p", data.since ? "「前回から変わったこと」は最後に実施した訪問からの変化です。" : "実施済みの訪問がないため、「前回から変わったこと」は直近7日の変化です。", "state-note"));
  // Posts open from the timeline: the sheet does not carry the chat's selected patient.
  for (const section of data.sections) box.append(renderPanel({...section, items: section.items.map(item => ({...item, message_id: null}))}));
}
async function showDay(date) {
  const current = hcScreen(); hc.patient = null; closeDetail();
  $("patient-name").textContent = "訪問予定"; $("patient-meta").textContent = date;
  const picker = el("form", null, "hc-inline"), input = el("input"); input.type = "date"; input.value = date;
  picker.append(input, el("button", "この日を表示", "button")); picker.onsubmit = event => {event.preventDefault(); if (input.value) showDay(input.value);};
  const list = el("section", null, "detail-section"), routeBox = el("section", null, "detail-section");
  const data = await api("hc/visits", {date}).catch(error => {fail(error); return null;});
  if (!current()) return;
  list.append(el("h3", "この日の訪問"));
  if (!data) list.append(el("p", "この日の訪問を読み込めませんでした。", "state-note"));
  for (const visit of data?.items || []) {
    const row = el("button", `${visit.start_time || "時刻未定"} · ${visit.patient_name} · ${hc.meta.visit_kinds[visit.kind] || visit.kind} · ${VISIT_STATUS[visit.status]}${visit.staff_name ? " · " + visit.staff_name : ""}`, "text-button");
    row.onclick = () => showSheet(visit.patient_id); list.append(row);
  }
  if (data && !data.items.length) list.append(el("p", "この日の訪問はありません。"));
  routeBox.append(el("h3", "訪問ルート"));
  if (!hc.routes) routeBox.append(el("p", "訪問ルートは設定されていません。設定すると、住所だけを地図サービスへ送って回る順番を作ります。"));
  else {
    const form = el("form", null, "hc-inline"), staffPick = field(["staff", "職員", "staff"], null, "route"), result = el("div");
    form.append(staffPick, el("button", "ルートを作る（住所を地図サービスへ送ります）", "button"));
    form.onsubmit = async event => {
      event.preventDefault(); result.replaceChildren(el("p", "作成中…"));
      try {
        const staff = form.querySelector('[name="staff"]').value, plan = await hcPost("route.plan", {date, staff: staff ? Number(staff) : null});
        result.replaceChildren();
        plan.stops.forEach((stop, index) => result.append(el("p", `${index + 1}. ${stop.patient_name} · 訪問開始の見込み ${stop.arrival}${stop.start_time ? `（予定 ${stop.start_time}）` : ""}`)));
        if (!plan.stops.length) result.append(el("p", "ルートに入れられる予定の訪問がありません。"));
        if (plan.unscheduled.length) result.append(el("p", `予定の時刻の前後15分に着けない訪問が${plan.unscheduled.length}件あり、ルートに入れていません。時刻か担当を見直してください。`, "state-note"));
        if (plan.unlocated.length) result.append(el("p", `住所から場所を1か所に決められなかった訪問が${plan.unlocated.length}件あり、ルートに入れていません。台帳の住所を番地まで確かめてください。`, "state-note"));
      } catch (error) {result.replaceChildren(el("p", error.message, "state-note"));}
    };
    routeBox.append(form, result);
  }
  hcMain("訪問予定", picker, list, routeBox);
}
function mcsLink(link, patientId, data, refresh) {
  const relink = async (op, project) => {try {await hcPost(op, {patient: patientId, project}); await loadHcPatients(); await refresh();} catch (error) {fail(error);}};
  link.replaceChildren(el("h3", "MCS の患者"));
  if (data.mcs.project_id) {
    link.append(el("p", `${data.mcs.patient_name || "閲覧範囲外の患者"}（MCS ID ${data.mcs.project_id}）と対応付けています。`));
    if (data.mcs.patient_name) {const open = el("button", "MCS のタイムラインを開く", "text-button"); open.onclick = () => openMcs(data.mcs.project_id); link.append(open);}
    const undo = el("button", "対応付けを解除", "text-button");
    undo.onclick = () => relink("unlink", data.mcs.project_id);
    link.append(undo);
  } else {
    link.append(el("p", "まだ MCS の患者と対応付けていません。氏名が同じでも自動では対応付けません。"));
    for (const candidate of data.candidates) {
      const pick = el("button", `${candidate.patient_name}（MCS ID ${candidate.project_id}）と対応付ける`, "text-button");
      pick.onclick = () => relink("link", candidate.project_id);
      link.append(pick);
    }
    const manual = el("form", null, "hc-inline"), input = el("input"); input.placeholder = "MCS の患者ID"; input.inputMode = "numeric";
    manual.append(input, el("button", "この ID と対応付ける", "button"));
    manual.onsubmit = event => {event.preventDefault(); relink("link", Number(input.value));};
    link.append(manual);
  }
}
function careTeam(box, patientId, members, reload) {
  box.replaceChildren(el("h3", "ケアチーム（この事業所の担当）"));
  for (const member of members) {
    const row = el("p", `${hc.meta.roles[member.role]?.label || member.role}: ${member.staff_name}`), stop = el("button", "担当を外す", "text-button");
    stop.onclick = async () => {try {await hcPost("team.end", {assignment: member.assignment_id}); reload();} catch (error) {fail(error);}};
    row.append(" ", stop); box.append(row);
  }
  if (!members.length) box.append(el("p", "担当者はまだいません。"));
  const roles = Object.entries(hc.meta.roles);
  if (!roles.length) return;
  const form = el("form", null, "hc-inline"), role = el("select"), person = el("select");
  for (const [code, spec] of roles) {const option = el("option", spec.label); option.value = code; role.append(option);}
  const people = () => {
    const needs = hc.meta.roles[role.value].requires;
    person.replaceChildren(...hc.staff.filter(s => s.active && s.qualifications.some(q => needs.includes(q))).map(s => {const option = el("option", s.name); option.value = String(s.staff_id); return option;}));
  };
  role.onchange = people; people();
  form.append(role, person, el("button", "担当にする", "button"));
  form.onsubmit = async event => {event.preventDefault(); try {const assignments = members.filter(m => m.role === role.value).map(m => m.assignment_id); await hcPost("team.assign", {patient: patientId, staff: Number(person.value), role: role.value, assignments}); reload();} catch (error) {fail(error); if (error.code === "team_changed") reload();}};
  box.append(form);
}
async function openMcs(projectId) {
  // The way back from a visit sheet always opens the timeline, not whichever view was last chosen.
  $("patient-search").value = ""; state.panel = ""; $("view-select").value = "";
  await setMode("mcs");
  const patient = state.patients.find(row => row.project_id === projectId)
    || (await api("patients", {q: String(projectId)})).items.find(row => row.project_id === projectId);
  if (patient) selectPatient(patient);
}
function setMode(mode) {
  document.body.classList.remove("picked");
  hc.mode = mode; hc.patient = null; ++state.patientRequest; ++hc.request; hcScreen(); state.project = null; closeDetail();
  for (const button of document.querySelectorAll("#hc-modes button")) button.classList.toggle("selected", button.dataset.mode === mode);
  for (const id of ["patient-context", "read-only", "detail"]) $(id).hidden = mode !== "mcs";
  document.querySelector(".snapshot-bar").hidden = mode !== "mcs";
  $("view-select").parentNode.hidden = mode !== "mcs";
  if (mode === "mcs") {
    $("patient-name").textContent = "患者を選んでください"; $("patient-meta").textContent = "";
    ++state.contextRequest; ++state.mainRequest; $("patient-context").replaceChildren(); $("coverage").hidden = true; $("older").hidden = true;
    $("timeline").replaceChildren(); $("panel-view").replaceChildren();
    $("timeline-status").textContent = "患者を選ぶと投稿を表示します。";
    $("panel-view").hidden = !state.panel; $("timeline").hidden = false; $("timeline-status").hidden = false;
    return loadPatients();
  }
  const listed = loadHcPatients().catch(fail);
  if (mode === "day") {
    const today = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
    showDay(today);
  } else if (mode === "staff") {
    $("patient-name").textContent = "職員"; $("patient-meta").textContent = "操作者と担当薬剤師の名簿";
    showStaff();
  } else {
    $("patient-name").textContent = "薬局の患者台帳"; $("patient-meta").textContent = "左の一覧から患者を選ぶか、登録してください";
    hcMain("薬局の患者台帳", el("p", "左の一覧から患者を選ぶか、「＋ 患者を登録」から登録してください。", "state-note"));
  }
  return listed;
}
async function showLinkedPharmacyPatient(projectId) {
  if (!hc.enabled) return;
  try {
    const data = await api("hc/linked", {project: projectId});
    if (data.patient && state.project === projectId) $("patient-meta").textContent += ` · 薬局の台帳: ${data.patient.name}`;
  } catch (error) {fail(error);}
}
async function initHomecare() {
  try {
    const session = await api("session");
    hc.csrf = session.csrf; hc.enabled = session.homecare; hc.routes = session.routes; hc.resident = session.resident; hc.user = session.user; hc.login = session.login;
    if (!hc.enabled) return;
    $("hc-tools").hidden = false;
    $("hc-actor").onchange = () => {hc.actor = $("hc-actor").value ? Number($("hc-actor").value) : null;};
    for (const button of document.querySelectorAll("#hc-modes button")) button.onclick = () => setMode(button.dataset.mode);
    state.onSelect = showLinkedPharmacyPatient;
    state.onSearch = () => (hc.mode === "mcs" ? loadPatients() : loadHcPatients().catch(fail));
    hc.meta = await api("hc/meta");
    await loadStaff();
    if (!hc.staff.length) setMode("staff");
  } catch (error) {fail(error);}
}
initHomecare();
