#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile, readdir, lstat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { Downloads, UA, hash, key, checkURL } from './network.mjs';
import { CSP, cssReferences, htmlCSSReferences, localURL, rewriteCSS, rewriteHTML, rewriteJS } from './rewrite.mjs';

const VIEWPORTS = [{ name: 'desktop', width: 1440, height: 1000 }, { name: 'mobile', width: 390, height: 844 }];
const TRACKING = /(?:google-analytics\.com|googletagmanager\.com|connect\.facebook\.net|\/analytics(?:\/|$)|\/collect(?:\?|$))/i;
const STATIC = /\.(?:m?js|css|woff2?|ttf|otf|png|jpe?g|gif|webp|avif|svg|ico|mp4|webm)(?:\?|$)/i;
const isCSS = e => /text\/css/i.test(e.contentType);
const isJS = e => /(?:javascript|ecmascript)/i.test(e.contentType);
const isHTML = e => /text\/html/i.test(e.contentType);
const styleReferences = e => (isHTML(e) ? htmlCSSReferences : cssReferences)(e.body.toString('utf8'), e.finalURL);
const json = async p => JSON.parse(await readFile(p, 'utf8'));
const saveJSON = (p, data) => writeFile(p, JSON.stringify(data, null, 2) + '\n');

async function ownedDirectory(out, resume) {
  await mkdir(out, { recursive: true });
  if ((await lstat(out)).isSymbolicLink()) throw new Error('Output must not be a symlink');
  const names = await readdir(out);
  if (names.length && !resume) throw new Error('Output is not empty; use a new folder or --resume on a previous capture');
  for (const name of ['raw', 'objects', 'screenshots']) {
    await mkdir(path.join(out, name), { recursive: true });
    if ((await lstat(path.join(out, name))).isSymbolicLink()) throw new Error(`Output subfolder is a symlink: ${name}`);
  }
}

export async function exercise(page, actions = [], viewport = 'desktop') {
  await page.evaluate(() => document.fonts.ready);
  // A finite traversal triggers lazy assets; no whole-site crawling or arbitrary
  // button clicking. Interaction selectors come only from the user's task file.
  const height = page.viewportSize().height;
  for (let step = 0; step < 60; step++) {
    const y = step * height * 0.8;
    if (y > await page.evaluate(() => document.documentElement.scrollHeight)) break;
    await page.evaluate(y => { for (const img of document.images) img.loading = 'eager'; scrollTo(0, y); }, y);
    await page.waitForTimeout(60);
  }
  for (const action of actions) {
    if (action.viewport && action.viewport !== viewport) continue;
    if (action.action === 'press') await page.keyboard.press(action.key);
    else if (action.action === 'click') {
      const locator = action.role ? page.getByRole(action.role, { name: action.name, exact: true }) : page.locator(action.selector);
      await locator.click({ timeout: 5000 });
      if (action.expect) {
        const actual = await locator.getAttribute(action.expect.attribute);
        if (actual !== action.expect.value) throw new Error(`Interaction assertion failed: ${action.name ?? action.selector}`);
      }
    } else throw new Error('Actions support only click or press');
    await page.waitForTimeout(120);
  }
  await page.evaluate(() => scrollTo(0, 0));
  await page.waitForTimeout(250);
}

function guardScript(manifest) {
  const known = Object.keys(manifest.routes);
  return `(() => {
    const paths = new Set(${JSON.stringify(known)});
    const origin = ${JSON.stringify(manifest.origin)};
    const entry = ${JSON.stringify(new URL(manifest.entry).pathname)};
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, init = {}) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : input, location.href);
      const method = (init.method || request?.method || 'GET').toUpperCase();
      if (!['GET', 'HEAD'].includes(method) || url.origin !== location.origin || !paths.has(url.pathname + url.search)) {
        return Promise.resolve(new Response('{"error":"Not available in local preview"}', { status: 503, headers: { 'Content-Type': 'application/json' } }));
      }
      return originalFetch(input, init);
    };
    document.addEventListener('submit', event => { event.preventDefault(); event.stopImmediatePropagation(); }, true);
    let dialog;
    document.addEventListener('click', event => {
      const a = event.target.closest?.('a[href]'); if (!a) return;
      const url = new URL(a.href, location.href);
      if (![location.origin, origin].includes(url.origin)) return;
      if ((url.pathname === entry || url.pathname === '/') && (!url.search || paths.has(url.pathname + url.search))) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!dialog) {
        dialog = document.createElement('dialog');
        dialog.setAttribute('aria-label', 'ローカルプレビュー');
        dialog.style.cssText = 'padding:24px;max-width:420px;border:1px solid #aaa;border-radius:12px;background:white;color:#222';
        const p = document.createElement('p'); p.textContent = 'このページはローカル確認用です。別ページや送信機能は収録していません。';
        const b = document.createElement('button'); b.textContent = '閉じる'; b.onclick = () => dialog.close();
        dialog.append(p, b); document.body.append(dialog);
      }
      if (!dialog.open) dialog.showModal();
    }, true);
  })();`;
}

