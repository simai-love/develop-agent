import assert from 'node:assert/strict';
import { renderMarkdown } from '../app/static/markdown.mjs';

class FakeNode {
  /** Create a fake DOM node from tag and optional text; return the node. */
  constructor(tagName, value = '') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.value = value;
  }

  /** Append children to this node and return nothing. */
  append(...children) { this.children.push(...children); }

  /** Replace child content with a text value and return nothing. */
  set textContent(value) { this.children = []; this.value = value; }

  /** Read descendant text and return its combined value. */
  get textContent() { return this.value + this.children.map((child) => child.textContent).join(''); }
}

globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (value) => new FakeNode('#text', value),
};

function descendants(node, tagName) {
  /** Collect descendants of node with tagName and return them. */
  return node.children.flatMap((child) => [
    ...(child.tagName === tagName.toUpperCase() ? [child] : []),
    ...descendants(child, tagName),
  ]);
}

const plan = renderMarkdown('# 開発計画\n\n## 確認したいこと\n> [!IMPORTANT]\n> - **対象は？**：A／B\n\n## 目的\n> [!NOTE]\n> **速度を改善する**');
assert.equal(descendants(plan, 'h1')[0].textContent, '開発計画');
assert.equal(descendants(plan, 'h2').length, 2);
assert.equal(descendants(plan, 'aside').length, 2);
assert.equal(descendants(plan, 'strong').length, 2);
assert.equal(descendants(plan, 'li')[0].textContent, '対象は？：A／B');

const report = renderMarkdown('**結果**と `測定値`\n\n| 条件 | 値 |\n| --- | --- |\n| 前 | 10 |\n\n![グラフ](../evidence/plot.png)', 'abcdef123456');
assert.equal(descendants(report, 'table').length, 1);
assert.equal(descendants(report, 'code')[0].textContent, '測定値');
assert.equal(descendants(report, 'img')[0].src, '/api/tasks/abcdef123456/evidence/plot.png');

const unsafe = renderMarkdown('<script>alert(1)</script> [危険](javascript:alert)');
assert.equal(descendants(unsafe, 'script').length, 0);
assert.equal(descendants(unsafe, 'a').length, 0);
assert.match(unsafe.textContent, /<script>alert\(1\)<\/script>/);

console.log('Markdown rendering tests passed');
