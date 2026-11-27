// End-to-end check of the single-file build (racepath.html).
// Opens it from file:// with ALL network access blocked, then runs
// analyze -> tap inside report -> decks (zip download) -> collect, on desktop and phone width.
//
// usage: node scripts/check_offline_html.js [racepath.html] [screenshot_dir]
const { chromium, devices } = require('playwright');
const fs = require('fs');
const os = require('os');
const path = require('path');

const html = path.resolve(process.argv[2] || path.join(__dirname, '..', 'racepath.html'));
const shots = process.argv[3];
const root = path.resolve(__dirname, '..');
const netlist = path.join(root, 'examples', 'sram_demo.cdl');

function assert(cond, msg) { if (!cond) { throw new Error('FAILED: ' + msg); } }

function resultsDir() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'rp-sim-'));
  fs.mkdirSync(path.join(d, 'tt_0p80v_25c.raw'));
  fs.writeFileSync(path.join(d, 'tt_0p80v_25c.raw', 'tran1.measure'),
    'rp006_margin = 120m\nrp001_margin = 50p\nrp001_margin_end = 40p\n' +
    'rp003_margin = 30p\nrp004_margin = 25p\nrp002_skew = 5p\nrp005_skew = 100p\n');
  fs.writeFileSync(path.join(d, 'ss_0p72v_m40c.mt0'),
    "$DATA1 SOURCE='HSPICE'\n.TITLE 'x'\n" +
    'rp006_margin rp001_margin rp001_margin_end rp003_margin rp004_margin rp002_skew rp005_skew temper alter#\n' +
    '1.0e-02 -1.0e-11 3.0e-11 1.0e-11 failed -5.0e-12 9.0e-11 -40.0 1\n');
  return d;
}

