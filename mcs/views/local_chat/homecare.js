"use strict";
// Home-care core screens (staff, patient register, care team, MCS links). Shares $, el, api, fail and state with app.js.
// Occupation modules add their own screens; the core only reads their declared qualifications and roles from api/hc/meta.
const hc = {enabled: false, csrf: "", actor: null, meta: {qualifications: {}, roles: {}}, staff: [], patients: [], patient: null, mode: "mcs", request: 0};
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
  not_qualified: "この役割に必要な資格を持つ在籍中の職員を選んでください。", role_not_allowed: "この役割は今は使えません。", still_assigned: "担当を持っています。先に担当を交代するか外してください。",
  actor_required: "操作者を選んでください。", invalid_fields: "入力を確認してください。", not_found: "記録が見つかりません。",
  project_not_allowed: "この MCS の患者は閲覧範囲の外です。"};
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
  if (type === "select") {
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
    values[key] = type === "checkbox" ? input.checked : input.value;
  }
  return values;
}
function recordForm(kind, spec, record, onSaved) {
  const form = el("form", null, "hc-form"), message = el("p", null, "state-note");
  for (const item of spec) form.append(field(item, record?.[item[0]], kind));
  const save = el("button", record ? "保存" : "登録", "button");
  form.append(save, message);
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true; message.textContent = "";
    for (const node of form.querySelectorAll(".invalid")) node.classList.remove("invalid");
    try {
      const values = formValues(form, spec);
      const data = record ? await hcPost(`${kind}.update`, {id: record[`${kind}_id`], values, expected_version: record.version})
                          : await hcPost(`${kind}.create`, {values});
      await onSaved(data.record);
    } catch (error) {
      message.textContent = error.message;
      for (const name of error.fields || []) form.querySelector(`[name="${name}"]`)?.closest(".hc-field")?.classList.add("invalid");
    } finally {save.disabled = false;}
  };
  return form;
}
async function loadStaff() {
  hc.staff = (await api("hc/staff")).items;
  const select = $("hc-actor"); select.replaceChildren(el("option", "操作者を選択"));
  select.firstChild.value = "";
  for (const person of hc.staff.filter(s => s.active)) {const option = el("option", person.name); option.value = String(person.staff_id); select.append(option);}
  if (hc.actor && hc.staff.some(s => s.staff_id === hc.actor && s.active)) select.value = String(hc.actor); else hc.actor = null;
}
function hcMain(title, ...nodes) {
  ++state.mainRequest;
  $("panel-view").hidden = false;
  for (const id of ["coverage", "older", "timeline", "timeline-status"]) $(id).hidden = true;
  const box = el("div", null, "panel"); box.append(el("h2", title), ...nodes);
  $("panel-view").replaceChildren(box);
}
async function showStaff() {
  closeDetail();
  const list = el("div");
  for (const person of hc.staff) {
    const row = el("details", null, "hc-row");
    row.append(el("summary", `${person.name}${person.active ? "" : "（退職）"} · ${person.qualifications.map(qualificationLabel).join("・")}${person.admin ? " · 管理者" : ""}`));
    row.append(recordForm("staff", STAFF_FORM, person, async () => {await loadStaff(); showStaff();}));
    list.append(row);
  }
  const added = el("details", null, "hc-row"); added.append(el("summary", "職員を追加"), recordForm("staff", STAFF_FORM, null, async saved => {if (!hc.actor) hc.actor = saved.staff_id; await loadStaff(); showStaff();}));
  hcMain("職員", ...(hc.staff.length ? [] : [el("p", "最初に自分を職員として登録してください。登録した人がそのまま操作者と管理者になります。", "state-note")]), list, added);
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
  hc.patient = patientId; closeDetail();
  for (const button of $("patients").querySelectorAll(".patient")) button.classList.toggle("selected", button.dataset.patient === String(patientId));
  if (patientId == null) {
    $("patient-name").textContent = "新しい患者"; $("patient-meta").textContent = "薬局の患者台帳";
    hcMain("患者の登録", recordForm("patient", PATIENT_FORM, null, async saved => {await loadHcPatients(); showHcPatient(saved.patient_id);}));
    return;
  }
  const data = await api("hc/patient", {patient: patientId});
  if (hc.patient !== patientId) return;
  const patient = data.patient;
  $("patient-name").textContent = patient.name;
  $("patient-meta").textContent = `薬局の患者台帳 · ${patient.kana || "フリガナ未登録"}`;
  const team = el("section", null, "detail-section"), link = el("section", null, "detail-section");
  // Team and link actions redraw only their own sections, so unsaved edits in the record form stay.
  const refresh = async () => {
    const fresh = await api("hc/patient", {patient: patientId});
    if (hc.patient === patientId) {careTeam(team, patientId, fresh.team, refresh); mcsLink(link, patientId, fresh, refresh);}
  };
  careTeam(team, patientId, data.team, refresh); mcsLink(link, patientId, data, refresh);
  hcMain("基本情報", recordForm("patient", PATIENT_FORM, patient, async () => {await loadHcPatients(); showHcPatient(patientId);}), team, link);
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
  form.onsubmit = async event => {event.preventDefault(); try {await hcPost("team.assign", {patient: patientId, staff: Number(person.value), role: role.value}); reload();} catch (error) {fail(error);}};
  box.append(form);
}
async function openMcs(projectId) {
  $("patient-search").value = "";
  await setMode("mcs");
  const patient = state.patients.find(row => row.project_id === projectId)
    || (await api("patients", {q: String(projectId)})).items.find(row => row.project_id === projectId);
  if (patient) selectPatient(patient);
}
function setMode(mode) {
  hc.mode = mode; hc.patient = null; ++state.patientRequest; state.project = null; closeDetail();
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
  if (mode === "staff") {
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
    hc.csrf = session.csrf; hc.enabled = session.homecare;
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
