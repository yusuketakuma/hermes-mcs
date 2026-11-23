"use strict";
// Dependency-free DOM contract checks, run by the Python test when Node is available.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(tag) {this.tagName = tag; this.children = []; this.className = ""; this.textContent = ""; this.hidden = false; this.value = ""; this.scrollTop = 0; this.scrollHeight = 0; this.isConnected = true; this.classList = {add(){}, remove(){}, toggle(){}};}
  append(...nodes) {for (const node of nodes) {if (node.tagName === "fragment") this.append(...node.children); else {node.parentNode = this; this.children.push(node);}}}
  prepend(node) {const items = node.tagName === "fragment" ? node.children : [node]; for (const item of items) item.parentNode = this; this.children.unshift(...items);}
  replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
  get firstElementChild() {return this.children[0];}
  remove() {this.parentNode.children = this.parentNode.children.filter(node => node !== this);}
  set id(value) {nodes.set(value, this);}
  setAttribute() {}
}
const nodes = new Map();
const document = {body: new Element("body"), getElementById(id) {if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element("fragment")};
const sandbox = {document, URLSearchParams, location: {}, fetch: () => new Promise(() => {})};
vm.createContext(sandbox);
const source = fs.readFileSync(process.argv[2], "utf8").replace(/loadPatients\(\);\s*$/, "");
vm.runInContext(source, sandbox);
function content(node) {return [node.textContent, ...node.children.map(content)].join("\n");}
const section = vm.runInContext('contextSection({patient_context:[{text:"糖尿病",subject:"family",evidence:"母に糖尿病の既往あり",details:[]}]})', sandbox);
assert.match(content(section), /対象: 家族/);
assert.match(content(section), /母に糖尿病の既往あり/);
assert.equal(section.children.find(node => node.tagName === "blockquote").textContent, "母に糖尿病の既往あり");
const unknown = vm.runInContext('contextSection({patient_context:[{text:"合成記録",subject:"unspecified",evidence:"合成記録"}]})', sandbox);
assert.match(content(unknown), /対象未確認/);
const mixed = vm.runInContext('contextSection({patient_context:[{text:"アレルギー：なし",subject:"unspecified",evidence:"アレルギー：なし",source_evidence:"アレルギー：なし\\n訪問しました。体温36.5℃。",scope_note:"続きあり・分類範囲は未確認"}]})', sandbox);
assert.equal(mixed.children.find(node => node.tagName === "blockquote").textContent, "アレルギー：なし");
assert.doesNotMatch(mixed.children.filter(node => node.tagName === "p").map(content).join("\n"), /訪問しました/);
assert.match(content(mixed.children.find(node => node.className === "context-source")), /原文全体（分類範囲は未確認）[\s\S]*訪問しました/);
async function checkReplyRecords() {
  sandbox.reply = {project_id: 101, message_id: 3, parent_id: 1};
  sandbox.api = async (kind, params) => {
    assert.equal(params.message, 3);
    return kind === "attachments" ? {items: []} : {message: {patient_context:[{text:"合成の返信の記録",subject:"family",evidence:"合成の家族情報"}]}};
  };
  await vm.runInContext("showMessageInfo(reply, state.detailRequest)", sandbox);
  const text = content(document.getElementById("message-info"));
  assert.match(text, /合成の返信の記録/);
  assert.match(text, /対象: 家族/);
  assert.match(text, /合成の家族情報/);
}
async function checkOrphanReply() {
  sandbox.orphan = {project_id: 101, message_id: 7, parent_id: 999, parent_available: false, body_text:"合成の孤立した返信", body_state:"full"};
  sandbox.api = async (kind, params) => {
    assert.equal(params.message, 7, "never request the unavailable parent");
    return kind === "evidence" ? {message: sandbox.orphan} : {items: []};
  };
  await vm.runInContext("openDetail(orphan)", sandbox);
  const text = content(document.getElementById("detail-content"));
  assert.match(text, /元の投稿は未取得/);
  assert.match(text, /合成の孤立した返信/);
}
async function checkDatePagination() {
  vm.runInContext("state.project = 101; state.query = ''", sandbox);
  const row = (id, day) => ({message_id:id, posted_at_ts:day, body_state:"full", body_text:`合成 ${id}`});
  const day = 1793491200;
  const pages = [
    {items:[row(4, day + 40), row(3, day + 30)], next_cursor:"older"},
    {items:[row(2, day + 20), row(1, day + 10)], next_cursor:"oldest"},
    {items:[row(0, day - 86400)], next_cursor:null}
  ];
  sandbox.api = async () => pages.shift();
  await vm.runInContext("loadTimeline()", sandbox);
  await vm.runInContext("loadTimeline(true)", sandbox);
  const timeline = document.getElementById("timeline");
  assert.equal(timeline.children.filter(node => node.className === "date-divider").length, 1);
  assert.equal(document.getElementById("timeline-status").textContent, "");
  assert.match(content(timeline), /合成 1[\s\S]*合成 2[\s\S]*合成 3[\s\S]*合成 4/);
  await vm.runInContext("loadTimeline(true)", sandbox);
  assert.equal(timeline.children.filter(node => node.className === "date-divider").length, 2);
}
async function checkPatientContext() {
  const context = text => ({sections:[{label:"病名",items:[{subject:"patient",text,evidence:`根拠 ${text}`,posted_at_ts:1793491200}]}]});
  vm.runInContext("state.project = 101", sandbox);
  sandbox.api = async (kind, params) => {assert.equal(kind, "context"); assert.equal(params.project, 101); return context("合成疾患A");};
  await vm.runInContext("loadPatientContext(101)", sandbox);
  assert.match(content(document.getElementById("patient-context")), /合成疾患A/);
  assert.match(content(document.getElementById("patient-context")), /根拠 合成疾患A/);
  let finishOld;
  sandbox.api = (kind, params) => params.project === 101 ? new Promise(resolve => {finishOld = resolve;}) : Promise.resolve(context("合成疾患B"));
  const old = vm.runInContext("loadPatientContext(101)", sandbox);
  assert.doesNotMatch(content(document.getElementById("patient-context")), /合成疾患A/);
  vm.runInContext("state.project = 102", sandbox);
  await vm.runInContext("loadPatientContext(102)", sandbox);
  finishOld(context("古い患者の合成記録")); await old;
  assert.match(content(document.getElementById("patient-context")), /合成疾患B/);
  assert.doesNotMatch(content(document.getElementById("patient-context")), /古い患者/);
  sandbox.api = async () => ({sections:[]});
  await vm.runInContext("loadPatientContext(102)", sandbox);
  assert.match(content(document.getElementById("patient-context")), /まだ保存されていません/);
}
async function checkPanel() {
  const view = {title:"合成 — 訪問前の確認ノート", head:["見出し"], items:[{group:"新しい報告", quotes:["<b>合成の引用</b>"], source:"11/16 看護師 #1", message_id:5}], notes:["履歴の取得は未完了"], page:0, pages:2};
  const pages = [];
  const node = vm.runInContext("renderPanel", sandbox)(view, page => pages.push(page));
  const text = content(node);
  assert.match(text, /新しい報告[\s\S]*<b>合成の引用<\/b>[\s\S]*11\/16 看護師 #1/);
  assert.match(text, /投稿を開く/);
  assert.match(text, /履歴の取得は未完了/);
  const pager = node.children.find(child => child.className === "panel-pager");
  pager.children.find(child => child.textContent === "次のページ").onclick();
  assert.deepEqual(pages, [1]);
  const empty = content(vm.runInContext("renderPanel", sandbox)({title:"合成", items:[], empty:"記録の有無は未確認です"}));
  assert.match(empty, /記録の有無は未確認です/);
  const summary = vm.runInContext("renderPanel", sandbox)({title:"まとめ", body:"■ 未完了タスク: なし\n履歴取得: 未完了"});
  assert.deepEqual(summary.children.slice(1).map(child => child.className), ["panel-group", "panel-line"]);
}
async function checkMedsOutOfOrder() {
  // Two presses on one panel: the first answer arriving last must not replace the second.
  vm.runInContext("state.project = 101", sandbox);
  const answers = {};
  sandbox.api = (kind, params) => new Promise(resolve => {answers[params.message] = resolve;});
  const target = new Element("div"), request = vm.runInContext("state.detailRequest", sandbox);
  const showMeds = vm.runInContext("showMeds", sandbox);
  const first = showMeds(target, 1, request), second = showMeds(target, 2, request);
  answers[2]({panel: {title: "返信の薬剤", items: []}}); await second;
  answers[1]({panel: {title: "元の投稿の薬剤", items: []}}); await first;
  assert.match(content(target), /返信の薬剤/);
  assert.doesNotMatch(content(target), /元の投稿の薬剤/);
}
async function main() {await checkReplyRecords(); await checkOrphanReply(); await checkDatePagination(); await checkPatientContext(); await checkPanel(); await checkMedsOutOfOrder(); console.log("local chat DOM contracts passed");}
main().catch(error => {console.error(error); process.exitCode = 1;});
