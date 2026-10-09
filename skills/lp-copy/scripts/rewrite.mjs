import * as html from 'parse5';
import postcss from 'postcss';
import values from 'postcss-value-parser';
import { parse as parseJS } from 'acorn';
import { simple } from 'acorn-walk';
import parseSrcset from 'parse-srcset';
import { hash, key } from './network.mjs';

export const CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'none'";
export function localURL(value, base, origin) {
  if (!value || /^(?:data:|blob:|#|mailto:|tel:|javascript:)/i.test(value)) return value;
  const u = new URL(value, base);
  if (!['http:', 'https:'].includes(u.protocol)) return value;
  return (u.origin === origin ? '' : `/_lp_external/${hash(u.origin).slice(0, 20)}`) + u.pathname + u.search + u.hash;
}
export function cssReferences(text, base) {
  const found = new Set();
  const collect = val => {
    if (val && !/^(?:data:|blob:|#)/i.test(val)) {
      try { const u = new URL(val, base); if (/^https?:$/.test(u.protocol)) found.add(key(u.href)); } catch { /* invalid CSS URL */ }
    }
  };
  const root = postcss.parse(text, { from: undefined });
  root.walkDecls(d => values(d.value).walk(n => { if (n.type === 'function' && n.value.toLowerCase() === 'url') collect(values.stringify(n.nodes).replace(/^(['"])(.*)\1$/s, '$2')); }));
  root.walkAtRules('import', r => {
    const node = values(r.params).nodes[0];
    if (node?.type === 'string') collect(node.value);
    else if (node?.type === 'function' && node.value === 'url') collect(values.stringify(node.nodes).replace(/^(['"])(.*)\1$/s, '$2'));
  });
  return [...found];
}
export function rewriteCSS(text, base, origin, exclude = []) {
  const root = postcss.parse(text, { from: undefined });
  root.walkAtRules('font-face', rule => {
    if (cssReferences(rule.toString(), base).some(url => exclude.some(part => url.includes(part)))) rule.remove();
  });
  const rewrite = input => {
    const parsed = values(input);
    parsed.walk(n => {
      if (n.type === 'function' && n.value.toLowerCase() === 'url') {
        const value = values.stringify(n.nodes).replace(/^(['"])(.*)\1$/s, '$2');
        n.nodes = [{ type: 'string', quote: '"', value: localURL(value, base, origin) }];
      }
    });
    return parsed.toString();
  };
  root.walkDecls(d => { d.value = rewrite(d.value); });
  root.walkAtRules('import', r => {
    const parsed = values(r.params), first = parsed.nodes[0];
    if (first?.type === 'string') { first.value = localURL(first.value, base, origin); r.params = parsed.toString(); }
    else r.params = rewrite(r.params);
  });
  return root.toString();
}
export function rewriteJS(text, base, origin, entries, warnings) {
  let ast;
  try { ast = parseJS(text, { ecmaVersion: 'latest', sourceType: 'module', allowReturnOutsideFunction: true }); }
  catch { warnings.push(`JavaScript could not be parsed; inspect external paths: ${base}`); return text; }
  const patches = [];
  simple(ast, { Literal(n) {
    // Only exact acquired absolute asset URLs; never blanket-replace a domain,
    // navigation text, a regular expression, or an unobserved API string.
    if (typeof n.value !== 'string' || !/^(https?:)?\/\//.test(n.value)) return;
    try {
      const entry = entries.get(key(new URL(n.value, base).href));
      if (entry && !entry.contentType.includes('text/html')) patches.push([n.start, n.end, JSON.stringify(localURL(n.value, base, origin))]);
    } catch { /* not a URL literal */ }
  } });
  for (const [start, end, value] of patches.sort((a, b) => b[0] - a[0])) text = text.slice(0, start) + value + text.slice(end);
  return text;
}
export function rewriteHTML(text, base, origin) {
  const doc = html.parse(text);
  const walk = (node, fn) => { fn(node); for (const child of [...node.childNodes ?? [], ...node.content?.childNodes ?? []]) walk(child, fn); };
  walk(doc, node => {
    const baseHref = node.tagName === 'base' && node.attrs.find(a => a.name === 'href');
    if (baseHref) base = new URL(baseHref.value, base).href;
  });
  walk(doc, node => {
    if (!node.attrs) return;
    const attr = name => node.attrs.find(a => a.name === name);
    const remove = () => { node.parentNode.childNodes = node.parentNode.childNodes.filter(x => x !== node); };
    if (node.tagName === 'meta' && (attr('http-equiv')?.value.toLowerCase() === 'content-security-policy' || attr('http-equiv')?.value.toLowerCase() === 'refresh')) { remove(); return; }
    if (node.tagName === 'link' && /(?:^|\s)(?:preconnect|dns-prefetch|prefetch)(?:\s|$)/.test(attr('rel')?.value ?? '')) { remove(); return; }
    node.attrs = node.attrs.filter(a => !['integrity', 'ping'].includes(a.name));
    for (const a of node.attrs) {
      if (a.name === 'style') a.value = rewriteCSS(`x{${a.value}}`, base, origin).slice(2, -1);
      else if (['srcset', 'data-srcset', 'imagesrcset'].includes(a.name)) a.value = parseSrcset(a.value).map(s => localURL(s.url, base, origin) + (s.w ? ` ${s.w}w` : s.d ? ` ${s.d}x` : '')).join(', ');
      else if (['src', 'poster', 'data-src'].includes(a.name) || (a.name === 'href' && ['link', 'base', 'image', 'use'].includes(node.tagName))) a.value = localURL(a.value, base, origin);
    }
    if (node.tagName === 'style') for (const child of node.childNodes ?? []) if (child.nodeName === '#text') child.value = rewriteCSS(child.value, base, origin);
  });
  const head = doc.childNodes.find(n => n.tagName === 'html').childNodes.find(n => n.tagName === 'head');
  const additions = html.parseFragment(`<meta http-equiv="Content-Security-Policy" content="${CSP}"><script src="/_lp_guard.js"></script>`).childNodes;
  for (const n of additions) n.parentNode = head;
  head.childNodes.unshift(...additions);
  return html.serialize(doc);
}
