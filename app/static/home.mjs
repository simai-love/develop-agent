import { openFileBrowser } from './files.mjs';
/* Home view: task cards for every project, memos, assigned Agents, daily reports, and night runs. */

const $ = (selector) => document.querySelector(selector);
const filterStorageKey = 'develop-agent.home.filter';
const sortStorageKey = 'develop-agent.home.sort';
const statusOrder = ['questions', 'awaiting-approval', 'review', 'verifying', 'running', 'approved', 'planning', 'not-started', 'completed'];
const filters = [['all', 'すべて'], ['open', '未完了'], ['done', '完了']];
const sorts = [['updated', '更新日時'], ['created', '作成日時'], ['status', 'ステータス']];
const runLabels = { implement: '実装', experiment: '追加実験', plan: '計画作成' };
const stateLabels = { succeeded: '成功', failed: '失敗', cancelled: '中断', skipped: '開始できず', stopped: '計画中で停止' };
let context = null;
let data = null;
let loadedAt = 0;
let loading = null;
const editingMemo = new Map();

function readStored(key, fallback, allowed) {
  /** Return a stored choice for key when allowed, otherwise fallback. */
  try {
    const value = window.localStorage.getItem(key);
    return allowed.some(([id]) => id === value) ? value : fallback;
  } catch { return fallback; }
}

function store(key, value) {
  /** Save a per-browser choice and return nothing. */
  try { window.localStorage.setItem(key, value); } catch { /* Storage may be unavailable. */ }
}

const view = { filter: readStored(filterStorageKey, 'open', filters), sort: readStored(sortStorageKey, 'updated', sorts) };

