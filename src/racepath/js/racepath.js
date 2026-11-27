/*
 * racepath.js - JavaScript port of racepath (src/racepath/cli.py).
 *
 * Runs in the browser (offline index.html) and in Node (parity tests).
 * Outputs are meant to be byte-identical to the Python CLI; tests/test_js_parity.py
 * checks that. When you change detection logic, change BOTH implementations.
 *
 * Python-specific behaviour reproduced here: float repr (pyFloat), html.escape,
 * csv quoting, json.dump(indent=1), os.path.commonprefix, fnmatch, str.splitlines.
 */
(function (root) {
"use strict";

var VERSION = "0.6.0";  // must equal racepath.__version__ (checked by tests)

var DEFAULT_CFG = {
  supplies: ["VDD*", "VSS*", "GND*", "VCC*", "VPWR*", "VGND*", "VPP*", "VBB*",
             "AVDD*", "AVSS*", "DVDD*", "DVSS*", "0"],
  high_supplies: ["VDD*", "VCC*", "VPWR*", "VPP*", "AVDD*", "DVDD*"],
  pmos_models: "^p|pch|pmos|pfet",
  nmos_models: "^n|nch|nmos|nfet",
  sources: [],
  static_inputs: [],
  hier_sep: ".",
  probe_prefix: "XDUT.",
  merge_resistors: true,
  ignore_nets: [],
  extra_checks: [],
  max_races: 2000,
  keep_bus_pairs: false,
  vdd: 0.8,
  t_m: "1n",
  th_mid: 0.5,
  th_pre: 0.9,
  sa_dv_req: 0.05
};

var CLASS_INFO = {
  sae_bl: ["SAE vs BL振幅", "SAE到達時のSA入力差電圧 - 必要差電圧"],
  wl_pre: ["WL vs プリチャージ", "WL立上り - プリチャージOFF時刻(開始側) / プリチャージON - WL立下り(終了側)"],
  hold: ["ラッチ ホールド", "データ到達 - クロック閉じ時刻"],
  clock_pair: ["クロック対(重なり)", "2入力の到達時間差(スキュー)"],
  generic: ["汎用競合", "2入力の到達時間差"]
};
var CLASS_ORDER = ["sae_bl", "wl_pre", "hold", "clock_pair", "generic"];
var PARAM_NAMES = ["vdd_m", "t_m", "th_mid", "th_pre", "sa_dv_req"];

class RacepathError extends Error {}

// ------------------------------------------------------------------ Python compat

function cmp(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }
function cmpTuple(x, y) {
  for (var i = 0; i < x.length; i++) { var c = cmp(x[i], y[i]); if (c) return c; }
  return 0;
}
function sortedStr(arr) { return arr.slice().sort(cmp); }

// repr() of a Python float
function pyFloat(x) {
  if (x !== x) return "nan";
  if (x === Infinity) return "inf";
  if (x === -Infinity) return "-inf";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  var s = x.toExponential();          // shortest round-trip digits
  var parts = s.split("e"), mant = parts[0], e = parseInt(parts[1], 10);
  var neg = mant[0] === "-";
  if (neg) mant = mant.slice(1);
  var digits = mant.replace(".", ""), out;
  if (e < -4 || e >= 16) {
    out = digits[0] + (digits.length > 1 ? "." + digits.slice(1) : "") + "e" + (e < 0 ? "-" : "+") +
      String(Math.abs(e)).padStart(2, "0");
  } else if (e >= 0) {
    out = digits.length <= e + 1 ? digits + "0".repeat(e + 1 - digits.length) + ".0"
                                 : digits.slice(0, e + 1) + "." + digits.slice(e + 1);
  } else {
    out = "0." + "0".repeat(-e - 1) + digits;
  }
  return (neg ? "-" : "") + out;
}
// str() of a JSON-loaded value (ints and floats cannot be told apart once parsed:
// integral numbers print as ints, e.g. a config value 1.0 prints "1" here but "1.0" in Python)
function pyStr(v) {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : pyFloat(v);
  if (Array.isArray(v)) return "[" + v.map(pyRepr).join(", ") + "]";
  if (typeof v === "object") return "{" + Object.keys(v).map(function (k) { return pyRepr(k) + ": " + pyRepr(v[k]); }).join(", ") + "}";
  return String(v);
}
function pyRepr(v) {
  if (typeof v === "string") return "'" + v.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
  return pyStr(v);
}

// html.escape(s, quote=True)
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}

// json.dump(obj, ensure_ascii=False, indent=1)
function jsonNum(x) { return Number.isInteger(x) ? String(x) : pyFloat(x); }
function pyJson(v, indent, level) {
  level = level || 0;
  if (v === null || v === undefined) return "null";
  if (v === true) return "true";
  if (v === false) return "false";
  if (typeof v === "number") return jsonNum(v);
  if (typeof v === "string") return JSON.stringify(v);
  var pad = "\n" + " ".repeat(indent * (level + 1)), end = "\n" + " ".repeat(indent * level);
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return "[" + pad + v.map(function (x) { return pyJson(x, indent, level + 1); }).join("," + pad) + end + "]";
  }
  var keys = Object.keys(v);
  if (!keys.length) return "{}";
  return "{" + pad + keys.map(function (k) { return JSON.stringify(k) + ": " + pyJson(v[k], indent, level + 1); })
    .join("," + pad) + end + "}";
}
// json.dumps(s) (ensure_ascii=True) for a string
function pyJsonAscii(s) {
  return JSON.stringify(s).replace(/[\u007f-￿]/g, function (c) {
    return "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0");
  });
}

// csv.writer row (QUOTE_MINIMAL, lineterminator \r\n)
function csvField(v) {
  var s = (v === null || v === undefined) ? "" : (typeof v === "string" ? v : pyStr(v));
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function csvRow(fields) {
  if (fields.length === 1 && csvField(fields[0]) === "") return '""\r\n';
  return fields.map(csvField).join(",") + "\r\n";
}

// fnmatch.fnmatchcase
var _fnCache = new Map();
function fnmatchcase(name, pat) {
  var re = _fnCache.get(pat);
  if (!re) {
    var i = 0, n = pat.length, res = "";
    while (i < n) {
      var c = pat[i++];
      if (c === "*") { res += "[\\s\\S]*"; }
      else if (c === "?") { res += "[\\s\\S]"; }
      else if (c === "[") {
        var j = i;
        if (j < n && pat[j] === "!") j++;
        if (j < n && pat[j] === "]") j++;
        while (j < n && pat[j] !== "]") j++;
        if (j >= n) { res += "\\["; }
        else {
          var stuff = pat.slice(i, j).replace(/\\/g, "\\\\").replace(/\]/g, "\\]");
          i = j + 1;
          if (stuff[0] === "!") stuff = "^" + stuff.slice(1);
          else if (stuff[0] === "^") stuff = "\\" + stuff;
          res += "[" + stuff + "]";
        }
      } else { res += c.replace(/[.*+?^${}()|[\]\\\/-]/g, "\\$&"); }
    }
    re = new RegExp("^(?:" + res + ")$");
    _fnCache.set(pat, re);
  }
  return re.test(name);
}
function globMatch(name, patterns) {
  var n = name.toLowerCase();
  return patterns.some(function (p) { return fnmatchcase(n, p.toLowerCase()); });
}

