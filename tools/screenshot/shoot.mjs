#!/usr/bin/env node
// Screenshot harness for the subcast web UI.
//
// Renders the REAL src/subcast/web/index.html in headless Chrome and
// captures a chosen "scene" — a known UI state populated with representative
// data, so no backend/casting session is needed. Uses the system Chrome by
// default (no `playwright install` required).
//
// Usage:
//   node tools/screenshot/shoot.mjs --scene player --out player.png
//   node tools/screenshot/shoot.mjs --scene full --width 760     # responsive
//   node tools/screenshot/shoot.mjs --list
//
// Flags:
//   --scene <name>     one of: setup | player | settings | library | full  (default: setup)
//   --out <path>       output PNG path (default: <scene>.png in cwd)
//   --width <px>       viewport width (default: per-scene)
//   --selector <css>   override the element to capture
//   --full-page        capture the whole page instead of an element
//   --scale <n>        deviceScaleFactor (default: 2)
//   --index <path>     override the index.html location
//   --art <dir>        inject cached posters (<key>.jpg) from an art_library dir
//                      so tiles show real artwork instead of the gradient
//   --list             print available scenes and exit
//
// Chrome resolution: $CHROME_PATH, then common system paths, then Playwright's
// bundled 'chrome' channel. Override with CHROME_PATH=/path/to/chrome.

import { existsSync } from 'fs';
import { execSync } from 'child_process';
import { createRequire } from 'module';
import { fileURLToPath, pathToFileURL } from 'url';
import { dirname, resolve, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');
const DEFAULT_INDEX = join(REPO_ROOT, 'src', 'subcast', 'web', 'index.html');

// ---- tiny arg parser ----
function parseArgs(argv) {
  const a = { scale: 2 };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--list') a.list = true;
    else if (k === '--full-page') a.fullPage = true;
    else if (k.startsWith('--')) a[k.slice(2)] = argv[++i];
  }
  return a;
}

// ---- scenes ----
// Each scene: { width, selector, apply } where apply() runs in the page to
// drive the UI into the desired state. apply() may reference the page's own
// top-level helpers (setNowArt, paintRange, renderLibrary, …) by name.
// `art` mirrors the {key, query, kind, year} descriptor the real backend attaches
// to each library item (see artwork.describe). The harness can inject matching
// cached posters by key via --art (otherwise tiles render the gradient).
const HOTD_ART = { key: 'tv_houseofthedragon', query: 'House of the Dragon', kind: 'tv', year: '' };
const BEAR_ART = { key: 'tv_thebear', query: 'The Bear', kind: 'tv', year: '' };
const SAMPLE_LIBRARY = [
  { title: 'Dune: Part Two', series: null, path: '/media/movies/Dune Part Two (2024).mkv', dir: 'movies', name: 'Dune Part Two (2024).mkv', mtime: 1718000000, season: null, episode: null, ep_title: null, art: { key: 'mv_duneparttwo', query: 'Dune Part Two', kind: 'movie', year: '' } },
  { title: 'Blade Runner 2049', series: null, path: '/media/movies/Blade Runner 2049.mkv', dir: 'movies', name: 'Blade Runner 2049.mkv', mtime: 1717000000, season: null, episode: null, ep_title: null, art: { key: 'mv_bladerunner_2049', query: 'Blade Runner', kind: 'movie', year: '2049' } },
  { title: 'House of the Dragon S02E07', series: 'House of the Dragon', path: '/media/tv/HotD/S02E07.mkv', dir: 'tv/HotD', name: 'S02E07.mkv', mtime: 1719000000, season: 2, episode: 7, ep_title: 'The Red Sowing', art: HOTD_ART },
  { title: 'House of the Dragon S02E08', series: 'House of the Dragon', path: '/media/tv/HotD/S02E08.mkv', dir: 'tv/HotD', name: 'S02E08.mkv', mtime: 1719500000, season: 2, episode: 8, ep_title: 'The Queen Who Ever Was', art: HOTD_ART },
  { title: 'The Bear S03E01', series: 'The Bear', path: '/media/tv/TheBear/S03E01.mkv', dir: 'tv/TheBear', name: 'S03E01.mkv', mtime: 1716000000, season: 3, episode: 1, ep_title: 'Tomorrow', art: BEAR_ART },
  { title: 'The Bear S03E02', series: 'The Bear', path: '/media/tv/TheBear/S03E02.mkv', dir: 'tv/TheBear', name: 'S03E02.mkv', mtime: 1716100000, season: 3, episode: 2, ep_title: 'Next', art: BEAR_ART },
];

