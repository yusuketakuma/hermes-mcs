"use strict";
// Dependency-free DOM contract checks, run by the Python test when Node is available.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(tag) {this.tagName = tag; this.children = []; this.className = ""; this.textContent = ""; this.hidden = false; this.value = ""; this.scrollTop = 0; this.scrollHeight = 0; this.isConnected = true; const names = new Set(); this.classList = {add: name => names.add(name), remove: name => names.delete(name), toggle: (name, on) => on ? names.add(name) : names.delete(name), contains: name => names.has(name)};}
  append(...nodes) {for (let node of nodes) {if (typeof node === "string") {const text = new Element("#text"); text.textContent = node; node = text;} if (node.tagName === "fragment") this.append(...node.children); else {node.parentNode = this; this.children.push(node);}}}
  prepend(node) {const items = node.tagName === "fragment" ? node.children : [node]; for (const item of items) item.parentNode = this; this.children.unshift(...items);}
  replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
  get firstElementChild() {return this.children[0];}
  get firstChild() {return this.children[0];}
  remove() {this.parentNode.children = this.parentNode.children.filter(node => node !== this);}
  set id(value) {nodes.set(value, this);}
  setAttribute() {}
  scrollIntoView() {}
  focus() {document.activeElement = this;}
  contains(node) {for (; node; node = node.parentNode) if (node === this) return true; return false;}
  get options() {return this.children.filter(node => node.tagName === "option");}
  matches(selector) {return selector === this.tagName || (selector.startsWith(".") && this.className.split(" ").includes(selector.slice(1))) || (selector.startsWith('[name="') && this.name === selector.slice(7, -2));}
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  querySelectorAll(selector) {const out = []; const walk = node => {for (const child of node.children) {if (child.matches && child.matches(selector)) out.push(child); walk(child);}}; walk(this); return out;}
  addEventListener(type, handler) {(this.listeners ||= {})[type] = handler;}
}
const nodes = new Map();
const listeners = {};
const document = {body: Object.assign(new Element("body"), {dataset: {}}), activeElement: null, addEventListener(type, handler) {(listeners[type] ||= []).push(handler);}, getElementById(id) {if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id);}, querySelectorAll: () => [], createElement: tag => new Element(tag), createDocumentFragment: () => new Element("fragment")};
const sandbox = {document, URLSearchParams, location: {}, fetch: () => new Promise(() => {})};
vm.createContext(sandbox);
const source = process.argv.slice(2).map(path => fs.readFileSync(path, "utf8")
  .replace(/loadPatients\(\);\s*$/, "").replace(/initHomecare\(\);\s*$/, "")).join("\n");
