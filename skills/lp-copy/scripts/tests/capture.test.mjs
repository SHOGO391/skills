import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { capture, verify, serve } from '../capture.mjs';
import { Downloads, checkURL } from '../network.mjs';
import { rewriteCSS, rewriteHTML, rewriteJS, cssReferences } from '../rewrite.mjs';

const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#0ac"/></svg>';
const listen = async handler => {
  const server = createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}` };
};
const close = server => new Promise(resolve => server.close(resolve));

test('URL and parsers preserve asset queries, CSS imports, data URLs, and ordinary links', async () => {
  await assert.rejects(checkURL('http://127.0.0.1/private'), /Private network/);
  await assert.rejects(checkURL('http://[::ffff:127.0.0.1]/private'), /Private network/);
  assert.equal(await checkURL('https://8.8.8.8/'), 'https://8.8.8.8/');
  await assert.rejects(checkURL('https://name:pass@example.com/'), /credential/);
  const css = '@import "nested.css" screen; x{background:url(../IMAGE.PNG?v=2);mask:url(#id)}';
  assert.deepEqual(cssReferences(css, 'https://example.com/css/site.css'), ['https://example.com/IMAGE.PNG?v=2', 'https://example.com/css/nested.css']);
  const result = rewriteCSS(css, 'https://example.com/css/site.css', 'https://example.com');
  assert.ok(result.includes('"/IMAGE.PNG?v=2"'));
  assert.ok(result.includes('"/css/nested.css" screen'));
  assert.ok(result.includes('url("#id")'));
  assert.equal(rewriteCSS('@font-face{font-family:X;src:url(/excluded.woff2)}body{color:red}', 'https://example.com/a.css', 'https://example.com', ['excluded.woff2']), 'body{color:red}');
  const doc = rewriteHTML('<head><base href="/path/"><meta http-equiv="refresh" content="0;url=/other"></head><body><a href="https://external.test/go">Go</a><img srcset="data:image/gif;base64,AAAA 1x, hi.PNG?q=1 2x"><script src="x.js" integrity="old"></script></body>', 'https://example.com/start', 'https://example.com');
  assert.ok(doc.includes('data:image/gif;base64,AAAA 1x, /path/hi.PNG?q=1 2x'));
  assert.ok(doc.includes('href="https://external.test/go"'));
  assert.ok(!doc.includes('integrity="old"'));
  assert.ok(!doc.includes('http-equiv="refresh"'));
  const entries = new Map([['https://cdn.test/logo.svg', { contentType: 'image/svg+xml' }]]);
  const js = rewriteJS('const a="https://cdn.test/logo.svg", nav="https://cdn.test/about";', 'https://example.com/app.js', 'https://example.com', entries, []);
  assert.ok(js.includes('/_lp_external/'));
  assert.ok(js.includes('nav="https://cdn.test/about"'));
});

test('download cache deduplicates, validates hash, revalidates, and caps response size', async () => {
  const out = await mkdtemp(path.join(os.tmpdir(), 'lp-download-'));
  let requests = 0;
  const source = await listen((req, res) => {
    requests++;
    if (req.url === '/redirect') { res.writeHead(302, { location: '/asset' }).end(); return; }
    if (req.headers['if-none-match'] === '"one"') { res.writeHead(304).end(); return; }
    res.writeHead(200, { 'content-type': 'image/svg+xml', etag: '"one"' }).end(svg);
  });
  try {
    const options = { allowPrivate: true };
    const cold = new Downloads(out, options);
    const result = await Promise.all([cold.get(source.url + '/asset'), cold.get(source.url + '/asset')]);
    assert.equal(requests, 1); assert.equal(result[0].hash, result[1].hash);
    const warm = new Downloads(out, options, cold.metadata());
    await warm.get(source.url + '/asset'); assert.equal(requests, 1); assert.equal(warm.metrics.cacheHits, 1);
    const refreshed = new Downloads(out, { ...options, refresh: true }, cold.metadata());
    await refreshed.get(source.url + '/asset'); assert.equal(requests, 2); assert.equal(refreshed.metrics.revalidated, 1);
    await writeFile(path.join(out, 'raw', result[0].hash), 'corrupt');
    const repaired = new Downloads(out, options, cold.metadata());
    await repaired.get(source.url + '/asset'); assert.equal(requests, 3);
    await assert.rejects(new Downloads(out, { ...options, maxAssetBytes: 5 }).get(source.url + '/big'), /size limit/);
    assert.equal((await new Downloads(out, options).get(source.url + '/redirect')).finalURL, source.url + '/asset');
  } finally { await close(source.server); await rm(out, { recursive: true, force: true }); }
});

test('quick and complete captures replay offline; warm rerun avoids network and preserves source originals', { timeout: 120000 }, async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'lp-browser-'));
  let writes = 0, hits = 0;
  const cdn = await listen((req, res) => { hits++; res.writeHead(200, { 'content-type': 'image/svg+xml' }).end(svg); });
  const source = await listen((req, res) => {
    hits++;
    if (req.method !== 'GET') writes++;
    const send = (type, text) => res.writeHead(200, { 'content-type': type }).end(text);
    const url = new URL(req.url, 'http://fixture');
    if (url.pathname === '/') send('text/html', `<!doctype html><html><head><title>Fixture</title><link rel="stylesheet" href="/css/site.css"></head><body><h1>テストLP</h1><img src="/image.svg?v=1"><picture><source media="(max-width:600px)" srcset="/image.svg?v=2"><img src="/image.svg?v=3"></picture><button aria-expanded="false" aria-controls="menu">Menu</button><div id="menu" hidden></div><div style="height:1000px"></div><img loading="lazy" src="/lazy.PNG"><script type="module" src="/js/app.js"></script></body></html>`);
    else if (url.pathname === '/css/site.css') send('text/css', '@import "nested.css"; @font-face{font-family:Unused;src:url(/unused.woff2)}body{margin:0}img{width:150px}');
    else if (url.pathname === '/css/nested.css') send('text/css', 'h1{color:rgb(20,80,120)}');
    else if (url.pathname === '/js/app.js') send('text/javascript', `import {value} from './util.js';const external="${cdn.url}/logo.svg";const img=new Image();img.src=external;document.body.append(img);document.querySelector('button').onclick=e=>{const b=e.currentTarget;const m=document.querySelector('#menu');m.hidden=!m.hidden;b.setAttribute('aria-expanded',String(!m.hidden));if(!m.hidden)m.innerHTML='<img src="/MENU.PNG?v=4">'};fetch('/api/private',{method:'POST',body:'blocked'}).catch(()=>{});`);
    else if (url.pathname === '/js/util.js') send('text/javascript', 'export const value=1;');
    else if (url.pathname === '/unused.woff2') send('font/woff2', 'UNUSED-FONT-FIXTURE');
    else if (/\.(?:svg|PNG)$/.test(url.pathname)) send('image/svg+xml', svg);
    else res.writeHead(404).end();
  });
  const actions = [{ action: 'click', role: 'button', name: 'Menu', expect: { attribute: 'aria-expanded', value: 'true' } }, { action: 'click', role: 'button', name: 'Menu', expect: { attribute: 'aria-expanded', value: 'false' } }];
  try {
    const out = path.join(root, 'quick');
    const first = await capture(source.url, out, { allowPrivate: true, actions });
    assert.equal(writes, 0); assert.equal(first.failures.length, 0);
    const manifest = JSON.parse(await readFile(path.join(out, 'capture.json')));
    for (const suffix of ['/image.svg?v=1', '/image.svg?v=2', '/image.svg?v=3', '/MENU.PNG?v=4', '/js/util.js']) assert.ok(manifest.routes[suffix], suffix);
    assert.ok(!manifest.routes['/unused.woff2']);
    assert.ok((await readFile(path.join(out, 'original.html'), 'utf8')).includes('src="/lazy.PNG"'));
    assert.equal((await verify(out)).passed, true);
    const oldHits = hits;
    const warm = await capture(source.url, out, { allowPrivate: true, actions, resume: true });
    assert.equal(warm.networkRequests, 0); assert.equal(hits, oldHits); assert.ok(warm.cacheHits > 0);
    assert.equal(warm.inspectionReused, true);
    const complete = await capture(source.url, out, { allowPrivate: true, actions, resume: true, mode: 'complete' });
    assert.equal(complete.networkRequests, 1); assert.ok(complete.assets > first.assets);
    assert.equal(complete.inspectionReused, true);
    assert.equal((await verify(out)).passed, true);
    const interrupted = path.join(root, 'interrupted');
    await assert.rejects(capture(source.url, interrupted, { allowPrivate: true, actions: [...actions, { action: 'unsupported' }] }), /only click or press/);
    assert.equal(JSON.parse(await readFile(path.join(interrupted, 'capture-progress.json'))).requestedURL, source.url + '/');
    const recovered = await capture(source.url, interrupted, { allowPrivate: true, actions, resume: true });
    // Failure occurred on desktop, so only the previously unseen mobile srcset
    // is fetched; every completed desktop download is reused.
    assert.equal(recovered.networkRequests, 1);
    assert.ok(recovered.cacheHits > 0);
    assert.equal((await verify(interrupted)).passed, true);
    const preview = await serve(out);
    try {
      assert.equal((await fetch(new URL('/raw/anything', preview.url))).status, 404);
      assert.equal((await fetch(preview.url, { method: 'POST' })).status, 405);
    } finally { await close(preview.server); }
    await assert.rejects(capture(source.url, out, { allowPrivate: true }), /not empty/);
    await assert.rejects(capture(source.url + '/other', out, { allowPrivate: true, resume: true }), /does not match/);
    assert.equal(writes, 0);
  } finally { await close(source.server); await close(cdn.server); await rm(root, { recursive: true, force: true }); }
});
