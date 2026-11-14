"use strict";

import { evidenceUrl } from './markdown.mjs';

const BASE_STYLE = `
:root { color:#d9e5da; font:13px/1.8 'DM Sans','Noto Sans JP',sans-serif; overflow-wrap:anywhere }
html,body { margin:0; background:transparent; overflow-y:hidden }
#document-root { display:flow-root; overflow-x:auto; padding:2px 2px 8px }
h1,h2,h3,h4 { color:#e7f1e8; line-height:1.5; margin:20px 0 8px }
h1 { font-size:19px } h2 { font-size:16px } h3 { font-size:14px; color:#cce8ce } h4,h5,h6 { font-size:12px; color:#bde1bf }
#document-root > :first-child { margin-top:0 }
p { margin:6px 0 } ul,ol { margin:6px 0 12px; padding-left:20px } li { margin:4px 0 }
strong { color:#f0f7ef } a { color:#a9ddae }
table { border-collapse:collapse; display:block; max-width:100%; overflow:auto; margin:10px 0 16px; font-size:12px }
th,td { border:1px solid #485d4b; padding:6px 10px; text-align:left; vertical-align:top }
th { background:#293b2d; color:#e7f1e8 }
svg { display:block; max-width:100%; height:auto; margin:10px 0 }
svg:not([width]) { width:min(100%,640px) }
svg text:not([fill]) { fill:currentColor }
img,video { display:block; max-width:min(100%,720px); max-height:520px; object-fit:contain; margin:8px 0; border-radius:5px }
figure { margin:12px 0 18px } figcaption { color:#a7b8a9; font-size:11px; margin-top:4px }
pre { white-space:pre-wrap; background:#141a15; padding:12px; border-radius:5px }
code { font:12px 'DM Mono',monospace; background:#26322a; padding:2px 4px; border-radius:3px } pre code { background:none; padding:0 }
blockquote { border-left:3px solid #789b7b; margin:8px 0 15px; padding:5px 14px; color:#c7d8c9 }
.callout { border-left:3px solid #8ccb90; background:#26392a; padding:9px 14px; margin:8px 0 15px; color:#e8f6e9; border-radius:0 5px 5px 0 }
.callout.important,.callout.warning { border-left-color:#e0ad61; background:#332b20 }
.callout > :first-child { margin-top:0 } .callout > :last-child { margin-bottom:0 }
hr { border:0; border-top:1px solid #465b49; margin:15px 0 }
.math-block { overflow-x:auto; margin:12px 0 }
`;

const knownHeights = new Map();
const EVIDENCE_REFERENCE = /\b(src|href|poster)\s*=\s*(["'])(?:\.\.\/|\.\/)?evidence\/([A-Za-z0-9._-]+)\2/gi;
const SKIPPED_MATH = /^<(svg|code|pre|style|script|textarea)\b/i;

function decodeEntities(value) {
  /** Read HTML text with common entities; return the decoded plain text. */
  return value
    .replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&amp;/g, '&');
}

function plainText(fragment) {
  /** Read an HTML fragment; return its readable text with one line per block, ignoring SVG, style, and comments. */
  return decodeEntities(fragment
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(svg|style|script|template)\b[\s\S]*?<\/\1\s*>/gi, '')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|li|tr|div|h[1-6]|ul|ol|table|blockquote|figcaption|section)\s*>/gi, '\n')
    .replace(/<\/t[dh]\s*>/gi, ' ')
    .replace(/<[^>]*>/g, ''))
    .split('\n').map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean).join('\n');
}

export function htmlSection(source, heading) {
  /** Read HTML source and a heading text; return the plain text under the first matching h1-h3 until the next h1/h2. */
  const pattern = /<h([1-3])\b[^>]*>([\s\S]*?)<\/h\1\s*>/gi;
  for (const match of source.replace(/<!--[\s\S]*?-->/g, '').matchAll(pattern)) {
    if (plainText(match[2]) !== heading) continue;
    const rest = match.input.slice(match.index + match[0].length);
    const end = rest.search(/<h[12]\b/i);
    return plainText(end < 0 ? rest : rest.slice(0, end));
  }
  return '';
}

