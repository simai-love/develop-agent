import { renderMarkdown } from './markdown.mjs';

const view = { repository: 'develop-agent', taskId: null, selected: null, request: 0, mode: 'source', file: null };
let dialog = null;

function element(tag, className = '', text = '') {
  /** Create tag with optional className and text; return the element. */
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function fileUrl(endpoint, path, extra = {}) {
  /** Build an /api/files URL for the current root and path; return the URL string. */
  const params = new URLSearchParams({ repo: view.repository, path, ...extra });
  if (view.taskId) params.set('task', view.taskId);
  return `/api/files${endpoint}?${params}`;
}

async function fetchJson(endpoint, path) {
  /** Fetch a file API endpoint for path and return its parsed JSON value. */
  const response = await fetch(fileUrl(endpoint, path));
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'ファイルを読み込めませんでした');
  return value;
}

function formatSize(size) {
  /** Convert a byte count into a short human-readable label; return it. */
  if (size === null || size === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit ? value.toFixed(1) : value} ${units[unit]}`;
}

function formatTime(iso) {
  /** Convert an ISO time into a local date-time label; return it. */
  return new Date(iso).toLocaleString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function ensureDialog() {
  /** Create the file browser dialog once; return the dialog. */
  if (dialog) return dialog;
  dialog = element('dialog', 'file-browser-dialog');
  dialog.setAttribute('aria-label', 'プロジェクトのファイル');
  dialog.innerHTML = `
    <div class="file-browser-head"><div><div class="dialog-eyebrow">PROJECT FILES / READ ONLY</div><h2>ファイル</h2><p class="file-browser-root"></p></div><button type="button" class="subtle-button file-browser-close" aria-label="ファイル表示を閉じる">閉じる</button></div>
    <div class="file-browser-body">
      <nav class="file-tree" aria-label="ファイルツリー"></nav>
      <section class="file-pane" aria-label="ファイルの内容">
        <div class="file-pane-bar"><div class="file-crumbs"></div><span class="file-pane-space"></span><div class="file-pane-actions"></div></div>
        <div class="file-pane-meta"></div>
        <div class="file-pane-content"></div>
      </section>
    </div>`;
  dialog.querySelector('.file-browser-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
  return dialog;
}

function treeItem(entry, depth) {
  /** Build a lazily expandable tree row for entry at depth; return its list item. */
  const item = element('li', 'file-tree-item');
  const row = element('button', `file-tree-row ${entry.type}`);
  row.type = 'button';
  row.style.paddingLeft = `${8 + depth * 14}px`;
  row.dataset.path = entry.path;
  row.title = entry.path;
  row.append(element('span', 'file-tree-icon', entry.type === 'dir' ? '▸' : '·'), element('span', 'file-tree-name', entry.name + (entry.symlink ? ' ↪' : '')));
  item.append(row);
  if (entry.type === 'dir') {
    const children = element('ul', 'file-tree-children hidden');
    item.append(children);
    row.addEventListener('click', async () => {
      await toggleDirectory(item, entry.path, depth);
      showDirectory(entry.path);
    });
  } else {
    row.addEventListener('click', () => showFile(entry.path));
  }
  return item;
}

async function loadChildren(list, path, depth) {
  /** Fill list with entries of directory path at depth; return the listing. */
  const listing = await fetchJson('', path);
  list.replaceChildren(...listing.entries.map((entry) => treeItem(entry, depth)));
  if (listing.truncated) list.append(element('li', 'file-tree-note', `先頭 ${listing.entries.length} 件のみ表示`));
  if (!listing.entries.length) list.append(element('li', 'file-tree-note', '空のフォルダ'));
  return listing;
}

async function toggleDirectory(item, path, depth, open = null) {
  /** Expand or collapse a directory tree item, loading children on demand; return nothing. */
  const children = item.querySelector(':scope > ul');
  const expand = open ?? children.classList.contains('hidden');
  if (expand && !children.dataset.loaded) {
    children.replaceChildren(element('li', 'file-tree-note', '読み込み中…'));
    children.classList.remove('hidden');
    try {
      await loadChildren(children, path, depth + 1);
      children.dataset.loaded = 'true';
    } catch (error) { children.replaceChildren(element('li', 'file-tree-note', error.message)); }
  }
  children.classList.toggle('hidden', !expand);
  item.querySelector(':scope > button .file-tree-icon').textContent = expand ? '▾' : '▸';
}

function markSelected(path) {
  /** Highlight the tree row for path and return nothing. */
  view.selected = path;
  dialog.querySelectorAll('.file-tree-row.selected').forEach((row) => row.classList.remove('selected'));
  dialog.querySelectorAll('.file-tree-row').forEach((row) => { if (row.dataset.path === path) row.classList.add('selected'); });
}

function renderCrumbs(path) {
  /** Render clickable breadcrumbs for path; return nothing. */
  const crumbs = dialog.querySelector('.file-crumbs');
  const parts = path ? path.split('/') : [];
  const root = element('button', 'file-crumb', 'ルート');
  root.type = 'button';
  root.addEventListener('click', () => showDirectory(''));
  crumbs.replaceChildren(root);
  parts.forEach((part, index) => {
    crumbs.append(element('span', 'file-crumb-separator', '/'));
    const target = parts.slice(0, index + 1).join('/');
    const crumb = element('button', 'file-crumb', part);
    crumb.type = 'button';
    crumb.addEventListener('click', () => (index === parts.length - 1 && view.file ? showFile(target) : revealPath(target).then(() => showDirectory(target))));
    crumbs.append(crumb);
  });
}

function actionButton(label, onClick, active = false) {
  /** Create a small pane action button with label and click handler; return it. */
  const button = element('button', `subtle-button file-action${active ? ' active' : ''}`, label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function copyButton(path) {
  /** Create a button that copies path to the clipboard; return it. */
  const button = actionButton('パスをコピー', async () => {
    await navigator.clipboard.writeText(path || '.');
    button.textContent = 'コピーしました';
    setTimeout(() => { button.textContent = 'パスをコピー'; }, 1500);
  });
  return button;
}

function downloadLink(path) {
  /** Create a download link for path through the raw endpoint; return it. */
  const link = element('a', 'subtle-button file-action', 'ダウンロード');
  link.href = fileUrl('/raw', path, { download: '1' });
  link.setAttribute('download', path.split('/').pop());
  return link;
}

async function showDirectory(path) {
  /** Show the entries of directory path in the content pane; return nothing. */
  const request = ++view.request;
  view.file = null;
  markSelected(path);
  renderCrumbs(path);
  dialog.querySelector('.file-pane-actions').replaceChildren(copyButton(path));
  const content = dialog.querySelector('.file-pane-content');
  const meta = dialog.querySelector('.file-pane-meta');
  meta.textContent = '';
  content.replaceChildren(element('p', 'file-pane-note', '読み込み中…'));
  try {
    const listing = await fetchJson('', path);
    if (request !== view.request) return;
    meta.textContent = `${listing.entries.length} 件${listing.truncated ? '（一部のみ表示）' : ''}`;
    const table = element('table', 'file-listing');
    for (const entry of listing.entries) {
      const row = element('tr');
      const name = element('td');
      const open = element('button', `file-listing-name ${entry.type}`, `${entry.type === 'dir' ? '▸ ' : ''}${entry.name}`);
      open.type = 'button';
      open.addEventListener('click', () => (entry.type === 'dir' ? revealPath(entry.path).then(() => showDirectory(entry.path)) : revealPath(entry.path).then(() => showFile(entry.path))));
      name.append(open);
      row.append(name, element('td', 'file-listing-size', formatSize(entry.size)), element('td', 'file-listing-time', formatTime(entry.modified)));
      table.append(row);
    }
    content.replaceChildren(listing.entries.length ? table : element('p', 'file-pane-note', '空のフォルダです。'));
  } catch (error) {
    if (request === view.request) content.replaceChildren(element('p', 'file-pane-note', error.message));
  }
}

function numberedText(text) {
  /** Render text with a line-number gutter; return the container element. */
  const lines = text.endsWith('\n') ? text.slice(0, -1).split('\n') : text.split('\n');
  const wrapper = element('div', 'file-code');
  const gutter = element('pre', 'file-code-gutter', lines.map((_, index) => index + 1).join('\n'));
  gutter.setAttribute('aria-hidden', 'true');
  const code = element('pre', 'file-code-text');
  code.textContent = lines.join('\n') || ' ';
  wrapper.append(gutter, code);
  return wrapper;
}

function htmlPreview(text) {
  /** Render HTML text in a script-disabled sandboxed iframe; return the iframe. */
  const frame = element('iframe', 'file-html-preview');
  frame.setAttribute('sandbox', '');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.title = 'HTML のプレビュー（スクリプト無効）';
  frame.srcdoc = text;
  return frame;
}

function renderFileBody() {
  /** Render the loaded file in the current source/preview mode; return nothing. */
  const file = view.file;
  const content = dialog.querySelector('.file-pane-content');
  const actions = dialog.querySelector('.file-pane-actions');
  const extension = file.name.split('.').pop().toLowerCase();
  const previewable = file.kind === 'text' && !file.truncated && (['md', 'markdown'].includes(extension) || ['html', 'htm'].includes(extension));
  actions.replaceChildren();
  if (previewable) {
    actions.append(actionButton('ソース', () => { view.mode = 'source'; renderFileBody(); }, view.mode === 'source'), actionButton('プレビュー', () => { view.mode = 'preview'; renderFileBody(); }, view.mode === 'preview'));
  }
  actions.append(copyButton(file.path), downloadLink(file.path));
  if (file.kind === 'image') {
    const image = element('img', 'file-image previewable-image');
    image.src = fileUrl('/raw', file.path);
    image.alt = file.path;
    image.tabIndex = 0;
    image.setAttribute('role', 'button');
    image.setAttribute('aria-label', `${file.name}を拡大`);
    content.replaceChildren(image);
  } else if (file.kind === 'binary') {
    content.replaceChildren(element('p', 'file-pane-note', 'このファイルはテキストとして表示できません。ダウンロードして確認してください。'));
  } else if (previewable && view.mode === 'preview') {
    content.replaceChildren(['html', 'htm'].includes(extension) ? htmlPreview(file.text) : renderMarkdown(file.text));
  } else {
    const nodes = [numberedText(file.text)];
    if (file.truncated) nodes.unshift(element('p', 'file-pane-warning', `ファイルが大きいため先頭 ${formatSize(new TextEncoder().encode(file.text).length)} のみ表示しています。全体はダウンロードしてください。`));
    content.replaceChildren(...nodes);
  }
}

async function showFile(path) {
  /** Load file path and show its text, image, or download notice; return nothing. */
  const request = ++view.request;
  markSelected(path);
  renderCrumbs(path);
  const content = dialog.querySelector('.file-pane-content');
  const meta = dialog.querySelector('.file-pane-meta');
  dialog.querySelector('.file-pane-actions').replaceChildren();
  meta.textContent = '';
  content.replaceChildren(element('p', 'file-pane-note', '読み込み中…'));
  try {
    const file = await fetchJson('/content', path);
    if (request !== view.request) return;
    view.file = file;
    view.mode = 'source';
    meta.textContent = `${formatSize(file.size)} ・ 更新 ${formatTime(file.modified)} ・ ${file.mime}`;
    renderFileBody();
  } catch (error) {
    if (request === view.request) content.replaceChildren(element('p', 'file-pane-note', error.message));
  }
}

async function revealPath(path) {
  /** Expand every ancestor directory of path in the tree; return nothing. */
  const parts = path.split('/');
  for (let index = 1; index < parts.length; index += 1) {
    const ancestor = parts.slice(0, index).join('/');
    const row = [...dialog.querySelectorAll('.file-tree-row.dir')].find((item) => item.dataset.path === ancestor);
    if (!row) return;
    await toggleDirectory(row.parentElement, ancestor, index - 1, true);
  }
}

export async function openFileBrowser({ repository = 'develop-agent', taskId = null, path = '' } = {}) {
  /** Open the read-only browser for a repository or task worktree, optionally at path; return nothing. */
  ensureDialog();
  Object.assign(view, { repository, taskId, selected: null, file: null, mode: 'source' });
  const tree = dialog.querySelector('.file-tree');
  const rootLabel = dialog.querySelector('.file-browser-root');
  const list = element('ul', 'file-tree-children');
  tree.replaceChildren(list);
  rootLabel.textContent = '読み込み中…';
  if (!dialog.open) dialog.showModal();
  try {
    const listing = await loadChildren(list, '', 0);
    rootLabel.textContent = `${listing.root_kind === 'worktree' ? `タスクの作業ツリー（${listing.branch}）` : 'リポジトリ本体'}: ${listing.root}`;
  } catch (error) {
    rootLabel.textContent = error.message;
    return;
  }
  if (!path) { await showDirectory(''); return; }
  await revealPath(path);
  await showFile(path);
}

function loadStylesheet() {
  /** Add the file browser stylesheet to the page once; return nothing. */
  if (document.querySelector('link[href="/files.css"]')) return;
  const link = element('link');
  link.rel = 'stylesheet';
  link.href = '/files.css';
  document.head.append(link);
}

loadStylesheet();
window.openFileBrowser = openFileBrowser;