// str.splitlines() after reading a file in text mode (universal newlines)
function splitlines(text) {
  text = text.replace(/\r\n?/g, "\n");
  if (!text) return [];
  var lines = text.split(/[\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (/[\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]$/.test(text)) lines.pop();
  return lines;
}
function pySplit(s) { s = s.trim(); return s ? s.split(/\s+/) : []; }
function commonprefix(names) {
  if (!names.length) return "";
  var s1 = names[0], s2 = names[0];
  names.forEach(function (x) { if (x < s1) s1 = x; if (x > s2) s2 = x; });
  for (var i = 0; i < s1.length; i++) if (s1[i] !== s2[i]) return s1.slice(0, i);
  return s1;
}
function basename(p) { return p.slice(p.lastIndexOf("/") + 1); }   // posixpath.basename
// str.rstrip(chars): strips any trailing character contained in chars
function rstripChars(s, chars) { var i = s.length; while (i > 0 && chars.indexOf(s[i - 1]) >= 0) i--; return s.slice(0, i); }
function truthy(v) { return Array.isArray(v) ? v.length > 0 : (v && typeof v === "object" ? Object.keys(v).length > 0 : !!v); }
function dirname(p) { var i = p.lastIndexOf("/"); return i < 0 ? "" : (i === 0 ? "/" : p.slice(0, i)); }
function splitext(p) {
  var b = basename(p), i = b.lastIndexOf("."), k = 0;
  while (k < b.length && b[k] === ".") k++;
  if (i <= k - 1 || i < 0) return [p, ""];
  return [p.slice(0, p.length - (b.length - i)), b.slice(i)];
}
function ljust(s, n) { s = String(s); return s.length >= n ? s : s + " ".repeat(n - s.length); }
function clone(o) { return JSON.parse(JSON.stringify(o)); }

// ------------------------------------------------------------------ config

function loadCfg(userCfg, overrides) {
  var cfg = clone(DEFAULT_CFG);
  if (userCfg) Object.keys(userCfg).forEach(function (k) { cfg[k] = userCfg[k]; });
  if (overrides) Object.keys(overrides).forEach(function (k) {
    if (overrides[k] !== null && overrides[k] !== undefined) cfg[k] = overrides[k];
  });
  return cfg;
}

// ------------------------------------------------------------------ parsing

function Elem(name, nodes, master, hint, pins, line) {
  this.name = name; this.nodes = nodes; this.master = master; this.hint = hint;
  this.pins = pins || null; this.line = line || 0;
}
function Subckt(name, ports) { this.name = name; this.ports = ports; this.elems = []; this.pininfo = {}; }
function ParseState() {
  this.subckts = new Map();
  this.main = new Subckt("__main__", []);
  this.cur = null;
  this.globals = new Set();
  this.warnings = [];
}
ParseState.prototype.owner = function () { return this.cur !== null ? this.cur : this.main; };
ParseState.prototype.warn = function (msg) { if (this.warnings.length < 200) this.warnings.push(msg); };

var SPECTRE_HINT = /^\s*(simulator\s+lang\s*=\s*spectre|subckt\s|inline\s+subckt\s|parameters\s)/im;
var SPECTRE_SKIP = new Set(["parameters", "include", "ahdl_include", "model", "statistics", "library",
  "endlibrary", "section", "endsection", "save", "options", "real", "if", "else",
  "ic", "nodeset", "simulator", "export", "paramset", "check", "checklimit",
  "info", "alter", "altergroup"]);

function detectFormat(path, rawLines, fmt) {
  if (fmt === "cdl" || fmt === "spice" || fmt === "hspice") return "spice";
  if (fmt === "spectre") return "spectre";
  var low = path.toLowerCase();
  if (low.endsWith(".scs") || low.endsWith(".spectre")) return "spectre";
  var head = rawLines.slice(0, 5000).join("\n");
  return SPECTRE_HINT.test(head) ? "spectre" : "spice";
}

function logicalLines(path, text, fmt) {
  var raw = splitlines(text);
  var lang = detectFormat(path, raw, fmt);
  var out = [], cont = false;
  for (var idx = 0; idx < raw.length; idx++) {
    var i = idx + 1, st = raw[idx].trim();
    var m = /^simulator\s+lang\s*=\s*(\w+)/i.exec(st);
    if (m) { lang = m[1].toLowerCase() === "spectre" ? "spectre" : "spice"; cont = false; continue; }
    if (lang === "spice") {
      if (!st) continue;
      if (st[0] === "*") {
        if (st.toUpperCase().startsWith("*.PININFO")) out.push(["spice", st, i]);
        continue;
      }
      if (st[0] === "+" && out.length && out[out.length - 1][0] === "spice") {
        out[out.length - 1][1] += " " + st.slice(1);
        continue;
      }
      out.push(["spice", st, i]);
    } else {
      if (st[0] === "*") continue;
      var s2 = st.replace(/\/\/.*$/, "").replace(/\s+$/, "");
      if (!s2) continue;
      var joined = s2.endsWith("\\");
      if (joined) s2 = s2.slice(0, -1).replace(/\s+$/, "");
      if (cont && out.length) out[out.length - 1][1] += " " + s2;
      else out.push(["spectre", s2, i]);
      cont = joined;
    }
  }
  return [out, lang];
}

function normEq(text) { return text.replace(/\s*=\s*/g, "="); }

function parseSpiceLine(text, ln, st) {
  if (text.toUpperCase().startsWith("*.PININFO")) {
    if (st.cur !== null) {
      pySplit(text).slice(1).forEach(function (tok) {
        var k = tok.lastIndexOf(":");
        if (k >= 0) st.cur.pininfo[tok.slice(0, k)] = tok.slice(k + 1).toUpperCase();
      });
    }
    return;
  }
  var pins = null;
  var m = /\$PINS\b(.*)$/i.exec(text);
  if (m) {
    pins = {};
    pySplit(normEq(m[1])).forEach(function (kv) {
      var k = kv.indexOf("=");
      if (k >= 0) pins[kv.slice(0, k)] = kv.slice(k + 1);
    });
    text = text.slice(0, m.index);
  }
  var mm = /\$\[([^\]]+)\]/.exec(text);
  var bracketModel = mm ? mm[1] : null;
  text = text.replace(/\$\[[^\]]*\]/g, " ");
  var sp = /(?:^|\s)\$|;/.exec(text);
  if (sp) text = text.slice(0, sp.index);
  text = normEq(text);
  var toks = pySplit(text);
  if (!toks.length) return;
  var t0 = toks[0], k0 = t0.toLowerCase();
  if (k0[0] === ".") {
    if (k0 === ".subckt" && toks.length >= 2) {
      var ports = toks.slice(2).filter(function (t) { return t.indexOf("=") < 0 && t.toLowerCase() !== "params:"; });
      var sub = new Subckt(toks[1], ports);
      st.subckts.set(toks[1].toLowerCase(), sub);
      st.cur = sub;
    } else if (k0 === ".ends") {
      st.cur = null;
    } else if (k0 === ".global") {
      toks.slice(1).forEach(function (g) { st.globals.add(g); });
    } else if (k0 === ".include" || k0 === ".inc" || k0 === ".lib") {
      st.warn("line " + ln + ": " + k0 + " is not followed (pass a flattened netlist or concatenate files)");
    }
    return;
  }
  var c = k0[0];
  var pos = toks.slice(1).filter(function (t) { return t.indexOf("=") < 0; });
  if (c === "m") {
    var nodes, model;
    if (bracketModel && pos.length >= 4) { nodes = pos.slice(0, 4); model = bracketModel; }
    else if (pos.length >= 5) { nodes = pos.slice(0, 4); model = pos[4]; }
    else { st.warn("line " + ln + ": cannot parse MOS '" + text.slice(0, 60) + "'"); return; }
    st.owner().elems.push(new Elem(t0, nodes, model, "m", null, ln));
  } else if (c === "x") {
    var master = null, xn = [];
    for (var j = 0; j < pos.length; j++) {
      var t = pos[j];
      if (t === "/") { master = j + 1 < pos.length ? pos[j + 1] : null; break; }
      if (t[0] === "/" && t.length > 1) { master = t.slice(1); break; }
      xn.push(t);
    }
    if (master === null) {
      if (!pos.length) return;
      xn = pos.slice(0, -1); master = pos[pos.length - 1];
    }
    st.owner().elems.push(new Elem(t0, xn, master, "x", pins, ln));
  } else if (c === "r") {
    if (pos.length >= 2) st.owner().elems.push(new Elem(t0, pos.slice(0, 2), bracketModel || "r", "r", null, ln));
  }
}

function parseSpectreLine(text, ln, st) {
  text = normEq(text);
  var m = /^(inline\s+)?subckt\s+(\S+)\s*(.*)$/i.exec(text);
  if (m) {
    var ports = pySplit(m[3].replace(/\(/g, " ").replace(/\)/g, " ")).filter(function (p) { return p.indexOf("=") < 0; });
    var sub = new Subckt(m[2], ports);
    st.subckts.set(m[2].toLowerCase(), sub);
    st.cur = sub;
    return;
  }
  if (/^ends\b/i.test(text)) { st.cur = null; return; }
  m = /^global\s+(.*)$/i.exec(text);
  if (m) {
    pySplit(m[1].replace(/\(/g, " ").replace(/\)/g, " ")).forEach(function (g) { st.globals.add(g); });
    return;
  }
  var first = pySplit(text)[0].toLowerCase();
  if (SPECTRE_SKIP.has(first)) return;
  var name, nodes, master;
  m = /^(\S+?)\s*\(([^)]*)\)\s*(\S+)\s*(.*)$/.exec(text);
  if (m) { name = m[1]; nodes = pySplit(m[2]); master = m[3]; }
  else {
    var pos = pySplit(text).filter(function (t) { return t.indexOf("=") < 0; });
    if (pos.length < 3) return;
    name = pos[0]; nodes = pos.slice(1, -1); master = pos[pos.length - 1];
  }
  var hint = master.toLowerCase() === "resistor" ? "r" : "s";
  st.owner().elems.push(new Elem(name, nodes, master, hint, null, ln));
}

function parseNetlist(path, text, fmt) {
  var st = new ParseState();
  var r = logicalLines(path, text, fmt || "auto");
  r[0].forEach(function (L) {
    try {
      if (L[0] === "spice") parseSpiceLine(L[1], L[2], st); else parseSpectreLine(L[1], L[2], st);
    } catch (e) {
      st.warn("line " + L[2] + ": parse error " + e);
    }
  });
  st.detected_format = r[1];
  return st;
}

// ------------------------------------------------------------------ flatten

function UnionFind(prefer) { this.p = new Map(); this.prefer = prefer || null; }
UnionFind.prototype.find = function (x) {
  var p = this.p;
  if (!p.has(x)) { p.set(x, x); return x; }
  var root = x;
  while (p.get(root) !== root) root = p.get(root);
  while (p.get(x) !== root) { var nx = p.get(x); p.set(x, root); x = nx; }
  return root;
};
UnionFind.prototype.union = function (a, b) {
  var ra = this.find(a), rb = this.find(b);
  if (ra === rb) return ra;
  var keep = this.prefer ? this.prefer(ra, rb) : ra;
  var drop = keep === ra ? rb : ra;
  this.p.set(drop, keep);
  return keep;
};