vm.runInContext(source, sandbox);
const realApi = sandbox.api;
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
async function checkDisplayGaps() {
  const run = code => vm.runInContext(code, sandbox), byId = id => document.getElementById(id);
  // F1: a complete post with no text has no body; only an incomplete one is 未取得.
  const body = row => run("postBody")(row).textContent;
  assert.equal(body({body_state: "full", body_text: ""}), "本文なし");
  assert.equal(body({body_state: "full", body_text: null}), "本文なし");
  assert.equal(body({body_state: "snippet", body_text: ""}), "本文未取得");
  assert.equal(body({body_state: "unknown", body_text: null}), "本文未取得");
  assert.equal(body({body_state: "deleted", body_text: null}), "削除された投稿");
  assert.equal(body({body_state: "snippet", body_text: "合成の一部"}), "合成の一部");
  // F2: the attachment chip says what is missing and turns amber.
  const files = groups => {const node = run("attachmentChip")(groups); return node && `${node.className}|${node.textContent}`;};
  assert.equal(files([]), null);
  assert.equal(files([{state: "downloaded", count: 2}, {state: "pruned", count: 1}]), "chip chip-muted|添付 3件");
  assert.equal(files([{state: "downloaded", count: 1}, {state: "failed", count: 1}, {state: "pending", count: 2}]), "chip chip-warn|添付 4件（取得失敗 1・未取得 2）");
  // F2: every attachment state, reason, size, OCR state, page and finding is shown.
  const entry = file => content(run("attachmentEntry")(file));
  const failed = entry({attachment_id: 1, name: "合成.pdf", state: "failed", bytes: 0, error: "download_failed", ocr: {state: "unavailable", page_count: 0}});
  assert.match(failed, /ファイル: 取得失敗/); assert.match(failed, /理由: ダウンロードに失敗/); assert.match(failed, /原本を取得できていないため作成できません/);
  assert.doesNotMatch(failed, /0 B/);
  assert.match(entry({attachment_id: 2, state: "downloaded", bytes: 2048, error: "not_recorded", ocr: {state: "failed"}}), /文字記録: 作成に失敗[\s\S]*2 KB/);
  assert.match(entry({attachment_id: 3, state: "downloaded", bytes: 10, ocr: {state: "unsupported"}}), /対応していない形式/);
  const unknownError = entry({attachment_id: 4, state: "failed", error: "something_new", ocr: {}});
  assert.match(unknownError, /文字記録: 状態を確認できません/); assert.match(unknownError, /理由: 理由を判別できません/); assert.doesNotMatch(unknownError, /something_new/);
  assert.match(entry({attachment_id: 8, state: "failed", error: "http_404", ocr: {}}), /理由: MCS サーバーのエラー（HTTP 404）/);
  assert.match(entry({attachment_id: 9, state: "pending", error: "rate_limited", ocr: {}}), /理由: MCS サーバーから時間をおくよう求められました/);
  const rejected = run("attachmentEntry")({attachment_id: 5, state: "downloaded", bytes: 10, ocr: {state: "rejected_by_policy", original_project_url: "https://example.invalid/project/101"}});
  const link = find(rejected, node => node.tagName === "a");
  assert.equal(link.href, "https://example.invalid/project/101"); assert.equal(link.rel, "noreferrer noopener");
  assert.match(content(rejected), /表の読み取りが不確かなため除外/);
  const unsafe = run("attachmentEntry")({attachment_id: 6, state: "downloaded", ocr: {state: "rejected_by_policy", original_project_url: "file:///Users/synthetic/a.pdf"}});
  assert.equal(find(unsafe, node => node.tagName === "a"), null, "never a link to a local file");
  const read = run("attachmentEntry")({attachment_id: 7, state: "pruned", bytes: 3145728, ocr: {state: "complete", original_available: false,
    pages: [{page: 1, text: "合成の1ページ目"}, {page: 2, text: ""}, {page: 3, text: null}],
    medical: {sections: [{heading: "傷病名"}, {heading: "傷病名"}, {heading: null}], pump_candidates: [{page: 1, label: "持続量", raw_value: "1mL/h", status: "needs_review"}]}}});
  const readText = content(read);
  assert.match(readText, /3\.0 MB/); assert.match(readText, /保持期間を過ぎています/);
  assert.match(readText, /1ページ[\s\S]*合成の1ページ目[\s\S]*2ページ[\s\S]*（文字なし）[\s\S]*3ページ[\s\S]*記録されていません/);
  assert.match(readText, /文書の見出し: 傷病名\n/); assert.match(readText, /1ページ 持続量: 1mL\/h[\s\S]*要確認/);
  // F3: physicians' 見ました is 未観測 (never 未読); unknown says so, with the reason in the detail.
  const viewed = (value, detailed = false) => {const node = run("physicianChip")(value, detailed); return node && node.textContent;};
  assert.equal(viewed(null), null);
  assert.equal(viewed({state: "known", physicians: 2, viewed_observed: 1, not_observed: 1}), "医師 見ました未観測 1/2");
  assert.equal(viewed({state: "known", physicians: 0, not_observed: 0}), null);
  assert.equal(viewed({state: "unknown", reason: "care_team_not_current"}), "医師 見ました 確認できません");
  assert.equal(viewed({state: "unknown", reason: "care_team_not_current"}, true), "医師 見ました 確認できません（ケアチーム情報が最新ではありません）");
  const own = run("post")({message_id: 1, body_state: "full", body_complete: true, body_text: "合成", physician_viewed: {state: "known", physicians: 3, not_observed: 2}});
  assert.match(content(own), /医師 見ました未観測 2\/3/); assert.doesNotMatch(content(own), /未読/);
  // F3: the year appears when it is not the current one.
  assert.match(run("postTime")(1700000000), /2023/);
  assert.doesNotMatch(run("postTime")(Date.now() / 1000), new RegExp(String(new Date().getFullYear())));
  // F3: browser errors are worded for the reader.
  sandbox.api = realApi;
  sandbox.fetch = async () => {throw new TypeError("Failed to fetch");};
  await assert.rejects(run("api")("timeline"), /ローカルのサーバーに接続できませんでした/);
  sandbox.fetch = async () => ({ok: true, json: async () => {throw new SyntaxError("Unexpected token <");}});
  await assert.rejects(run("api")("timeline"), /応答を読み取れませんでした/);
  // F3: snapshot age and warnings.
  run("state.generation = null");
  sandbox.fetch = async () => ({ok: true, json: async () => ({snapshot: {generation_id: "g", generated_at: Date.now() / 1000 - 600}, warnings: ["history_completeness_unverified", "synthetic_new_warning"], items: []})});
  await run("api")("timeline");
  assert.match(byId("snapshot-time").textContent, /スナップショット .* JST · 10分前/);
  assert.match(byId("snapshot-note").textContent, /（10分前）時点[\s\S]*すべて取得できたかは確認できていません[\s\S]*synthetic_new_warning/);
  sandbox.fetch = () => new Promise(() => {});
  // F4: a context item opens the full record at its group, by keyboard or touch.
  run("state.project = 101");
  sandbox.api = async () => ({sections: [{label: "合成の分類", items: [{subject: "patient", text: "合成の長い記録", posted_at_ts: 1793491200}]}]});
  await run("loadPatientContext(101)");
  const panel = byId("patient-context"), brief = find(panel, node => node.className === "context-brief");
  assert.equal(brief.tagName, "button");
  brief.onclick();
  const details = find(panel, node => node.className === "context-records");
  assert.equal(details.open, true);
  assert.match(content(document.activeElement), /合成の分類[\s\S]*合成の長い記録/);
}
async function checkMcsData() {
  const run = code => vm.runInContext(code, sandbox), byId = id => document.getElementById(id);
  const texts = nodes => nodes.map(node => node.textContent).join("|");
  // 1. Post row: urgency with reasons, summary state, self/own station, latest reply, observed flags and changes.
  const row = {message_id: 11, body_state: "full", body_complete: true, body_text: "合成", sender_name: "合成 看護", sender_self: true, sender_own_station: true,
    urgency: {level: "high", label: "至急", qc_note: "？", reasons: ["合成の根拠"], review_label: "要確認"}, summary_state_label: "要約 作成失敗", summary_state: "failed",
    last_reply: {posted_at_ts: 1793491200, sender_name: "合成 薬剤", profession: "薬剤師", complete: false},
    flags: {mention_self: {value: true}, pinned: {value: null, state: "not_fetched"}, bookmarked: {value: false}},
    change_observed_at: 1793491200, deleted_observed_at: null,
    stamps: {state: "observed", self: "👍", counts: [{emoji: "👍", count: 1}], actors: {state: "stale", items: [{emoji: "👍", name: "合成 医師", self: false}], observed_at: 1793491200}}};
  const plain = content(run("post")(row)), detailed = content(run("post")(row, true));
  assert.match(plain, /合成 看護（自分）/); assert.doesNotMatch(plain, /自事業所/, "self is not repeated as own station");
  assert.match(plain, /至急？/); assert.match(plain, /要確認/); assert.doesNotMatch(plain, /AI|判定/);
  assert.match(detailed, /緊急度の根拠: 「合成の根拠」/);
  assert.match(plain, /要約 作成失敗/); assert.doesNotMatch(plain, /要約なし/);
  assert.match(plain, /最新返信 .* 合成 薬剤・薬剤師（未取得の返信あり）/);
  assert.match(plain, /@自分宛て/); assert.doesNotMatch(plain, /ピン留め|ブックマーク/);
  assert.match(plain, /変更を検知（取得側の観測 /); assert.doesNotMatch(plain, /削除を観測/);
  assert.match(plain, /自分のスタンプ 👍/);
  assert.match(detailed, /押した人（古い情報）: 👍 合成 医師/);
  assert.equal(texts(run("urgencyChips")({level: "low", label: null, review_label: null})), "");
  const other = content(run("post")({message_id: 12, body_state: "full", body_complete: true, body_text: "合成", sender_name: "合成 事務", sender_own_station: true, deleted_observed_at: 1793491200}));
  assert.match(other, /自事業所/); assert.match(other, /削除を観測（取得側の観測 /);
  assert.equal(run("lastReplyChip")({posted_at_ts: 1793491200, sender_name: "合成", complete: true}).className, "chip chip-muted");
  // 2. Stamps: each actor-list state is said in the detail.
  const actors = state => texts(run("stampDetail")({state: "observed", counts: [{emoji: "👍", count: 1}], actors: {state}}));
  assert.equal(actors("not_fetched"), "押した人: 未取得"); assert.equal(actors("failed"), "押した人: 取得失敗");
  assert.equal(texts(run("stampDetail")({state: "invalid"})), "スタンプの記録を読み取れませんでした。");
  // 3/4. Identity and freshness on the heading and list.
  assert.equal(run("patientTitle")({heading: "合成 花子（合成施設）", patient_name: "合成 花子", project_id: 1}), "合成 花子（合成施設）");
  assert.equal(run("placeNames")({heading: "合成 花子（合成施設）", station_name: "合成施設", group_name: "合成グループ"}).join("|"), "合成グループ");
  const fetch = patient => texts(run("fetchChips")({history_floor: -1, ...patient}));
  assert.equal(fetch({fetch_state: "complete"}), "");
  assert.equal(fetch({fetch_state: "incomplete", fetch_reason_label: "通信エラー"}), "一部未取得（通信エラー）");
  assert.equal(fetch({fetch_state: "forbidden"}), "閲覧権限なし");
  assert.equal(fetch({fetch_state: "complete", initial_import: "capturing"}), "初回取込中");
  assert.equal(fetch({fetch_state: "complete", initial_import: "held"}), "初回取込 保留");
  assert.equal(fetch({fetch_state: "complete", initial_import: "unknown"}), "初回取込 状態不明");
  assert.equal(fetch({fetch_state: "complete", history_floor: 5}), "履歴全体の取得は未確認");
  assert.match(run("fetchChips")({fetch_state: "failed", history_floor: -1, last_complete_fetch: null})[0].title, /完了した取得の記録なし/);
  assert.equal(run("mcsRoomLink")("https://example.invalid/projects/1")[0].rel, "noreferrer noopener");
  assert.equal(run("mcsRoomLink")("https://example.invalid/projects/1")[0].target, "_blank");
  assert.equal(run("mcsRoomLink")("file:///synthetic").length, 0); assert.equal(run("mcsRoomLink")("http://example.invalid").length, 0);
  // 5. Counts: partial is never 0件, failed is 取得できません; list badges likewise.
  const counts = data => texts(run("countChips")(data));
  assert.equal(counts({open_signals: {state: "confirmed", count: 2}, replies: {state: "observed", requests: 3, not_found: 1, unknown: 1}}), "未解消シグナル 2|返答確認 依頼 3（返答が見つからない 1・確認できない 1）");
  assert.equal(counts({open_signals: {state: "partial", count: 0}, replies: {state: "failed"}}), "未解消シグナル 判定中|返答確認 取得できません");
  assert.equal(counts({open_signals: {state: "partial", count: 2}, replies: {state: "observed", requests: 0}}), "未解消シグナル 2以上（判定中）|返答確認 依頼 0");
  assert.equal(counts({open_signals: {state: "failed", count: null}, replies: {}}), "未解消シグナル 取得できません|返答確認 取得できません");
  // Server values added in parallel: partial replies and the partial-signal reason; an unknown state is never read as confirmed.
  assert.equal(counts({open_signals: {state: "partial", count: 0, reason: "unreadable"}, replies: {state: "partial", requests: 2, not_found: 1}}), "未解消シグナル 一部読み取れません|返答確認 判定中（依頼 2以上・返答が見つからない 1）");
  assert.equal(counts({open_signals: {state: "partial", count: 1, reason: "pending"}, replies: {state: "partial", requests: 0}}), "未解消シグナル 1以上（判定中）|返答確認 判定中（依頼 0以上）");
  assert.match(run("countChips")({open_signals: {state: "partial", count: 0, reason: "unreadable"}, replies: {}})[0].title, /一部のシグナルの状態を読み取れません/);
  assert.equal(counts({open_signals: {state: "synthetic_new", count: 0}, replies: {state: "synthetic_new", requests: 0}}), "未解消シグナル 取得できません|返答確認 取得できません");
  assert.equal(counts({open_signals: {state: "complete", count: 0}, replies: {}}), "未解消シグナル 取得できません|返答確認 取得できません");
  const badge = signals => run("signalBadge")(signals)?.textContent ?? null;
  assert.equal(badge({state: "confirmed", count: 0}), null); assert.equal(badge({state: "confirmed", count: 3}), "シグナル 3");
  assert.equal(badge({state: "synthetic_new", count: 0}), "シグナル 不明"); assert.equal(badge({state: "partial", count: 0, reason: "unreadable"}), "シグナル 一部不明");
  assert.equal(badge({state: "partial", count: 0}), "シグナル 判定中"); assert.equal(badge({state: "partial", count: 1}), "シグナル 1+");
  assert.equal(badge({state: "failed", count: null}), "シグナル 不明");
  run("state.project = 101; state.countBox = el('span'); state.countRequest = 0");
  let shown = null; run("showView = view => {globalThis.shownView = view;}"); // stubbed navigation
  sandbox.api = async kind => {assert.equal(kind, "counts"); return {open_signals: {state: "confirmed", count: 1}, replies: {state: "observed", requests: 1}};};
  await run("loadCounts()");
  const box = run("state.countBox");
  box.children[1].onclick(); shown = run("globalThis.shownView");
  assert.equal(shown, "replies"); assert.equal(box.children[0].tagName, "button");
  sandbox.api = async () => {throw new Error("x");};
  await run("loadCounts()");
  assert.equal(texts(box.children), "未解消シグナル 取得できません|返答確認 取得できません");
  // Reply checks on the list row: not_computed is 未集計, never 0; partial is 判定中.
  const reply = replies => run("replyBadge")(replies)?.textContent ?? null;
  assert.equal(reply({state: "observed", requests: 3, not_found: 0, unknown: 0}), null);
  assert.equal(reply({state: "observed", requests: 3, not_found: 1, unknown: 1}), "返答確認 2");
  assert.equal(reply({state: "partial", requests: 1, not_found: 0, unknown: 0}), "返答確認 判定中");
  assert.equal(reply({state: "partial", requests: 2, not_found: 1, unknown: 0}), "返答確認 1+");
  assert.equal(reply({state: "not_computed", requests: null, not_found: null, unknown: null}), "返答確認 未集計");
  assert.equal(reply({state: "failed", requests: null}), "返答確認 不明"); assert.equal(reply({state: "synthetic_new", requests: 0}), "返答確認 不明");
  run("state.patients = [{project_id: 3, heading: '合成 三', message_count: 1, replies: {state: 'not_computed', requests: null}, open_signals: {state: 'confirmed', count: 0}}]; state.openPatient = null; patientList()");
  assert.match(content(byId("patients")), /返答確認 未集計/);
  // Summary states: attention and unknown are warnings next to pending/failed, on the row and in the detail.
  const summary = (value, label, detailed) => {const node = find(run("post")({message_id: 20, body_state: "full", body_complete: true, body_text: "合成", summary_state: value, summary_state_label: label}, detailed), node => node.textContent === label); return node && node.className;};
  assert.equal(summary("attention", "要約 要確認"), "chip chip-warn"); assert.equal(summary("unknown", "要約 状況不明", true), "chip chip-warn");
  assert.equal(summary("pending", "要約 処理待ち"), "chip chip-muted"); assert.equal(summary("failed", "要約 作成失敗", true), "chip chip-warn");
  // Attachment chip: OCR failed / unprocessed turn it amber; unreadable OCR settings stay off the row.
  const files = (groups, ocr) => {const node = run("attachmentChip")(groups, ocr); return node && `${node.className}|${node.textContent}`;};
  assert.equal(files([{state: "downloaded", count: 2}], [{state: "failed", count: 1}, {state: "unprocessed", count: 1}]), "chip chip-warn|添付 2件（OCR失敗 1・OCR未作成 1）");
  assert.equal(files([{state: "downloaded", count: 1}], [{state: "complete", count: 1}]), "chip chip-muted|添付 1件");
  assert.equal(files([{state: "downloaded", count: 1}], null), "chip chip-muted|添付 1件");
  assert.equal(files([{state: "downloaded", count: 1}]), "chip chip-muted|添付 1件");
  assert.equal(files([{state: "downloaded", count: 2}], [{state: "unknown", count: 2}]), "chip chip-muted|添付 2件", "unreadable settings are not an OCR failure");
  const unknownOcr = content(run("attachmentEntry")({attachment_id: 30, state: "downloaded", bytes: 10, ocr: {state: "unknown", reason: "config_unreadable"}}));
  assert.match(unknownOcr, /OCR 状態不明（設定を読み取れません）/); assert.doesNotMatch(unknownOcr, /失敗|未取得|未作成/);
  const unknownSummary = find(run("post")({message_id: 23, body_state: "full", body_complete: true, body_text: "合成", summary_state: "unknown", summary_state_label: "要約 状況不明"}), node => node.textContent === "要約 状況不明");
  assert.match(unknownSummary.title, /設定ファイルを読み取れない/);
  sandbox.api = async kind => kind === "attachments" ? {items: [], next_cursor: null} : {message: {}};
  run("state.detailRequest = 50; state.project = 101");
  await run("showMessageInfo({project_id: 101, message_id: 21, attachments_ocr: null}, 50, false)");
  assert.match(content(byId("message-info")), /OCR状態を読み取れません/);
  await run("showMessageInfo({project_id: 101, message_id: 22, attachments_ocr: []}, 50, false)");
  assert.doesNotMatch(content(byId("message-info")), /OCR状態/);
  // 7. 連携サマリー: the four states and a failed latest fetch.
  const karte = value => content(run("karteCard")(value));
  assert.match(karte({state: "present", comment: "合成の連携コメント", updated_at: "2026-11-01", updater_profession: "看護師"}), /あり[\s\S]*合成の連携コメント[\s\S]*MCS の更新 2026-11-01 · 更新した職種 看護師/);
  assert.match(karte({state: "present", comment: "合成", failed_at: 1793491200}), /前回取得した内容を表示しています/);
  assert.match(karte({state: "not_fetched"}), /未取得[\s\S]*まだ取得していません/);
  assert.match(karte({state: "failed", failed_at: 1793491200}), /取得失敗[\s\S]*一度も取得できていません/);
  assert.match(karte({state: "empty"}), /空（MCS に記載がありません）[\s\S]*空でした/);
  // A stored summary that cannot be confirmed for the current karte shows nothing, and a failed latest fetch
  // never claims the previous content is shown.
  const unconfirmed = karte({state: "unknown", comment: null, failed_at: 1793491200});
  assert.match(unconfirmed, /未確認[\s\S]*確認できないため、表示できる内容はありません[\s\S]*最新の取得に失敗しました/);
  assert.doesNotMatch(unconfirmed, /前回取得した内容/);
  assert.match(karte({state: "empty", failed_at: 1793491200}), /空でした[\s\S]*前回取得した内容を表示しています/);
  assert.match(karte(null), /取得できませんでした/);
  // 9. Search: source and page, notes, empty, 続きを検索, unavailable.
  run("state.project = 101; state.query = '合成'; state.panel = ''");
  const pages = [{state: "searched", title: "検索結果", head: [], items: [{source: "chat", text: "合成の本文", message_id: 1}, {source: "ocr", page: 2, text: "合成のOCR", message_id: 2}], notes: ["添付の一部は未処理です。"], next_cursor: "c1", range_complete: false},
    {state: "searched", items: [{source: "ocr", page: null, text: "合成の続き", message_id: 3}], notes: [], next_cursor: null, range_complete: true}];
  const cursors = [], senderCalls = [];
  const posts = [{items: [{message_id: 8, body_state: "full", body_complete: true, body_text: "合成の送信者の投稿", sender_name: "合成 太郎"}], next_cursor: "s1"},
    {items: [{message_id: 9, body_state: "full", body_complete: true, body_text: "合成の送信者の古い投稿", sender_name: "合成 太郎"}], next_cursor: null}];
  sandbox.api = async (kind, params) => {if (kind === "senders") {assert.equal(params.q, "合成"); senderCalls.push(params.cursor); return posts.shift();} assert.equal(kind, "sources"); cursors.push(params.cursor); return pages.shift();};
  await run("loadSearch()");
  let view = content(byId("panel-view"));
  assert.match(view, /合成の本文[\s\S]*本文[\s\S]*合成のOCR[\s\S]*添付OCR 2ページ/); assert.match(view, /添付の一部は未処理です/);
  assert.equal(run("state.search.more").hidden, false);
  await run("state.search.more.onclick()");
  assert.equal(cursors.join("|"), "|c1"); view = content(byId("panel-view"));
  assert.match(view, /合成のOCR[\s\S]*合成の続き\n\n添付OCR\n/); assert.equal(run("state.search.more").hidden, true);
  // Sender matches come from api/senders, with their own 続きを表示.
  const senders = find(byId("panel-view"), node => node.className === "sender-matches");
  assert.match(content(senders), /投稿者名が一致する投稿[\s\S]*合成の送信者の投稿/);
  const senderMore = find(senders, node => node.textContent === "続きを表示");
  assert.equal(senderMore.hidden, false);
  await senderMore.onclick();
  assert.match(content(senders), /合成の送信者の投稿[\s\S]*合成の送信者の古い投稿/); assert.equal(senderMore.hidden, true);
  assert.equal(senderCalls.join("|"), "|s1", "senders pages on its own cursor, not on 続きを検索");
  // Notes of every range searched so far: a running skipped total, the latest notes, earlier notes kept.
  const ranged = [{state: "searched", items: [], notes: ["まだ調べていない範囲があります。「続きを検索」で進めます。", "合成の前の範囲の注記"], skipped: {ocr_pending: 2, body_unavailable: 1, unclassified: 0}, next_cursor: "r1", range_complete: false},
    {state: "searched", items: [], notes: ["まだ調べていない範囲があります。「続きを検索」で進めます。"], skipped: {ocr_pending: 1, body_unavailable: null, unclassified: 3}, next_cursor: "r2", range_complete: false},
    {state: "searched", items: [], notes: ["合成の最後の注記"], skipped: null, next_cursor: null, range_complete: true}];
  sandbox.api = async kind => kind === "senders" ? {items: [], next_cursor: null} : ranged.shift();
  await run("loadSearch()");
  await run("state.search.more.onclick()");
  let notes = texts(run("state.search.notes").children);
  assert.match(notes, /ここまでの範囲で: OCR未完了 3件 \/ 本文未取得 1件（件数の報告がない範囲を除く） \/ 未分類 3件(\||$)/);
  assert.match(notes, /合成の前の範囲の注記/); assert.equal((notes.match(/まだ調べていない範囲/g) || []).length, 1);
  await run("state.search.more.onclick()");
  notes = texts(run("state.search.notes").children);
  assert.match(notes, /件数の報告がない範囲を除く/); assert.match(notes, /合成の最後の注記[\s\S]*合成の前の範囲の注記/);
  assert.doesNotMatch(notes, /以上/, "an unreported count is excluded, not a lower bound");
  // OCR not examined (settings unreadable): null in every range never reads as 「OCR未完了 0件」.
  const unexamined = [{state: "searched", items: [], notes: ["合成のOCR未確認の注記"], skipped: {ocr_pending: null, body_unavailable: 0, unclassified: 0}, next_cursor: null, range_complete: true}];
  sandbox.api = async kind => kind === "senders" ? {items: [], next_cursor: null} : unexamined.shift();
  await run("loadSearch()");
  notes = texts(run("state.search.notes").children);
  assert.match(notes, /ここまでの範囲で: 本文未取得 0件 \/ 未分類 0件(\||$)/); assert.doesNotMatch(notes, /OCR未完了/);
  assert.doesNotMatch(notes, /まだ調べていない範囲/, "only the latest range says a range is left");
  // A query with no sender word skips the block; other failures say so.
  sandbox.api = async kind => {if (kind === "senders") throw Object.assign(new Error("x"), {code: "bad_request"}); return {state: "searched", items: [], notes: [], next_cursor: null};};
  await run("loadSearch()");
  assert.equal(find(byId("panel-view"), node => node.className === "sender-matches"), null); assert.doesNotMatch(content(byId("panel-view")), /投稿者名での検索/);
  // 0 hits with a cursor, then hits: the empty line says "so far" and goes away when an item arrives.
  const later = [{state: "searched", items: [], notes: [], next_cursor: "c2", range_complete: false, empty: "合成の一致なし"},
    {state: "searched", items: [{source: "chat", text: "合成の後の一致", message_id: 4}], notes: [], next_cursor: null, range_complete: true}];
  sandbox.api = async kind => kind === "senders" ? {items: [], next_cursor: null} : later.shift();
  await run("loadSearch()");
  assert.match(content(byId("panel-view")), /ここまでに検索した範囲では見つかりませんでした/);
  assert.equal(find(byId("panel-view"), node => node.className === "sender-matches"), null);
  await run("state.search.more.onclick()");
  view = content(byId("panel-view"));
  assert.match(view, /合成の後の一致/); assert.doesNotMatch(view, /見つかりませんでした|合成の一致なし/); assert.equal(run("state.search.empty").hidden, true);
  sandbox.api = async kind => kind === "senders" ? {items: [], next_cursor: null} : {state: "searched", items: [], notes: [], next_cursor: null, range_complete: true, empty: "合成の一致なし。"};
  await run("loadSearch()");
  assert.match(content(byId("panel-view")), /合成の一致なし。見つからないことは、記録がないことを意味しません。/);
  sandbox.api = async kind => {if (kind === "senders") throw Object.assign(new Error("x"), {code: "cursor_scope_or_generation_changed"}); return {state: "searched", items: [], notes: [], next_cursor: null};};
  await run("loadSearch()");
  assert.match(content(byId("panel-view")), /投稿者名での検索はできませんでした/);
  // Punctuation is a search word: it is searched, not refused.
  run("state.query = '、。'");
  sandbox.api = async (kind, params) => {assert.equal(params.q, "、。"); return kind === "senders" ? {items: [], next_cursor: null} : {state: "searched", items: [], notes: [], next_cursor: null, range_complete: true, empty: "合成の一致なし。"};};
  await run("loadSearch()");
  assert.match(content(byId("panel-view")), /合成の一致なし。/); assert.doesNotMatch(content(byId("panel-view")), /語句を入れてください/);
  run("state.query = '合成'");
  sandbox.api = async kind => kind === "senders" ? {items: [], next_cursor: null} : {state: "unavailable", items: [], notes: [], empty: "合成の検索していない理由"};
  await run("loadSearch()");
  assert.match(content(byId("panel-view")), /合成の検索していない理由/);
  assert.equal(run("errorTexts.bad_search_filter").includes("検索の条件"), true);
  assert.equal(run("errorTexts.search_cursor_invalid").includes("もう一度検索"), true);
  assert.equal(run("errorTexts.config_unreadable"), "設定を読み取れないため表示できません。");
  sandbox.api = realApi;
  sandbox.fetch = async () => ({ok: false, json: async () => ({error: "bad_request"})});
  await assert.rejects(run("api")("senders"), error => error.code === "bad_request");
  sandbox.fetch = () => new Promise(() => {});
  assert.match(run("errorTexts.search_query_empty"), /語句[\s\S]*資料: 分類: 開始: 終了:/);
  // 8. MCS登録データ: separate record, states, compact rows and 他N件.
  const registered = data => content(run("renderRegistered")(data));
  assert.match(registered({state: "unavailable", collection_enabled: true}), /チャットの投稿とは別の記録[\s\S]*読み取れません/);
  assert.match(registered({state: "not_linked", collection_enabled: false, datasets: []}), /収集は設定で無効[\s\S]*取得していません/);
  const unreadable = registered({state: "not_linked", collection_enabled: null, config_state: "unreadable", datasets: []});
  assert.match(unreadable, /設定を読み取れません/); assert.doesNotMatch(unreadable, /無効/);
  const missing = registered({state: "not_linked", collection_enabled: false, collection_reason: "config_missing", config_state: "missing", datasets: []});
  assert.match(missing, /設定ファイルがないため、登録データは収集していません/); assert.doesNotMatch(missing, /無効/);
  const pending = run("renderRegistered")({state: "listed", collection_enabled: true, datasets: [
    {dataset: "observation_values", label: "観測値", state: "complete", state_label: "取得済み", item: {name: "合成値A", unit: "mg"}, rows: [{issued_at: "2026-01-01", scalar: 1}], rows_total: 1},
    {dataset: "observation_values", label: "観測値", state: "unknown", state_label: "未取得・未確認", item: {name: "合成値B", unit: "mmHg", reference: {upper_reference_limit_scalar: 9}}, rows: [], rows_total: 0},
    {dataset: "observation_values", label: "観測値", state: "unknown", state_label: "未取得・未確認", item: {name: "合成値C", unit: null, reference: {}}, rows: [], rows_total: 0}]});
  const pendingText = content(pending);
  assert.match(pendingText, /観測値（未取得の項目）[\s\S]*未取得 2項目[\s\S]*合成値B[\s\S]*mmHg[\s\S]*〜9[\s\S]*未取得[\s\S]*合成値C[\s\S]*未取得/);
  assert.equal(find(pending, node => node.className === "cards").children.length, 2, "unfetched items share one card");
  const listed = run("renderRegistered")({state: "listed", collection_enabled: true, datasets: [
    {dataset: "medication_periods", label: "合成の服薬期間", state: "complete", state_label: "取得済み", rows: [{begin_date: "2026-01-01", end_date: null, medicines: ["合成薬A"]}], rows_total: 3},
    {dataset: "observation_values", label: "合成の測定値", state: "failed", state_label: "取得失敗", reason: "http_error", rows: [], rows_total: 0},
    {dataset: "observation_items", label: "合成の項目", state: "unknown", state_label: "未確認", reason: "synthetic_new_reason", rows: [], rows_total: 0}]});
  const listedText = content(listed);
  assert.match(listedText, /合成の服薬期間[\s\S]*取得済み[\s\S]*開始[\s\S]*2026-01-01[\s\S]*未記録[\s\S]*合成薬A[\s\S]*他2件（先頭 1 件を表示）/);
  assert.match(listedText, /合成の測定値[\s\S]*取得失敗[\s\S]*行を表示できません（取得失敗）/);
  assert.ok(find(listed, node => node.tagName === "table"));
  assert.match(listedText, /理由: MCS がエラーを返しました/); assert.match(listedText, /理由: 理由を判別できません/);
  assert.doesNotMatch(listedText, /http_error|synthetic_new_reason|理由コード/);
  // 10. The open patient is remembered by #project=N and found even off the first page.
  sandbox.location.hash = "#project=205";
  run("state.patients = [{project_id: 1}]");
  sandbox.api = async (kind, params) => {assert.equal(params.q, "205"); return {items: [{project_id: 2050}, {project_id: 205, heading: "合成"}]};};
  assert.equal((await run("rememberedPatient()")).project_id, 205);
  sandbox.location.hash = "#other";
  assert.equal(await run("rememberedPatient()"), null);
  sandbox.location.hash = "";
  run("state.patients = [{project_id: 1, heading: '合成 一', message_count: 1}]; state.project = 205; state.openPatient = {project_id: 205, heading: '合成 二', message_count: 2}; patientList()");
  const listRows = byId("patients").children;
  assert.equal(listRows[0].className, "patient selected"); assert.match(content(listRows[0]), /合成 二/); assert.equal(listRows.length, 2);
  // 記録の状態 lists each flag, including the ones not fetched.
  const lines = texts(run("flagLines")({mention_self: {value: null, state: "self_unknown"}, pinned: {value: null, state: "not_fetched"}, bookmarked: {value: false, state: "observed"}}));
  assert.equal(lines, "@自分宛て: 自分のアカウントを特定できません|ピン留め: 未取得|ブックマーク: なし");
  assert.equal(texts(run("flagLines")({})), "@自分宛て: 状態不明|ピン留め: 状態不明|ブックマーク: 状態不明");
}

