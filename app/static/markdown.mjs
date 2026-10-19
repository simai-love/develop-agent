"use strict";

function safeHref(value, taskId) {
  /** Convert a safe Markdown destination to a URL, or return null. */
  const evidence = value.match(/^(?:\.\.\/)?evidence\/([A-Za-z0-9._-]+)$/);
  if (evidence && taskId) return `/api/tasks/${taskId}/evidence/${encodeURIComponent(evidence[1])}`;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function appendInline(parent, value, taskId) {
  /** Append safe links, images, math, emphasis, and code from one Markdown line. */
  const pattern = /(!?)\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\$([^$\n]+)\$|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*]+)\*/g;
  let position = 0;
  for (const match of value.matchAll(pattern)) {
    parent.append(document.createTextNode(value.slice(position, match.index)));
    if (match[2]) {
      const href = safeHref(match[3], taskId);
      if (!href) parent.append(document.createTextNode(match[0]));
      else if (match[1] && /\.(png|jpe?g|webp|gif)$/i.test(match[3])) {
        const image = document.createElement('img');
        image.src = href;
        image.alt = match[2];
        image.loading = 'lazy';
        parent.append(image);
      } else {
        const link = document.createElement('a');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = match[2];
        parent.append(link);
      }
    } else if (match[5]) {
      const formula = document.createElement('span');
      if (globalThis.katex) globalThis.katex.render(match[5], formula, { throwOnError: false, trust: false });
      else formula.textContent = match[0];
      parent.append(formula);
    } else {
      const tag = match[4] ? 'code' : match[8] ? 'em' : 'strong';
      const element = document.createElement(tag);
      element.textContent = match[4] || match[6] || match[7] || match[8];
      parent.append(element);
    }
    position = match.index + match[0].length;
  }
  parent.append(document.createTextNode(value.slice(position)));
}

function tableCells(line) {
  /** Split one pipe-delimited Markdown row and return its cell strings. */
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

export function renderMarkdown(markdown, taskId = null) {
  /** Convert Markdown input into safe DOM nodes and return the rendered container. */
  const container = document.createElement('div');
  container.className = 'rendered-markdown';
  const lines = markdown.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/);
  let list = null;
  let listParent = null;
  let paragraph = null;
  let paragraphParent = null;
  let callout = null;
  let code = null;

  for (let index = 0; index < lines.length; index += 1) {
    const source = lines[index];
    let line = source.trim();
    if (line.startsWith('```')) {
      if (code) code = null;
      else {
        const pre = document.createElement('pre');
        code = document.createElement('code');
        pre.append(code);
        (callout || container).append(pre);
      }
      list = null;
      paragraph = null;
      continue;
    }
    if (code) { code.textContent += `${source}\n`; continue; }
    if (!line) { list = null; paragraph = null; callout = null; continue; }
    const quoted = line.startsWith('>');
    if (quoted) {
      line = line.replace(/^>\s?/, '');
      const marker = line.match(/^\[!(NOTE|IMPORTANT|TIP|WARNING)\]$/);
      if (marker) {
        callout = document.createElement('aside');
        callout.className = `markdown-callout ${marker[1].toLowerCase()}`;
        container.append(callout);
        list = null;
        paragraph = null;
        continue;
      }
      if (!callout) {
        callout = document.createElement('blockquote');
        container.append(callout);
      }
    } else callout = null;
    const parent = callout || container;

    if (line.startsWith('$$')) {
      const formula = [line.slice(2)];
      while (!formula.at(-1).endsWith('$$') && index + 1 < lines.length) formula.push(lines[++index].replace(/^>\s?/, '').trim());
      formula[formula.length - 1] = formula.at(-1).replace(/\$\$$/, '');
      const block = document.createElement('div');
      block.className = 'math-block';
      if (globalThis.katex) globalThis.katex.render(formula.join('\n'), block, { displayMode: true, throwOnError: false, trust: false });
      else block.textContent = `$$${formula.join('\n')}$$`;
      parent.append(block);
      list = null;
      paragraph = null;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInline(element, heading[2], taskId);
      parent.append(element);
      list = null;
      paragraph = null;
      continue;
    }
    if (/^(?:-{3,}|\*{3,})$/.test(line)) {
      parent.append(document.createElement('hr'));
      list = null;
      paragraph = null;
      continue;
    }

    if (line.includes('|') && index + 1 < lines.length) {
      const headers = tableCells(line);
      const separators = tableCells(lines[index + 1]);
      if (headers.length === separators.length && separators.every((cell) => /^:?-{3,}:?$/.test(cell))) {
        const table = document.createElement('table');
        const head = document.createElement('thead');
        const headerRow = document.createElement('tr');
        for (const value of headers) {
          const cell = document.createElement('th');
          appendInline(cell, value, taskId);
          headerRow.append(cell);
        }
        head.append(headerRow);
        table.append(head);
        const body = document.createElement('tbody');
        index += 2;
        while (index < lines.length && lines[index].trim().includes('|')) {
          const row = document.createElement('tr');
          for (const value of tableCells(lines[index])) {
            const cell = document.createElement('td');
            appendInline(cell, value, taskId);
            row.append(cell);
          }
          body.append(row);
          index += 1;
        }
        table.append(body);
        parent.append(table);
        index -= 1;
        list = null;
        paragraph = null;
        continue;
      }
    }

    const bullet = line.match(/^[-*+]\s+(.+)$/);
    const numbered = line.match(/^\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      const tag = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag || listParent !== parent) {
        list = document.createElement(tag);
        parent.append(list);
        listParent = parent;
      }
      const item = document.createElement('li');
      appendInline(item, (bullet || numbered)[1], taskId);
      list.append(item);
      paragraph = null;
      continue;
    }

    if (!paragraph || paragraphParent !== parent) {
      paragraph = document.createElement('p');
      parent.append(paragraph);
      paragraphParent = parent;
    } else paragraph.append(document.createElement('br'));
    appendInline(paragraph, line, taskId);
    list = null;
  }
  return container;
}