function element(tag, className = '', text = '') {
  /** Create an element with an optional class and text; return it. */
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function formatTime(value) {
  /** Format an ISO timestamp as a short local date and time; return text. */
  return value ? new Intl.DateTimeFormat('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '';
}

export function initHome(options) {
  /** Store app callbacks: api, toast, openTask, createTask, models, and providers; return nothing. */
  context = options;
  $('#close-home-report').addEventListener('click', () => $('#home-report-dialog').close());
}

export async function showHome(force = false) {
  /** Load home data when forced or stale, then render the home view; return nothing. */
  if (!force && editingMemo.size) return;
  if (!force && data && Date.now() - loadedAt < 15000) { renderHome(); return; }
  loading ??= context.api('/api/home').then((value) => { data = value; loadedAt = Date.now(); }).finally(() => { loading = null; });
  await loading;
  renderHome();
}

export function renderApproval(task, { busy, dirty }) {
  /** Update the plan approval button and readiness text for task; return nothing. */
  const button = $('#approve-plan');
  const approved = Boolean(task.plan_approved);
  button.textContent = approved ? '承認を取り消す' : '計画を承認';
  button.disabled = busy || task.status === 'completed' || (!approved && (!task.can_implement || dirty || Boolean(task.child_count)));
  button.title = approved ? '夜間の自動実装を取りやめます' : '承認すると夜間（0:00）に担当 Agent が実装します';
  if (approved) {
    $('#readiness-reason').textContent = task.auto_run_plan_hash === task.plan_approval.plan_hash
      ? 'この計画は承認済みで、夜間に自動実行しました。必要なら手動で実装に進めます。'
      : '計画を承認済みです。夜間（0:00）に自動で実装します。今すぐ実装に進むこともできます。';
  }
}

function visibleTasks(tasks) {
  /** Filter and sort tasks by the selected home options; return the visible list. */
  const filtered = tasks.filter((task) => view.filter === 'all' || (view.filter === 'done') === (task.status === 'completed'));
  const byTime = (key) => (a, b) => b[key].localeCompare(a[key]);
  const compare = view.sort === 'created' ? byTime('created_at')
    : view.sort === 'status' ? (a, b) => statusOrder.indexOf(a.display_status.key) - statusOrder.indexOf(b.display_status.key) || byTime('updated_at')(a, b)
      : byTime('updated_at');
  return filtered.sort(compare);
}

function renderToolbar() {
  /** Render the filter and sort controls; return nothing. */
  const toolbar = $('#home-toolbar');
  const group = (label, options, key, storageKey) => {
    const wrapper = element('div', 'home-segment');
    wrapper.setAttribute('role', 'group');
    wrapper.setAttribute('aria-label', label);
    wrapper.append(element('span', 'home-segment-label', label));
    for (const [id, name] of options) {
      const button = element('button', view[key] === id ? 'active' : '', name);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(view[key] === id));
      button.addEventListener('click', () => { view[key] = id; store(storageKey, id); renderHome(); });
      wrapper.append(button);
    }
    return wrapper;
  };
  const refresh = element('button', 'subtle-button', '再読み込み');
  refresh.type = 'button';
  refresh.addEventListener('click', () => showHome(true).catch((error) => context.toast(error.message)));
  toolbar.replaceChildren(group('表示', filters, 'filter', filterStorageKey), group('並び順', sorts, 'sort', sortStorageKey), refresh);
}

function taskCard(project, task) {
  /** Build one clickable task card for project; return it. */
  const card = element('button', `home-card ${task.display_status.key}`);
  card.type = 'button';
  card.title = `${task.title}：${task.display_status.label}（${task.display_status.detail}）`;
  const chips = element('div', 'home-card-chips');
  chips.append(element('span', `home-chip ${task.display_status.key}`, task.display_status.label));
  if (task.plan_approved && task.display_status.key !== 'approved') chips.append(element('span', 'home-chip approval', '計画承認済み'));
  card.append(element('strong', 'home-card-title', task.title), chips, element('span', 'home-card-detail', task.display_status.detail));
  if (task.parent_title) card.append(element('span', 'home-card-parent', `親: ${task.parent_title}`));
  card.append(element('span', 'home-card-time', `更新 ${formatTime(task.updated_at)}`));
  card.addEventListener('click', () => context.openTask(project.id, task.id).catch((error) => context.toast(error.message)));
  return card;
}

function agentPicker(project) {
  /** Build the assigned Agent CLI and model selectors for project; return them. */
  const wrapper = element('div', 'home-agent');
  wrapper.append(element('span', 'home-agent-label', '担当 Agent'));
  const provider = element('select');
  provider.setAttribute('aria-label', `${project.name} の担当 Agent CLI`);
  const providers = context.providers();
  for (const [id, name] of [['codex', 'Codex'], ['claude', 'Claude Code']]) {
    const option = new Option(name + (providers[id] ? '' : '（未導入）'), id);
    provider.add(option);
  }
  provider.value = project.agent.provider;
  const model = element('select');
  model.setAttribute('aria-label', `${project.name} の担当 Agent のモデル`);
  const catalog = context.models()[project.agent.provider] || [];
  if (project.agent.provider === 'claude') model.add(new Option('既定（CLI 設定）', ''));
  for (const item of catalog) model.add(new Option(item.name, item.id));
  if (project.agent.model && !catalog.some((item) => item.id === project.agent.model)) model.add(new Option(project.agent.model, project.agent.model));
  model.value = project.agent.model;
  const save = async (changes) => {
    try {
      project.agent = await context.api('/api/home/agent', { method: 'PATCH', body: JSON.stringify({ ...project.agent, ...changes }) }, project.id);
      renderHome();
      context.toast('担当 Agent を保存しました');
    } catch (error) { context.toast(error.message); }
  };
  provider.addEventListener('change', () => {
    const next = provider.value;
    save({ provider: next, model: next === 'codex' ? (context.models().codex[0]?.id || project.agent.model) : '' });
  });
  model.addEventListener('change', () => save({ model: model.value }));
  wrapper.append(provider, model);
  return wrapper;
}

function memoForm(project, memo = null) {
  /** Build a create or edit form for a memo of project; return it. */
  const form = element('form', 'home-memo-form');
  const title = element('input');
  title.placeholder = 'メモのタイトル';
  title.maxLength = 120;
  title.required = true;
  title.value = memo?.title || '';
  title.setAttribute('aria-label', 'メモのタイトル');
  const body = element('textarea');
  body.rows = 4;
  body.placeholder = 'やりたいこと、背景、確かめたいこと';
  body.value = memo?.body || '';
  body.setAttribute('aria-label', 'メモの本文');
  const allow = element('label', 'home-memo-allow');
  const checkbox = element('input');
  checkbox.type = 'checkbox';
  checkbox.checked = Boolean(memo?.allow_agent);
  allow.append(checkbox, document.createTextNode(' plan 作成 → 実装を Agent に許可（夜間に実行）'));
  const actions = element('div', 'home-memo-actions');
  const cancel = element('button', 'subtle-button', 'キャンセル');
  cancel.type = 'button';
  cancel.addEventListener('click', () => { editingMemo.delete(project.id); renderHome(); });
  const submit = element('button', 'secondary-button', memo ? '保存' : '追加');
  submit.type = 'submit';
  actions.append(cancel, submit);
  form.append(title, body, allow, actions);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const fields = { title: title.value, body: body.value, allow_agent: checkbox.checked };
    try {
      await context.api(memo ? `/api/home/memos/${memo.id}` : '/api/home/memos', { method: memo ? 'PATCH' : 'POST', body: JSON.stringify(fields) }, project.id);
      editingMemo.delete(project.id);
      await showHome(true);
    } catch (error) { context.toast(error.message); }
  });
  return form;
}

