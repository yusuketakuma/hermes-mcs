"use strict";
// Dependency-free DOM contract checks, run by the Python test when Node is available.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(tag) {this.tagName = tag; this.children = []; this.className = ""; this.textContent = ""; this.hidden = false; this.value = ""; this.classList = {add(){}, remove(){}};}
  append(...nodes) {for (const node of nodes) {if (node.tagName === "fragment") this.append(...node.children); else this.children.push(node);}}
  prepend(node) {this.children.unshift(...(node.tagName === "fragment" ? node.children : [node]));}
  replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
  setAttribute() {}
}
const nodes = new Map();
const document = {getElementById(id) {if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id);}, createElement: tag => new Element(tag), createDocumentFragment: () => new Element("fragment")};
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
console.log("local chat DOM contracts passed");