function flatten(st, cfg, top) {
  var sep = cfg.hier_sep, subs = st.subckts, gl = st.globals, supPats = cfg.supplies;
  var preP = new RegExp(cfg.pmos_models, "i"), preN = new RegExp(cfg.nmos_models, "i");
  var fn = { mos: [], top: null, top_ports: [], pininfo: {}, supplies: new Set(), nets: new Set(), warnings: [] };
  var unknown = new Map();
  function mosType(model) { if (preP.test(model)) return "p"; if (preN.test(model)) return "n"; return null; }
  function isSupplyLocal(n) { return globMatch(n, supPats); }
  function subOf(master) { return subs.get((master || "").toLowerCase()); }

  var t;
  if (top) {
    t = subs.get(top.toLowerCase());
    if (t === undefined) throw new RacepathError("error: top subckt '" + top + "' not found");
  } else if (st.main.elems.some(function (e) {
    return ((e.hint === "x" || e.hint === "m" || e.hint === "s") && subs.has((e.master || "").toLowerCase())) || e.hint === "m";
  })) {
    t = st.main;
  } else {
    var inst = new Set();
    subs.forEach(function (s) { s.elems.forEach(function (e) { inst.add((e.master || "").toLowerCase()); }); });
    var cands = [];
    subs.forEach(function (s, k) { if (!inst.has(k)) cands.push(s); });
    if (!cands.length) throw new RacepathError("error: cannot determine top cell; use --top");
    var memo = new Map();
    var size = function (s, depth) {
      if (memo.has(s.name)) return memo.get(s.name);
      if (depth > 64) return 0;
      var n = 0;
      s.elems.forEach(function (e) {
        var ch = subOf(e.master);
        n += (ch !== undefined && e.hint !== "m") ? size(ch, depth + 1) : 1;
      });
      memo.set(s.name, n);
      return n;
    };
    var keyed = cands.map(function (s, i) { return [-size(s, 0), i, s]; });
    keyed.sort(function (x, y) { return x[0] - y[0] || x[1] - y[1]; });
    cands = keyed.map(function (x) { return x[2]; });
    t = cands[0];
    if (cands.length > 1) {
      fn.warnings.push("several top candidates (" + cands.slice(0, 5).map(function (c) { return c.name; }).join(", ") +
        "); using '" + t.name + "'. Use --top to choose.");
    }
  }
  fn.top = t.name;
  fn.top_ports = t.ports.slice();
  fn.pininfo = Object.assign({}, t.pininfo);

  function nodeName(local, prefix, nmap) {
    if (nmap.has(local)) return nmap.get(local);
    if (gl.has(local) || local === "0" || isSupplyLocal(local)) return local;
    return prefix + local;
  }
  var resPairs = [];
  function walk(sub, prefix, nmap, depth) {
    if (depth > 64) { fn.warnings.push("hierarchy too deep at " + prefix); return; }
    sub.elems.forEach(function (e) {
      var masterL = (e.master || "").toLowerCase();
      var child = (e.hint === "x" || e.hint === "s") ? subs.get(masterL) : undefined;
      if (child !== undefined) {
        var cmap = new Map();
        if (e.pins) {
          var pinsL = new Map();
          Object.keys(e.pins).forEach(function (k) { pinsL.set(k.toLowerCase(), e.pins[k]); });
          child.ports.forEach(function (p) {
            var v = pinsL.has(p.toLowerCase()) ? pinsL.get(p.toLowerCase()) : p;
            cmap.set(p, nodeName(v, prefix, nmap));
          });
        } else {
          if (e.nodes.length !== child.ports.length) {
            fn.warnings.push(prefix + e.name + ": " + e.nodes.length + " nodes vs " + child.ports.length +
              " ports of " + child.name);
          }
          var n = Math.min(e.nodes.length, child.ports.length);
          for (var i = 0; i < n; i++) cmap.set(child.ports[i], nodeName(e.nodes[i], prefix, nmap));
        }
        walk(child, prefix + e.name + sep, cmap, depth + 1);
        return;
      }
      if (e.hint === "r" || masterL === "resistor") {
        if (e.nodes.length >= 2) resPairs.push([nodeName(e.nodes[0], prefix, nmap), nodeName(e.nodes[1], prefix, nmap)]);
        return;
      }
      if (e.hint === "m" || ((e.nodes.length === 3 || e.nodes.length === 4) && (e.hint === "x" || e.hint === "s"))) {
        var ty = mosType(e.master || "");
        if (ty === null) {
          if (e.hint === "m" || e.hint === "x") unknown.set(e.master, (unknown.get(e.master) || 0) + 1);
          return;
        }
        var nodes = e.nodes.map(function (x) { return nodeName(x, prefix, nmap); });
        if (nodes.length === 3) nodes.push(nodes[2]);
        fn.mos.push({ name: prefix + e.name, type: ty, d: nodes[0], g: nodes[1], s: nodes[2], b: nodes[3], model: e.master });
      }
    });
  }
  var topMap = new Map();
  t.ports.forEach(function (p) { topMap.set(p, p); });
  walk(t, "", topMap, 0);

  var uk = Array.from(unknown.entries());
  uk.sort(function (x, y) { return y[1] - x[1]; });   // stable: ties keep first-seen order
  uk.slice(0, 10).forEach(function (x) {
    fn.warnings.push("model '" + x[0] + "' not recognised as MOS (" + x[1] + " instances) - " +
      "check pmos_models/nmos_models or subckt definitions");
  });

  var allNets = new Set();
  fn.mos.forEach(function (m) { allNets.add(m.d); allNets.add(m.g); allNets.add(m.s); });
  resPairs.forEach(function (p) { allNets.add(p[0]); allNets.add(p[1]); });
  function isSup(n) {
    var k = n.lastIndexOf(sep), leaf = k >= 0 ? n.slice(k + sep.length) : n;
    return n === "0" || (gl.has(n) && isSupplyLocal(n)) || isSupplyLocal(leaf);
  }
  var sup = new Set();
  allNets.forEach(function (n) { if (isSup(n)) sup.add(n); });

  var merge = cfg.merge_resistors === undefined ? true : cfg.merge_resistors;
  if (merge && resPairs.length) {
    var prefer = function (a, b) {
      if (sup.has(a) !== sup.has(b)) return sup.has(a) ? a : b;
      return cmpTuple([a.length, a], [b.length, b]) <= 0 ? a : b;
    };
    var uf = new UnionFind(prefer);
    resPairs.forEach(function (p) { uf.union(p[0], p[1]); });
    fn.mos.forEach(function (m) { ["d", "g", "s", "b"].forEach(function (k) { m[k] = uf.find(m[k]); }); });
    var merged = new Set();
    allNets.forEach(function (n) { merged.add(uf.find(n)); });
    allNets = merged;
    var sup2 = new Set();
    allNets.forEach(function (n) { if (sup.has(n) || isSup(n)) sup2.add(n); });
    sup = sup2;
  }
  fn.supplies = sup;
  fn.nets = allNets;
  fn.warnings = st.warnings.concat(fn.warnings);
  return fn;
}

// ------------------------------------------------------------------ graph

function CCCGraph(fn) {
  this.fn = fn;
  var sup = this.sup = fn.supplies;
  var uf = new UnionFind();
  fn.mos.forEach(function (m) {
    var ds = sup.has(m.d), ss = sup.has(m.s);
    if (!ds && !ss) uf.union(m.d, m.s);
    else if (!ds) uf.find(m.d);
    else if (!ss) uf.find(m.s);
  });
  var rootSet = new Set();
  uf.p.forEach(function (_v, n) { rootSet.add(uf.find(n)); });
  var roots = sortedStr(Array.from(rootSet));
  var rid = new Map();
  roots.forEach(function (r, i) { rid.set(r, i); });
  this.n_ccc = roots.length;
  this.members = roots.map(function () { return new Set(); });
  this.devs = roots.map(function () { return []; });
  this.ccc_of = new Map();
  var self = this;
  uf.p.forEach(function (_v, n) {
    var c = rid.get(uf.find(n));
    self.members[c].add(n);
    self.ccc_of.set(n, c);
  });
  fn.mos.forEach(function (m) {
    var ch = !sup.has(m.d) ? m.d : (!sup.has(m.s) ? m.s : null);
    if (ch === null) { m.ccc = null; return; }
    var c = self.ccc_of.get(ch);
    m.ccc = c;
    self.devs[c].push(m);
  });
  this.inputs = roots.map(function () { return new Set(); });
  var fan = new Map();
  this.devs.forEach(function (devs, c) {
    devs.forEach(function (m) {
      var g = m.g;
      if (sup.has(g) || self.members[c].has(g)) return;
      self.inputs[c].add(g);
      if (!fan.has(g)) fan.set(g, new Set());
      fan.get(g).add(c);
    });
  });
  this.fanout = new Map();
  fan.forEach(function (cs, g) { self.fanout.set(g, Array.from(cs).sort(function (a, b) { return a - b; })); });
  this.members_sorted = this.members.map(function (ms) {
    return Array.from(ms).sort(function (x, y) { return cmpTuple([x.length, x], [y.length, y]); });
  });
  this._sa = new Map();
  this._fb = new Map();
}
CCCGraph.prototype.bfs = function (src) {
  var distN = new Map([[src, 0]]), predN = new Map([[src, null]]), predC = new Map();
  var q = [src], qi = 0;
  while (qi < q.length) {
    var n = q[qi++];
    var cs = this.fanout.get(n) || [];
    for (var i = 0; i < cs.length; i++) {
      var c = cs[i];
      if (predC.has(c)) continue;
      predC.set(c, n);
      var d = distN.get(n) + 1;
      var ms = this.members_sorted[c];
      for (var j = 0; j < ms.length; j++) {
        if (!distN.has(ms[j])) { distN.set(ms[j], d); predN.set(ms[j], c); q.push(ms[j]); }
      }
    }
  }
  return [distN, predN, predC];
};
CCCGraph.path = function (net, predN, predC) {
  var nets = [net], cccs = [], cur = net, guard = 0;
  while (predN.get(cur) !== null && predN.get(cur) !== undefined && guard < 100000) {
    var c = predN.get(cur);
    cccs.push(c);
    cur = predC.get(c);
    nets.push(cur);
    guard++;
  }
  nets.reverse(); cccs.reverse();
  return [nets, cccs];
};
CCCGraph.prototype.inputRole = function (c, g) {
  var sup = this.sup;
  var devs = this.devs[c].filter(function (m) { return m.g === g; });
  var pull = devs.filter(function (m) { return sup.has(m.d) || sup.has(m.s); });
  var types = sortedStr(Array.from(new Set(devs.map(function (m) { return m.type; })))).join("");
  return {
    n_dev: devs.length,
    pass_only: devs.length > 0 && !pull.length,
    drives: pull.length > 0,
    precharge: devs.length > 0 && devs.every(function (m) { return m.type === "p"; }) && pull.length > 0,
    types: types
  };
};
CCCGraph.prototype.senseAmps = function (c) {
  if (this._sa.has(c)) return this._sa.get(c);
  var devs = this.devs[c], mem = this.members[c], sup = this.sup;
  var found = [], seen = new Set();
  ["n", "p"].forEach(function (ty) {
    var ds = devs.filter(function (m) { return m.type === ty && mem.has(m.g); });
    if (ds.length > 400) ds = ds.filter(function (m) { return !sup.has(m.s) && !sup.has(m.d); });
    var byTerm = new Map();
    ds.forEach(function (m) {
      [m.d, m.s].forEach(function (t) { if (!byTerm.has(t)) byTerm.set(t, []); byTerm.get(t).push(m); });
    });
    ds.forEach(function (i) {
      var js = byTerm.get(i.g) || [];
      js.forEach(function (j) {
        if (j === i || j.g === i.g || (j.g !== i.d && j.g !== i.s)) return;
        var common = [];
        [i.d, i.s].forEach(function (x) { if ((x === j.d || x === j.s) && common.indexOf(x) < 0) common.push(x); });
        common.forEach(function (cn) {
          if (sup.has(cn)) return;
          devs.forEach(function (t) {
            if (t === i || t === j || (cn !== t.d && cn !== t.s)) return;
            var other = t.d === cn ? t.s : t.d;
            if (sup.has(other) && !mem.has(t.g)) {
              var key = t.g + "\u0000" + sortedStr([i.g, j.g]).join("\u0000");
              if (seen.has(key)) return;
              seen.add(key);
              found.push({ enable: t.g, nodes: [j.g, i.g], tail: cn, tail_type: t.type });
            }
          });
        });
      });
    });
  });
  this._sa.set(c, found);
  return found;
};
CCCGraph.prototype.neighbourViaPass = function (c, node) {
  var out = [], mem = this.members[c], sup = this.sup;
  this.devs[c].forEach(function (m) {
    if ((node === m.d || node === m.s) && !mem.has(m.g)) {
      var other = m.d === node ? m.s : m.d;
      if (!sup.has(other) && other !== node) out.push([other, m.g]);
    }
  });
  return out;
};
CCCGraph.prototype.drivenNode = function (c, g) {
  var devs = this.devs[c], sup = this.sup;
  for (var i = 0; i < devs.length; i++) {
    var m = devs[i];
    if (m.g !== g) continue;
    if (sup.has(m.s) && !sup.has(m.d)) return m.d;
    if (sup.has(m.d) && !sup.has(m.s)) return m.s;
  }
  return null;
};
CCCGraph.prototype.label = function (c, sep) {
  var devs = this.devs[c];
  var pref = commonprefix(devs.map(function (m) { return m.name; }));
  pref = pref.indexOf(sep) >= 0 ? pref.slice(0, pref.lastIndexOf(sep) + 1) : "";
  var nets = this.members_sorted[c].slice(0, 4);
  var more = this.members[c].size > 4 ? "…" : "";
  var where = rstripChars(pref, sep) || "(top)";
  return "CCC#" + c + " [" + where + "] " + devs.length + "Tr: " + nets.join(", ") + more;
};