export async function serve(out, port = 0) {
  const manifest = await json(path.join(out, 'capture.json'));
  if (manifest.version !== 1) throw new Error('Unsupported capture manifest');
  const server = createServer(async (req, res) => {
    try {
      if (!/^(?:127\.0\.0\.1|localhost)(?::\d+)?$/.test(req.headers.host ?? '')) { res.writeHead(403).end(); return; }
      if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405, { Allow: 'GET, HEAD' }).end(); return; }
      const url = new URL(req.url, 'http://127.0.0.1');
      if (url.pathname === '/_lp_guard.js') {
        res.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8', 'Cache-Control': 'no-store' });
        res.end(req.method === 'HEAD' ? undefined : guardScript(manifest)); return;
      }
      const route = manifest.routes[url.pathname + url.search] ?? (url.pathname === '/' && !url.search ? manifest.routes[manifest.entryPath] : null);
      if (!route || !/^[a-f0-9]{64}$/.test(route.file)) { res.writeHead(404).end('Not captured'); return; }
      const body = await readFile(path.join(out, 'objects', route.file));
      res.writeHead(200, { 'Content-Type': route.contentType, 'Content-Length': body.length, 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' });
      res.end(req.method === 'HEAD' ? undefined : body);
    } catch { res.writeHead(500).end('Local preview error'); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  return { server, url: `http://127.0.0.1:${server.address().port}${manifest.entryPath}` };
}

export async function capture(url, out, options = {}) {
  const started = performance.now();
  url = await checkURL(url, options.allowPrivate);
  let previous = {};
  let previousManifest, previousReport;
  if (options.resume) {
    let manifest;
    try { manifest = await json(path.join(out, 'capture.json')); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      manifest = await json(path.join(out, 'capture-progress.json'));
    }
    previousManifest = manifest;
    if (manifest.version !== 1 || manifest.requestedURL !== url) throw new Error('Resume URL does not match this capture');
    previous = manifest.raw ?? {};
    try { previousReport = await json(path.join(out, 'report.json')); } catch { /* Interrupted capture. */ }
    options = { ...options, actions: options.actions ?? manifest.actions, exclude: options.exclude ?? manifest.options?.exclude, dataURLs: options.dataURLs ?? manifest.options?.dataURLs, include: options.include ?? manifest.options?.include };
  }
  await ownedDirectory(out, options.resume);
  await saveJSON(path.join(out, 'capture-progress.json'), { version: 1, requestedURL: url, actions: options.actions ?? [], options: { exclude: options.exclude ?? [], dataURLs: options.dataURLs ?? [], include: options.include ?? [] } });
  const downloads = new Downloads(out, options, previous);
  const blocked = [], failures = [], warnings = [], pageChecks = [];
  const excluded = u => TRACKING.test(u) || (options.exclude ?? []).some(part => u.includes(part));
  const entry = await downloads.get(url);
  if (!isHTML(entry)) throw new Error('The entry URL did not return HTML');
  const finalURL = entry.finalURL, origin = new URL(finalURL).origin;
  await writeFile(path.join(out, 'original.html'), entry.body);
  const inspectionKey = hash(JSON.stringify({ url, ua: UA, actions: options.actions ?? [], exclude: options.exclude ?? [], dataURLs: options.dataURLs ?? [] }));
  let inspectionReused = Boolean(previousManifest?.inspectionKey === inspectionKey && previousReport && !options.refresh && !previousReport.failures.length && Object.values(previous).every(e => Date.now() - e.fetchedAt < (options.cacheAge ?? 3600) * 1000));
  if (inspectionReused) {
    // Hash-check every cache object before reusing the prior source inspection.
    // Missing/corrupt/stale bytes trigger network retrieval and a fresh scan.
    await Promise.all(Object.keys(previous).map(source => downloads.get(source)));
    inspectionReused = downloads.metrics.networkRequests === 0;
  }
  if (inspectionReused) {
    pageChecks.push(...previousReport.pageChecks);
    blocked.push(...previousReport.blocked);
  } else {
    const browser = await chromium.launch({ headless: true });
    try {
    for (const viewport of VIEWPORTS) {
      const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, userAgent: UA, reducedMotion: 'reduce', serviceWorkers: 'block', acceptDownloads: false });
      const page = await context.newPage();
      await context.routeWebSocket('**/*', socket => socket.close());
      await context.route('**/*', async route => {
        const req = route.request(), target = key(req.url()), type = req.resourceType();
        const dataAllowed = (options.dataURLs ?? []).includes(target);
        const safeType = ['stylesheet', 'script', 'image', 'font', 'media'].includes(type) || (['fetch', 'xhr'].includes(type) && (STATIC.test(target) || dataAllowed));
        const documentAllowed = type === 'document' && req.frame() === page.mainFrame() && [url, finalURL].includes(target);
        if (req.method() !== 'GET' || excluded(target) || !(safeType || documentAllowed)) {
          blocked.push({ url: target, type, method: req.method() });
          await route.fulfill({ status: 403, body: '' }); return;
        }
        try {
          const item = await downloads.get(target);
          if (isHTML(item) && type !== 'document') throw new Error('An asset returned HTML');
          if (item.finalURL !== target) {
            await route.fulfill({ status: 302, headers: { location: item.finalURL } });
          } else await route.fulfill({ status: 200, contentType: item.contentType, headers: { 'access-control-allow-origin': '*' }, body: item.body });
        } catch (error) {
          failures.push({ url: target, error: error.message });
          await route.fulfill({ status: 404, body: '' });
        }
      });
      context.on('page', popup => { if (popup !== page) void popup.close(); });
      await page.goto(finalURL, { waitUntil: 'networkidle', timeout: 60000 });
      await exercise(page, options.actions, viewport.name);
      await page.waitForLoadState('networkidle');
      pageChecks.push(await page.evaluate(name => ({ viewport: name, title: document.title, h1: [...document.querySelectorAll('h1')].map(n => n.textContent), height: document.documentElement.scrollHeight }), viewport.name));
      await page.screenshot({ path: path.join(out, 'screenshots', `source-${viewport.name}.png`), fullPage: true, animations: 'disabled' });
      await context.close();
    }
    } finally { await browser.close(); }
  }
  if (options.mode === 'complete') {
    // Complete means the observed pages plus their CSS resource graph. It does
    // not invent JS paths or crawl unrelated application/admin routes.
    const processed = new Set();
    for (let round = 0; round < 20; round++) {
      const styles = [...downloads.entries.values()].filter(e => (isCSS(e) || isHTML(e)) && !processed.has(e.url));
      if (!styles.length) break;
      const refs = new Set();
      for (const css of styles) {
        processed.add(css.url);
        for (const ref of styleReferences(css)) if (!excluded(ref)) refs.add(ref);
      }
      await Promise.all([...refs].map(ref => downloads.get(ref).catch(e => failures.push({ url: ref, error: e.message }))));
      if (round === 19) warnings.push('CSS import depth limit reached');
    }
  }
  // Optional extra assets discovered by the AI during static JS inspection.
  await Promise.all((options.include ?? []).map(ref => downloads.get(new URL(ref, finalURL).href).catch(e => failures.push({ url: ref, error: e.message }))));
  const routes = {};
  for (const [source, item] of downloads.entries) {
    let body = item.body;
    if (isHTML(item)) body = Buffer.from(rewriteHTML(body.toString('utf8'), item.finalURL, origin));
    else if (isCSS(item)) body = Buffer.from(rewriteCSS(body.toString('utf8'), item.finalURL, origin, options.exclude));
    else if (isJS(item)) body = Buffer.from(rewriteJS(body.toString('utf8'), item.finalURL, origin, downloads.entries, warnings));
    const file = hash(body);
    await writeFile(path.join(out, 'objects', file), body);
    routes[localURL(source, source, origin)] = { file, contentType: item.contentType };
    if (source === finalURL) await writeFile(path.join(out, 'index.html'), body);
  }
  const deferred = new Set();
  for (const css of downloads.entries.values()) if (isCSS(css) || isHTML(css)) for (const ref of styleReferences(css)) if (!downloads.entries.has(ref)) deferred.add(ref);
  const manifest = { version: 1, requestedURL: url, entry: finalURL, entryPath: localURL(finalURL, finalURL, origin), origin, mode: options.mode ?? 'quick', actions: options.actions ?? [], options: { exclude: options.exclude ?? [], dataURLs: options.dataURLs ?? [], include: options.include ?? [] }, inspectionKey, routes, raw: downloads.metadata() };
  const report = { mode: manifest.mode, inspectionReused, elapsedSeconds: Number(((performance.now() - started) / 1000).toFixed(3)), assets: downloads.entries.size, savedBytes: downloads.total, ...downloads.metrics, blocked, failures, deferredCSS: [...deferred], warnings, pageChecks };
  await saveJSON(path.join(out, 'capture.json'), manifest);
  await saveJSON(path.join(out, 'report.json'), report);
  return report;
}

export async function verify(out, options = {}) {
  const manifest = await json(path.join(out, 'capture.json'));
  const source = await json(path.join(out, 'report.json'));
  const { server, url } = await serve(out);
  const origin = new URL(url).origin;
  let browser;
  const cases = [];
  try {
    browser = await chromium.launch({ headless: true });
    for (const viewport of VIEWPORTS) {
      const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, userAgent: UA, reducedMotion: 'reduce', serviceWorkers: 'block' });
      const external = [], errors = [], missing = [];
      await context.route('**/*', route => {
        if (new URL(route.request().url()).origin !== origin || !['GET', 'HEAD'].includes(route.request().method())) { external.push(route.request().url()); return route.abort(); }
        return route.continue();
      });
      await context.routeWebSocket('**/*', ws => { external.push(ws.url()); ws.close(); });
      const page = await context.newPage();
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
      page.on('response', response => { if (response.status() >= 400) missing.push(new URL(response.url()).pathname + new URL(response.url()).search); });
      await page.goto(url, { waitUntil: 'networkidle' });
      await exercise(page, options.actions ?? manifest.actions, viewport.name);
      await page.waitForLoadState('networkidle');
      const state = await page.evaluate(() => ({
        title: document.title, h1: [...document.querySelectorAll('h1')].map(n => n.textContent),
        overflow: document.documentElement.scrollWidth > innerWidth,
        brokenImages: [...document.images].filter(i => i.getBoundingClientRect().width && i.loading !== 'lazy' && (!i.complete || !i.naturalWidth)).map(i => i.currentSrc || i.src)
      }));
      const expected = source.pageChecks.find(x => x.viewport === viewport.name);
      const headlineMatches = JSON.stringify(state.h1) === JSON.stringify(expected.h1);
      const passed = !external.length && !errors.length && !missing.length && !state.brokenImages.length && !state.overflow && headlineMatches;
      cases.push({ viewport: viewport.name, passed, ...state, headlineMatches, external, errors, missing });
      await page.screenshot({ path: path.join(out, 'screenshots', `local-${viewport.name}.png`), fullPage: true, animations: 'disabled' });
      await context.close();
    }
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
  const result = { passed: cases.every(c => c.passed), cases };
  await saveJSON(path.join(out, 'verification.json'), result);
  return result;
}

