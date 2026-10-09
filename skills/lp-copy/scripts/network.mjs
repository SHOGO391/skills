import { createHash } from 'node:crypto';
import { lookup } from 'node:dns/promises';
import { BlockList, isIP } from 'node:net';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Agent } from 'undici';

export const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
export const hash = value => createHash('sha256').update(value).digest('hex');
export const key = value => { const u = new URL(value); u.hash = ''; return u.href; };
const privateIPs = new BlockList();
for (const [ip, bits] of [['0.0.0.0', 8], ['10.0.0.0', 8], ['127.0.0.0', 8], ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.168.0.0', 16], ['224.0.0.0', 4]]) privateIPs.addSubnet(ip, bits, 'ipv4');
for (const [ip, bits] of [['::', 128], ['::1', 128], ['fc00::', 7], ['fe80::', 10], ['ff00::', 8]]) privateIPs.addSubnet(ip, bits, 'ipv6');
const isPrivate = x => privateIPs.check(x.address, x.family === 6 ? 'ipv6' : 'ipv4');
// Validate the addresses used by the actual socket as well as URL preflight,
// so a changed DNS answer cannot redirect a public capture into the LAN.
const publicDispatcher = new Agent({ connect: { lookup(host, options, callback) {
  lookup(host, { all: true }).then(addresses => {
    if (!addresses.length || addresses.some(isPrivate)) throw new Error('Private network address blocked at connection time');
    if (options.all) callback(null, addresses);
    else callback(null, addresses[0].address, addresses[0].family);
  }).catch(callback);
} } });

export async function checkURL(value, allowPrivate = false) {
  const u = new URL(value);
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error('Only credential-free HTTP(S) URLs are supported');
  if (!allowPrivate) {
    const host = u.hostname.replace(/^\[|\]$/g, '');
    const addresses = isIP(host) ? [{ address: host, family: isIP(host) }] : await lookup(host, { all: true });
    if (!addresses.length || addresses.some(isPrivate)) throw new Error('Private network URL blocked; use --allow-private only for your own fixture');
  }
  return key(u.href);
}

export class Downloads {
  constructor(out, options = {}, previous = {}) {
    this.out = out;
    this.options = options;
    this.previous = previous;
    this.entries = new Map();
    this.pending = new Map();
    this.active = 0;
    this.queue = [];
    this.metrics = { networkRequests: 0, networkBytes: 0, cacheHits: 0, revalidated: 0 };
    this.total = 0;
  }
  async slot(fn) {
    if (this.active >= (this.options.concurrency ?? 8)) await new Promise(resolve => this.queue.push(resolve));
    this.active++;
    try { return await fn(); }
    finally { this.active--; this.queue.shift()?.(); }
  }
  get(url) {
    url = key(url);
    if (!this.pending.has(url)) this.pending.set(url, this.obtain(url).then(async item => {
      const { body, ...metadata } = item;
      // Per-URL checkpoints survive an interaction failure before capture.json
      // can be finalized. Names are hashes, never remote path components.
      await writeFile(path.join(this.out, 'raw', hash(url) + '.json'), JSON.stringify(metadata));
      return item;
    }));
    return this.pending.get(url);
  }
  async obtain(url, redirects = 0) {
    await checkURL(url, this.options.allowPrivate);
    if (redirects > 5) throw new Error('Redirect limit exceeded');
    if (this.pending.size > (this.options.maxAssets ?? 600)) throw new Error('Asset count limit exceeded');
    let old = this.previous[url];
    if (!old) {
      try { old = JSON.parse(await readFile(path.join(this.out, 'raw', hash(url) + '.json'), 'utf8')); }
      catch { /* No usable checkpoint yet. */ }
    }
    let cached;
    if (old?.url === url && /^[a-f0-9]{64}$/.test(old.hash) && old.ua === UA) {
      try {
        const data = await readFile(path.join(this.out, 'raw', old.hash));
        if (hash(data) === old.hash) cached = { ...old, body: data };
      } catch { /* A missing/corrupt cache object is fetched again. */ }
    }
    const remember = record => {
      if (!this.entries.has(url)) {
        this.total += record.body.length;
        if (this.total > (this.options.maxTotalBytes ?? 256 * 1024 * 1024)) throw new Error('Total capture size limit exceeded');
      }
      this.entries.set(url, record);
      return record;
    };
    if (cached && !this.options.refresh && Date.now() - cached.fetchedAt < (this.options.cacheAge ?? 3600) * 1000) {
      this.metrics.cacheHits++;
      return remember(cached);
    }
    const result = await this.slot(async () => {
      const headers = { 'user-agent': UA, accept: '*/*' };
      if (cached?.etag) headers['if-none-match'] = cached.etag;
      else if (cached?.modified) headers['if-modified-since'] = cached.modified;
      this.metrics.networkRequests++;
      const response = await fetch(url, { headers, redirect: 'manual', signal: AbortSignal.timeout(this.options.timeout ?? 20000), ...(this.options.allowPrivate ? {} : { dispatcher: publicDispatcher }) });
      if (response.status === 304 && cached) {
        this.metrics.revalidated++;
        return { ...cached, fetchedAt: Date.now() };
      }
      if (response.status >= 300 && response.status < 400) {
        const next = response.headers.get('location');
        if (!next) throw new Error(`Redirect without Location: ${response.status}`);
        await response.body?.cancel();
        return { redirect: new URL(next, url).href };
      }
      if (!response.ok || response.status === 206) {
        await response.body?.cancel();
        throw new Error(`HTTP ${response.status}: ${url}`);
      }
      const chunks = [];
      let size = 0;
      for await (const chunk of response.body ?? []) {
        size += chunk.length;
        this.metrics.networkBytes += chunk.length;
        if (size > (this.options.maxAssetBytes ?? 32 * 1024 * 1024)) throw new Error('Asset size limit exceeded');
        chunks.push(chunk);
      }
      const body = Buffer.concat(chunks);
      const record = { url, finalURL: url, hash: hash(body), bytes: body.length, contentType: response.headers.get('content-type') ?? 'application/octet-stream', etag: response.headers.get('etag'), modified: response.headers.get('last-modified'), fetchedAt: Date.now(), ua: UA, body };
      await mkdir(path.join(this.out, 'raw'), { recursive: true });
      await writeFile(path.join(this.out, 'raw', record.hash), body);
      return record;
    });
    if (result.redirect) {
      // Validate each redirect before requesting it. Do not forward credentials.
      const target = await this.obtain(key(result.redirect), redirects + 1);
      return remember({ ...target, url, finalURL: target.finalURL });
    }
    return remember(result);
  }
  metadata() { return Object.fromEntries([...this.entries].map(([url, { body, ...entry }]) => [url, entry])); }
}