// ------------------------------------------------------------------ race search

function resolveNet(name, nets) {
  if (nets.has(name)) return name;
  var low = new Map();
  nets.forEach(function (n) { low.set(n.toLowerCase(), n); });
  var r = low.get(name.toLowerCase());
  return r === undefined ? null : r;
}
function defaultSources(fn, G) {
  var ins = Object.keys(fn.pininfo).filter(function (p) { return fn.pininfo[p].startsWith("I") && !G.sup.has(p); });
  if (ins.length) return ins;
  ins = fn.top_ports.filter(function (p) { return !G.sup.has(p) && !G.ccc_of.has(p); });
  return ins.length ? ins : fn.top_ports.filter(function (p) { return !G.sup.has(p); });
}
function busBase(s) { return s.replace(/\\?<[^>]*>|\[[^\]]*\]/g, "<#>"); }
function norm(s) { return s.replace(/<[^>]*>|\[[^\]]*\]|\d+/g, "#"); }

function groupRaces(races) {
  var groups = new Map(), order = [];
  races.forEach(function (r) {
    var cl = r.ccc_label.replace(/^CCC#\d+ /, "");
    var sig = JSON.stringify([r.class, norm(r.a), norm(r.b), norm(cl.split(":")[0]), r.stages_a, r.stages_b]);
    if (!groups.has(sig)) {
      groups.set(sig, r); r.group_size = 1; r.group_members = []; r._pins = r.sources.slice(); order.push(r);
    } else {
      var g = groups.get(sig);
      g.group_size += 1;
      r.sources.forEach(function (s) { if (g._pins.indexOf(s) < 0) g._pins.push(s); });
      if (g.group_members.length < 50) g.group_members.push({ ccc: r.ccc_label, a: r.a, b: r.b, probes: r.probes });
    }
  });
  return order;
}
function stageParityNote(n) { return n % 2 === 0 ? "同相" : "逆相"; }
// console wording: stages counted from the divergence point
function stageSummary(r) {
  if (r.manual) return r.a + " / " + r.b + "(手動)";
  var one = function (net, n) { return net + " " + n + "段" + (n === 0 ? "(分岐点そのもの)" : ""); };
  return "分岐点 " + r.divergence + " から " + one(r.a, r.stages_a) + " / " + one(r.b, r.stages_b) + "(差" + r.stage_diff + "段)";
}

function classify(G, T, a, b, hasFb) {
  var ra = G.inputRole(T, a), rb = G.inputRole(T, b);
  var saByEn = new Map();
  G.senseAmps(T).forEach(function (s) { saByEn.set(s.enable, s); });
  var probes = {}, cls = "generic";
  if ((saByEn.has(a) && rb.pass_only) || (saByEn.has(b) && ra.pass_only)) {
    var en, other;
    if (saByEn.has(a) && rb.pass_only) { en = a; other = b; } else { en = b; other = a; }
    var sa = saByEn.get(en);
    cls = "sae_bl";
    var n1 = sa.nodes[0], n2 = sa.nodes[1];
    probes = { sae: en, wl: other, sa_t: n1, sa_c: n2 };
    var bl1 = G.neighbourViaPass(T, n1), bl2 = G.neighbourViaPass(T, n2);
    if (bl1.length && bl2.length) { probes.bl_t = bl1[0][0]; probes.bl_c = bl2[0][0]; }
  } else if (hasFb && ((ra.pass_only && rb.drives) || (rb.pass_only && ra.drives))) {
    var clk = ra.pass_only ? a : b, dat = ra.pass_only ? b : a;
    cls = "hold";
    probes = { clk: clk, data_in: dat };
    var dn = G.drivenNode(T, dat);
    if (dn) probes.data_node = dn;
  } else if ((ra.precharge && rb.pass_only) || (rb.precharge && ra.pass_only)) {
    cls = "wl_pre";
    probes = ra.precharge ? { pre: a, wl: b } : { pre: b, wl: a };
  } else if (ra.pass_only && rb.pass_only) {
    cls = "clock_pair";
    probes = { a: a, b: b };
  } else {
    probes = { a: a, b: b };
  }
  return [cls, probes, ra, rb];
}

function combinations2(arr) {
  var out = [];
  for (var i = 0; i < arr.length; i++) for (var j = i + 1; j < arr.length; j++) out.push([arr[i], arr[j]]);
  return out;
}

function hasStructuralFb(G, T, limit) {
  limit = limit || 6;
  var targets = G.inputs[T], frontier = new Set(G.members[T]), seenC = new Set([T]);
  for (var it = 0; it < limit; it++) {
    var nxt = new Set(), hit = false;
    frontier.forEach(function (n) {
      if (hit) return;
      (G.fanout.get(n) || []).forEach(function (c) {
        if (hit || seenC.has(c)) return;
        seenC.add(c);
        G.members[c].forEach(function (x) { if (targets.has(x)) hit = true; });
        if (hit) return;
        G.members[c].forEach(function (x) { nxt.add(x); });
      });
    });
    if (hit) return true;
    frontier = nxt;
    if (!frontier.size) break;
  }
  return false;
}

function buildRace(G, T, a, b, pa, pb, hasFb, src, cfg) {
  var netsA = pa[0], netsB = pb[0], k = 0;
  while (k < Math.min(netsA.length, netsB.length) && netsA[k] === netsB[k]) k++;
  var div = k > 0 ? netsA[k - 1] : src;
  var brA = k > 0 ? netsA.slice(k - 1) : netsA, brB = k > 0 ? netsB.slice(k - 1) : netsB;
  var sa = brA.length - 1, sb = brB.length - 1;
  var c = classify(G, T, a, b, hasFb);
  return {
    "class": c[0], ccc: T, ccc_label: G.label(T, cfg.hier_sep), a: a, b: b,
    role_a: c[2], role_b: c[3], divergence: div,
    path_a: brA, path_b: brB, full_path_a: netsA, full_path_b: netsB,
    stages_a: sa, stages_b: sb, stage_diff: Math.abs(sa - sb),
    edge_a: stageParityNote(netsA.length - 1), edge_b: stageParityNote(netsB.length - 1),
    derived: netsA.indexOf(b) >= 0 || netsB.indexOf(a) >= 0,
    has_feedback: hasFb, sources: [src], probes: c[1]
  };
}

function findRaces(G, fn, sources, cfg, log) {
  var races = new Map();
  var ign = cfg.ignore_nets || [];
  var netsAll = new Set(G.fanout.keys());
  G.ccc_of.forEach(function (_v, n) { netsAll.add(n); });
  sources.forEach(function (s0) {
    var s = resolveNet(s0, netsAll);
    if (s === null) { log("  warning: source '" + s0 + "' not found in netlist"); return; }
    var r = G.bfs(s), distN = r[0], predN = r[1], predC = r[2];
    var Ts = Array.from(predC.keys()).sort(function (x, y) { return x - y; });
    Ts.forEach(function (T) {
      var ins = sortedStr(Array.from(G.inputs[T]).filter(function (g) { return distN.has(g); }));
      if (ins.length < 2) return;
      var paths = new Map(), fb = new Set();
      ins.forEach(function (g) {
        var p = CCCGraph.path(g, predN, predC);
        if (p[1].indexOf(T) >= 0) fb.add(g); else paths.set(g, p);
      });
      var live = sortedStr(Array.from(paths.keys()));
      if (!cfg.keep_bus_pairs) {
        var seenB = new Set(), reps = [];
        live.forEach(function (g) { var bb = busBase(g); if (seenB.has(bb)) return; seenB.add(bb); reps.push(g); });
        live = reps;
      }
      if (live.length < 2) return;
      if (!G._fb.has(T)) G._fb.set(T, fb.size > 0 || hasStructuralFb(G, T));
      var hasFb = G._fb.get(T);
      combinations2(live).forEach(function (ab) {
        var a = ab[0], b = ab[1];
        if (ign.length && (globMatch(a, ign) || globMatch(b, ign))) return;
        var key = T + "\u0000" + a + "\u0000" + b;
        if (races.has(key)) {
          var rr = races.get(key);
          if (rr.sources.indexOf(s) < 0) rr.sources.push(s);
          return;
        }
        races.set(key, buildRace(G, T, a, b, paths.get(a), paths.get(b), hasFb, s, cfg));
      });
    });
  });
  var out = Array.from(races.values());
  // tightest races first (smallest stage difference); classes are not used for ordering
  out.sort(function (x, y) {
    return cmpTuple([x.stage_diff, x.ccc, x.a, x.b], [y.stage_diff, y.ccc, y.a, y.b]);
  });
  if (out.length > cfg.max_races) {
    log("  note: " + out.length + " races found, keeping the first " + cfg.max_races + " (max_races)");
    out = out.slice(0, cfg.max_races);
  }
  return out;
}

function addExtraChecks(races, cfg, netsAll, log) {
  (cfg.extra_checks || []).forEach(function (chk, i) {
    var cls = chk["class"] || "generic", probes = {}, ok = true;
    Object.keys(chk).forEach(function (k) {
      if (k === "class" || k === "name") return;
      var n = resolveNet(chk[k], netsAll);
      if (n === null) { log("  warning: extra_checks[" + i + "]: net '" + chk[k] + "' not found"); ok = false; }
      probes[k] = n;
    });
    if (!ok) return;
    var vals = Object.keys(probes).map(function (k) { return probes[k]; });
    races.push({
      "class": cls, ccc: -1, ccc_label: "manual: " + (chk.name !== undefined ? pyStr(chk.name) : "check" + i),
      a: probes.a || vals[0], b: probes.b || vals[vals.length - 1],
      role_a: {}, role_b: {}, divergence: "", path_a: [], path_b: [],
      full_path_a: [], full_path_b: [], stages_a: 0, stages_b: 0,
      stage_diff: 0, edge_a: "", edge_b: "", derived: false,
      has_feedback: false, sources: [], probes: probes, manual: true
    });
  });
}

// ------------------------------------------------------------------ measures

function vref(net, prefix, sup) {
  var out = net.replace(/\\/g, "");
  return sup.has(net) ? "v(" + out + ")" : "v(" + prefix + out + ")";
}
function measureLines(r, prefix, sup) {
  var rid = r.id, p = r.probes;
  var V = function (n) { return vref(n, prefix, sup); };
  var mid = "'th_mid*vdd_m'";
  var L = ["* [" + rid + "] " + (r.manual ? "手動の測定" : "入力端子 " + (r.pins || []).join(", ")) + " : " + r.ccc_label];
  if (r.manual) {
    L.push("*   " + Object.keys(p).map(function (k) { return k + "=" + pyStr(p[k]); }).join(", "));
  } else {
    var src = (r.sources && r.sources.length) ? r.sources[0] : "起点";
    var one = function (net, n, edge) {
      return n === 0 ? net + " は分岐点そのもの(0段、" + src + " と" + edge + ")" : net + " まで" + n + "段(" + src + " と" + edge + ")";
    };
    L.push("*   分岐点 " + r.divergence + " から " + one(r.a, r.stages_a, r.edge_a) + "、" +
      one(r.b, r.stages_b, r.edge_b) + "、差" + r.stage_diff + "段");
    if (r.explain) L.push("*   NG条件: " + r.explain);
    if ((r.group_size || 1) > 1) {
      L.push("*   同型の競合 " + r.group_size + "件の代表 (全件は races.json の group_members / --no-group)");
    }
  }
  var cls = r["class"];
  if (cls === "sae_bl") {
    L.push(".measure tran " + rid + "_tsae when " + V(p.sae) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_twl when " + V(p.wl) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_vt find " + V(p.sa_t) + " at=" + rid + "_tsae",
      ".measure tran " + rid + "_vc find " + V(p.sa_c) + " at=" + rid + "_tsae",
      ".measure tran " + rid + "_dv param='abs(" + rid + "_vt-" + rid + "_vc)'",
      ".measure tran " + rid + "_margin param='" + rid + "_dv-sa_dv_req'",
      ".measure tran " + rid + "_dt_wl_sae param='" + rid + "_tsae-" + rid + "_twl'");
    if ("bl_t" in p) {
      L.push(".measure tran " + rid + "_blt find " + V(p.bl_t) + " at=" + rid + "_tsae",
        ".measure tran " + rid + "_blc find " + V(p.bl_c) + " at=" + rid + "_tsae",
        ".measure tran " + rid + "_dvbl param='abs(" + rid + "_blt-" + rid + "_blc)'");
    }
  } else if (cls === "wl_pre") {
    var preTh = "'th_pre*vdd_m'";
    L.push(".measure tran " + rid + "_tpre_off when " + V(p.pre) + "=" + preTh + " cross=1 td='t_m'",
      ".measure tran " + rid + "_twl_on when " + V(p.wl) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_margin param='" + rid + "_twl_on-" + rid + "_tpre_off'",
      ".measure tran " + rid + "_twl_off when " + V(p.wl) + "=" + mid + " cross=2 td='t_m'",
      ".measure tran " + rid + "_tpre_on when " + V(p.pre) + "=" + preTh + " cross=2 td='t_m'",
      ".measure tran " + rid + "_margin_end param='" + rid + "_tpre_on-" + rid + "_twl_off'");
  } else if (cls === "hold") {
    var dn = p.data_node || p.data_in;
    L.push(".measure tran " + rid + "_tclk when " + V(p.clk) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_tdata when " + V(dn) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_margin param='" + rid + "_tdata-" + rid + "_tclk'");
  } else {
    var a = "a" in p ? p.a : r.a, b = "b" in p ? p.b : r.b;
    L.push(".measure tran " + rid + "_ta when " + V(a) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_tb when " + V(b) + "=" + mid + " cross=1 td='t_m'",
      ".measure tran " + rid + "_skew param='" + rid + "_ta-" + rid + "_tb'");
  }
  return L;
}
function paramValues(cfg, vdd) {
  return { vdd_m: (vdd !== null && vdd !== undefined) ? vdd : cfg.vdd, t_m: cfg.t_m,
           th_mid: cfg.th_mid, th_pre: cfg.th_pre, sa_dv_req: cfg.sa_dv_req };
}
function primaryMetric(cls) { return { sae_bl: "margin", wl_pre: "margin", hold: "margin" }[cls] || "skew"; }

function where(r) {
  var m = /\[([^\]]*)\]/.exec(r.ccc_label || "");
  var w = m ? m[1] : "";
  return (w === "" || w === "(top)") ? "トップ階層のブロック" : w + " 内のブロック";
}
function explainRace(r, cfg) {
  var p = r.probes, cls = r["class"];
  if (r.manual) return "手動で追加した測定です(" + Object.keys(p).map(function (k) { return k + "=" + pyStr(p[k]); }).join(", ") + ")。";
  if (cls === "sae_bl") {
    var bl = "bl_t" in p ? "(" + p.bl_t + "/" + p.bl_c + ")" : "";
    return p.wl + " が開いてBL" + bl + "の差電圧が十分に広がる前に " + p.sae + " が立ち上がると、" +
      "SAが小さな入力差のまま増幅を始めて誤読出しになります。" +
      "SAE立上り時点のSA入力差(" + p.sa_t + "/" + p.sa_c + ")が " + pyStr(cfg.sa_dv_req) + "V を下回るとNG。";
  }
  if (cls === "wl_pre") {
    return "プリチャージ(" + p.pre + ")が切れる前に " + p.wl + " が開くと、プリチャージ電流とセル電流がぶつかって" +
      "BLが下がりきらず、読出し不良や貫通電流の原因になります。" +
      "サイクルの終わりも同様で、" + p.wl + " が閉じる前にプリチャージが再開するとNG。";
  }
  if (cls === "hold") {
    var dn = "data_node" in p ? p.data_node : p.data_in;
    return "ラッチが " + p.clk + " で閉じきる前に、" + p.data_in + " 経由の次のデータ(" + dn + ")が届くと、" +
      "保持すべき値が上書きされてホールド違反になります。データ到達がクロック閉じより早いとNG。";
  }
  if (cls === "clock_pair") {
    return r.a + " と " + r.b + " は同じブロック(トランスミッションゲートやクロックドインバータ)を開閉する組です。" +
      "ずれが大きいと両方が開く、または両方が閉じる瞬間ができ、データの突き抜けやノードの浮きが起きます。";
  }
  if (r.derived) {
    return r.a + " と " + r.b + " は一方がもう一方を遅らせた信号で、" + where(r) + "で再び合流します。" +
      "2本の時間差がそのままパルス幅(グリッチ幅)になるため、差が小さすぎると後段が反応せず、" +
      "大きすぎると意図しないパルスが残ります。";
  }
  return r.a + " と " + r.b + " が" + where(r) + "で合流しています。到達順がコーナーによって入れ替わると、" +
    "出力にグリッチや誤動作が出るおそれがあります。全コーナーで順序が変わらないことを確認してください。";
}

