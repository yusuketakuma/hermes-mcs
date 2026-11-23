"use strict";
// Pharmacy module screens: prescription history in the patient register and on the visit sheet.
// Uses the core's el, api, hcPost and hc from homecare.js; routes live under api/hc/pharmacy/.
const RX_LINE = [["drug_name", "薬剤名（必須）"], ["dose", "用量"], ["usage", "用法"], ["days", "日数", "number"]];
const rxLineText = line => [line.drug_name, line.dose, line.usage, line.days ? `${line.days}日分` : ""].filter(Boolean).join(" ");
function rxLineRow(line = {}) {
  const row = el("div", null, "hc-inline rx-line");
  for (const [key, label, type] of RX_LINE) {
    const input = el("input"); input.name = key; input.placeholder = label; input.type = type || "text"; input.autocomplete = "off";
    input.value = line[key] ?? ""; if (type === "number") {input.min = "1"; input.max = "365";}
    row.append(input);
  }
  // The master code stays with the line it was chosen for; typing a different name drops it.
  row.dataset.code = line.drug_code || ""; row.dataset.system = line.code_system || ""; row.dataset.name = line.drug_name || "";
  const remove = el("button", "削除", "text-button"); remove.type = "button"; remove.onclick = () => row.remove(); row.append(remove);
  return row;
}
function rxLines(form) {
  return [...form.querySelectorAll(".rx-line")].map(row => {
    const value = name => row.querySelector(`[name="${name}"]`).value.trim();
    const line = {drug_name: value("drug_name"), dose: value("dose"), usage: value("usage"), days: value("days") ? Number(value("days")) : null};
    if (row.dataset.code && row.dataset.name === line.drug_name) {line.code_system = row.dataset.system; line.drug_code = row.dataset.code;}
    return line;
  });
}
function rxForm(patientId, record, onSaved) {
  const form = el("form", null, "hc-form"), message = el("p", null, "state-note"), lines = el("div");
  for (const [key, label, type] of [["issued_on", "処方日（必須）", "date"], ["institution", "医療機関"], ["prescriber", "処方医"], ["note", "メモ", "textarea"]]) form.append(field([key, label, type], record?.[key], "rx"));
  for (const line of record?.lines || [{}]) lines.append(rxLineRow(line));
  const more = el("button", "薬剤を追加", "text-button"); more.type = "button"; more.onclick = () => lines.append(rxLineRow());
  const save = el("button", record ? "保存" : "登録", "button");
  form.append(el("h4", "薬剤"), lines, more, save, message);
  let current = record;
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true; message.textContent = "";
    for (const node of form.querySelectorAll(".invalid")) node.classList.remove("invalid");
    const values = {issued_on: form.querySelector('[name="issued_on"]').value, institution: form.querySelector('[name="institution"]').value,
                    prescriber: form.querySelector('[name="prescriber"]').value, note: form.querySelector('[name="note"]').value, lines: rxLines(form)};
    let data;
    try {
      data = current ? await hcPost("pharmacy/prescription.update", {id: current.prescription_id, values, expected_version: current.version})
                     : await hcPost("pharmacy/prescription.create", {values: {...values, patient_id: patientId}});
    } catch (error) {
      message.textContent = error.message;
      for (const name of error.fields || []) {
        const [top, index, key] = name.split(".");
        const target = top === "lines" && index ? lines.children[Number(index) - 1]?.querySelector(`[name="${key || "drug_name"}"]`) : form.querySelector(`[name="${top}"]`);
        (target?.closest(".hc-field") || target)?.classList.add("invalid");
      }
      save.disabled = false;
      return;
    }
    current = data.record;
    await afterSave(save, message, onSaved);
  };
  return form;
}
function rxSummary(rx) {
  const box = el("div");
  box.append(el("p", `${rx.issued_on}${rx.institution ? " · " + rx.institution : ""}${rx.prescriber ? " · " + rx.prescriber : ""}`));
  const list = el("ul"); for (const line of rx.lines) list.append(el("li", rxLineText(line))); box.append(list);
  if (rx.note) box.append(el("p", rx.note, "hc-note"));
  return box;
}
hc.modules.pharmacy = {
  async patient(box, patientId, limit = 20) {
    const reload = () => hc.modules.pharmacy.patient(box, patientId, limit);
    const data = await api("hc/pharmacy/prescriptions", {patient: patientId, limit}).catch(error => {if (box.childElementCount) throw error; fail(error); return null;});
    if (hc.patient !== patientId) return;
    box.replaceChildren(el("h3", "処方"));
    if (!data) {box.append(el("p", "処方の記録を読み込めませんでした。", "state-note")); return;}
    for (const rx of data.items) {
      const row = el("details", null, "hc-row"); row.append(el("summary", `${rx.issued_on} · ${rx.lines.map(line => line.drug_name).join("、")}`));
      row.append(rxSummary(rx), rxForm(patientId, rx, reload)); box.append(row);
    }
    if (!data.items.length) box.append(el("p", "処方の記録はまだありません。"));
    if (data.more) box.append(olderButton("以前の処方を表示", () => hc.modules.pharmacy.patient(box, patientId, limit + 20)));
    const add = el("details", null, "hc-row"); add.append(el("summary", "処方を登録する"), rxForm(patientId, null, reload)); box.append(add);
  },
  sheet(box, part) {
    box.append(el("h3", part.label));
    for (const rx of part.prescriptions) box.append(rxSummary(rx));
    if (!part.prescriptions.length) box.append(el("p", "処方の記録はまだありません。"));
  },
};
