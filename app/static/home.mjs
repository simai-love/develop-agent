import { openFileBrowser } from './files.mjs';
/* Home view: task cards for every project, memos, assigned Agents, daily reports, and night runs. */

const $ = (selector) => document.querySelector(selector);
const filterStorageKey = 'develop-agent.home.filter';
const sortStorageKey = 'develop-agent.home.sort';
const statusOrder = ['questions', 'awaiting-approval', 'review', 'verifying', 'running', 'approved', 'planning', 'not-started', 'completed'];
const filters = [['all', 'すべて'], ['open', '未完了'], ['agent', 'Agent 作業中'], ['user', 'ユーザー待ち'], ['scheduled', '夜間実行待ち'], ['done', '完了']];
const ownerFilters = new Set(['agent', 'user', 'scheduled']);
const sorts = [['updated', '更新日時'], ['created', '作成日時'], ['status', 'ステータス'], ['manual', '手動']];
const taskOrderStorageKey = 'develop-agent.home.task-order';
const collapsedStorageKey = 'develop-agent.home.collapsed';
const sideOpenStorageKey = 'develop-agent.home.side-open';
const ownerList = [['agent', 'Agent 作業中'], ['user', 'ユーザー待ち'], ['scheduled', '夜間実行待ち'], ['done', '完了']];
const attentionLimit = 6;
const projectOrderStorageKey = 'develop-agent.home.project-order';
let dragging = null;
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
  setInterval(() => {
    const visible = !$('#home-view').classList.contains('hidden');
    const running = data?.projects.some((project) => project.tasks.some((task) => task.display_status.owner === 'agent'));
    if (visible && !dragging && !document.hidden && (running || Date.now() - loadedAt > 30000)) showHome(true).catch(() => {});
  }, 5000);
}