// ------------------------------------------------------------------ reports

function racesCsv(races) {
  var out = "\ufeff" + csvRow(["id", "pins", "ccc", "input_a", "input_b", "divergence",
    "stages_a", "stages_b", "stage_diff", "derived", "group_size", "probes", "path_a", "path_b"]);
  races.forEach(function (r) {
    out += csvRow([r.id, (r.pins || []).join(" "), r.ccc_label, r.a, r.b,
      r.divergence, r.stages_a, r.stages_b, r.stage_diff, r.derived, r.group_size || 1,
      Object.keys(r.probes).map(function (k) { return k + "=" + pyStr(r.probes[k]); }).join("; "),
      r.path_a.join(" -> "), r.path_b.join(" -> ")]);
  });
  return out;
}
function netkey(n) { return n.replace(/\\/g, ""); }
function netlink(net, rid, cls) {
  cls = cls || "net";
  return '<a class="' + cls + '" href="#m-' + rid + '" data-rid="' + rid + '" data-net="' + esc(netkey(net)) + '">' + esc(net) + "</a>";
}
function chain(nets, rid) { return nets.map(function (n) { return netlink(n, rid); }).join('<span class="arrow"> → </span>'); }
function measHtml(lines, prefix) {
  return lines.map(function (ln) {
    if (ln[0] === "*") return '<span class="cm">' + esc(ln) + "</span>";
    var re = /v\(([^()]*)\)/g, pos = 0, buf = "", m;
    while ((m = re.exec(ln)) !== null) {
      buf += esc(ln.slice(pos, m.index));
      var full = m[1];
      var net = (prefix && full.startsWith(prefix)) ? full.slice(prefix.length) : full;
      buf += 'v(<span class="v" data-net="' + esc(net) + '">' + esc(full) + "</span>)";
      pos = m.index + m[0].length;
    }
    return buf + esc(ln.slice(pos));
  }).join("\n");
}
var PROBE_JA = { sae: "SAE", wl: "WL", sa_t: "SA入力(T)", sa_c: "SA入力(C)", bl_t: "BL", bl_c: "BLB",
  pre: "プリチャージ", clk: "クロック", data_in: "データ入力", data_node: "データ(内部)", a: "入力A", b: "入力B" };

function pinsText(r) { return r.manual ? "手動の測定" : "入力端子 " + (r.pins || []).join(", "); }