async function main() {
  const command = process.argv[2];
  const { values: args } = parseArgs({ args: process.argv.slice(3), options: {
    url: { type: 'string' }, out: { type: 'string' }, mode: { type: 'string', default: 'quick' },
    resume: { type: 'boolean' }, refresh: { type: 'boolean' }, 'allow-private': { type: 'boolean' },
    actions: { type: 'string' }, include: { type: 'string', multiple: true }, exclude: { type: 'string', multiple: true },
    'data-url': { type: 'string', multiple: true }, port: { type: 'string', default: '8766' },
    'cache-age': { type: 'string', default: '3600' }, concurrency: { type: 'string', default: '8' }
  } });
  if (!['capture', 'verify', 'serve'].includes(command) || !args.out || (command === 'capture' && !args.url)) throw new Error('Usage: node capture.mjs capture --url URL --out NEW_FOLDER [--mode quick|complete] [--actions FILE] [--resume] | verify --out FOLDER | serve --out FOLDER [--port 8766]');
  if (!['quick', 'complete'].includes(args.mode)) throw new Error('Mode must be quick or complete');
  const concurrency = Number(args.concurrency), cacheAge = Number(args['cache-age']), port = Number(args.port);
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16 || !Number.isFinite(cacheAge) || cacheAge < 0 || !Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid concurrency, cache age, or port');
  const out = path.resolve(args.out), actions = args.actions ? await json(args.actions) : undefined;
  if (actions && (!Array.isArray(actions) || actions.length > 100)) throw new Error('Actions must be an array of at most 100 items');
  if (command === 'serve') { const { url } = await serve(out, port); console.log(url); }
  else if (command === 'verify') { const result = await verify(out, { actions }); console.log(JSON.stringify(result, null, 2)); if (!result.passed) process.exitCode = 1; }
  else {
    const result = await capture(args.url, out, { mode: args.mode, resume: args.resume, refresh: args.refresh, allowPrivate: args['allow-private'], actions, include: args.include, exclude: args.exclude, dataURLs: args['data-url'], cacheAge, concurrency });
    console.log(JSON.stringify({ ...result, blocked: result.blocked.length, failures: result.failures, deferredCSS: result.deferredCSS.length }, null, 2));
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(e => { console.error(e.message); process.exitCode = 1; });
