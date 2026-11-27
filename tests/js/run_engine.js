#!/usr/bin/env node
// Command-line front end for the JavaScript engine with the same arguments and outputs as
// `python -m racepath`, used by tests/test_js_parity.py to compare the two implementations.
//
//   node tests/js/run_engine.js analyze NETLIST [-o OUT] [-f FMT] [-t TOP] [-s NET]... [--static GLOB]... [-c CFG.json]
//                                       [--prefix P] [--show N] [--no-group] [-q]
//   node tests/js/run_engine.js decks RACES_JSON CORNERS_JSON [-o OUT] [--sim spectre|hspice]
//   node tests/js/run_engine.js collect RACES_JSON RESULTS... [-o OUT] [--warn S] [--warn-v V] [--skew-warn S]
const fs = require('fs');
const path = require('path');
const E = require('../../src/racepath/js/racepath.js');

function parse(argv, spec) {
  const o = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (spec.flags.includes(a)) { o[a] = true; continue; }
    if (spec.opts.includes(a)) { const v = argv[++i]; if (spec.multi.includes(a)) (o[a] = o[a] || []).push(v); else o[a] = v; continue; }
    o._.push(a);
  }
  return o;
}
function readText(p) { return new TextDecoder('utf-8', { ignoreBOM: true }).decode(fs.readFileSync(p)); }
function walk(dir, rel, out) {
  for (const name of fs.readdirSync(dir).sort()) {
    const full = path.join(dir, name), r = rel ? rel + '/' + name : name;
    if (fs.statSync(full).isDirectory()) walk(full, r, out); else out.push({ path: full, rel: r });
  }
  return out;
}

const [cmd, ...rest] = process.argv.slice(2);
try {
  if (cmd === 'analyze') {
    const a = parse(rest, { flags: ['--no-group', '-q'], opts: ['-o', '-f', '-t', '-s', '--static', '-c', '--prefix', '--show'], multi: ['-s', '--static'] });
    const netlist = a._[0], out = a['-o'] || 'racepath_out';
    const res = E.analyze({
      text: readText(netlist), netlist, netlistAbs: path.resolve(netlist), out,
      format: a['-f'] || 'auto', top: a['-t'], sources: a['-s'] || [], static: a['--static'] || [],
      config: a['-c'] ? JSON.parse(readText(a['-c'])) : null, prefix: a['--prefix'],
      noGroup: !!a['--no-group'], show: a['--show'] !== undefined ? parseInt(a['--show'], 10) : 30, quiet: !!a['-q'],
    });
    fs.mkdirSync(out, { recursive: true });
    for (const [n, t] of Object.entries(res.files)) fs.writeFileSync(path.join(out, n), t);
    process.stdout.write(res.log);
  } else if (cmd === 'decks') {
    const a = parse(rest, { flags: [], opts: ['-o', '--sim'], multi: [] });
    const out = a['-o'] || 'racepath_decks';
    const res = E.decks({
      racesJson: readText(a._[0]), corners: JSON.parse(readText(a._[1])), sim: a['--sim'], out,
      measPath: path.resolve(path.dirname(a._[0]), 'races.meas'),
    });
    fs.mkdirSync(out, { recursive: true });
    for (const [n, t] of Object.entries(res.files)) fs.writeFileSync(path.join(out, n), t);
    fs.chmodSync(path.join(out, 'run_all.sh'), 0o755);
    process.stdout.write(res.log);
  } else if (cmd === 'collect') {
    const a = parse(rest, { flags: [], opts: ['-o', '--warn', '--warn-v', '--skew-warn'], multi: [] });
    const out = a['-o'] || 'racepath_summary.csv';
    let files = [];
    for (const p of a._.slice(1)) {
      if (fs.statSync(p).isDirectory()) files = files.concat(E.selectResultFiles(walk(p, '', []).map(f => ({ path: path.join(p, f.rel), rel: f.rel }))));
      else if (fs.existsSync(p)) files.push({ path: p, rel: path.basename(p) });
    }
    files = files.map(f => ({ path: f.path, text: readText(f.path) }));
    const num = k => a[k] !== undefined ? parseFloat(a[k]) : undefined;
    const res = E.collect({ racesJson: readText(a._[0]), files, out, warn: num('--warn'), warnV: num('--warn-v'), skewWarn: num('--skew-warn') });
    fs.writeFileSync(out, res.csv);
    process.stdout.write(res.log);
  } else {
    throw new Error('usage: run_engine.js analyze|decks|collect ...');
  }
} catch (e) {
  if (e.log !== undefined) { process.stdout.write(e.log.slice(0, e.log.length - e.message.length - 1)); }
  process.stderr.write(e.message + '\n');
  process.exit(1);
}