async function checkOpenMcsPatientSelection() {
  const run = code => vm.runInContext(code, sandbox), byId = id => document.getElementById(id);
  const p11 = {project_id: 11, heading: "合成 十一", message_count: 1}, p22 = {project_id: 22, heading: "合成 二十二", message_count: 1}, p33 = {project_id: 33, heading: "合成 三十三", message_count: 1};
  run("hc.enabled = true; hc.meta = {qualifications: {}, roles: {}, visit_kinds: {}}; state.project = 11; state.openPatient = null", sandbox);
  run("loadMain = async () => {}; loadPatientContext = async () => {}; loadSideStatus = async () => {}; loadCounts = async () => {}", sandbox);
  run("state.patients = [{project_id: 22, heading: '合成 旧二十二', message_count: 1}]", sandbox);
  sandbox.api = async (kind, params) => ({items: params.q === "22" ? [] : [p11], next_cursor: null});
  await run("openMcs(22)");
  assert.equal(run("state.project", sandbox), null);
  assert.equal(byId("patient-name").textContent, "患者が見つかりません");
  assert.equal(byId("patient-meta").textContent, "ID 22");
  assert.match(byId("timeline-status").textContent, /見つかりません/);

  const filtered = {project_id: 44, heading: "合成 旧検索", message_count: 1};
  byId("patient-search").value = "旧検索";
  sandbox.api = async () => ({items: [filtered], next_cursor: "old-search-page"});
  await run("loadPatients()");
  assert.match(content(byId("patients")), /旧検索/);
  sandbox.api = async (kind, params) => {
    if (kind === "brief") return {sections: []};
    if (kind === "hc/patients") return {items: []};
    if (params.q === "22" && !params.cursor) return {items: [], next_cursor: "next"};
    if (params.q === "22" && params.cursor === "next") return {items: [{...p22, heading: "合成 新二十二"}], next_cursor: null};
    assert.equal(params.q, "");
    if (!params.cursor) return {items: [p11], next_cursor: "all-patients-page"};
    assert.equal(params.cursor, "all-patients-page", "an unfiltered page uses its own cursor");
    return {items: [p33], next_cursor: null};
  };
  await run('setMode("pharmacy")');
  const card = run('mcsCard({project_id: 22, patient_name: "合成 二十二"}, () => true)');
  await button(card, "タイムラインを開く").onclick();
  assert.equal(byId("patient-search").value, "");
  assert.equal(run("state.project"), 22);
  assert.equal(run("state.openPatient.heading"), "合成 新二十二");
  assert.match(content(byId("patients")), /合成 十一/);
  assert.doesNotMatch(content(byId("patients")), /合成 旧検索/);
  assert.equal(byId("more-patients").hidden, false);
  await byId("more-patients").onclick();
  assert.match(content(byId("patients")), /合成 三十三/);
  assert.equal(byId("more-patients").hidden, true);
  assert.equal(run("state.project"), 22);

  let answer22;
  sandbox.api = async (kind, params) => {
    if (kind === "brief") return {sections: []};
    if (params.q === "22") return new Promise(resolve => {answer22 = resolve;});
    if (params.q === "33") return {items: [p33], next_cursor: null};
    return {items: [], next_cursor: null};
  };
  const first = button(card, "タイムラインを開く").onclick();
  for (let i = 0; i < 20 && !answer22; i++) await Promise.resolve();
  assert.equal(typeof answer22, "function");
  const secondCard = run('mcsCard({project_id: 33, patient_name: "合成 三十三"}, () => true)');
  const second = button(secondCard, "タイムラインを開く").onclick();
  await second;
  assert.equal(run("state.project", sandbox), 33);
  answer22({items: [p22], next_cursor: null});
  await first;
  assert.equal(run("state.project", sandbox), 33);

  // A later search owns the sidebar and selection even if the linked lookup then fails.
  let rejectOld;
  sandbox.api = async (kind, params) => {
    if (kind === "brief") return {sections: []};
    if (params.q === "22") return new Promise((resolve, reject) => {rejectOld = reject;});
    return {items: params.q ? [p33] : [p11], next_cursor: params.q ? null : "new-page"};
  };
  const stale = button(card, "タイムラインを開く").onclick();
  for (let i = 0; i < 20 && !rejectOld; i++) await Promise.resolve();
  assert.equal(typeof rejectOld, "function");
  assert.equal(byId("more-patients").hidden, true, "the sidebar opens with its linked selection");
  assert.match(content(byId("patients")), /読み込んでいます/);
  assert.equal(byId("patient-count").textContent, "");
  byId("patient-search").value = "三十三";
  byId("patient-form").onsubmit({preventDefault() {}});
  for (let i = 0; i < 20 && run("state.project") !== 33; i++) await Promise.resolve();
  assert.equal(run("state.project"), 33);
  byId("error").hidden = true;
  rejectOld(new Error("合成 旧リンクの失敗"));
  await stale;
  assert.equal(byId("error").hidden, true);
  assert.match(content(byId("patients")), /合成 三十三/);

  // The first-page request can fail while the individually authorized linked patient opens.
  let rejectList;
  sandbox.api = async (kind, params) => {
    if (params.q === "") return new Promise((resolve, reject) => {rejectList = reject;});
    return {items: params.q === "22" ? [p22] : [filtered], next_cursor: params.q === "22" ? null : "old-search-page"};
  };
  byId("patient-search").value = "旧検索";
  await run("loadPatients()");
  const linked = button(card, "タイムラインを開く").onclick();
  rejectList(new Error("合成 一覧エラー"));
  await linked;
  assert.equal(run("state.project"), 22);
  assert.equal(byId("patient-name").textContent, p22.heading);
  assert.equal(byId("patient-count").textContent, "");
  assert.match(content(byId("patients")), /患者一覧を取得できません/);
  assert.match(content(byId("patients")), /合成 二十二/);
  assert.doesNotMatch(content(byId("patients")), /旧検索|該当する患者はいません/);
  assert.equal(byId("more-patients").hidden, true);

  // A newer successful search replaces both the error and cursor, even if an earlier list fails late.
  const obsolete = button(card, "タイムラインを開く").onclick();
  byId("patient-search").value = "三十三";
  sandbox.api = async (kind, params) => {
    assert.equal(params.q, "三十三"); assert.equal(params.cursor, null);
    return {items: [p33], next_cursor: null};
  };
  byId("patient-form").onsubmit({preventDefault() {}});
  for (let i = 0; i < 20 && run("state.project") !== 33; i++) await Promise.resolve();
  rejectList(new Error("合成 古い一覧エラー"));
  await obsolete;
  assert.equal(run("state.project"), 33);
  assert.equal(byId("patient-count").textContent, "1");
  assert.match(content(byId("patients")), /合成 三十三/);
  assert.doesNotMatch(content(byId("patients")), /取得できません|旧検索|一覧エラー/);
  assert.equal(byId("error").hidden, true);
  sandbox.api = async () => ({items: [], next_cursor: null});
  await run("loadPatients()");
  assert.equal(byId("patient-count").textContent, "0");
  assert.match(content(byId("patients")), /該当する患者はいません/);
}