function applyPlayer() {
  const $ = (id) => document.getElementById(id);
  const title = 'House of the Dragon — S02E08';
  $('setup').style.display = 'none';
  $('player').style.display = 'block';
  $('connDot').classList.add('live');
  $('nowTitle').textContent = title;
  $('deviceName').textContent = 'Living Room TV';
  $('connText').textContent = 'Living Room TV';
  document.body.classList.add('casting', 'has-art');
  setNowArt(title, { key: 'tv_houseofthedragon', query: 'House of the Dragon', kind: 'tv', year: '' });
  last.dur = 7245; last.t = 1325; last.at = performance.now(); last.state = 'PLAYING';
  $('stateBadge').textContent = 'PLAYING';
  setPlayIcon(true);
  $('seek').max = last.dur; $('seek').value = last.t; paintRange($('seek'));
  $('curTime').textContent = fmtClock(last.t);
  $('durTime').textContent = fmtClock(last.dur);
  $('volume').value = 0.8; paintRange($('volume')); setVolIcon(false);
  $('subRow').style.display = 'block';
  subOffset = 0.5; renderSubOff();
}

function applySubResults() {
  // Setup view with online subtitle results listed and the 2nd result chosen —
  // exercises the collapse-on-select + green "done" button states.
  const $ = (id) => document.getElementById(id);
  document.body.classList.add('has-art');
  $('source').value = 'https://host/House.of.the.Dragon.S02E08.1080p.WEB-DL.mkv';
  $('trackBlock').style.display = 'block';
  // eslint-disable-next-line no-undef
  onlineAvailable = true;
  const box = $('subResults');
  box.innerHTML = "<div class='head'>Online (OpenSubtitles)</div>";
  const results = [
    { name: 'House.of.the.Dragon.S02E08.1080p.WEB-DL.SuccessfulCrab', lang: 'English', download_count: 4210, file_id: 1 },
    { name: 'House of the Dragon - 2x08 - The Queen Who Ever Was', lang: 'English', download_count: 980, file_id: 2 },
    { name: 'HotD.S02E08.WEBRip.x264-MeGusta', lang: 'English', download_count: 152, file_id: 3 },
  ];
  // eslint-disable-next-line no-undef
  results.forEach((r) => box.appendChild(onlineRow(r)));
  box.style.display = 'block';
  const rows = box.querySelectorAll('.res');
  // eslint-disable-next-line no-undef
  selectSidecar('/media/subs/hotd.s02e08.en.srt', 'hotd.s02e08.en.srt');
  // eslint-disable-next-line no-undef
  chooseResult(rows[1], rows[1].querySelector('button'));
}

function applySettings() {
  const $ = (id) => document.getElementById(id);
  fillLangSelect($('setLang'), 'en', 'en');
  $('setMediaRoot').value = '/media';
  $('setApiKey').value = '';
  $('settingsBack').classList.add('show');
}

function applyLibrary(items) {
  // eslint-disable-next-line no-undef
  libItems = items;
  document.getElementById('libCount').textContent = items.length + ' videos';
  renderLibrary();
  // The sidebar is an overlay drawer (off-canvas by default) — slide it in so the
  // element capture frames the populated panel rather than its hidden position.
  document.getElementById('shell').classList.add('lib-open');
}

function applyFull(items) {
  // Setup view + a populated sidebar, for responsive / whole-shell shots.
  // eslint-disable-next-line no-undef
  libItems = items;
  document.getElementById('libCount').textContent = items.length + ' videos';
  renderLibrary();
  document.body.classList.add('has-art');
  // Pretend a source was entered so the setup backdrop reflects its poster
  // (matches what /api/probe returns for an entered URL/path).
  document.getElementById('source').value =
    'https://host/House.of.the.Dragon.S02E08.1080p.WEB-DL.mkv';
  // eslint-disable-next-line no-undef
  setSetupArt('House of the Dragon', { key: 'tv_houseofthedragon', query: 'House of the Dragon', kind: 'tv', year: '' });
}

function applyDrawer(items) {
  // Setup view with the library drawer slid OPEN over the centred card + scrim.
  // Inlined (not calling applyFull) because each apply() is serialized standalone.
  // eslint-disable-next-line no-undef
  libItems = items;
  document.getElementById('libCount').textContent = items.length + ' videos';
  renderLibrary();
  document.body.classList.add('has-art');
  document.getElementById('source').value =
    'https://host/House.of.the.Dragon.S02E08.1080p.WEB-DL.mkv';
  // eslint-disable-next-line no-undef
  setSetupArt('House of the Dragon', { key: 'tv_houseofthedragon', query: 'House of the Dragon', kind: 'tv', year: '' });
  document.getElementById('shell').classList.add('lib-open');
  document.getElementById('libToggle').setAttribute('aria-expanded', 'true');
}