export async function showHome(force = false) {
  /** Load home data when forced or stale, then render the home view; return nothing. */
  if (editingMemo.size) return;
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

function matchesFilter(task, filter) {
  /** Return whether task belongs to the home filter: all, open, done, or an owner key. */
  if (filter === 'all') return true;
  if (ownerFilters.has(filter)) return task.display_status.owner === filter;
  return (filter === 'done') === (task.status === 'completed');
}

function readJson(key, fallback) {
  /** Return the parsed per-browser JSON value for key, or fallback. */
  try { return JSON.parse(window.localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}

function ordered(items, savedIds) {
  /** Return items with unsaved ones first (newest first), then in savedIds order. */
  const rank = new Map(savedIds.map((id, index) => [id, index]));
  return [...items].sort((a, b) => (rank.get(a.id) ?? -1) - (rank.get(b.id) ?? -1) || (rank.has(a.id) ? 0 : b.created_at?.localeCompare(a.created_at ?? '') ?? 0));
}

function moveId(ids, movedId, targetId, after) {
  /** Return ids with movedId placed before or after targetId. */
  const next = ids.filter((id) => id !== movedId);
  next.splice(next.indexOf(targetId) + (after ? 1 : 0), 0, movedId);
  return next;
}

function dropPosition(event, node, horizontal) {
  /** Return whether the pointer is in the latter half of node along the layout axis. */
  const box = node.getBoundingClientRect();
  return horizontal ? event.clientX > box.left + box.width / 2 : event.clientY > box.top + box.height / 2;
}

function bindDrag(node, kind, id, onDrop, horizontal) {
  /** Make node a drag source and drop target of kind that calls onDrop(movedId, after); return nothing. */
  node.addEventListener('dragstart', (event) => {
    dragging = { kind, id };
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', id);
    requestAnimationFrame(() => node.classList.add('dragging'));
    event.stopPropagation();
  });
  node.addEventListener('dragend', () => { dragging = null; node.classList.remove('dragging'); document.querySelectorAll('.drop-before,.drop-after').forEach((item) => item.classList.remove('drop-before', 'drop-after')); });
  node.addEventListener('dragover', (event) => {
    if (dragging?.kind !== kind || dragging.id === id) return;
    event.preventDefault();
    event.stopPropagation();
    const after = dropPosition(event, node, horizontal);
    node.classList.toggle('drop-after', after);
    node.classList.toggle('drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('drop-before', 'drop-after'));
  node.addEventListener('drop', (event) => {
    if (dragging?.kind !== kind || dragging.id === id) return;
    event.preventDefault();
    event.stopPropagation();
    onDrop(dragging.id, dropPosition(event, node, horizontal));
  });
}

function orderedProjects() {
  /** Return loaded projects in the saved browser order, unsaved ones last. */
  const rank = new Map(readJson(projectOrderStorageKey, []).map((id, index) => [id, index]));
  return [...data.projects].sort((a, b) => (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity));
}

function visibleTasks(tasks, projectId) {
  /** Filter and sort tasks by the selected home options; return the visible list. */
  return sortedTasks(tasks, projectId).filter((task) => matchesFilter(task, view.filter));
}

function sortedTasks(tasks, projectId) {
  /** Sort a copy of tasks by the selected order, or the saved manual order; return it. */
  if (view.sort === 'manual') return ordered(tasks, readJson(taskOrderStorageKey, {})[projectId] || []);
  const byTime = (key) => (a, b) => b[key].localeCompare(a[key]);
  const compare = view.sort === 'created' ? byTime('created_at')
    : view.sort === 'status' ? (a, b) => statusOrder.indexOf(a.display_status.key) - statusOrder.indexOf(b.display_status.key) || byTime('updated_at')(a, b)
      : byTime('updated_at');
  return [...tasks].sort(compare);
}

function renderToolbar() {
  /** Render the filter and sort controls; return nothing. */
  const toolbar = $('#home-toolbar');
  const allTasks = data.projects.flatMap((project) => project.tasks);
  const group = (label, options, key, storageKey) => {
    const wrapper = element('div', 'home-segment');
    wrapper.setAttribute('role', 'group');
    wrapper.setAttribute('aria-label', label);
    wrapper.append(element('span', 'home-segment-label', label));
    for (const [id, name] of options) {
      const button = element('button', `${view[key] === id ? 'active' : ''} ${ownerFilters.has(id) ? `owner-filter ${id}` : ''}`.trim(), name);
      if (key === 'filter') button.append(element('span', 'home-segment-count', String(allTasks.filter((task) => matchesFilter(task, id)).length)));
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
  const card = element('button', `home-card ${task.display_status.key} owner-${task.display_status.owner}`);
  card.type = 'button';
  card.title = `${task.title}：${task.display_status.label}（${task.display_status.detail}）`;
  const chips = element('div', 'home-card-chips');
  chips.append(element('span', `home-chip ${task.display_status.key}`, task.display_status.label));
  if (task.plan_approved && task.display_status.key !== 'approved') chips.append(element('span', 'home-chip approval', '計画承認済み'));
  const owner = element('span', `home-owner ${task.display_status.owner}`);
  owner.append(element('i', 'home-owner-dot'), document.createTextNode(task.display_status.owner_label));
  card.append(owner, element('strong', 'home-card-title', task.title), chips, element('span', 'home-card-detail', task.display_status.detail));
  if (task.parent_title) card.append(element('span', 'home-card-parent', `親: ${task.parent_title}`));
  card.append(element('span', 'home-card-time', `更新 ${formatTime(task.updated_at)}`));
  card.addEventListener('click', () => context.openTask(project.id, task.id).catch((error) => context.toast(error.message)));
  card.draggable = true;
  bindDrag(card, `task:${project.id}`, task.id, (movedId, after) => {
    const orders = readJson(taskOrderStorageKey, {});
    const current = sortedTasks(project.tasks, project.id).map((item) => item.id);
    orders[project.id] = moveId(current, movedId, task.id, after);
    store(taskOrderStorageKey, JSON.stringify(orders));
    view.sort = 'manual';
    store(sortStorageKey, 'manual');
    renderHome();
  }, true);
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

function storedSet(key) {
  /** Return the per-browser set of project IDs saved under key. */
  return new Set(readJson(key, []));
}

function toggleStored(key, projectId, on) {
  /** Add or remove projectId in the per-browser set saved under key; return nothing. */
  const ids = storedSet(key);
  if (on) ids.add(projectId); else ids.delete(projectId);
  store(key, JSON.stringify([...ids]));
}

function ownerCounts(tasks) {
  /** Count tasks by who acts next (agent, user, scheduled, done); return an object. */
  const counts = { agent: 0, user: 0, scheduled: 0, done: 0 };
  for (const task of tasks) counts[task.display_status.owner] += 1;
  return counts;
}

function openProjectSection(projectId) {
  /** Expand and scroll to the project's board on the home view; return nothing. */
  toggleStored(collapsedStorageKey, projectId, false);
  renderHome();
  document.querySelector(`.home-project[data-project-id="${CSS.escape(projectId)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderOverview() {
  /** Render the one-screen summary: totals by owner and each project's counts and tasks needing action; return nothing. */
  const projects = orderedProjects();
  const totals = ownerCounts(projects.flatMap((project) => project.tasks));
  const box = element('section', 'home-overview-box');
  const stats = element('div', 'home-ov-stats');
  for (const [owner, label] of ownerList) {
    const stat = element('button', `home-ov-stat ${owner}${totals[owner] ? '' : ' zero'}`);
    stat.type = 'button';
    stat.title = `${label}のタスクだけを表示`;
    stat.append(element('b', '', String(totals[owner])), element('span', '', label));
    stat.addEventListener('click', () => { view.filter = owner === 'done' ? 'done' : owner; store(filterStorageKey, view.filter); renderHome(); });
    stats.append(stat);
  }
  box.append(stats);
  const rows = element('div', 'home-ov-rows');
  for (const project of projects) {
    const counts = ownerCounts(project.tasks);
    const row = element('div', 'home-ov-row');
    const name = element('button', 'home-ov-name', project.name);
    name.type = 'button';
    name.title = `${project.name} のタスクへ移動`;
    name.addEventListener('click', () => openProjectSection(project.id));
    const bar = element('div', 'home-ov-bar');
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', ownerList.map(([owner, label]) => `${label} ${counts[owner]}`).join('、'));
    for (const [owner] of ownerList) if (counts[owner]) { const part = element('i', owner); part.style.flexGrow = String(counts[owner]); bar.append(part); }
    const nums = element('div', 'home-ov-nums');
    for (const [owner, label] of ownerList) if (owner !== 'done' && counts[owner]) nums.append(element('span', owner, `${label} ${counts[owner]}`));
    nums.append(element('span', 'done', `完了 ${counts.done}/${project.tasks.length}`));
    const attention = element('div', 'home-ov-tasks');
    const needing = project.tasks.filter((task) => task.display_status.owner !== 'done')
      .sort((a, b) => statusOrder.indexOf(a.display_status.key) - statusOrder.indexOf(b.display_status.key));
    for (const task of needing.slice(0, attentionLimit)) {
      const chip = element('button', `home-ov-task ${task.display_status.owner}`);
      chip.type = 'button';
      chip.title = `${task.title}：${task.display_status.owner_label}・${task.display_status.label}（${task.display_status.detail}）`;
      chip.append(element('i', 'home-owner-dot'), document.createTextNode(`${task.title} · ${task.display_status.label}`));
      chip.addEventListener('click', () => context.openTask(project.id, task.id).catch((error) => context.toast(error.message)));
      attention.append(chip);
    }
    if (needing.length > attentionLimit) attention.append(element('span', 'home-ov-more', `ほか ${needing.length - attentionLimit} 件`));
    if (!needing.length) attention.append(element('span', 'home-ov-more', '対応が必要なタスクはありません'));
    row.append(name, bar, nums, attention);
    rows.append(row);
  }
  box.append(rows);
  $('#home-overview').replaceChildren(box);
}

function projectSection(project) {
  /** Build one project's board: header, task cards, memos, and records; return it. */
  const collapsed = storedSet(collapsedStorageKey).has(project.id);
  const section = element('section', `home-project${collapsed ? ' collapsed' : ''}`);
  section.dataset.projectId = project.id;
  const fold = element('button', 'home-fold', collapsed ? '▸' : '▾');
  fold.type = 'button';
  fold.title = collapsed ? 'タスクを開く' : 'タスクを畳む';
  fold.setAttribute('aria-expanded', String(!collapsed));
  fold.addEventListener('click', () => { toggleStored(collapsedStorageKey, project.id, !collapsed); renderHome(); });
  const handle = element('span', 'home-drag-handle', '⠿');
  handle.title = 'ドラッグしてプロジェクトの順番を変更';
  handle.setAttribute('aria-hidden', 'true');
  handle.addEventListener('pointerdown', () => { section.draggable = true; });
  handle.addEventListener('pointerup', () => { section.draggable = false; });
  section.addEventListener('dragend', () => { section.draggable = false; });
  bindDrag(section, 'project', project.id, (movedId, after) => {
    const current = orderedProjects().map((item) => item.id);
    store(projectOrderStorageKey, JSON.stringify(moveId(current, movedId, project.id, after)));
    renderHome();
  }, false);
  const head = element('div', 'home-project-head');
  const title = element('div');
  const tasks = visibleTasks([...project.tasks], project.id);
  const emptyNote = project.tasks.length ? `「${filters.find(([id]) => id === view.filter)[1]}」のタスクはありません` : 'タスクはまだありません';
  const open = project.tasks.filter((task) => task.status !== 'completed').length;
  const name = element('h2');
  name.append(handle, fold, document.createTextNode(project.name));
  title.append(name, element('small', '', `${project.path} · 未完了 ${open} / 全 ${project.tasks.length}${tasks.length ? '' : ` · ${emptyNote}`}`));
  const create = element('button', 'subtle-button', '＋ タスク');
  create.type = 'button';
  create.addEventListener('click', () => context.createTask(project.id).catch((error) => context.toast(error.message)));
  const files = element('button', 'subtle-button', 'ファイル');
  files.type = 'button';
  files.addEventListener('click', () => openFileBrowser({ repository: project.id }));
  head.append(title, agentPicker(project), files, create);
  const board = element('div', 'home-board');
  if (!tasks.length) section.classList.add('idle');
  for (const task of tasks) board.append(taskCard(project, task));
  const side = element('details', 'home-side-details');
  side.open = storedSet(sideOpenStorageKey).has(project.id) || editingMemo.has(project.id);
  side.addEventListener('toggle', () => { if (!editingMemo.has(project.id)) toggleStored(sideOpenStorageKey, project.id, side.open); });
  const lastNight = project.nightly[0];
  side.append(element('summary', '', `メモ ${project.memos.length} ・ 日報 ${project.reports.length} ・ 夜間の自動実行 ${project.nightly.length}${lastNight ? `（最新 ${lastNight.date}）` : ''}`));
  const sideGrid = element('div', 'home-side-grid');
  sideGrid.append(memoSection(project), recordSection(project));
  side.append(sideGrid);
  const body = element('div', 'home-body');
  if (tasks.length) body.append(board);
  body.append(side);
  section.append(head, body);
  return section;
}

function renderHome() {
  /** Render the toolbar and every project's board from loaded data; return nothing. */
  if (!data) return;
  renderOverview();
  renderToolbar();
  const sections = orderedProjects().map(projectSection);
  for (const failure of data.errors) sections.push(element('p', 'home-error', `${failure.name || failure.id}: ${failure.error}`));
  $('#home-projects').replaceChildren(...sections);
}