function reportHtml(races, meta, pins) {
  var prefix = meta.probe_prefix || "";
  var cards = {}, blocks = [];
  races.forEach(function (r) {
    var rid = r.id, flags = [];
    if (r.derived) flags.push("派生");
    if (r.manual) flags.push("手動");
    if ((r.group_size || 1) > 1) flags.push("同型×" + r.group_size);
    var flagHtml = flags.map(function (f) { return '<span class="flag">' + f + "</span>"; }).join("");
    var probes = Object.keys(r.probes).filter(function (k) { return r.probes[k]; }).map(function (k) {
      var v = r.probes[k], ja = PROBE_JA[k] !== undefined ? PROBE_JA[k] : k;
      return '<span class="pb">' + (ja === v ? "" : "<em>" + esc(ja) + "</em>") + netlink(v, rid, "net pbn") + "</span>";
    }).join("");
    var paths;
    if (r.manual) paths = "";
    else paths = '<div class="paths">\n' +
      '<div class="pl"><span class="lab">A</span><span class="chain">' + chain(r.path_a, rid) + '</span><span class="st">' + r.stages_a + "段</span></div>\n" +
      '<div class="pl"><span class="lab">B</span><span class="chain">' + chain(r.path_b, rid) + '</span><span class="st">' + r.stages_b + "段</span></div>\n" +
      '<div class="sub">分岐点 ' + netlink(r.divergence, rid) + " から数えた段数(差" + r.stage_diff + "段) ・ 合流 " + esc(r.ccc_label) + "</div></div>";
    cards[rid] = '<section class="card" id="r-' + rid + '">\n' +
      '<header><a class="rid" href="#m-' + rid + '">' + rid + "</a>" + flagHtml + "</header>\n" +
      '<div class="sub">' + esc(pinsText(r)) + "</div>\n" +
      paths + "\n" +
      '<div class="probes"><span class="lab2">プローブ</span>' + probes + "</div>\n" +
      '<div class="sub"><a href="#m-' + rid + '">測定文を見る ↓</a></div>\n' +
      "</section>";
    blocks.push('<div class="mblk" id="m-' + rid + '" data-rid="' + rid + '">\n' +
      '<div class="mh"><b>' + rid + '</b> <span class="pins">' + esc(pinsText(r)) + '</span> <a href="#r-' + rid + '">↑ カードへ戻る</a></div>\n' +
      '<div class="note"></div>\n' +
      "<pre>" + measHtml(r.meas || [], prefix) + "</pre></div>");
  });
  // one section per input pin; a race shared by several pins is drawn under its first pin
  // and linked from the others
  var byId = {};
  races.forEach(function (r) { byId[r.id] = r; });
  var nav = [], sections = [];
  pins.forEach(function (pe, i) {
    var p = pe.pin, ids = pe.races;
    nav.push('<a class="tag t-pin" href="#p-' + i + '">' + esc(p) + " " + ids.length + "</a>");
    var own = ids.filter(function (x) { return byId[x].pins[0] === p; }).map(function (x) { return cards[x]; });
    var others = ids.filter(function (x) { return byId[x].pins[0] !== p; });
    var sec = '<h2 id="p-' + i + '">入力端子 ' + esc(p) + ' <span class="cnt">競合 ' + ids.length + "件</span></h2>\n" + own.join("");
    if (others.length) {
      sec += '<p class="also">ほかの入力端子の下に載せた競合: ' + others.map(function (x) {
        return '<a href="#r-' + x + '">' + x + "</a>(" + esc(byId[x].pins[0]) + ")";
      }).join("、") + "</p>";
    }
    if (!ids.length) sec += '<p class="meta">この端子から始まる競合はありません。</p>';
    sections.push(sec);
  });
  var manual = races.filter(function (r) { return r.manual; });
  if (manual.length) {
    nav.push('<a class="tag t-pin" href="#p-manual">手動の測定 ' + manual.length + "</a>");
    sections.push('<h2 id="p-manual">手動の測定 <span class="cnt">' + manual.length + "件</span></h2>\n" +
      manual.map(function (r) { return cards[r.id]; }).join(""));
  }
  var summary = nav.join(" ");
  var warn = "";
  if (meta.warnings && meta.warnings.length) {
    warn = "<details><summary>警告 " + meta.warnings.length + "件</summary><ul>" +
      meta.warnings.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul></details>";
  }
  var jsPrefix = pyJsonAscii(prefix);
  var staticHtml = (meta.static_inputs && meta.static_inputs.length) ? " ・ 固定端子 " + esc(meta.static_inputs.join(", ")) : "";
  return `<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>racepath report - ${esc(meta.top)}</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--fg:#1d1d1b;--mute:#6b6a64;--line:#e4e1d8;--hit:#ffe58a;
--wl:#2a6fb4;--gen:#76746c}
*{box-sizing:border-box}
body{font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;background:var(--bg);color:var(--fg);
margin:0;padding:20px;line-height:1.55;max-width:1100px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 10px}
.meta{color:var(--mute);font-size:13px;margin-bottom:10px;word-break:break-all}
.mono,.chain,.pb,pre{font-family:ui-monospace,Menlo,Consolas,monospace}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--gen);border-radius:10px;
padding:14px 16px;margin:12px 0}
header{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}
.rid{font-family:ui-monospace,monospace;font-weight:700;color:var(--fg);text-decoration:none;margin-right:2px}
.tag{display:inline-block;font-size:12px;padding:2px 9px;border-radius:10px;color:#fff;white-space:nowrap}
.t-pin{background:var(--wl);text-decoration:none;margin:2px 0} .cnt{font-size:13px;color:var(--mute);font-weight:400}
.pins{font-size:12px;color:var(--mute)} .also{font-size:13px;color:var(--mute)}
.flag{font-size:11px;border:1px solid var(--line);border-radius:8px;padding:1px 7px;color:var(--mute)}
.ex{margin:4px 0 10px;font-size:14.5px}
.paths{background:#faf9f5;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px}
.pl{display:flex;gap:8px;align-items:baseline;margin:3px 0}
.lab{font-size:11px;font-weight:700;color:#fff;background:var(--mute);border-radius:4px;padding:0 5px;flex:none}
.chain{font-size:13px;flex:1;word-break:break-all} .arrow{color:var(--mute)}
.st{font-size:12px;color:var(--mute);flex:none}
.sub{font-size:12px;color:var(--mute);margin-top:4px;word-break:break-all}
a.net{color:#1f5fa8;text-decoration:none;border-bottom:1px dotted #9db7d8}
a.net:active,a.net:hover{background:#e8f0fb}
.probes{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12.5px}
.lab2{font-size:12px;color:var(--mute);margin-right:2px}
.pb{border:1px solid var(--line);border-radius:6px;padding:1px 7px;background:#fff}
.pb em{font-style:normal;color:var(--mute);font-family:-apple-system,"Hiragino Sans",sans-serif;margin-right:5px;font-size:11.5px}
.mblk{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;
transition:box-shadow .3s}
.mblk.flash{box-shadow:0 0 0 3px #f2c94c}
.mh{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px} .mh a{font-size:12px}
pre{font-size:12px;overflow-x:auto;white-space:pre;margin:8px 0 0;line-height:1.5}
.cm{color:#7c7a72} .v.hit{background:var(--hit);border-radius:3px;outline:2px solid #f2c94c}
.note{font-size:13px;margin-top:6px;display:none;padding:8px 10px;border-radius:8px}
.note.ok{display:block;background:#fff7d6} .note.miss{display:block;background:#eef3fb}
.note code{display:block;margin-top:6px;font-size:12px;user-select:all;-webkit-user-select:all;
background:#fff;border:1px solid var(--line);border-radius:6px;padding:6px 8px;overflow-x:auto;white-space:pre}
.note button{margin-top:6px;font-size:12px;padding:3px 10px;border:1px solid var(--line);border-radius:6px;background:#fff}
details{margin:10px 0;font-size:13px}
</style></head><body>
<h1>racepath レポート: ${esc(meta.top)}</h1>
<div class="meta">${esc(meta.netlist)} (${meta.format}) ・ Tr ${meta.n_mos} ・ CCC ${meta.n_ccc} ・
起点 ${esc(meta.sources.join(', '))}${staticHtml} ・ 競合 ${races.length}件</div>
<div>${summary}</div>${warn}
<p class="meta">ノード名をタップすると、その競合の測定文へ移動して該当箇所をハイライトします。段数は、分岐点(2本の経路が分かれる最後の共通ネット)から、合流するブロックの入力までに通る論理段の数です。インバータや NAND など、チャネルでつながったトランジスタのまとまり(CCC)1つを1段と数えます。0段はその入力が分岐点そのものであることを、同相/逆相は起点(CLK など)から見た信号の向きを表します。</p>
${sections.join('')}
<h2>測定文 (races.meas)</h2>
${blocks.join('')}
<script>
(function(){
  var PREFIX = ${jsPrefix};
  function clear(){
    document.querySelectorAll('.v.hit').forEach(function(x){x.classList.remove('hit');});
    document.querySelectorAll('.note').forEach(function(x){x.className='note';x.innerHTML='';});
  }
  function esc(t){return t.replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function jump(id){
    var t = document.getElementById(id); if(!t) return false;
    t.scrollIntoView({behavior:'smooth', block:'start'});
    try { if(history.replaceState) history.replaceState(null, '', '#'+id); } catch(err) {}
    return true;
  }
  document.addEventListener('click', function(e){
    var a = e.target.closest ? e.target.closest('a.net') : null;
    if(!a){
      // in-page links (card <-> measures, pin sections) scrolled by script, so they also work
      // when this report is shown from a blob: URL inside the single-file racepath.html
      var h = e.target.closest ? e.target.closest('a[href^="#"]') : null;
      if(h && jump(h.getAttribute('href').slice(1))) e.preventDefault();
      return;
    }
    e.preventDefault();
    var rid = a.getAttribute('data-rid'), net = a.getAttribute('data-net');
    var blk = document.getElementById('m-'+rid); if(!blk) return;
    clear();
    var hits = [];
    blk.querySelectorAll('.v').forEach(function(x){ if(x.getAttribute('data-net')===net) hits.push(x); });
    var note = blk.querySelector('.note');
    if(hits.length){
      hits.forEach(function(x){x.classList.add('hit');});
      note.className = 'note ok';
      note.textContent = net + ' はこの競合の ' + hits.length + ' 行で測定しています。';
    } else {
      var nm = rid + '_t_' + net.replace(/[^A-Za-z0-9_]/g,'_');
      var line = '.measure tran ' + nm + " when v(" + PREFIX + net + ")='th_mid*vdd_m' cross=1 td='t_m'";
      note.className = 'note miss';
      note.innerHTML = esc(net) + ' はパス上の中間ノードで、まだ測定していません。到達時刻を測るなら次の1行を races.meas に追加してください。'
        + '<code>' + esc(line) + '</code><button type="button">コピー</button>';
      note.querySelector('button').addEventListener('click', function(ev){
        var b = ev.target;
        try { navigator.clipboard.writeText(line).then(function(){b.textContent='コピーしました';},
               function(){b.textContent='長押しで選択してコピー';}); }
        catch(err){ b.textContent='長押しで選択してコピー'; }
      });
    }
    blk.scrollIntoView({behavior:'smooth', block:'start'});
    blk.classList.add('flash'); setTimeout(function(){blk.classList.remove('flash');}, 1400);
    try { if(history.replaceState) history.replaceState(null, '', '#m-'+rid); } catch(err) {}
  });
})();
</script>
</body></html>`;
}

