"use strict";
// Dependency-free DOM contract checks, run by the Python test when Node is available.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(tag) {this.tagName = tag; this.children = []; this.className = ""; this.textContent = ""; this.hidden = false; this.value = ""; this.scrollTop = 0; this.scrollHeight = 0; this.isConnected = true; const names = new Set(); this.classList = {add: name => names.add(name), remove: name => names.delete(name), toggle: (name, on) => on ? names.add(name) : names.delete(name), contains: name => names.has(name)};}
  append(...nodes) {for (const node of nodes) {if (node.tagName === "fragment") this.append(...node.children); else {node.parentNode = this; this.children.push(node);}}}
  prepend(node) {const items = node.tagName === "fragment" ? node.children : [node]; for (const item of items) item.parentNode = this; this.children.unshift(...items);}
  replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
  get firstElementChild() {return this.children[0];}
  remove() {this.parentNode.children = this.parentNode.children.filter(node => node !== this);}
  set id(value) {nodes.set(value, this);}
  setAttribute() {}
  scrollIntoView() {}
  focus() {document.activeElement = this;}
  contains(node) {for (; node; node = node.parentNode) if (node === this) return true; return false;}
}
const nodes = new Map();
const listeners = {};
const document = {body: new Element("body"), activeElement: null, addEventListener(type, handler) {(listeners[type] ||= []).push(handler);}, getElementById(id) {if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element("fragment")};
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
function find(node, test) {if (test(node)) return node; for (const child of node.children || []) {const hit = find(child, test); if (hit) return hit;} return null;}
const button = (node, text) => find(node, child => child.tagName === "button" && child.textContent === text);
const tick = () => new Promise(resolve => setImmediate(resolve));
function checkStateChips() {
  // Fetch states are chips in the heading; record states are chips in the post footer, none dropped.
  const fetchChips = vm.runInContext("fetchChips", sandbox);
  assert.equal(fetchChips({fetch_state: "complete", history_floor: -1}).length, 0);
  assert.equal(fetchChips({fetch_state: "failed", history_floor: null}).map(chip => chip.textContent).join("|"), "取得失敗|履歴全体の取得は未確認");
  assert.equal(fetchChips({fetch_state: "complete", history_floor: 5}).map(chip => chip.textContent).join("|"), "履歴全体の取得は未確認");
  assert.equal(fetchChips({fetch_state: "pending", history_floor: -1}).map(chip => chip.textContent).join("|"), "未取得");
  const row = {message_id: 9, sender_name: "合成 投稿者", body_text: "合成本文", body_state: "partial", body_complete: false, stamp_line: "スタンプ 未取得", reply_count: 3, stored_replies: 1, attachments: [{count: 2}], parent_id: 4};
  for (const detailed of [false, true]) {
    const node = vm.runInContext("post", sandbox)(row, detailed);
    const footer = find(node, child => child.className === "post-footer");
    const chips = footer.children.filter(child => child.className.startsWith("chip")).map(child => child.textContent);
    for (const text of ["スタンプ 未取得", "本文は一部未取得です", "未取得の返信があります"]) assert.ok(chips.includes(text), `${text} (detailed=${detailed})`);
    assert.equal(chips.includes("添付 2件"), !detailed);
    assert.equal(chips.includes("返信の検索結果"), !detailed);
    assert.equal(Boolean(button(footer, "詳細・返信 1件")), !detailed);
  }
}
async function checkPostNavigation() {
  // 投稿を開く keeps the open detail live until the new post has loaded; a failure leaves it usable.
  vm.runInContext("state.project = 101; document.getElementById('error').textContent = ''", sandbox);
  const open = {message_id: 1, body_text: "合成の開いている投稿", body_state: "full", body_complete: true};
  sandbox.api = async kind => kind === "evidence" ? {message: open} : kind === "panel" ? {panel: {title: "合成の薬剤", items: []}} : {items: []};
  await vm.runInContext("openDetail({message_id: 1})", sandbox);
  const detail = vm.runInContext("state.detailRequest", sandbox);
  const meds = button(document.getElementById("detail-content"), "この投稿の薬剤を表示");
  const panel = vm.runInContext("renderPanel", sandbox)({title: "合成", items: [{text: "合成の項目", message_id: 5}]});
  const go = button(panel, "投稿を開く");
  sandbox.api = async () => {throw new Error("合成の取得失敗");};
  await go.onclick();
  assert.equal(vm.runInContext("state.detailRequest", sandbox), detail, "a failed navigation leaves the open detail's token alone");
  assert.equal(document.getElementById("error").textContent, "合成の取得失敗");
  sandbox.api = async () => ({panel: {title: "合成の薬剤", items: []}});
  meds.onclick(); await tick();
  assert.match(content(document.getElementById("detail-content")), /合成の薬剤/, "the open detail's buttons still work");
  // Late answers: after a close, a patient change or a newer navigation they open nothing.
  const realOpen = sandbox.openDetail, opened = [];
  sandbox.openDetail = message => opened.push(message.message_id);
  const answers = [];
  sandbox.api = () => new Promise(resolve => answers.push(resolve));
  const closed = go.onclick(); vm.runInContext("closeDetail()", sandbox); answers.shift()({message: {message_id: 5}}); await closed;
  const moved = go.onclick(); vm.runInContext("state.project = 102", sandbox); answers.shift()({message: {message_id: 5}}); await moved;
  vm.runInContext("state.project = 101", sandbox);
  const other = vm.runInContext("renderPanel", sandbox)({title: "合成", items: [{text: "別の項目", message_id: 6}]});
  const first = go.onclick(), second = button(other, "投稿を開く").onclick();
  const [answerFirst, answerSecond] = answers.splice(0);
  answerSecond({message: {message_id: 6}}); await second;
  answerFirst({message: {message_id: 5}}); await first;
  assert.deepEqual(opened, [6]);
  sandbox.openDetail = realOpen;
}
async function checkSideStatus() {
  // いまの状況: up to five items per card, a short failure line, and nothing drawn for a patient no longer selected.
  const rail = document.getElementById("side-status");
  const answers = {};
  sandbox.api = (kind, params) => new Promise((resolve, reject) => {assert.equal(kind, "panel"); answers[`${params.project}:${params.section}`] = {resolve, reject};});
  vm.runInContext("state.project = 101", sandbox);
  const stale = vm.runInContext("loadSideStatus()", sandbox);
  vm.runInContext("state.project = 102", sandbox);
  answers["101:signals"].resolve({panel: {items: [{text: "古い患者の合成シグナル"}]}});
  answers["101:replies"].reject(new Error("old"));
  await stale;
  assert.doesNotMatch(content(rail), /古い患者|取得できませんでした/);
  const fresh = vm.runInContext("loadSideStatus()", sandbox);
  const items = Array.from({length: 7}, (_, index) => ({text: `合成シグナル${index + 1}`, source: "11/16 看護師 #1"}));
  answers["102:signals"].resolve({panel: {items, notes: ["判定済みの分だけです"]}});
  answers["102:replies"].reject(new Error("合成の失敗"));
  await fresh;
  const text = content(rail);
  assert.match(text, /いまの状況/);
  assert.match(text, /合成シグナル5/);
  assert.doesNotMatch(text, /合成シグナル6/);
  assert.match(text, /続きは「すべて見る」/);
  assert.match(text, /判定済みの分だけです/);
  assert.match(text, /連絡・返答の確認[\s\S]*取得できませんでした/);
  sandbox.api = () => new Promise(() => {});
  button(rail, "すべて見る").onclick();
  assert.equal(vm.runInContext("state.panel", sandbox), "signals");
  vm.runInContext("state.panel = ''; state.project = null", sandbox);
  await vm.runInContext("loadSideStatus()", sandbox);
  assert.equal(rail.children.length, 0, "no patient, no rail");
}
async function checkReviewFixes() {
  const run = code => vm.runInContext(code, sandbox), byId = id => document.getElementById(id);
  // Stamp tone: every non-complete wording of stamp_format is amber; actor names such as 氏名不明 are not.
  const tone = line => run("post")({message_id: 1, body_state: "full", body_complete: true, stamp_line: line}).children[1].children.find(node => node.className === "post-footer").children.find(node => node.textContent === line).className;
  for (const line of ["スタンプ 未取得", "スタンプ 未取得・再取得失敗", "スタンプ 👍2 · 押した人 取得失敗", "スタンプ 👍2 · 押した人 古い情報", "押した人（取得状態不明）: なし", "スタンプ 👍1 · 11-18 10:00時点・再取得失敗"]) assert.equal(tone(line), "chip chip-warn", line);
  for (const line of ["スタンプ なし · 11-18 10:00時点", "スタンプ 👍1 氏名不明 · 11-18 10:00時点", "スタンプ 👍1 · 投稿時点"]) assert.equal(tone(line), "chip chip-muted", line);
  // On phones the page scrolls, not the main column: the newest post is scrolled to on the page.
  const page = new Element("html"); page.scrollHeight = 900; document.scrollingElement = page;
  sandbox.getComputedStyle = node => ({overflowY: node === byId("timeline-panel") ? "visible" : "auto"});
  run("state.project = 101; state.query = ''");
  sandbox.api = async () => ({items: [{message_id: 1, posted_at_ts: 1793491200, body_state: "full", body_text: "合成"}], next_cursor: null});
  await run("loadTimeline()");
  assert.equal(page.scrollTop, 900);
  sandbox.api = async () => ({panel: {title: "合成", items: []}});
  run("state.panel = 'summary'"); await run("loadPanel()");
  assert.equal(page.scrollTop, 0);
  delete sandbox.getComputedStyle; run("state.panel = ''");
  // A view tab keeps keyboard focus across the tablist rebuild.
  run("state.sections = [['', 'タイムライン'], ['summary', 'まとめ']]; viewTabs()");
  sandbox.api = () => new Promise(() => {});
  const summaryTab = byId("view-tabs").children[0].children[1];
  summaryTab.focus(); summaryTab.onclick();
  const rebuilt = byId("view-tabs").children[0].children[1];
  assert.notEqual(rebuilt, summaryTab); assert.equal(document.activeElement, rebuilt);
  run("state.panel = ''");
  // Esc closes the open detail and focus returns to the opener; with the sheet open, Esc closes the sheet first.
  const opener = new Element("button"); opener.focus();
  sandbox.api = async kind => kind === "evidence" ? {message: {message_id: 1, body_state: "full", body_complete: true}} : {items: []};
  await run("openDetail({message_id: 1})");
  assert.equal(document.activeElement, byId("close-detail"));
  byId("detail").append(byId("close-detail")); byId("close-detail").focus();
  byId("detail").classList.add("open");
  run("shell.openList(true)");
  assert.equal(document.activeElement, byId("patient-search")); assert.equal(byId("workspace").inert, true);
  byId("sidebar").append(byId("patient-search"));
  listeners.keydown.forEach(handler => handler({key: "Escape"}));
  assert.equal(document.body.classList.contains("list-open"), false); assert.equal(byId("workspace").inert, false);
  assert.equal(document.activeElement, byId("close-detail"), "the sheet hands focus back to its opener");
  listeners.keydown.forEach(handler => handler({key: "Escape"}));
  assert.equal(byId("detail").classList.contains("open"), false);
  assert.equal(document.activeElement, opener, "the detail hands focus back to its opener");
  // Context preview: the post date beside the label, the scope note on its own line.
  run("state.project = 101");
  sandbox.api = async () => ({sections: [{label: "アレルギー", items: [{subject: "patient", text: "合成", posted_at_ts: null, scope_note: "続きあり・分類範囲は未確認"}]}]});
  await run("loadPatientContext(101)");
  const brief = find(byId("patient-context"), node => node.className === "context-brief");
  assert.match(content(brief), /日付未取得/);
  assert.equal(brief.children.find(node => node.className === "context-scope").textContent, "続きあり・分類範囲は未確認");
  // The rail shows each item's kind.
  sandbox.api = async (kind, params) => ({panel: {items: params.section === "signals" ? [{group: "合成の種類", text: "合成の未解消"}] : []}});
  await run("loadSideStatus()");
  assert.match(content(byId("side-status")), /合成の種類[\s\S]*合成の未解消/);
}
function checkTabRow() {
  // The tab row scrolls sideways: fades mark only the sides with hidden tabs, the chosen tab is scrolled in.
  const run = code => vm.runInContext(code, sandbox), box = document.getElementById("view-tabs");
  run("state.sections = [['', 'タイムライン'], ['a', '合成A'], ['b', '合成B'], ['c', '合成C']]; state.panel = ''; viewTabs()");
  const strip = box.children[0];
  Object.assign(strip, {scrollWidth: 500, clientWidth: 200, scrollLeft: 0});
  const fades = () => ["fade-start", "fade-end"].filter(name => box.classList.contains(name)).join(" ");
  strip.onscroll(); assert.equal(fades(), "fade-end");
  strip.scrollLeft = 120; strip.onscroll(); assert.equal(fades(), "fade-start fade-end");
  strip.scrollLeft = 300; strip.onscroll(); assert.equal(fades(), "fade-start");
  strip.clientWidth = 500; strip.scrollLeft = 0; strip.onscroll(); assert.equal(fades(), "", "no fade when every tab fits");
  // A vertical wheel scrolls the row only when it overflows.
  strip.clientWidth = 200; let prevented = false;
  strip.onwheel({deltaY: 60, deltaX: 0, preventDefault() {prevented = true;}});
  assert.equal(strip.scrollLeft, 60); assert.ok(prevented);
  // revealTab brings the chosen tab in clear of the fade, from either side.
  const tab = {offsetLeft: 400, offsetWidth: 100};
  strip.scrollLeft = 0; run("revealTab")(strip, tab); assert.equal(strip.scrollLeft, 340);
  strip.scrollLeft = 450; run("revealTab")(strip, tab); assert.equal(strip.scrollLeft, 360);
  run("state.sections = [['', 'タイムライン']]; state.panel = ''");
}
async function main() {await checkReplyRecords(); await checkOrphanReply(); await checkDatePagination(); await checkPatientContext(); await checkPanel(); await checkMedsOutOfOrder(); checkStateChips(); await checkPostNavigation(); await checkSideStatus(); await checkReviewFixes(); checkTabRow(); console.log("local chat DOM contracts passed");}
main().catch(error => {console.error(error); process.exitCode = 1;});
