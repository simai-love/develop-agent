import assert from 'node:assert/strict';
import { setEvidenceRepository } from '../app/static/markdown.mjs';
import { buildSrcdoc, htmlSection } from '../app/static/html-document.mjs';

const plan = `<!-- 案内 -->
<h1>開発計画</h1>
<h2>確認したいこと</h2>
<div class="callout important"><ul><li><strong>対象は？</strong>：A／B</li><li>範囲は？</li></ul></div>
<h2 id="purpose">目的</h2>
<div class="callout note"><p><strong>速度を&amp;改善する</strong></p></div>
<svg viewBox="0 0 10 10"><text>図の文字</text></svg>
<h3>補足</h3><p>同じ節の補足</p>
<h2>実装方針</h2><p>別の節</p>`;
assert.equal(htmlSection(plan, '確認したいこと'), '対象は？：A／B\n範囲は？');
assert.equal(htmlSection(plan, '目的'), '速度を&改善する\n補足\n同じ節の補足');
assert.equal(htmlSection(plan, '存在しない'), '');
assert.equal(htmlSection('<h2>結論</h2><div class="callout note"><p><strong>改善を確認</strong></p></div>', '結論'), '改善を確認');

const report = '<script>alert(1)</script><link rel="stylesheet" href="https://example.com/a.css"><img src="../evidence/plot.png" alt="図"><video controls src="evidence/run.mp4"></video><a href="https://example.com">外部</a>';
const srcdoc = buildSrcdoc(report, 'abcdef123456', 'http://127.0.0.1:8000');
assert.doesNotMatch(srcdoc, /<script/i);
assert.doesNotMatch(srcdoc, /example\.com\/a\.css/);
assert.match(srcdoc, /src="http:\/\/127\.0\.0\.1:8000\/api\/tasks\/abcdef123456\/evidence\/plot\.png"/);
assert.match(srcdoc, /src="http:\/\/127\.0\.0\.1:8000\/api\/tasks\/abcdef123456\/evidence\/run\.mp4"/);
assert.match(srcdoc, /Content-Security-Policy" content="default-src 'none'; img-src http:\/\/127\.0\.0\.1:8000 data:/);
assert.match(srcdoc, /<div id="document-root">/);
assert.doesNotMatch(srcdoc, /katex\.min\.css/);

setEvidenceRepository('gpu_solver');
assert.match(buildSrcdoc('<img src="../evidence/a.png">', 'abcdef123456', 'http://localhost'), /evidence\/a\.png\?repo=gpu_solver"/);
setEvidenceRepository('develop-agent');

globalThis.katex = { renderToString: (source, options) => `<span class="katex">${options.displayMode ? 'block' : 'inline'}:${source}</span>` };
const math = buildSrcdoc('<p>速度は $v = d/t$ です。</p><p>$$a &lt; b$$</p><code>$x$</code><svg><text>$y$</text></svg>', 'abcdef123456', 'http://localhost');
assert.match(math, /inline:v = d\/t/);
assert.match(math, /<div class="math-block"><span class="katex">block:a < b<\/span><\/div>/);
assert.match(math, /<code>\$x\$<\/code>/);
assert.match(math, /<text>\$y\$<\/text>/);
assert.match(math, /katex\.min\.css/);
delete globalThis.katex;

console.log('HTML document tests passed');