// ------------------------------------------------------------------ commands

/*
 * analyze(opts) -> {files: {name: text}, log, races, meta}
 *   opts.text        netlist contents (string)
 *   opts.netlist     netlist path as given on the command line (log line, format detection)
 *   opts.netlistAbs  absolute path stored in races.json meta / report (default: opts.netlist)
 *   opts.out         output directory shown in the log (default "racepath_out")
 *   opts.format, opts.top, opts.sources [], opts.static [] (added to static_inputs),
 *   opts.config (object), opts.prefix, opts.noGroup,
 *   opts.show (default 30), opts.quiet
 * Throws RacepathError (with .log holding the console text so far) on fatal errors.
 */
function analyze(opts) {
  var logLines = [];
  var log = opts.quiet ? function () {} : function (s) { logLines.push(s); };
  var out = opts.out || "racepath_out";
  try {
    var cfg = loadCfg(opts.config || null, { probe_prefix: opts.prefix });
    if (opts.sources && opts.sources.length) cfg.sources = opts.sources.slice();
    if (opts.static && opts.static.length) cfg.static_inputs = (cfg.static_inputs || []).concat(opts.static);
    log("racepath " + VERSION + ": reading " + opts.netlist);
    var st = parseNetlist(opts.netlist, opts.text, opts.format || "auto");
    var fn = flatten(st, cfg, opts.top || null);
    var G = new CCCGraph(fn);
    log("  format=" + st.detected_format + " top=" + fn.top + " transistors=" + fn.mos.length + " CCC=" + G.n_ccc +
      " supplies=" + fn.supplies.size);
    var staticPats = cfg.static_inputs || [];
    var isStatic = function (n) { return staticPats.length > 0 && globMatch(n, staticPats); };
    var staticPorts = fn.top_ports.filter(function (p) { return !fn.supplies.has(p) && globMatch(p, staticPats); });
    var srcs;
    if (cfg.sources && cfg.sources.length) {
      srcs = [];
      cfg.sources.forEach(function (s) {
        if (isStatic(s)) log("  warning: source '" + s + "' is a static input (static_inputs); ignored");
        else srcs.push(s);
      });
    } else {
      srcs = defaultSources(fn, G).filter(function (s) { return !isStatic(s); });
    }
    log("  sources: " + srcs.join(", "));
    if (staticPats.length) {
      log("  static inputs: " + (staticPorts.length ? staticPorts.join(", ") : "(none matched " + staticPats.join(", ") + ")") +
        "  (held constant: not used as launch nets)");
    }
    var races = findRaces(G, fn, srcs, cfg, log);
    var nRaw = races.length;
    if (!opts.noGroup) {
      races = groupRaces(races);
      if (races.length < nRaw) log("  grouped " + nRaw + " races into " + races.length + " structural groups (--no-group to keep all)");
    }
    var netsAll = new Set(G.fanout.keys());
    G.ccc_of.forEach(function (_v, n) { netsAll.add(n); });
    addExtraChecks(races, cfg, netsAll, log);
    // input pins (launch nets as resolved in the netlist), in the order given
    var pinOrder = [];
    srcs.forEach(function (s) { var n = resolveNet(s, netsAll); if (n !== null && pinOrder.indexOf(n) < 0) pinOrder.push(n); });
    var rank = new Map();
    pinOrder.forEach(function (p, i) { rank.set(p, i); });
    races.forEach(function (r) {
      var rp = r._pins !== undefined ? r._pins : r.sources;
      delete r._pins;
      r.pins = rp.slice().sort(function (x, y) {
        return cmpTuple([rank.has(x) ? rank.get(x) : rank.size, x], [rank.has(y) ? rank.get(y) : rank.size, y]);
      });
    });
    var prefix = cfg.probe_prefix;
    races.forEach(function (r, i) {
      r.id = "rp" + String(i + 1).padStart(3, "0");
      r.explain = explainRace(r, cfg);
      r.meas = measureLines(r, prefix, fn.supplies);
    });
    var files = {};
    var meas = ["* racepath auto-probe measures (HSPICE / Spectre SPICE-mode syntax)",
      "* netlist: " + basename(opts.netlist) + "  top: " + fn.top + "  probe prefix: '" + prefix + "'",
      "* required params: " + PARAM_NAMES.join(" ") + "  (see races_params.inc)", ""];
    races.forEach(function (r) { meas = meas.concat(r.meas, [""]); });
    files["races.meas"] = meas.join("\n");
    var pv = paramValues(cfg);
    files["races_params.inc"] = "* default measurement parameters for races.meas\n.param " +
      Object.keys(pv).map(function (k) { return k + "=" + pyStr(pv[k]); }).join(" ") + "\n";
    var meta = {
      tool: "racepath " + VERSION, netlist: opts.netlistAbs || opts.netlist,
      format: st.detected_format, top: fn.top, top_ports: fn.top_ports,
      pininfo: fn.pininfo, sources: srcs, n_mos: fn.mos.length, n_ccc: G.n_ccc,
      supplies_top: fn.top_ports.filter(function (p) { return fn.supplies.has(p); }),
      static_inputs: staticPorts,
      probe_prefix: prefix, cfg: cfg, warnings: fn.warnings
    };
    var pins = pinOrder.map(function (p) {
      return { pin: p, races: races.filter(function (r) { return r.pins.indexOf(p) >= 0; }).map(function (r) { return r.id; }) };
    });
    files["races.json"] = pyJson({ meta: meta, pins: pins, races: races }, 1);
    files["races.csv"] = racesCsv(races);
    files["report.html"] = reportHtml(races, meta, pins);

    log("  races: " + races.length + "  (入力端子 " + pinOrder.length + "本)");
    var byId = {};
    races.forEach(function (r) { byId[r.id] = r; });
    var groups = pins.map(function (pe) {
      return ["入力端子 " + pe.pin + "  競合 " + pe.races.length + "件", pe.races.map(function (x) { return byId[x]; })];
    });
    var manual = races.filter(function (r) { return r.manual; });
    if (manual.length) groups.push(["手動の測定  " + manual.length + "件", manual]);
    var show = opts.show === undefined ? 30 : opts.show, shown = 0;
    groups.forEach(function (gp) {
      log("  ■ " + gp[0]);
      for (var i = 0; i < gp[1].length && shown < show; i++) {
        var r = gp[1][i], g = (r.group_size || 1) > 1 ? "  x" + r.group_size : "";
        shown++;
        log("   " + r.id + " " + stageSummary(r) + "  @ " + r.ccc_label + g);
      }
    });
    fn.warnings.slice(0, 10).forEach(function (w) { log("  warning: " + w); });
    log("  wrote " + out + "/races.json, races.csv, races.meas, races_params.inc, report.html");
    return { files: files, log: logLines.map(function (l) { return l + "\n"; }).join(""), races: races, meta: meta, pins: pins };
  } catch (e) {
    var err = e instanceof RacepathError ? e : new RacepathError(String(e && e.stack || e));
    err.log = logLines.map(function (l) { return l + "\n"; }).join("") + err.message + "\n";
    throw err;
  }
}

function isHigh(net, cfg) { return globMatch(net, cfg.high_supplies || DEFAULT_CFG.high_supplies); }

/*
 * decks(opts) -> {files: {name: text}, log}
 *   opts.racesJson  contents of races.json (string or parsed object)
 *   opts.corners    corners definition (object)
 *   opts.measPath   absolute path of races.meas as written into the decks
 *   opts.out        output directory shown in the log, opts.sim "spectre"|"hspice"
 */