function memoSection(project) {
  /** Build project's memo list with add, edit, and delete controls; return it. */
  const section = element('section', 'home-panel');
  const head = element('div', 'home-panel-head');
  head.append(element('h3', '', 'メモ'));
  const add = element('button', 'subtle-button', '＋ メモ');
  add.type = 'button';
  add.addEventListener('click', () => { editingMemo.set(project.id, 'new'); renderHome(); });
  head.append(add);
  section.append(head);
  const editing = editingMemo.get(project.id);
  if (editing === 'new') section.append(memoForm(project));
  if (!project.memos.length && editing !== 'new') section.append(element('p', 'home-empty', 'メモはまだありません。'));
  for (const memo of [...project.memos].reverse()) {
    if (editing === memo.id) { section.append(memoForm(project, memo)); continue; }
    const item = element('article', 'home-memo');
    const top = element('div', 'home-memo-top');
    top.append(element('strong', '', memo.title));
    if (memo.task_id) {
      const linked = element('button', 'home-chip linked', 'タスク化済み');
      linked.type = 'button';
      linked.addEventListener('click', () => context.openTask(project.id, memo.task_id).catch((error) => context.toast(error.message)));
      top.append(linked);
    } else if (memo.allow_agent) top.append(element('span', 'home-chip approval', 'Agent に許可'));
    item.append(top);
    if (memo.body.trim()) item.append(element('p', 'home-memo-body', memo.body));
    const actions = element('div', 'home-memo-actions');
    const edit = element('button', 'subtle-button', '編集');
    edit.type = 'button';
    edit.addEventListener('click', () => { editingMemo.set(project.id, memo.id); renderHome(); });
    const remove = element('button', 'subtle-button', '削除');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      if (!window.confirm(`メモ「${memo.title}」を削除しますか？`)) return;
      try { await context.api(`/api/home/memos/${memo.id}`, { method: 'DELETE' }, project.id); await showHome(true); }
      catch (error) { context.toast(error.message); }
    });
    actions.append(edit, remove);
    item.append(actions);
    section.append(item);
  }
  return section;
}

async function openReport(project, date) {
  /** Load project's daily report for date into the sandboxed viewer; return nothing. */
  try {
    const report = await context.api(`/api/home/reports/${date}`, {}, project.id);
    $('#home-report-title').textContent = `${project.name} / ${date} の日報`;
    $('#home-report-frame').srcdoc = report.html;
    $('#home-report-dialog').showModal();
  } catch (error) { context.toast(error.message); }
}

function recordSection(project) {
  /** Build project's daily report list and night run records; return them. */
  const section = element('section', 'home-panel');
  section.append(element('h3', '', '日報'));
  if (!project.reports.length) section.append(element('p', 'home-empty', '日報はまだありません。毎朝 6:00 に前日の更新があれば作成します。'));
  const reports = element('div', 'home-report-list');
  for (const date of project.reports.slice(0, 14)) {
    const button = element('button', 'subtle-button', date);
    button.type = 'button';
    button.addEventListener('click', () => openReport(project, date));
    reports.append(button);
  }
  section.append(reports, element('h3', '', '夜間の自動実行'));
  if (!project.nightly.length) section.append(element('p', 'home-empty', '記録はありません。承認済みの計画や許可済みのメモがあれば 0:00 に実行します。'));
  for (const record of project.nightly) {
    const details = element('details', 'home-night');
    details.append(element('summary', '', `${record.date}（${record.entries.length} 件）`));
    const list = element('ul');
    for (const entry of record.entries) {
      const item = element('li');
      const label = element('button', 'home-night-task', entry.title);
      label.type = 'button';
      label.disabled = !entry.task_id;
      label.addEventListener('click', () => context.openTask(project.id, entry.task_id).catch((error) => context.toast(error.message)));
      item.append(label, element('span', `home-chip ${entry.state === 'succeeded' ? 'completed' : 'review'}`, `${runLabels[entry.run] || entry.run}・${stateLabels[entry.state] || entry.state}`));
      if (entry.message) item.append(element('p', '', entry.message));
      list.append(item);
    }
    details.append(list);
    section.append(details);
  }
  return section;
}

function projectSection(project) {
  /** Build one project's board: header, task cards, memos, and records; return it. */
  const section = element('section', 'home-project');
  const head = element('div', 'home-project-head');
  const title = element('div');
  const open = project.tasks.filter((task) => task.status !== 'completed').length;
  title.append(element('h2', '', project.name), element('small', '', `${project.path} · 未完了 ${open} / 全 ${project.tasks.length}`));
  const create = element('button', 'subtle-button', '＋ タスク');
  create.type = 'button';
  create.addEventListener('click', () => context.createTask(project.id).catch((error) => context.toast(error.message)));
  const files = element('button', 'subtle-button', 'ファイル');
  files.type = 'button';
  files.addEventListener('click', () => openFileBrowser({ repository: project.id }));
  head.append(title, agentPicker(project), files, create);
  const board = element('div', 'home-board');
  const tasks = visibleTasks([...project.tasks]);
  if (!tasks.length) board.append(element('p', 'home-empty', view.filter === 'done' ? '完了したタスクはありません。' : 'このプロジェクトのタスクはありません。'));
  for (const task of tasks) board.append(taskCard(project, task));
  const side = element('div', 'home-side');
  side.append(memoSection(project), recordSection(project));
  const layout = element('div', 'home-layout');
  layout.append(board, side);
  section.append(head, layout);
  return section;
}

function renderHome() {
  /** Render the toolbar and every project's board from loaded data; return nothing. */
  if (!data) return;
  renderToolbar();
  const sections = data.projects.map(projectSection);
  for (const failure of data.errors) sections.push(element('p', 'home-error', `${failure.name || failure.id}: ${failure.error}`));
  $('#home-projects').replaceChildren(...sections);
}
