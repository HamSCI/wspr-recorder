// Run the carrier browser's page script under node with a stub DOM and a
// canned API, then call one of its functions and report what it did.
// usage: node run_page.js <script.js> <api.json> <function-name>
// api.json maps a URL path (no query) to the JSON body fetch() returns.
"use strict";
const fs = require("fs"), vm = require("vm");
const [script, apiFile, fname] = process.argv.slice(2);
const api = JSON.parse(fs.readFileSync(apiFile, "utf8"));

// An element stub: remembers what is assigned to it, and answers any method
// call or unknown property with another stub, so page code never trips on
// the DOM itself -- only on its own mistakes.
function stub(name) {
  const store = {value: "", textContent: "", innerHTML: "", hidden: false, checked: false,
                 style: {}, dataset: {}, classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
                 children: [], options: []};
  const fn = function () { return stub(name + "()"); };
  return new Proxy(fn, {
    get(t, k) {
      if (k in store) return store[k];
      if (k === Symbol.toPrimitive) return () => "";
      if (k === "then") return undefined;
      if (k === "getContext") return () => stub("ctx");
      if (k === "querySelectorAll") return () => [];
      if (k === "getBoundingClientRect") return () => ({width: 800, height: 300, left: 0, top: 0});
      if (["clientWidth", "offsetWidth", "width"].includes(k)) return 800;
      if (["clientHeight", "offsetHeight", "height"].includes(k)) return 300;
      return stub(name + "." + String(k));
    },
    set(t, k, v) { store[k] = v; return true; },
    apply() { return stub(name + "()"); },
  });
}
const els = {};
const document = new Proxy({}, {
  get(t, k) {
    if (k === "getElementById") return id => (els[id] = els[id] || stub("#" + id));
    if (k === "createElement") return tag => stub("<" + tag + ">");
    if (k === "querySelectorAll") return () => [];
    if (k === "documentElement" || k === "body") return stub(String(k));
    return stub("document." + String(k));
  },
});
const errors = [];
const ctx = {
  document, console, Math, JSON, Date, Object, Array, Number, String, Promise, Error, isFinite, isNaN,
  parseFloat, parseInt, encodeURIComponent, decodeURIComponent, URLSearchParams, Intl, Set, Map,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, requestAnimationFrame: () => 0,
  getComputedStyle: () => ({getPropertyValue: () => "#888"}),
  location: {hash: "", search: "", pathname: "/"}, history: {replaceState() {}},
  addEventListener() {}, matchMedia: () => ({matches: false, addEventListener() {}}),
  devicePixelRatio: 1, innerWidth: 1200,
  fetch: async url => {
    const path = String(url).split("?")[0];
    if (!(path in api)) return {ok: false, status: 404, json: async () => ({})};
    return {ok: true, status: 200, json: async () => api[path]};
  },
};
ctx.window = ctx; ctx.self = ctx;
process.on("unhandledRejection", e => errors.push(String(e && e.stack || e)));
(async () => {
  try {
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(script, "utf8"), ctx, {filename: "page.js"});
    await new Promise(r => setImmediate(r));
    if (els.date) els.date.value = api.__date || "";
    await vm.runInContext(fname + "()", ctx);
  } catch (e) { errors.push(String(e && e.stack || e)); }
  await new Promise(r => setImmediate(r));
  process.stdout.write(JSON.stringify({errors, status: els.status ? els.status.textContent : null}));
})();