async function checkPatientSearchSelection() {
  const run = code => vm.runInContext(code, sandbox), byId = id => document.getElementById(id);
  const search = byId("patient-search"), rows = byId("patients"), more = byId("more-patients");
  const patient = id => ({project_id: id, heading: `合成 患者${id}`, message_count: 1});
  const submit = () => byId("patient-form").onsubmit({preventDefault() {}});
  let answer;
  const pages = [];
  sandbox.api = async (kind, {q, cursor}) => {
    if (kind === "hc/patients") return {items: []};
    if (q === "旧検索") return {items: [patient(11), patient(22)], next_cursor: "old-page"};
    pages.push([q, cursor]);
    if (cursor) return {items: [patient(55)], next_cursor: null};
    return new Promise(resolve => {answer = resolve;});
  };
  search.value = "旧検索"; submit(); await new Promise(setImmediate);
  search.value = "新検索"; submit();
  rows.children.find(row => content(row).includes("合成 患者22")).onclick();
  assert.equal(byId("patient-name").textContent, "合成 患者22");
  assert.equal(more.hidden, true);
  answer({items: [patient(33)], next_cursor: "new-page"}); await new Promise(setImmediate);
  assert.match(content(rows), /合成 患者33/);
  assert.doesNotMatch(content(rows), /合成 患者11|合成 患者22/);
  assert.equal(more.hidden, false);
  search.value = "";  // Editing the field does not submit another search.
  await more.onclick();
  assert.deepEqual(pages, [["新検索", null], ["新検索", "new-page"]]);
  assert.match(content(rows), /合成 患者33[\s\S]*合成 患者55/);
  assert.doesNotMatch(content(rows), /合成 患者22/);
  assert.equal(byId("patient-name").textContent, "合成 患者22");
  assert.equal(more.hidden, true);

  search.value = "切替前"; submit();
  await run('setMode("pharmacy")');
  answer({items: [patient(66)], next_cursor: "abandoned-page"}); await new Promise(setImmediate);
  assert.equal(byId("patient-name").textContent, "患者台帳");
  assert.doesNotMatch(content(rows), /合成 患者66/);
  assert.equal(more.hidden, true);
}


