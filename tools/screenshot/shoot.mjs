#!/usr/bin/env node
// Screenshot harness for the vidstreamer web UI.
//
// Renders the REAL src/vidstreamer/web/index.html in headless Chrome and
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
const DEFAULT_INDEX = join(REPO_ROOT, 'src', 'vidstreamer', 'web', 'index.html');

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
const SAMPLE_LIBRARY = [
  { title: 'Dune: Part Two', series: null, path: '/media/movies/Dune Part Two (2024).mkv', dir: 'movies', name: 'Dune Part Two (2024).mkv', mtime: 1718000000, season: null, episode: null, ep_title: null },
  { title: 'Blade Runner 2049', series: null, path: '/media/movies/Blade Runner 2049.mkv', dir: 'movies', name: 'Blade Runner 2049.mkv', mtime: 1717000000, season: null, episode: null, ep_title: null },
  { title: 'House of the Dragon S02E07', series: 'House of the Dragon', path: '/media/tv/HotD/S02E07.mkv', dir: 'tv/HotD', name: 'S02E07.mkv', mtime: 1719000000, season: 2, episode: 7, ep_title: 'The Red Sowing' },
  { title: 'House of the Dragon S02E08', series: 'House of the Dragon', path: '/media/tv/HotD/S02E08.mkv', dir: 'tv/HotD', name: 'S02E08.mkv', mtime: 1719500000, season: 2, episode: 8, ep_title: 'The Queen Who Ever Was' },
  { title: 'The Bear S03E01', series: 'The Bear', path: '/media/tv/TheBear/S03E01.mkv', dir: 'tv/TheBear', name: 'S03E01.mkv', mtime: 1716000000, season: 3, episode: 1, ep_title: 'Tomorrow' },
  { title: 'The Bear S03E02', series: 'The Bear', path: '/media/tv/TheBear/S03E02.mkv', dir: 'tv/TheBear', name: 'S03E02.mkv', mtime: 1716100000, season: 3, episode: 2, ep_title: 'Next' },
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
  setNowArt(title);
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
}

function applyFull(items) {
  // Setup view + a populated sidebar, for responsive / whole-shell shots.
  // eslint-disable-next-line no-undef
  libItems = items;
  document.getElementById('libCount').textContent = items.length + ' videos';
  renderLibrary();
  document.body.classList.add('has-art');
}

const SCENES = {
  setup:    { width: 620,  selector: '.app',          apply: null },
  player:   { width: 620,  selector: '#player',       apply: applyPlayer },
  settings: { width: 620,  selector: '#settingsBack .modal', apply: applySettings },
  // ≥821px so the sidebar is in desktop flow (below 820px it slides off-canvas).
  library:  { width: 900,  selector: '#sidebar',      apply: applyLibrary, arg: SAMPLE_LIBRARY },
  full:     { width: 1200, selector: null, fullPage: true, apply: applyFull, arg: SAMPLE_LIBRARY },
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
await page.waitForTimeout(300);           // let transitions settle

if (fullPage) {
  await page.screenshot({ path: out, fullPage: true });
} else {
  const el = await page.$(selector);
  if (!el) { console.error(`Selector not found: ${selector}`); await browser.close(); process.exit(1); }
  await el.screenshot({ path: out });
}
await browser.close();
console.log(`wrote ${out}  (scene=${sceneName}, width=${width}, ${fullPage ? 'full-page' : selector})`);