function decks(opts) {
  var data = typeof opts.racesJson === "string" ? JSON.parse(opts.racesJson) : opts.racesJson;
  var meta = data.meta, cfg = meta.cfg, cc = opts.corners;
  var sim = (opts.sim || cc.simulator || "spectre").toLowerCase();
  var measPath = opts.measPath, netlist = meta.netlist, top = meta.top, ports = meta.top_ports;
  var inst = rstripChars(cfg.probe_prefix, cfg.hier_sep) || "XDUT";
  var stim = cc.stimulus || {}, stat = cc["static"] || {}, supplyVals = cc.supplies || {};
  var tstop = cc.tran_stop !== undefined ? cc.tran_stop : "20n";
  var tstep = cc.tran_step !== undefined ? cc.tran_step : "1p";
  var stimFile = cc.stimulus_file;
  var firstDelay = null, files = {}, written = [];
  var S = pyStr;
  cc.corners.forEach(function (corner) {
    var name = corner.name;
    var vdd = corner.vdd !== undefined ? corner.vdd : cfg.vdd;
    var temp = corner.temp !== undefined ? corner.temp : 25;
    var lib = corner.lib;
    var pv = paramValues(cfg, vdd);
    var cparams = corner.params || {};
    Object.keys(cparams).forEach(function (k) { pv[k] = cparams[k]; });
    var L = [], srcLines = [];
    ports.forEach(function (p) {
      if (meta.supplies_top.indexOf(p) >= 0) {
        var val = p in supplyVals ? supplyVals[p] : (isHigh(p, cfg) ? "vdd_m" : 0);
        srcLines.push(["dc", "V_" + p, p, val]);
      } else if (p in stim || (meta.sources.indexOf(p) >= 0 && !truthy(stimFile))) {
        var s = { delay: "1n", period: "4n", width: "2n", tr: "20p", tf: null, v0: 0, v1: "vdd_m" };
        var sp = stim[p] || {};
        Object.keys(sp).forEach(function (k) { s[k] = sp[k]; });
        s.tf = s.tf || s.tr;
        if (firstDelay === null) firstDelay = s.delay;
        srcLines.push(["pulse", "V_" + p, p, s]);
      } else if (p in stat) {
        srcLines.push(["dc", "V_" + p, p, stat[p]]);
      } else if (!truthy(stimFile)) {
        srcLines.push(["tie", "R_" + p, p, "1e9"]);
      }
    });
    if (!("t_m" in cparams) && firstDelay !== null && !("t_m" in cc)) pv.t_m = firstDelay;
    if ("t_m" in cc) pv.t_m = cc.t_m;
    var ext;
    if (sim === "hspice") {
      L.push("* racepath deck  corner=" + name, "");
      if (truthy(lib)) L.push(Array.isArray(lib) ? ".lib '" + S(lib[0]) + "' " + S(lib[1]) : ".include '" + S(lib) + "'");
      L.push(".temp " + S(temp), ".param " + Object.keys(pv).map(function (k) { return k + "=" + S(pv[k]); }).join(" "),
        ".option post=0 measout=1", ".include '" + netlist + "'");
      if (truthy(stimFile)) L.push(".include '" + S(stimFile) + "'");
      L.push(".include '" + measPath + "'");
      L.push(inst + " " + ports.join(" ") + " " + top);
      srcLines.forEach(function (x) {
        var kind = x[0], nm = x[1], p = x[2], val = x[3];
        if (kind === "dc") L.push(typeof val === "string" ? nm + " " + p + " 0 '" + val + "'" : nm + " " + p + " 0 " + S(val));
        else if (kind === "tie") L.push(nm + " " + p + " 0 " + val);
        else L.push(nm + " " + p + " 0 pulse(" + S(val.v0) + " '" + S(val.v1) + "' " + S(val.delay) + " " + S(val.tr) + " " +
          S(val.tf) + " " + S(val.width) + " " + S(val.period) + ")");
      });
      L.push(".tran " + S(tstep) + " " + S(tstop), ".end");
      ext = ".sp";
    } else {
      L.push("// racepath deck  corner=" + name, "simulator lang=spectre", "");
      if (truthy(lib)) L.push(Array.isArray(lib) ? 'include "' + S(lib[0]) + '" section=' + S(lib[1]) : 'include "' + S(lib) + '"');
      L.push("parameters " + Object.keys(pv).map(function (k) { return k + "=" + S(pv[k]); }).join(" "));
      if (meta.format === "spectre") L.push('include "' + netlist + '"');
      else L.push("simulator lang=spice", ".include '" + netlist + "'", "simulator lang=spectre");
      if (truthy(stimFile)) L.push('include "' + S(stimFile) + '"');
      L.push(inst + " (" + ports.join(" ") + ") " + top);
      srcLines.forEach(function (x) {
        var kind = x[0], nm = x[1], p = x[2], val = x[3];
        if (kind === "dc") L.push(nm + " (" + p + " 0) vsource dc=" + S(val));
        else if (kind === "tie") L.push(nm + " (" + p + " 0) resistor r=" + val);
        else L.push(nm + " (" + p + " 0) vsource type=pulse val0=" + S(val.v0) + " val1=" + S(val.v1) + " " +
          "delay=" + S(val.delay) + " rise=" + S(val.tr) + " fall=" + S(val.tf) + " width=" + S(val.width) + " " +
          "period=" + S(val.period));
      });
      L.push("simulator lang=spice", ".include '" + measPath + "'", "simulator lang=spectre",
        "racepathOpts options temp=" + S(temp) + " tnom=" + S(cc.tnom !== undefined ? cc.tnom : 25),
        "tran1 tran stop=" + S(tstop));
      ext = ".scs";
    }
    files[name + ext] = L.join("\n") + "\n";
    written.push(name + ext);
  });
  var run = '#!/bin/sh\n# run every corner deck; results land in <corner>/\ncd "$(dirname "$0")"\n';
  written.forEach(function (fp) {
    var b = splitext(fp)[0];
    if (sim === "hspice") run += "mkdir -p " + b + " && hspice -i " + fp + " -o " + b + "/" + b + "\n";
    else run += "spectre " + fp + " -raw " + b + " +aps =log " + b + ".log\n";
  });
  files["run_all.sh"] = run;
  var out = opts.out || "racepath_decks";
  return { files: files, log: "wrote " + written.length + " " + sim + " decks + run_all.sh to " + out + "\n" };
}

var SCALE = { f: 1e-15, p: 1e-12, n: 1e-9, u: 1e-6, m: 1e-3, k: 1e3, meg: 1e6, g: 1e9 };
var NUM = "[-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?[a-zA-Z]*";

function toFloat(s) {
  s = s.trim();
  var l = s.toLowerCase();
  if (l === "failed" || l === "fail" || l === "nan" || l === "n/a") return null;
  var m = /^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([a-zA-Z]*)$/.exec(s);
  if (!m) return null;
  var v = parseFloat(m[1]), suf = m[2].toLowerCase();
  var order = ["meg", "f", "p", "n", "u", "m", "k", "g"];
  for (var i = 0; i < order.length; i++) if (suf.startsWith(order[i])) return v * SCALE[order[i]];
  return v;
}
function parseResults(txt) {
  var vals = {}, any = false;
  var re = new RegExp("^\\s*(rp\\d{3}_\\w+)\\s*=\\s*(" + NUM + "|failed)", "gim");
  var m;
  while ((m = re.exec(txt)) !== null) { vals[m[1].toLowerCase()] = toFloat(m[2]); any = true; }
  if (any) return vals;
  var lines = splitlines(txt).filter(function (ln) { return ln.trim() && ln[0] !== "$" && ln[0] !== "."; });
  var toks = pySplit(lines.join(" "));
  var low = toks.map(function (t) { return t.toLowerCase(); });
  var idx = low.indexOf("alter#");
  if (idx >= 0) {
    var names = low.slice(0, idx + 1), values = toks.slice(idx + 1, idx + 1 + names.length);
    for (var i = 0; i < Math.min(names.length, values.length); i++) {
      if (names[i].startsWith("rp")) vals[names[i]] = toFloat(values[i]);
    }
  }
  return vals;
}

var RESULT_GLOBS = ["*.mt*", "*.ms*", "*.measure", "*.lis", "*.log", "*.out"];

/*
 * Emulates glob.glob(os.path.join(dir, "**", ext), recursive=True) for the result patterns:
 * entries are [{path, rel, ...}] for every file under a results folder (rel = path below it).
 */
function selectResultFiles(entries) {
  return entries.filter(function (f) {
    var parts = f.rel.split("/");
    if (parts.some(function (p) { return p[0] === "."; })) return false;   // glob skips hidden names
    var b = parts[parts.length - 1];
    return RESULT_GLOBS.some(function (g) { return fnmatchcase(b, g); });
  });
}

/*
 * collect(opts) -> {header, rows, csv, log}
 *   opts.racesJson, opts.files [{path, text}] (already selected, e.g. by selectResultFiles),
 *   opts.out, opts.warn, opts.warnV, opts.skewWarn
 */
function collect(opts) {
  var data = typeof opts.racesJson === "string" ? JSON.parse(opts.racesJson) : opts.racesJson;
  var races = data.races;
  var warn = opts.warn !== undefined ? opts.warn : 20e-12;
  var warnV = opts.warnV !== undefined ? opts.warnV : 0.02;
  var skewWarn = opts.skewWarn !== undefined ? opts.skewWarn : 10e-12;
  var files = opts.files.slice();
  var seenP = new Set();
  files = files.filter(function (f) { if (seenP.has(f.path)) return false; seenP.add(f.path); return true; });
  files.sort(function (x, y) { return cmp(x.path, y.path); });
  var corners = new Map();
  files.forEach(function (f) {
    var v = parseResults(f.text);
    if (!Object.keys(v).length) return;
    var name = splitext(basename(f.path))[0];
    if (["tran1", "tran", "input", "log", "spectre"].indexOf(name.toLowerCase()) >= 0) {
      var parent = basename(dirname(f.path));
      name = parent.replace(/\.(raw|psf)$/, "") || name;
    }
    if (!corners.has(name)) corners.set(name, {});
    Object.assign(corners.get(name), v);
  });
  if (!corners.size) throw new RacepathError("no measurement results found");
  var cnames = sortedStr(Array.from(corners.keys()));
  var rows = [];
  races.forEach(function (r) {
    var metrics = [primaryMetric(r["class"])];
    if (r["class"] === "wl_pre") metrics.push("margin_end");
    metrics.forEach(function (mt) {
      var key = (r.id + "_" + mt).toLowerCase();
      var vals = cnames.map(function (c) { var o = corners.get(c); return key in o ? o[key] : null; });
      var good = vals.filter(function (x) { return x !== null; });
      var worst = null, verdict;
      if (mt === "skew") {
        good.forEach(function (x) { if (worst === null || Math.abs(x) < Math.abs(worst)) worst = x; });
        var signs = new Set(good.filter(function (x) { return x !== 0; }).map(function (x) { return x > 0; }));
        verdict = signs.size > 1 ? "順序反転" : ((worst !== null && Math.abs(worst) < skewWarn) ? "要確認" : "");
      } else {
        good.forEach(function (x) { if (worst === null || x < worst) worst = x; });
        var lim = r["class"] === "sae_bl" ? warnV : warn;
        verdict = worst === null ? "" : (worst < 0 ? "NG" : (worst < lim ? "要注意" : "OK"));
      }
      if (vals.some(function (x) { return x === null; })) {
        verdict = (verdict === "" || verdict === "OK") ? "測定失敗あり" : verdict + " / 測定失敗あり";
      }
      var wc = "";
      if (worst !== null) { for (var i = 0; i < vals.length; i++) if (vals[i] === worst) { wc = cnames[i]; break; } }
      rows.push([r.id, mt, r.a, r.b].concat(vals, [worst, wc, verdict]));
    });
  });
  var header = ["id", "metric", "input_a", "input_b"].concat(cnames, ["worst", "worst_corner", "verdict"]);
  var cell = function (v) { return v === null ? "" : (typeof v === "number" ? pyFloat(v) : v); };
  var csv = "\ufeff" + csvRow(header) + rows.map(function (row) { return csvRow(row.map(cell)); }).join("");
  var out = opts.out || "racepath_summary.csv";
  var log = cnames.length + " corners, " + rows.length + " metrics -> " + out + "\n";
  rows.forEach(function (row) {
    var v = row[row.length - 1];
    if (v && v !== "OK") {
      var w = row[row.length - 3];
      log += "  " + row[0] + " " + ljust(row[1], 10) + " worst=" +
        (w === null ? "None" : pyFloat(w)) + " @ " + row[row.length - 2] + "  " + v + "\n";
    }
  });
  return { header: header, rows: rows.map(function (row) { return row.map(cell); }), csv: csv, log: log };
}

var api = {
  VERSION: VERSION, DEFAULT_CFG: DEFAULT_CFG, CLASS_INFO: CLASS_INFO, CLASS_ORDER: CLASS_ORDER,
  RacepathError: RacepathError, analyze: analyze, decks: decks, collect: collect,
  selectResultFiles: selectResultFiles,
  // exposed for tests
  _pyFloat: pyFloat, _fnmatchcase: fnmatchcase, _splitlines: splitlines, _pyJson: pyJson,
  _csvRow: csvRow, _commonprefix: commonprefix, _splitext: splitext
};
if (typeof module !== "undefined" && module.exports) module.exports = api;
else root.RacepathEngine = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