async function flow(browser, opts, tag) {
  const ctx = await browser.newContext({ ...opts, acceptDownloads: true });
  const blocked = [];
  await ctx.route('**/*', route => {
    const u = route.request().url();
    if (u.startsWith('file:') || u.startsWith('blob:') || u.startsWith('data:')) return route.continue();
    blocked.push(u);
    return route.abort();
  });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  const t0 = Date.now();
  await p.goto('file://' + html);
  await p.waitForFunction(() => !document.getElementById('btn-analyze').disabled, null, { timeout: 120000 });
  const boot = (Date.now() - t0) / 1000;
  assert(await p.isHidden('#tab-path'), 'path tab must be hidden offline');

  await p.setInputFiles('#netfile', netlist);
  const t1 = Date.now();
  await p.click('#btn-analyze');
  await p.waitForSelector('#st-analyze.ok, #st-analyze.err', { timeout: 120000 });
  const status = await p.textContent('#st-analyze');
  assert(status.includes('競合 6 件'), 'analyze status: ' + status + ' / ' + (await p.textContent('#r-log')));
  const chips = await p.$$eval('#r-chips .chip', e => e.map(x => x.innerText));
  assert(chips.length === 1 && chips[0].startsWith('CLK') && chips[0].includes('6'), 'input pin chips ' + chips);

  // The report is loaded into the iframe from a blob URL. Right after it appears, a click can
  // land a few milliseconds before the report's own script has attached its click handler, so
  // tap, check, and retry a few times (a person cannot tap that fast).
  const fr = p.frameLocator('#r-frame');
  const sae = fr.locator('a.net[data-rid="rp006"][data-net="SAE"]').first();
  let hits = [], tries = 0;
  while (tries < 5 && !(hits.length === 1 && hits[0] === 'XDUT.SAE')) {
    tries++;
    await sae.click();
    await p.waitForTimeout(400);
    hits = await fr.locator('#m-rp006 .v.hit').allTextContents();
  }
  assert(hits.length === 1 && hits[0] === 'XDUT.SAE', 'report tap highlight ' + hits);
  const tapTries = tries;

  // plain in-page links of the report (card -> measures -> back to card) must scroll too
  const frame = p.frames().find(f => f.url().startsWith('blob:'));
  await frame.evaluate(() => window.scrollTo(0, 0));
  await fr.locator('#r-rp006 a:has-text("測定文を見る")').click();
  await p.waitForTimeout(800);
  // the block is near the end of the page, so it may stop short of the top: it must be scrolled to and visible
  const m = await frame.evaluate(() => ({ top: Math.round(document.getElementById('m-rp006').getBoundingClientRect().top),
    y: Math.round(scrollY), h: innerHeight, max: document.documentElement.scrollHeight - innerHeight }));
  assert(m.y > 0 && m.top > -30 && m.top < m.h - 40 && (Math.abs(m.top) < 30 || m.y >= m.max - 2),
    'link to measures did not scroll: ' + JSON.stringify(m));
  await fr.locator('#m-rp006 a:has-text("カードへ戻る")').click();
  await p.waitForTimeout(800);
  const cTop = await frame.evaluate(() => Math.round(document.getElementById('r-rp006').getBoundingClientRect().top));
  assert(Math.abs(cTop) < 30, 'link back to the card did not scroll: top=' + cTop);

  // download races.meas
  const [dlMeas] = await Promise.all([p.waitForEvent('download'), p.click('#r-files a:has-text("races.meas")')]);
  const measText = fs.readFileSync(await dlMeas.path(), 'utf8');
  assert(measText.includes('rp006_tsae') && dlMeas.suggestedFilename() === 'races.meas', 'races.meas download');

  // decks
  await p.click('#btn-decks');
  await p.waitForSelector('#st-decks.ok, #st-decks.err', { timeout: 60000 });
  assert((await p.getAttribute('#st-decks', 'class')).includes('ok'), 'decks: ' + await p.textContent('#st-decks'));
  const deckList = await p.textContent('#d-out pre');
  assert(deckList.includes('races.meas') && deckList.includes('sram_demo.cdl'), 'portable decks list ' + deckList);
  const [dlZip] = await Promise.all([p.waitForEvent('download'), p.click('#d-out a')]);
  assert(dlZip.suggestedFilename().endsWith('_decks.zip'), 'zip name ' + dlZip.suggestedFilename());
  const zipBytes = fs.readFileSync(await dlZip.path());
  assert(zipBytes.slice(0, 2).toString() === 'PK' && zipBytes.includes(Buffer.from('decks/tt_0p80v_25c.scs')), 'zip content');

  // collect (folder upload)
  await p.setInputFiles('#c-dir', resultsDir());
  await p.click('#btn-collect');
  await p.waitForSelector('#st-collect.ok, #st-collect.err', { timeout: 60000 });
  assert((await p.getAttribute('#st-collect', 'class')).includes('ok'), 'collect: ' + await p.textContent('#st-collect'));
  const verdicts = await p.$$eval('#c-out tr td:nth-child(3)', e => e.map(x => x.textContent));
  assert(verdicts.includes('NG') && verdicts.includes('順序反転'), 'verdicts ' + verdicts);

  // reopen from the in-session job list
  await p.click('#jobs li:first-child button');
  await p.waitForTimeout(300);
  assert(await p.isVisible('#c-out table'), 'reopen job');

  const sw = await p.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  assert(sw[0] <= sw[1], 'horizontal scroll ' + sw);
  assert(errors.length === 0, 'page errors ' + errors);
  if (shots) {
    await p.locator('#sec-result').scrollIntoViewIfNeeded();
    await p.screenshot({ path: path.join(shots, `offline_${tag}.png`) });
  }
  console.log(`${tag}: OK  boot ${boot.toFixed(1)}s, report tap ${tapTries} try, analyze ${((Date.now() - t1) / 1000).toFixed(1)}s total,` +
    ` blocked network requests: ${blocked.length}`);
  assert(blocked.length === 0, 'network requests attempted: ' + blocked);
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    await flow(browser, { viewport: { width: 1280, height: 900 } }, 'desktop');
    await flow(browser, { ...devices['iPhone 13'], colorScheme: 'dark' }, 'phone_dark');
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e.message || e); process.exit(1); });
