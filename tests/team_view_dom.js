// A small DOM for running the team view's script (levain/team/view.py JS) under node in the tests: elements, text
// nodes, attributes, classList, the few selectors the script uses, and an HTML serializer that escapes every text and
// attribute value. It has no HTML parser: nothing in it can turn a string into elements, so a test that finds an
// element proves the script created it.
"use strict";

const created = [];

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

class Text {
  constructor(t) { this.data = String(t); this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = new Map();
    this.listeners = {};
    this.value = "";
    this.hidden = false;
    this.dataset = {};
    this.style = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      add: (c) => { if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (c) => { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
      toggle: (c, on) => { (on === undefined ? !self.classList.contains(c) : on) ? self.classList.add(c) : self.classList.remove(c); },
    };
  }
  get className() { return this.attrs.get("class") || ""; }
  set className(v) { if (v) this.attrs.set("class", String(v)); else this.attrs.delete("class"); }
  get id() { return this.attrs.get("id") || ""; }
  set id(v) { this.attrs.set("id", String(v)); }
  get type() { return this.attrs.get("type") || ""; }
  set type(v) { this.attrs.set("type", String(v)); }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  get parentElement() { return this.parentNode; }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this; this.childNodes.push(c); return c;
  }
  removeChild(c) {
    const i = this.childNodes.indexOf(c);
    if (i >= 0) { this.childNodes.splice(i, 1); c.parentNode = null; }
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; if (v !== "") this.appendChild(new Text(v)); }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  dispatchEvent(e) { (this.listeners[e.type] || []).forEach((fn) => fn(e)); return true; }
  click() { this.dispatchEvent({ type: "click" }); }
  matches(sel) {
    // ".a.b", "tag", ".a:not([hidden])"; a descendant selector is handled by querySelectorAll
    let notHidden = false;
    if (sel.endsWith(":not([hidden])")) { notHidden = true; sel = sel.slice(0, -":not([hidden])".length); }
    if (notHidden && this.hidden) return false;
    if (sel.startsWith(".")) return sel.slice(1).split(".").every((c) => this.classList.contains(c));
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    return this.tagName === sel.toUpperCase();
  }
  _all(out) { for (const c of this.children) { out.push(c); c._all(out); } return out; }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    let scope = [this];
    for (const p of parts) {
      const next = [];
      for (const s of scope) for (const d of s._all([])) if (d.matches(p) && !next.includes(d)) next.push(d);
      scope = next;
    }
    return scope;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { let n = this; while (n && !(n instanceof Element && n.matches(sel))) n = n.parentNode; return n || null; }
  get scrollHeight() { return 0; }
  get clientHeight() { return 0; }
  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    let a = "";
    // class first, then id, then the rest in the order they were set
    const keys = [...this.attrs.keys()].sort((x, y) => (x === "class" ? -2 : x === "id" ? -1 : 0) - (y === "class" ? -2 : y === "id" ? -1 : 0));
    for (const k of keys) a += ` ${k}="${esc(this.attrs.get(k))}"`;
    if (this.hidden) a += " hidden";
    if (tag === "input") return `<input${a}>`;
    return `<${tag}${a}>` + this.childNodes.map((c) => c instanceof Element ? c.outerHTML : esc(c.data)).join("") + `</${tag}>`;
  }
}

function makeDocument() {
  const body = new Element("body");
  const deck = new Element("div"); deck.className = "deck";
  const status = new Element("p"); status.className = "note"; status.id = "status"; status.textContent = "Loading the team view…";
  const app = new Element("div"); app.id = "app";
  deck.appendChild(status); deck.appendChild(app); body.appendChild(deck);
  return {
    body, deck, title: "team view",
    createElement: (t) => { created.push(String(t).toLowerCase()); return new Element(t); },
    createTextNode: (t) => new Text(t),
    getElementById: (id) => (body.id === id ? body : body.querySelector("#" + id)),
    querySelectorAll: (s) => body.querySelectorAll(s),
    querySelector: (s) => body.querySelector(s),
    addEventListener() {},
    hidden: false,
  };
}

module.exports = { makeDocument, created, Element, esc };