function renderMath(source) {
  /** Replace $...$ and $$...$$ in text outside SVG, code, and pre with KaTeX HTML; return the source and whether math was found. */
  const katex = globalThis.katex;
  if (!katex?.renderToString || !source.includes('$')) return { html: source, math: false };
  let math = false;
  let skipped = null;
  const parts = source.split(/(<[^>]+>)/);
  const html = parts.map((part) => {
    if (part.startsWith('<')) {
      const open = part.match(SKIPPED_MATH);
      if (!skipped && open && !part.endsWith('/>')) skipped = open[1].toLowerCase();
      else if (skipped && new RegExp(`^</${skipped}\\s*>`, 'i').test(part)) skipped = null;
      return part;
    }
    if (skipped) return part;
    return part
      .replace(/\$\$([\s\S]+?)\$\$/g, (_, formula) => { math = true; return `<div class="math-block">${katex.renderToString(decodeEntities(formula), { displayMode: true, throwOnError: false, trust: false })}</div>`; })
      .replace(/\$([^$\n]+?)\$/g, (_, formula) => { math = true; return katex.renderToString(decodeEntities(formula), { throwOnError: false, trust: false }); });
  }).join('');
  return { html, math };
}

export function buildSrcdoc(source, taskId, origin) {
  /** Read plan or report HTML, its task ID, and the page origin; return a self-contained srcdoc with base styles, CSP, and evidence URLs. */
  const body = source
    .replace(/<script\b[\s\S]*?<\/script\s*>/gi, '')
    .replace(/<(meta|base|link)\b[^>]*>/gi, '')
    .replace(/<\/?(html|head|body)\b[^>]*>/gi, '')
    .replace(/<!doctype[^>]*>/gi, '')
    .replace(EVIDENCE_REFERENCE, (_, attribute, quote, name) => `${attribute}=${quote}${origin}${evidenceUrl(taskId, name)}${quote}`);
  const { html, math } = renderMath(body);
  const policy = `default-src 'none'; img-src ${origin} data:; media-src ${origin}; style-src 'unsafe-inline' ${origin}; font-src ${origin} data:`;
  return '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
    + `<meta http-equiv="Content-Security-Policy" content="${policy}">`
    + '<base target="_blank">'
    + (math ? `<link rel="stylesheet" href="${origin}/vendor/katex/katex.min.css">` : '')
    + `<style>${BASE_STYLE}</style></head><body><div id="document-root">${html}</div></body></html>`;
}

export function fitFrame(frame) {
  /** Read a same-origin sandboxed iframe; set its height to the rendered content height and return nothing. */
  const root = frame.contentDocument?.getElementById('document-root');
  if (!root || !frame.offsetParent) return;
  const height = Math.ceil(root.getBoundingClientRect().height) + 2;
  if (height <= 0) return;
  frame.style.height = `${height}px`;
  knownHeights.set(frame.srcdoc, height);
}

export function renderHtmlDocument(source, taskId, label) {
  /** Read HTML source, a task ID, and an accessible label; return a script-free sandboxed iframe that fits its content. */
  const frame = document.createElement('iframe');
  frame.className = 'html-document';
  frame.title = label;
  frame.setAttribute('sandbox', 'allow-same-origin allow-popups allow-popups-to-escape-sandbox');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.srcdoc = buildSrcdoc(source, taskId, location.origin);
  if (knownHeights.has(frame.srcdoc)) frame.style.height = `${knownHeights.get(frame.srcdoc)}px`;
  frame.addEventListener('load', () => fitFrame(frame));
  new ResizeObserver(() => fitFrame(frame)).observe(frame);
  return frame;
}