async function checkRouteStaffRace() {
  const run = code => vm.runInContext(code, sandbox);
  run('hc.enabled = true; hc.routes = true; hc.actor = 1; hc.csrf = "synthetic"; hc.staff = [{staff_id:1,name:"合成 一",active:true},{staff_id:2,name:"合成 二",active:true}]; hc.meta = {qualifications:{},roles:{},visit_kinds:{}}', sandbox);
  const route = run('routeCard("2026-11-27")', sandbox), form = route.card.querySelector("form"), select = form.querySelector("select"), result = route.card.querySelector(".route-result");
  assert.ok(form && select && result);
  let answer1, answer2;
  sandbox.fetch = (_url, options) => {
    const body = JSON.parse(options.body);
    return new Promise(resolve => {if (body.staff === 1) answer1 = resolve; else answer2 = resolve;})
      .then(plan => ({ok: true, json: async () => plan}));
  };
  select.value = "1";
  const first = form.onsubmit({preventDefault() {}});
  for (let i = 0; i < 20 && !answer1; i++) await Promise.resolve();
  select.value = "2";
  select.listeners.change();
  answer1({stops: [{patient_name: "合成一の患者", start_time: "10:00", arrival: "09:50"}], unscheduled: [], unlocated: []});
  await first;
  assert.doesNotMatch(content(result), /合成一の患者/);
  const second = form.onsubmit({preventDefault() {}});
  for (let i = 0; i < 20 && !answer2; i++) await Promise.resolve();
  answer2({stops: [{patient_name: "合成二の患者", start_time: "11:00", arrival: "10:50"}], unscheduled: [], unlocated: []});
  await second;
  assert.match(content(result), /合成二の患者/);
  assert.doesNotMatch(content(result), /合成一の患者/);
}

async function main() {await checkReplyRecords(); await checkOrphanReply(); await checkDatePagination(); await checkPatientContext(); await checkPanel(); await checkMedsOutOfOrder(); checkStateChips(); await checkPostNavigation(); await checkSideStatus(); await checkReviewFixes(); checkTabRow(); await checkDisplayGaps(); await checkMcsData(); await checkOpenMcsPatientSelection(); await checkPatientSearchSelection(); await checkRouteStaffRace(); console.log("local chat DOM contracts passed");}
main().catch(error => {console.error(error); process.exitCode = 1;});