const SCENES = {
  setup:    { width: 620,  selector: '.app',          apply: null },
  player:   { width: 980,  selector: '#player',       apply: applyPlayer },
  subs:     { width: 620,  selector: '#trackBlock',   apply: applySubResults },
  settings: { width: 620,  selector: '#settingsBack .modal', apply: applySettings },
  library:  { width: 900,  selector: '#sidebar',      apply: applyLibrary, arg: SAMPLE_LIBRARY },
  full:     { width: 1200, selector: null, fullPage: true, apply: applyFull, arg: SAMPLE_LIBRARY },
  // Drawer open over the cast card at desktop width (full-page so the scrim shows).
  drawer:   { width: 1200, selector: null, fullPage: true, apply: applyDrawer, arg: SAMPLE_LIBRARY },
};

// ---- chrome / playwright resolution ----
function findChrome() {
  if (process.env.CHROME_PATH && existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const cands = [
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  return cands.find(existsSync) || null;
}

async function loadPlaywright() {
  try { return await import('playwright'); }
  catch {
    const globalRoot = execSync('npm root -g').toString().trim();
    const require = createRequire(import.meta.url);
    return require(join(globalRoot, 'playwright'));
  }
}

// ---- main ----
const args = parseArgs(process.argv.slice(2));

if (args.list) {
  console.log('Available scenes:');
  for (const [name, s] of Object.entries(SCENES)) {
    console.log(`  ${name.padEnd(9)} → ${s.fullPage ? 'full page' : s.selector} @ ${s.width}px`);
  }
  process.exit(0);
}

const sceneName = args.scene || 'setup';
const scene = SCENES[sceneName];
if (!scene) {
  console.error(`Unknown scene "${sceneName}". Try --list.`);
  process.exit(1);
}

const indexPath = args.index ? resolve(args.index) : DEFAULT_INDEX;
if (!existsSync(indexPath)) { console.error(`index.html not found at ${indexPath}`); process.exit(1); }

const out = args.out || `${sceneName}.png`;
const width = args.width ? +args.width : scene.width;
const selector = args.selector || scene.selector;
const fullPage = args.fullPage || (!args.selector && scene.fullPage) || false;

const { chromium } = await loadPlaywright();
const chromePath = findChrome();
const launchOpts = chromePath ? { executablePath: chromePath } : { channel: 'chrome' };

const browser = await chromium.launch(launchOpts);
const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: +args.scale });
page.on('pageerror', () => {});           // swallow the offline API errors fired on load
await page.goto(pathToFileURL(indexPath).href, { waitUntil: 'domcontentloaded' });

if (scene.apply) await page.evaluate(scene.apply, scene.arg);

// Inject real cached art (no backend needed). The --art dir is an art_library:
// <key>.jpg is a poster, <key>.bg.jpg a wide backdrop. We map each to its
// composite id (poster -> key, backdrop -> "backdrop:key"), point the page's
// artUrlFor at the local files, mark them ready, and let the real onArtReady /
// applyToEl do the rest (so backdrops fill, posters tile, the player bg swaps).
if (args.art) {
  const artDir = resolve(args.art);
  const { readdirSync } = await import('fs');
  const dirUrl = pathToFileURL(join(artDir, '/')).href;
  const entries = readdirSync(artDir)
    .filter((f) => f.endsWith('.jpg'))
    .map((f) => {
      const isBg = f.endsWith('.bg.jpg');
      const key = f.slice(0, f.length - (isBg ? '.bg.jpg'.length : '.jpg'.length));
      return { cid: isBg ? 'backdrop:' + key : key, key,
               variant: isBg ? 'backdrop' : 'poster', url: dirUrl + f };
    });
  await page.evaluate((entries) => {
    const map = {};
    entries.forEach((e) => (map[e.cid] = e.url));
    // eslint-disable-next-line no-undef, no-global-assign
    artUrlFor = (cid) => map[cid] || ('/api/art/' + cid);   // resolve to local files
    entries.forEach((e) => {
      // eslint-disable-next-line no-undef
      const d = artDesc.get(e.cid) || {};
      d.key = e.key; d.variant = e.variant;
      // eslint-disable-next-line no-undef
      artDesc.set(e.cid, d); artState.set(e.cid, 'ready'); onArtReady(e.cid);
    });
  }, entries);
}

await page.waitForTimeout(400);           // let transitions + poster images settle

if (fullPage) {
  await page.screenshot({ path: out, fullPage: true });
} else {
  const el = await page.$(selector);
  if (!el) { console.error(`Selector not found: ${selector}`); await browser.close(); process.exit(1); }
  await el.screenshot({ path: out });
}
await browser.close();
console.log(`wrote ${out}  (scene=${sceneName}, width=${width}, ${fullPage ? 'full-page' : selector})`);
