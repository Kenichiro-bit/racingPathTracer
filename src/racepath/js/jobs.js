/*
 * jobs.js - in-browser job store for the offline single-file racepath (racepath.html).
 *
 * Mirrors racepath.jobs.App (Python) and answers the same API paths as `racepath serve`,
 * so the shared page (racepath.web.INDEX_HTML) works unchanged through window.racepathLocal.
 * Everything stays in memory: nothing is sent anywhere and nothing survives a page reload.
 */
(function (root) {
"use strict";

var E = root.RacepathEngine || (typeof require !== "undefined" ? require("./racepath.js") : null);

var DEFAULT_CORNERS = root.RACEPATH_DEFAULT_CORNERS || null;   // set by the build (= examples/corners.json)
var OUTPUT_FILES = ["report.html", "races.meas", "races_params.inc", "races.csv", "races.json"];
var RESULT_EXT = /\.(mt\d*|ms\d*|measure|lis|log|out)$/i;
var TYPES = { html: "text/html", csv: "text/csv", json: "application/json", zip: "application/zip" };

function ApiError(status, message, log) { this.status = status; this.message = message; this.log = log || ""; }

function safeName(name, dflt) {
  name = name.trim().replace(/[^A-Za-z0-9._<>+-]/g, "_").replace(/^[._]+|[._]+$/g, "");
  return name.slice(0, 120) || dflt || "file";
}
function safeRelpath(rel) {
  var parts = rel.split(/[\\/]+/).filter(function (p) { return p && p !== "." && p !== ".."; })
    .map(function (p) { return safeName(p); });
  return parts.length ? parts.join("/") : "file";
}
function pad(n) { return String(n).padStart(2, "0"); }
function stamp(d, a, b, c) {
  return d.getFullYear() + a + pad(d.getMonth() + 1) + a + pad(d.getDate()) + b +
    pad(d.getHours()) + c + pad(d.getMinutes()) + c + pad(d.getSeconds());
}
var dec = new TextDecoder("utf-8", { ignoreBOM: true }), enc = new TextEncoder();
function text(bytes) { return dec.decode(bytes); }

// ---------------------------------------------------------------- zip (stored, no compression)
var CRC = (function () {
  var t = new Uint32Array(256);
  for (var n = 0; n < 256; n++) { var c = n; for (var k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
function crc32(b) { var c = 0xffffffff; for (var i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
function makeZip(entries) {   // [{name, bytes, mode}]
  var now = new Date();
  var dtime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
  var ddate = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  var chunks = [], central = [], offset = 0;
  entries.forEach(function (e) {
    var name = enc.encode(e.name), crc = crc32(e.bytes), size = e.bytes.length;
    var h = new DataView(new ArrayBuffer(30));
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true);
    h.setUint16(8, 0, true); h.setUint16(10, dtime, true); h.setUint16(12, ddate, true);
    h.setUint32(14, crc, true); h.setUint32(18, size, true); h.setUint32(22, size, true);
    h.setUint16(26, name.length, true); h.setUint16(28, 0, true);
    chunks.push(new Uint8Array(h.buffer), name, e.bytes);
    var c = new DataView(new ArrayBuffer(46));
    c.setUint32(0, 0x02014b50, true); c.setUint16(4, (3 << 8) | 20, true); c.setUint16(6, 20, true);
    c.setUint16(8, 0x0800, true); c.setUint16(10, 0, true); c.setUint16(12, dtime, true); c.setUint16(14, ddate, true);
    c.setUint32(16, crc, true); c.setUint32(20, size, true); c.setUint32(24, size, true);
    c.setUint16(28, name.length, true); c.setUint32(38, ((0x8000 | (e.mode || 0o644)) << 16) >>> 0, true);
    c.setUint32(42, offset, true);
    central.push(new Uint8Array(c.buffer), name);
    offset += 30 + name.length + size;
  });
  var csize = central.reduce(function (s, x) { return s + x.length; }, 0);
  var end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
  end.setUint32(12, csize, true); end.setUint32(16, offset, true);
  var all = chunks.concat(central, [new Uint8Array(end.buffer)]);
  var out = new Uint8Array(offset + csize + 22), p = 0;
  all.forEach(function (x) { out.set(x, p); p += x.length; });
  return out;
}

// ---------------------------------------------------------------- job store
function Jobs() { this.jobs = new Map(); }

Jobs.prototype.job = function (name) {
  if (typeof name !== "string" || !this.jobs.has(name)) throw new ApiError(404, "解析ジョブ " + name + " が見つかりません");
  return this.jobs.get(name);
};
Jobs.prototype.newJob = function (stem) {
  var base = stamp(new Date(), "", "-", "") + "_" + safeName(stem, "netlist"), job = base, i = 1;
  while (this.jobs.has(job)) { i++; job = base + "-" + i; }
  return job;
};
Jobs.prototype.list = function () {
  return Array.from(this.jobs.keys()).sort().reverse().map(function (k) {
    var j = this.jobs.get(k);
    return { job: k, netlist: j.netlistName, races: j.out ? j.out.races.length : null, created: j.created };
  }, this);
};
Jobs.prototype.summary = function (name) {
  var j = this.job(name);
  var res = { job: name, netlist_name: j.netlistName, netlist_path: j.netlistName, options: j.options,
              log: j.log || "", analyzed: false };
  if (j.out) {
    var m = j.out.meta, counts = {};
    j.out.races.forEach(function (r) { counts[r["class"]] = (counts[r["class"]] || 0) + 1; });
    Object.assign(res, {
      analyzed: true, top: m.top, format: m.format, n_mos: m.n_mos, n_ccc: m.n_ccc,
      sources: m.sources, warnings: m.warnings, counts: counts,
      races: j.out.races.map(function (r) {
        return { id: r.id, "class": r["class"], a: r.a, b: r.b, stages_a: r.stages_a, stages_b: r.stages_b,
                 divergence: r.divergence, group_size: r.group_size || 1, derived: !!r.derived, explain: r.explain || "" };
      }),
      files: OUTPUT_FILES.slice()
    });
  }
  if (j.decks) res.decks = Object.keys(j.decks.files).sort();
  if (j.collect) res.collect = { header: j.collect.header, rows: j.collect.rows };
  return res;
};
Jobs.prototype.upload = function (name, bytes) {
  name = safeName(basename(name || "netlist.cdl"), "netlist.cdl");
  var job = this.newJob(name.replace(/\.[^.]*$/, "") || name);
  this.jobs.set(job, { netlistName: name, netlist: bytes, created: stamp(new Date(), "-", " ", ":"),
                       options: {}, results: new Map() });
  return { job: job, netlist_name: name };
};
function basename(p) { var parts = String(p).split(/[\\/]/); return parts[parts.length - 1]; }

Jobs.prototype.analyze = function (req) {
  if (req.path) throw new ApiError(400, "このページではパスを指定できません。ファイルを選んでください。");
  var j = this.job(req.job), opts = {};
  var fmt = req.format || "auto";
  if (["auto", "cdl", "spice", "hspice", "spectre"].indexOf(fmt) < 0) throw new ApiError(400, "形式 " + fmt + " には対応していません");
  opts.format = fmt;
  var top = String(req.top || "").trim();
  if (top) opts.top = top;
  var srcs = String(req.sources || "").split(/[\s,]+/).filter(Boolean);
  if (srcs.length) opts.sources = srcs.join(" ");
  var prefix = String(req.prefix || "").trim();
  if (prefix) opts.prefix = prefix;
  if (req.no_group) opts.no_group = true;
  var cfgText = String(req.config || "").trim(), cfg = null;
  if (cfgText) {
    try { cfg = JSON.parse(cfgText); } catch (e) { throw new ApiError(400, "設定(JSON)を読めません: " + e.message); }
    opts.config = cfgText;
  }
  j.options = opts; j.out = null; j.decks = null; j.collect = null;
  try {
    j.out = E.analyze({ text: text(j.netlist), netlist: j.netlistName, out: "out", format: fmt, top: top || null,
                        sources: srcs, config: cfg, prefix: prefix || undefined, noGroup: !!req.no_group, show: 200 });
    j.log = j.out.log;
  } catch (e) {
    j.log = e.log !== undefined ? e.log : String(e);
    throw new ApiError(422, "解析に失敗しました。ログを確認してください。", j.log);
  }
  return this.summary(req.job);
};
Jobs.prototype.decks = function (req) {
  var j = this.job(req.job);
  if (!j.out) throw new ApiError(400, "先に解析を実行してください");
  var corners;
  try { corners = JSON.parse(String(req.corners || "").trim()); }
  catch (e) { throw new ApiError(400, "コーナー定義(JSON)を読めません: " + e.message); }
  var sim = req.sim || "spectre";
  if (sim !== "spectre" && sim !== "hspice") throw new ApiError(400, "シミュレータ " + sim + " には対応していません");
  var res;
  try {
    // the netlist is referenced by its file name and races.meas sits next to the decks,
    // so the unzipped folder runs as is (same layout as the "portable" option of serve)
    res = E.decks({ racesJson: j.out.files["races.json"], corners: corners, sim: sim, measPath: "races.meas", out: "decks" });
  } catch (e) { throw new ApiError(422, "デッキを作れませんでした。", String(e && e.message || e)); }
  var files = {};
  Object.keys(res.files).forEach(function (k) { files[k] = enc.encode(res.files[k]); });
  files["races.meas"] = enc.encode(j.out.files["races.meas"]);
  var netName = (j.netlistName in files) ? "netlist_" + j.netlistName : j.netlistName;
  if (netName !== j.netlistName) {
    Object.keys(res.files).forEach(function (k) {
      if (/\.(scs|sp)$/.test(k)) files[k] = enc.encode(res.files[k].split("'" + j.netlistName + "'").join("'" + netName + "'")
        .split('"' + j.netlistName + '"').join('"' + netName + '"'));
    });
  }
  files[netName] = j.netlist;
  var names = Object.keys(files).sort();
  j.decks = { files: files, zip: makeZip(names.map(function (n) {
    return { name: "decks/" + n, bytes: files[n], mode: n.endsWith(".sh") ? 0o755 : 0o644 };
  })) };
  return { log: res.log, decks: names, portable: true };
};
Jobs.prototype.uploadResult = function (job, rel, bytes) {
  var j = this.job(job);
  rel = safeRelpath(rel || "result.measure");
  if (!RESULT_EXT.test(rel)) return { skipped: rel };
  j.results.set(rel, bytes);
  return { saved: rel };
};
Jobs.prototype.clearResults = function (job) { this.job(job).results = new Map(); return { ok: true }; };
Jobs.prototype.collect = function (req) {
  var j = this.job(req.job);
  if (!j.out) throw new ApiError(400, "先に解析を実行してください");
  if (req.path) throw new ApiError(400, "このページではパスを指定できません。ファイルを選んでください。");
  if (!j.results.size) throw new ApiError(400, "結果ファイルを選んでください");
  var num = function (key, scale, dflt) {
    var v = req[key];
    if (v === undefined || v === null || v === "") return dflt;
    var f = Number(v);
    if (!isFinite(f) || String(v).trim() === "") throw new ApiError(400, key + " が数値ではありません: " + v);
    return f * scale;
  };
  var entries = Array.from(j.results.keys()).map(function (rel) { return { path: "results/" + rel, rel: rel }; });
  var files = E.selectResultFiles(entries).map(function (f) { return { path: f.path, text: text(j.results.get(f.rel)) }; });
  try {
    j.collect = E.collect({ racesJson: j.out.files["races.json"], files: files, out: "summary.csv",
                            warn: num("warn_ps", 1e-12, undefined), warnV: num("warn_v", 1, undefined),
                            skewWarn: num("skew_warn_ps", 1e-12, undefined) });
  } catch (e) {
    if (e instanceof ApiError) throw e;
    j.collect = null;
    throw new ApiError(422, "集計できませんでした。", String(e && e.message || e));
  }
  return { header: j.collect.header, rows: j.collect.rows, log: j.collect.log };
};
Jobs.prototype.file = function (job, rel) {
  var j = this.job(job), bytes = null, name = rel.split("/").pop();
  if (rel.indexOf("out/") === 0 && OUTPUT_FILES.indexOf(rel.slice(4)) >= 0 && j.out) {
    var t = j.out.files[rel.slice(4)];
    bytes = enc.encode(t);
  } else if (rel === "decks.zip" && j.decks) { bytes = j.decks.zip; name = job + "_decks.zip"; }
  else if (rel === "summary.csv" && j.collect) { bytes = enc.encode(j.collect.csv); }
  if (!bytes) throw new ApiError(404, "見つかりません");
  return { name: name, bytes: bytes, type: TYPES[rel.split(".").pop()] || "text/plain" };
};

// ---------------------------------------------------------------- page adapter
// Same paths as the HTTP API of `racepath serve` (racepath/web.py).
Jobs.prototype.route = function (method, path, q, body, bytes) {
  if (method === "GET") {
    if (path === "/api/jobs") return { jobs: this.list() };
    if (path === "/api/job") return this.summary(q.job || "");
    if (path === "/api/default-corners") return JSON.parse(JSON.stringify(DEFAULT_CORNERS));
  } else {
    if (path === "/api/upload") return this.upload(q.name || "", bytes);
    if (path === "/api/upload-result") return this.uploadResult(q.job, q.name || "", bytes);
    if (path === "/api/analyze") return this.analyze(body);
    if (path === "/api/decks") return this.decks(body);
    if (path === "/api/collect") return this.collect(body);
    if (path === "/api/clear-results") return this.clearResults(body.job);
  }
  throw new ApiError(404, "見つかりません");
};

function install(win) {
  var jobs = new Jobs();
  function tick() { return new Promise(function (r) { setTimeout(r, 30); }); }   // let the status text paint
  win.racepathLocal = {
    ready: Promise.resolve(),
    api: async function (method, path, body, raw) {
      var u = new URL(path, "http://local/"), q = {};
      u.searchParams.forEach(function (v, k) { q[k] = v; });
      var bytes = raw ? new Uint8Array(await body.arrayBuffer()) : null;
      await tick();
      try {
        return JSON.parse(JSON.stringify(jobs.route(method, u.pathname, q, raw ? {} : (body || {}), bytes)));
      } catch (e) {
        var err = new Error(e instanceof ApiError ? e.message : "内部エラーです: " + (e && e.message || e));
        err.log = e instanceof ApiError ? e.log : String(e && e.stack || e);
        throw err;
      }
    },
    file: async function (job, rel) {
      var f;
      try { f = jobs.file(job, rel); } catch (e) { throw new Error(e.message); }
      return { name: f.name, blob: new Blob([f.bytes], { type: f.type }) };
    }
  };
  return jobs;
}

var api = { Jobs: Jobs, ApiError: ApiError, install: install, makeZip: makeZip, crc32: crc32,
            safeName: safeName, safeRelpath: safeRelpath };
if (typeof module !== "undefined" && module.exports) module.exports = api;
else root.RacepathJobs = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
