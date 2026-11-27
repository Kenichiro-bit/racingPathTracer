// Drives src/racepath/js/jobs.js (the in-browser job store) under Node, the way the page does,
// and writes what the page would offer for download into OUT_DIR for tests/test_js_jobs.py.
//   node tests/js/jobs_flow.js NETLIST CORNERS_JSON RESULTS_DIR OUT_DIR
const fs = require('fs');
const path = require('path');
const [netlist, cornersPath, resultsDir, outDir] = process.argv.slice(2);
globalThis.RACEPATH_DEFAULT_CORNERS = JSON.parse(fs.readFileSync(cornersPath, 'utf8'));
globalThis.RacepathEngine = require('../../src/racepath/js/racepath.js');
const { Jobs, ApiError } = require('../../src/racepath/js/jobs.js');

const jobs = new Jobs();
const call = (m, p, q, b, bytes) => JSON.parse(JSON.stringify(jobs.route(m, p, q || {}, b || {}, bytes || null)));
const errOf = f => { try { f(); return null; } catch (e) { return e instanceof ApiError ? { status: e.status, message: e.message } : String(e); } };
const res = {};
const up = call('POST', '/api/upload', { name: path.basename(netlist) }, null, new Uint8Array(fs.readFileSync(netlist)));
res.upload = up;
res.analyze = call('POST', '/api/analyze', {}, { job: up.job });
res.decks = call('POST', '/api/decks', {}, { job: up.job, corners: JSON.stringify(call('GET', '/api/default-corners')), sim: 'hspice' });
const walk = (d, rel) => fs.readdirSync(d).sort().flatMap(n => {
  const f = path.join(d, n), r = rel ? rel + '/' + n : n;
  return fs.statSync(f).isDirectory() ? walk(f, r) : [[r, f]];
});
res.uploads = walk(resultsDir, '').map(([r, f]) => call('POST', '/api/upload-result', { job: up.job, name: r }, null, new Uint8Array(fs.readFileSync(f))));
res.collect = call('POST', '/api/collect', {}, { job: up.job, warn_ps: '20', warn_v: '0.02', skew_warn_ps: '10' });
res.jobs = call('GET', '/api/jobs');
res.summary = call('GET', '/api/job', { job: up.job });
res.errors = {
  path: errOf(() => jobs.route('POST', '/api/analyze', {}, { path: '/etc/passwd' })),
  badcfg: errOf(() => jobs.route('POST', '/api/analyze', {}, { job: up.job, config: '{bad' })),
  nojob: errOf(() => jobs.route('GET', '/api/job', { job: 'nope' }, {})),
  badfile: errOf(() => jobs.file(up.job, 'job.json')),
};
fs.mkdirSync(outDir, { recursive: true });
for (const rel of ['out/report.html', 'out/races.meas', 'out/races_params.inc', 'out/races.csv', 'out/races.json', 'decks.zip', 'summary.csv']) {
  const f = jobs.file(up.job, rel);
  fs.writeFileSync(path.join(outDir, f.name), f.bytes);
}
const top = call('POST', '/api/upload', { name: 'x.cdl' }, null, new TextEncoder().encode('.SUBCKT A x\nMP0 x x VDD VDD pch\n.ENDS\n'));
res.errors.notop = errOf(() => jobs.route('POST', '/api/analyze', {}, { job: top.job, top: 'NOPE' }));
fs.writeFileSync(path.join(outDir, 'flow.json'), JSON.stringify(res, null, 1));
