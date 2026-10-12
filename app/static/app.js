import { renderMarkdown } from './markdown.mjs';

const $ = (selector) => document.querySelector(selector);
const outcomeLabels = { supported: '仮説を支持', not_supported: '仮説を支持せず', inconclusive: '判断保留' };
const chatHeightStorageKey = 'develop-agent.chat-height';
const chatHeightMin = 260;
const chatHeightMax = 1000;
const state = { projects: [], project: null, tasks: [], task: null, tab: 'plan', planEditing: false, drafts: { plan: '', result: '' }, parentId: null, createProjectId: null, deleteTargetId: null, renameTarget: null, messages: [], evidence: [], agentAvailability: { codex: false, claude: false }, modelCatalog: { codex: [], claude: [] }, activeRuns: new Map() };
let refreshingUntrackedRun = false;

function isTaskBusy(task) {
  /** Return whether the selected task has a running Agent request. */
  return Boolean(task && (state.activeRuns.has(task.id) || task.active || task.implementation?.state === 'running'));
}

function taskStatus(task) {
  /** Return a task's visible status, including a locally started Agent run. */
  return isTaskBusy(task)
    ? { key: 'running', label: '実行中', detail: 'Agentが作業しています' }
    : task.display_status;
}

function renderAgentProgress() {
  /** Show the active Agent phase above the chat panel; return nothing. */
  const run = state.activeRuns.get(state.task.id);
  const banner = $('#agent-progress');
  const busy = isTaskBusy(state.task);
  banner.classList.toggle('hidden', !busy);
  if (!busy) return;
  const title = $('#agent-progress-title');
  const description = $('#agent-progress-description');
  if (run?.kind === 'chat' && run.phase === 'plan') {
    title.textContent = '計画を作成中';
    description.textContent = 'Agentが要件と仮説を整理しています。完了すると計画が更新されます。';
  } else if (run?.phase === 'result') {
    title.textContent = '追加検証・修正中';
    description.textContent = 'Agentが追加の確認を進めています。結果が得られると新しいレポートを表示します。';
  } else if (run?.kind === 'implement') {
    title.textContent = '実装中：Agentが作業しています';
    description.textContent = '計画に沿ってコードを変更し、続けて検証します。完了後に結果を確認できます。';
  } else {
    title.textContent = 'Agentが作業中';
    description.textContent = '作業が終わるとタスクの表示が更新されます。';
  }
  const activity = run?.activity || [];
  const running = [...activity].reverse().find((item) => item.state === 'running');
  if (running) description.textContent = `現在実行中: ${running.label}`;
  const list = $('#agent-progress-activity');
  list.replaceChildren(...activity.slice(-5).reverse().map((item) => {
    const row = document.createElement('li');
    row.className = `agent-activity-${item.state}`;
    row.textContent = `${item.state === 'running' ? '実行中' : item.state === 'failed' ? '失敗' : '完了'} · ${item.label}`;
    return row;
  }));
}

function setChatHeight(height) {
  /** Apply a bounded pixel height to the chat panel and return that height. */
  const bounded = Math.max(chatHeightMin, Math.min(chatHeightMax, Math.round(height)));
  $('#chat-panel').style.height = `${bounded}px`;
  $('#chat-resize-handle').setAttribute('aria-valuenow', String(bounded));
  return bounded;
}

function bindChatResize() {
  /** Restore the saved chat height and bind pointer and keyboard resize actions. */
  const panel = $('#chat-panel');
  const handle = $('#chat-resize-handle');
  const stored = Number(window.localStorage.getItem(chatHeightStorageKey));
  if (Number.isFinite(stored) && stored > 0) setChatHeight(stored);
  else handle.setAttribute('aria-valuenow', window.matchMedia('(max-width:1100px)').matches ? '300' : '340');
  let startY = null;
  let startHeight = 0;
  let activePointerId = null;
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || activePointerId !== null) return;
    event.preventDefault();
    startY = event.clientY;
    startHeight = panel.getBoundingClientRect().height;
    activePointerId = event.pointerId;
    handle.setPointerCapture(event.pointerId);
  });
  handle.addEventListener('pointermove', (event) => {
    if (event.pointerId === activePointerId) setChatHeight(startHeight + event.clientY - startY);
  });
  handle.addEventListener('lostpointercapture', (event) => {
    if (event.pointerId !== activePointerId) return;
    startY = null;
    activePointerId = null;
    window.localStorage.setItem(chatHeightStorageKey, String(Math.round(panel.getBoundingClientRect().height)));
  });
  handle.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 32 : -32;
    const height = setChatHeight(panel.getBoundingClientRect().height + step);
    window.localStorage.setItem(chatHeightStorageKey, String(height));
  });
}

function agentAvailable() {
  /** Read the selected provider and return whether its CLI is installed. */
  return Boolean(state.agentAvailability[state.task?.provider || 'codex']);
}

function modelSelected() {
  /** Return whether the selected task has an explicit Codex model when needed. */
  return state.task?.provider !== 'codex' || Boolean(state.task.model);
}

async function api(path, options = {}) {
  /** Send path and options to the local API; return its parsed JSON response. */
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作に失敗しました');
  return value;
}

function toast(message) {
  /** Show message as a short status notice; return nothing. */
  const element = $('#toast');
  element.textContent = message;
  element.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.add('hidden'), 3200);
}

function hasChanges() {
  /** Read the plan draft and return whether it has unsaved changes. */
  return state.task && state.drafts.plan !== state.task.plan;
}

function planSection(markdown, heading) {
  /** Read Markdown and a section heading; return its plain text for the key points. */
  const lines = markdown.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === `## ${heading}`);
  if (start < 0) return '';
  const body = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,2}\s/.test(line)) break;
    const plain = line.trim().replace(/^>\s?/, '').replace(/^\[!(IMPORTANT|NOTE)\]$/, '').replace(/^-\s+/, '').replace(/\*\*/g, '').trim();
    if (plain) body.push(plain);
  }
  return body.join('\n');
}

function renderPlanHighlights() {
  /** Read the current plan draft and render its questions and purpose; return nothing. */
  const questions = planSection(state.drafts.plan, '確認したいこと');
  const purpose = planSection(state.drafts.plan, '目的');
  $('#plan-questions-text').textContent = questions;
  $('#plan-purpose-text').textContent = purpose;
  $('#plan-questions-card').classList.toggle('hidden', !questions);
  $('#plan-purpose-card').classList.toggle('hidden', !purpose);
  $('#plan-highlights').classList.toggle('hidden', state.tab !== 'plan' || (!questions && !purpose));
}

function renderTree() {
  /** Read task metadata and render the hierarchy; return nothing. */
  const tree = $('#task-tree');
  tree.replaceChildren();
  if (!state.projects.length) {
    const empty = document.createElement('div');
    empty.className = 'tree-empty';
    empty.textContent = 'プロジェクトはまだありません。＋から作成できます。';
    tree.append(empty);
    return;
  }
  const children = new Map();
  for (const task of state.tasks) {
    const key = task.parent_id || 'root';
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(task);
  }
  const appendTask = (task, depth) => {
    const item = document.createElement('div');
    item.className = 'tree-item';
    item.style.paddingLeft = `${Math.min(depth, 8) * 12}px`;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `tree-row${state.task?.id === task.id ? ' selected' : ''}`;
    const status = taskStatus(task);
    row.title = `${task.title}：${status.label}（${status.detail}）`;
    const label = document.createElement('span');
    label.className = 'tree-text';
    label.textContent = task.title;
    const statusBadge = document.createElement('span');
    statusBadge.className = `tree-status ${status.key}`;
    statusBadge.textContent = status.label;
    const statusDetail = document.createElement('span');
    statusDetail.className = 'tree-status-detail';
    statusDetail.textContent = status.detail;
    const summary = document.createElement('span');
    summary.className = 'tree-summary';
    const meta = document.createElement('span');
    meta.className = 'tree-meta';
    meta.append(statusBadge, statusDetail);
    summary.append(label, meta);
    row.append(summary);
    row.addEventListener('click', () => selectTask(task.id));
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'tree-action';
    add.textContent = '＋';
    add.title = `「${task.title}」に子タスクを追加`;
    add.setAttribute('aria-label', add.title);
    add.addEventListener('click', () => openCreate(task.id));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'tree-action delete';
    remove.textContent = '×';
    remove.title = `「${task.title}」を削除`;
    remove.setAttribute('aria-label', remove.title);
    remove.disabled = subtreeIds(task.id).some((id) => isTaskBusy(state.tasks.find((item) => item.id === id)));
    remove.addEventListener('click', () => openDelete(task.id));
    item.append(row, add, remove);
    tree.append(item);
    for (const child of children.get(task.id) || []) appendTask(child, depth + 1);
  };
  for (const project of state.projects) {
    const item = document.createElement('div');
    item.className = 'tree-item project-item';
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `tree-row project-row${state.project?.id === project.id ? ' selected' : ''}`;
    const projectName = document.createElement('span');
    projectName.className = 'tree-text';
    projectName.textContent = `▣ ${project.name} (${project.completed_count}/${project.task_count})`;
    row.append(projectName);
    if (state.tasks.some((task) => task.project_id === project.id && isTaskBusy(task))) {
      const running = document.createElement('span');
      running.className = 'tree-status running';
      running.textContent = '実行中';
      row.append(running);
    }
    row.title = project.branch;
    row.addEventListener('click', () => selectProject(project.id));
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'tree-action';
    add.textContent = '＋';
    add.title = `プロジェクト「${project.name}」にタスクを追加`;
    add.setAttribute('aria-label', add.title);
    add.disabled = project.status === 'completed' || project.needs_review || state.tasks.some(isTaskBusy);
    add.addEventListener('click', () => openCreate(null, project.id));
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'tree-action';
    rename.textContent = '✎';
    rename.title = `プロジェクト「${project.name}」の名前を変更`;
    rename.setAttribute('aria-label', rename.title);
    rename.addEventListener('click', () => openRename('project', project.id));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'tree-action delete';
    remove.textContent = '×';
    remove.title = `プロジェクト「${project.name}」を削除`;
    remove.setAttribute('aria-label', remove.title);
    remove.disabled = state.tasks.some((task) => task.project_id === project.id && isTaskBusy(task));
    remove.addEventListener('click', () => deleteProject(project.id));
    item.append(row, add, rename, remove);
    tree.append(item);
    for (const task of children.get('root') || []) {
      if (task.project_id !== project.id) continue;
      appendTask(task, 1);
    }
  }
}

function subtreeIds(taskId) {
  /** Read taskId and return its descendant IDs, including taskId. */
  const ids = [taskId];
  for (const id of ids) {
    ids.push(...state.tasks.filter((task) => task.parent_id === id).map((task) => task.id));
  }
  return ids;
}

function render() {
  /** Read the selected task and tab, then render the GUI; return nothing. */
  const task = state.task;
  $('#empty-view').classList.toggle('hidden', Boolean(task || state.project));
  $('#task-view').classList.toggle('hidden', !task);
  $('#project-view').classList.toggle('hidden', !state.project);
  renderTree();
  if (state.project) {
    const project = state.project;
    $('#breadcrumb').textContent = project.name;
    $('#project-title').textContent = project.name;
    $('#project-branch').textContent = `ブランチ: ${project.branch}`;
    $('#project-status').textContent = project.status === 'completed' ? '完了済み' : '進行中';
    $('#project-progress').textContent = `${project.completed_count} / ${project.task_count} タスク完了。全タスク完了後に dev へマージできます。`;
    $('#migration-action').classList.toggle('hidden', !project.needs_review);
    $('#legacy-branch-action').classList.toggle('hidden', !project.legacy || project.status === 'completed');
    if (document.activeElement !== $('#legacy-branch-slug')) $('#legacy-branch-slug').value = project.slug;
    $('#add-project-task').disabled = project.needs_review || project.status === 'completed' || state.tasks.some(isTaskBusy);
    $('#complete-project').disabled = project.needs_review || project.status === 'completed' || project.completed_count !== project.task_count || !project.task_count;
    return;
  }
  if (!task) { $('#breadcrumb').textContent = '概要'; return; }
  const isPlan = state.tab === 'plan';
  const parent = state.tasks.find((item) => item.id === task.parent_id);
  const migrationPending = Boolean(state.projects.find((project) => project.id === task.project_id)?.needs_review);
  $('#breadcrumb').textContent = task.title;
  $('#task-title').textContent = task.title;
  $('#task-number').textContent = `/ ${task.id.slice(0, 6).toUpperCase()}`;
  $('#task-parent').textContent = parent ? `親タスク / ${parent.title}` : `プロジェクト / ${state.projects.find((project) => project.id === task.project_id)?.name || ''}`;
  const status = taskStatus(task);
  $('#status-label').textContent = `${status.label} · ${status.detail}`;
  $('#status-label').className = `status-pill ${status.key}`;
  $('#tab-plan').classList.toggle('active', isPlan);
  $('#tab-result').classList.toggle('active', !isPlan);
  $('#tab-plan').setAttribute('aria-selected', String(isPlan));
  $('#tab-result').setAttribute('aria-selected', String(!isPlan));
  $('#section-kicker').textContent = isPlan ? 'PLAN / HYPOTHESIS' : 'RESULT / EVIDENCE';
  $('#section-title').textContent = isPlan ? '開発の計画' : '検証の結果';
  $('#section-description').textContent = isPlan
    ? '目的から仮説、要件、実装手順までをMarkdownで整理します。'
    : '検証のたびに増えるレポートを開いて、結論と根拠を確認できます。';
  $('#file-path').textContent = `tasks/${task.id}/plan.md`;
  $('#task-path').textContent = `tasks/${task.id}/`;
  $('#markdown-editor').value = state.drafts.plan;
  $('#markdown-editor').placeholder = '# 開発計画\n\n## 目的\nしてほしいことを一文で書く。\n\n## 現状と困っていること\n- 一つ目\n- 二つ目\n- 三つ目\n';
  $('#plan-preview').replaceChildren(renderMarkdown(state.drafts.plan, task.id));
  if (!state.drafts.plan.trim()) {
    const emptyPreview = document.createElement('p');
    emptyPreview.className = 'preview-empty';
    emptyPreview.textContent = '計画を作成すると、ここに書式付きで表示されます。';
    $('#plan-preview').append(emptyPreview);
  }
  $('#plan-preview').classList.toggle('hidden', state.planEditing);
  $('#markdown-editor').classList.toggle('hidden', !state.planEditing);
  $('#save-button').classList.toggle('hidden', !state.planEditing);
  $('#plan-mode-toggle').textContent = state.planEditing ? 'プレビューに戻る' : 'Markdownを編集';
  renderPlanHighlights();
  $('#plan-editor').classList.toggle('hidden', !isPlan);
  $('#save-indicator').classList.toggle('hidden', !isPlan);
  $('#report-section').classList.toggle('hidden', isPlan);
  if (!isPlan) renderReports();
  $('#plan-actions').classList.toggle('hidden', !isPlan);
  $('#result-actions').classList.toggle('hidden', isPlan);
  $('#summary-actions').classList.toggle('hidden', isPlan || !task.child_count || task.status === 'completed');
  $('#evidence-section').classList.toggle('hidden', isPlan);
  $('#chat-context').textContent = isPlan ? '計画について議論' : '検証・修正について議論';
  $('#chat-input').placeholder = isPlan ? 'Agentに相談する内容を入力' : task.child_count ? '子タスクの総括ボタンを使用してください' : task.implementation?.changed_files?.length ? '追加検証や修正したいことを入力' : '実装後に追加検証や修正を依頼できます';
  const busy = isTaskBusy(task);
  renderAgentProgress();
  $('#rename-task').disabled = busy;
  $('#add-child').disabled = migrationPending || busy;
  $('#delete-task').disabled = subtreeIds(task.id).some((id) => isTaskBusy(state.tasks.find((item) => item.id === id)));
  $('#plan-empty').classList.toggle('hidden', !isPlan || Boolean(task.plan.trim()) || Boolean(state.drafts.plan.trim()));
  $('#execute-button').disabled = migrationPending || Boolean(task.child_count) || !task.can_implement || state.drafts.plan !== task.plan || !agentAvailable() || !modelSelected() || busy || task.status === 'completed' || task.implementation?.state === 'running';
  $('#execute-button').title = task.readiness?.reason || 'Agentの実装可否判定を待っています';
  $('#readiness-reason').textContent = migrationPending ? 'プロジェクト画面で移行前のGit変更を確認してください。' : task.can_implement ? 'Agentが計画を実装可能と判定しました。確認後に開始できます。' : task.readiness?.reason || 'Agentに計画を相談してください。';
  const childrenDone = state.tasks.filter((item) => item.parent_id === task.id).every((item) => item.status === 'completed');
  const summaryReady = task.reports?.at(-1)?.kind === 'summary';
  $('#summary-button').disabled = migrationPending || !childrenDone || busy || !agentAvailable() || !modelSelected();
  $('#complete-button').disabled = migrationPending || task.status === 'completed' || busy || (task.child_count ? !childrenDone || !summaryReady : task.implementation?.state !== 'succeeded' || !task.result.trim());
  $('#complete-button').textContent = task.status === 'completed' ? '完了済み ✓' : 'タスクを完了 →';
  $('#complete-button').title = task.implementation?.state !== 'succeeded' ? 'CLIによる実装が確認されてから完了できます' : '';
  $('#result-actions p').textContent = task.implementation?.validation_outcome
    ? `Agentの検証結果：${outcomeLabels[task.implementation.validation_outcome]}。結果を確認し、目的を達成したと判断したら完了にしてください。`
    : '目的を達成したと判断したら、この開発項目を完了にします。';
  $('#changed-files').textContent = task.implementation?.changed_files?.length
    ? `実装と追加修正で変更したファイル：\n${task.implementation.changed_files.join('\n')}`
    : 'このタスクで確認されたコード変更はまだありません。';
  const dirty = state.drafts.plan !== task.plan;
  $('#save-indicator').textContent = dirty ? '未保存の変更' : '保存済み';
  $('#save-indicator').classList.toggle('dirty', dirty);
  $('#markdown-editor').disabled = busy || task.implementation?.state === 'running';
  $('#save-button').disabled = !dirty || busy || task.implementation?.state === 'running';
  renderModel();
  renderEvidence();
  renderChat();
  renderProposals();
}

function renderProposals() {
  /** Render pending child proposals for the selected task and return nothing. */
  const items = state.task.child_proposals?.filter((item) => item.status === 'pending') || [];
  $('#proposal-section').classList.toggle('hidden', !items.length);
  const list = $('#proposal-list');
  list.replaceChildren();
  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'proposal-card';
    const title = document.createElement('input');
    title.value = item.title;
    title.setAttribute('aria-label', '子タスク名');
    const detail = document.createElement('p');
    detail.textContent = item.stale ? '計画が変更されたため、この提案は無効です。Agentに再提案を依頼してください。' : item.reason;
    const fields = {};
    const fieldRows = [];
    for (const [key, label] of [['purpose', '目的'], ['scope', '範囲'], ['verification', '確かめ方']]) {
      const row = document.createElement('label');
      row.textContent = label;
      const input = document.createElement('textarea');
      input.value = item[key];
      input.rows = 2;
      input.setAttribute('aria-label', label);
      row.append(input);
      fields[key] = input;
      fieldRows.push(row);
    }
    const approve = document.createElement('button');
    approve.className = 'secondary-button';
    approve.textContent = '承認して子タスクを作成';
    approve.disabled = Boolean(item.stale || state.projects.find((project) => project.id === state.task.project_id)?.needs_review);
    approve.addEventListener('click', async () => {
      try {
        const child = await api(`/api/tasks/${state.task.id}/proposals/${item.id}/approve`, { method: 'POST', body: JSON.stringify({ title: title.value.trim(), purpose: fields.purpose.value.trim(), scope: fields.scope.value.trim(), verification: fields.verification.value.trim() }) });
        await refreshTasks();
        await selectTask(child.id);
      } catch (error) { toast(error.message); }
    });
    const reject = document.createElement('button');
    reject.className = 'subtle-button';
    reject.textContent = '却下';
    reject.addEventListener('click', async () => {
      try {
        state.task = await api(`/api/tasks/${state.task.id}/proposals/${item.id}/reject`, { method: 'POST', body: '{}' });
        render();
      } catch (error) { toast(error.message); }
    });
    card.append(title, detail, ...fieldRows, approve, reject);
    list.append(card);
  }
}

function renderReports() {
  /** Render the selected task's reports as collapsible entries; return nothing. */
  const list = $('#report-list');
  list.replaceChildren();
  const reports = state.task.reports || [];
  if (!reports.length) {
    const empty = document.createElement('div');
    empty.className = 'report-empty';
    empty.textContent = '検証レポートはまだありません。実装と最初の検証が終わると、ここに追加されます。';
    list.append(empty);
    return;
  }
  for (const report of [...reports].reverse()) {
    const card = document.createElement('details');
    card.className = 'report-card';
    card.open = report.number === reports.length;
    const summary = document.createElement('summary');
    const number = document.createElement('span');
    number.className = 'report-number';
    number.textContent = `REPORT ${String(report.number).padStart(2, '0')}`;
    const title = document.createElement('strong');
    title.textContent = planSection(report.content, '結論').replace(/\n/g, ' ') || `検証レポート ${report.number}`;
    const meta = document.createElement('small');
    meta.textContent = `${outcomeLabels[report.outcome] || '結果を確認'} · ${new Date(report.created_at).toLocaleString('ja-JP')}`;
    summary.append(number, title, meta);
    const body = document.createElement('div');
    body.className = 'report-body';
    const path = document.createElement('div');
    path.className = 'report-path';
    path.textContent = `tasks/${state.task.id}/${report.file}`;
    body.append(path, renderMarkdown(report.content, state.task.id));
    card.append(summary, body);
    list.append(card);
  }
}

function renderModel() {
  /** Render the CLI catalog and any saved model; return nothing. */
  const select = $('#model-select');
  const choices = state.modelCatalog[state.task.provider] || [];
  select.replaceChildren();
  if (!state.task.model) {
    const placeholder = new Option(choices.length ? 'モデルを選択' : 'モデルを取得できません', '');
    placeholder.disabled = true;
    select.add(placeholder);
  }
  for (const model of choices) select.add(new Option(model.name, model.id));
  if (state.task.model && !choices.some((model) => model.id === state.task.model)) {
    select.add(new Option(`${state.task.model}（保存済み・一覧外）`, state.task.model));
  }
  select.value = state.task.model || '';
  select.disabled = isTaskBusy(state.task) || (!choices.length && !state.task.model);
  select.title = choices.length ? 'CLIが提示するモデル候補です。利用可否はアカウント設定によります。' : 'モデル一覧を取得できません。画面を更新してください。';
  $('#model-picker').classList.toggle('hidden', state.task.provider === 'claude');
  $('#provider-select').value = state.task.provider || 'codex';
  $('#provider-select').disabled = isTaskBusy(state.task);
  $('#provider-label').textContent = state.task.provider === 'claude' ? 'CLAUDE CODE' : 'CODEX CLI';
}

function renderEvidence() {
  /** Render task evidence metadata as safe media elements; return nothing. */
  const container = $('#evidence-list');
  container.replaceChildren();
  if (!state.evidence.length) {
    const empty = document.createElement('p');
    empty.textContent = '検証資料はまだありません。';
    container.append(empty);
  }
  for (const entry of state.evidence) {
    const card = document.createElement('div');
    card.className = 'evidence-card';
    const url = `/api/tasks/${state.task.id}/evidence/${encodeURIComponent(entry.name)}`;
    if (entry.kind === 'image') {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = document.createElement('img');
      image.src = url;
      image.alt = entry.name;
      link.append(image);
      card.append(link);
    } else if (entry.kind === 'video') {
      const video = document.createElement('video');
      video.src = url;
      video.controls = true;
      video.preload = 'metadata';
      card.append(video);
    }
    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = entry.name;
    card.append(link);
    container.append(card);
  }
}

function renderChat() {
  /** Read current messages and render them in the top chat panel; return nothing. */
  const container = $('#chat-messages');
  container.replaceChildren();
  if (!state.messages.length) {
    const note = document.createElement('div');
    note.className = 'chat-note';
    note.innerHTML = !modelSelected()
      ? '<span>SELECT A MODEL</span><h3>モデルを選択してください</h3><p>チャットを始める前に、上のモデル欄から使用するモデルを選んでください。</p>'
      : state.tab === 'plan'
      ? '<span>START A CONVERSATION</span><h3>Agentにチャットしてください</h3><p>開発の目的を伝えると、仮説・要件・実装方針の整理を始められます。</p>'
      : '<span>DISCUSS THE RESULT</span><h3>追加検証や修正をAgentに依頼</h3><p>実装後の結果を調べ、必要ならコードを直して再検証できます。</p>';
    container.append(note);
  }
  const latestAssistant = [...state.messages].reverse().find((message) => message.role === 'assistant');
  const busy = isTaskBusy(state.task);
  $('#question-callout').classList.toggle('hidden', state.tab !== 'plan' || !latestAssistant?.questions?.length || busy || state.task.status === 'completed');
  for (const message of state.messages) {
    const card = document.createElement('div');
    card.className = `chat-message ${message.role}`;
    const heading = document.createElement('div');
    heading.className = 'chat-message-header';
    heading.textContent = message.role === 'user' ? 'YOU' : message.role === 'assistant' ? `${message.provider === 'claude' ? 'CLAUDE' : 'CODEX'}${message.model ? ` / ${message.model}` : ''}` : 'SYSTEM';
    const content = document.createElement('div');
    content.className = 'chat-message-content';
    content.append(renderMarkdown(message.content, state.task.id));
    card.append(heading, content);
    if (message.role === 'assistant') {
      if (state.tab === 'plan' && message === latestAssistant && message.questions?.length) {
        const form = document.createElement('form');
        form.className = 'choice-form';
        message.questions.forEach((question, index) => {
          const fieldset = document.createElement('fieldset');
          const legend = document.createElement('legend');
          legend.textContent = question.prompt;
          fieldset.append(legend);
          question.options.forEach((option) => {
            const label = document.createElement('label');
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = `question-${index}`;
            radio.value = option;
            label.append(radio, document.createTextNode(option));
            fieldset.append(label);
          });
          const freeLabel = document.createElement('label');
          freeLabel.className = 'question-free-label';
          freeLabel.textContent = '自由記述（選択肢にない回答や補足）';
          const free = document.createElement('textarea');
          free.name = `question-free-${index}`;
          free.placeholder = 'この質問への回答を自由に記入';
          free.rows = 2;
          freeLabel.append(free);
          fieldset.append(freeLabel);
          form.append(fieldset);
        });
        const extraLabel = document.createElement('label');
        extraLabel.textContent = 'その他の要望・補足があればどうぞ';
        const extra = document.createElement('textarea');
        extra.name = 'extra';
        extra.placeholder = '自由に記入できます（任意）';
        extraLabel.append(extra);
        form.append(extraLabel);
        const submit = document.createElement('button');
        submit.type = 'submit';
        submit.className = 'secondary-button';
        submit.textContent = '回答をAgentに送る';
        form.append(submit);
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const answers = [];
          for (const [index, question] of message.questions.entries()) {
            const selected = form.querySelector(`input[name="question-${index}"]:checked`)?.value || '';
            const free = form.elements[`question-free-${index}`].value.trim();
            if (!selected && !free) {
              toast('各質問に選択肢か自由記述で回答してください');
              form.elements[`question-free-${index}`].focus();
              return;
            }
            answers.push(`${question.prompt}${selected ? `\n選択: ${selected}` : ''}${free ? `\n自由記述: ${free}` : ''}`);
          }
          const supplement = extra.value.trim();
          const text = `選択式質問への回答:\n${answers.join('\n\n')}${supplement ? `\n\nその他の要望・補足:\n${supplement}` : ''}`;
          await sendChat(text);
        });
        card.append(form);
      }
    }
    container.append(card);
  }
  container.scrollTop = container.scrollHeight;
  const canChat = agentAvailable() && modelSelected() && !busy && state.task.status !== 'completed' && (state.tab === 'plan' || (!state.task.child_count && Boolean(state.task.implementation?.changed_files?.length)));
  $('#chat-input').disabled = !canChat;
  $('#chat-send').disabled = !canChat;
  $('#agent-dot').classList.toggle('available', agentAvailable());
}

async function loadChat() {
  /** Fetch messages for the selected task and tab; return nothing. */
  if (!state.task) return;
  const taskId = state.task.id;
  const phase = state.tab;
  const messages = await api(`/api/tasks/${taskId}/chat?phase=${phase}`);
  if (state.task?.id === taskId && state.tab === phase) {
    state.messages = messages;
    renderChat();
  }
}

async function loadEvidence() {
  /** Fetch evidence for the selected task and refresh its gallery; return nothing. */
  if (!state.task) return;
  const taskId = state.task.id;
  const evidence = await api(`/api/tasks/${taskId}/evidence`);
  if (state.task?.id === taskId) {
    state.evidence = evidence;
    renderEvidence();
  }
}

async function sendChat(message) {
  /** Send a phase message to the Agent and start polling; return nothing. */
  if (!modelSelected()) { toast('先にCodexのモデルを選択してください'); return; }
  try {
    await savePendingDrafts();
    const run = await api(`/api/tasks/${state.task.id}/chat`, {
      method: 'POST', body: JSON.stringify({ phase: state.tab, message }),
    });
    $('#chat-input').value = '';
    await loadChat();
    watchRun(run);
  } catch (error) { toast(error.message); }
}

async function pollRun(runId, taskId) {
  /** Poll runId for taskId and refresh its view when finished; return nothing. */
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const run = await api(`/api/runs/${runId}`);
    if (run.state === 'running') {
      state.activeRuns.set(taskId, run);
      if (state.task?.id === taskId) renderAgentProgress();
      continue;
    }
    state.activeRuns.delete(taskId);
    await refreshTasks();
    if (state.task?.id === taskId) {
      state.task = await api(`/api/tasks/${taskId}`);
      state.drafts = { plan: state.task.plan, result: state.task.result };
      state.planEditing = false;
      await loadChat();
      await loadEvidence();
      render();
    } else if (state.project) {
      state.project = await api(`/api/projects/${state.project.id}`);
      render();
    }
    toast(run.state === 'succeeded' ? 'Agentの実行が完了しました' : `Agentの実行に失敗しました: ${run.message}`);
    break;
  }
}

function watchRun(run) {
  /** Track a started Agent run and return nothing. */
  state.activeRuns.set(run.task_id, run);
  render();
  pollRun(run.id, run.task_id).catch((error) => toast(error.message));
}

async function refreshUntrackedRun() {
  /** Refresh an Agent run found after page reload; return nothing. */
  const taskId = state.task?.id;
  if (refreshingUntrackedRun || !taskId || state.activeRuns.has(taskId) || !isTaskBusy(state.task)) return;
  refreshingUntrackedRun = true;
  try {
    const task = await api(`/api/tasks/${taskId}`);
    if (state.task?.id !== taskId || state.activeRuns.has(taskId) || isTaskBusy(task)) return;
    state.task = task;
    state.drafts = { plan: task.plan, result: task.result };
    await refreshTasks();
    await Promise.all([loadChat(), loadEvidence()]);
    render();
    toast('Agentの実行が終了しました');
  } finally { refreshingUntrackedRun = false; }
}

async function refreshTasks() {
  /** Fetch task metadata and update the navigation tree; return nothing. */
  [state.projects, state.tasks] = await Promise.all([api('/api/projects'), api('/api/tasks')]);
  renderTree();
}

async function selectProject(id) {
  /** Load project id, show its overview, and return nothing. */
  if (hasChanges() && !window.confirm('未保存の変更を破棄して移動しますか？')) return;
  state.project = await api(`/api/projects/${id}`);
  state.task = null;
  render();
}

async function selectTask(id) {
  /** Load task ID after checking unsaved edits; return nothing. */
  if (hasChanges() && !window.confirm('未保存の変更を破棄して移動しますか？')) return;
  state.task = await api(`/api/tasks/${id}`);
  state.project = null;
  state.drafts = { plan: state.task.plan, result: state.task.result };
  state.tab = 'plan';
  state.planEditing = false;
  state.messages = [];
  state.evidence = [];
  render();
  if (state.task.active_run_id && !state.activeRuns.has(id)) {
    try { watchRun(await api(`/api/runs/${state.task.active_run_id}`)); } catch { /* The run may have just finished. */ }
  }
  try { await Promise.all([loadChat(), loadEvidence()]); } catch { toast('タスク資料を読み込めません。サーバーを再起動してください'); }
}

async function saveCurrent() {
  /** Save the plan Markdown draft; return the updated task. */
  if (!state.task) return;
  state.task = await api(`/api/tasks/${state.task.id}`, {
    method: 'PATCH', body: JSON.stringify({ plan: state.drafts.plan }),
  });
  state.planEditing = false;
  render();
  toast('変更を保存しました');
  return state.task;
}

async function savePendingDrafts() {
  /** Save a changed plan draft before an Agent run; return nothing. */
  if (!state.task) return;
  if (state.drafts.plan === state.task.plan) return;
  state.task = await api(`/api/tasks/${state.task.id}`, { method: 'PATCH', body: JSON.stringify({ plan: state.drafts.plan }) });
  render();
}

function openCreate(parentId = null, projectId = null) {
  /** Open the creation form for a parent task or project ID; return nothing. */
  state.parentId = parentId;
  state.createProjectId = projectId;
  $('#dialog-title').textContent = parentId ? '子タスクを作成' : projectId ? 'タスクを作成' : 'プロジェクトを作成';
  $('#dialog-description').textContent = parentId
    ? `「${state.tasks.find((task) => task.id === parentId)?.title || '親タスク'}」の子タスクを作成します。`
    : projectId ? `「${state.projects.find((project) => project.id === projectId)?.name || 'プロジェクト'}」の直下にタスクを作成します。`
      : 'プロジェクト名、ブランチ名、最初のタスクを指定してください。';
  $('#new-title-label').textContent = parentId ? '子タスク名' : projectId ? 'タスク名' : 'プロジェクト名';
  $('#project-create-fields').classList.toggle('hidden', Boolean(parentId || projectId));
  $('#new-slug').required = !parentId && !projectId;
  $('#first-task-title').required = false;
  $('#new-title').value = '';
  $('#new-slug').value = '';
  $('#first-task-title').value = '';
  $('#create-dialog').showModal();
  $('#new-title').focus();
}

function openRename(kind, id) {
  /** Fill the rename dialog for a project or task ID and return nothing. */
  const item = kind === 'project' ? state.projects.find((project) => project.id === id) : state.tasks.find((task) => task.id === id);
  if (!item || (kind === 'task' && isTaskBusy(item))) return;
  state.renameTarget = { kind, id, name: kind === 'project' ? item.name : item.title };
  $('#rename-dialog-title').textContent = kind === 'project' ? 'プロジェクト名を変更' : '開発項目の名前を変更';
  $('#rename-title').value = state.renameTarget.name;
  $('#rename-dialog').showModal();
  $('#rename-title').focus();
  $('#rename-title').select();
}

async function deleteProject(projectId) {
  /** Confirm and delete projectId, then refresh the visible workspace. */
  const project = state.projects.find((item) => item.id === projectId);
  if (!project || !window.confirm(`プロジェクト「${project.name}」と全タスクの記録・作業ブランチを削除しますか？`)) return;
  try {
    await api(`/api/projects/${projectId}`, { method: 'DELETE' });
    if (state.project?.id === projectId) state.project = null;
    if (state.task?.project_id === projectId) {
      state.task = null;
      state.messages = [];
      state.evidence = [];
      state.drafts = { plan: '', result: '' };
    }
    await refreshTasks();
    render();
    toast('プロジェクトを削除しました');
  } catch (error) { toast(error.message); }
}

function openDelete(taskId = state.task?.id) {
  /** Show the deletion scope for taskId and return nothing. */
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  const descendants = subtreeIds(task.id).length - 1;
  state.deleteTargetId = taskId;
  $('#delete-description').textContent = `「${task.title}」${descendants ? `と子タスク ${descendants} 件` : ''}を削除します。`;
  $('#delete-dialog').showModal();
}

function bindEvents() {
  /** Attach actions to task, editor, and dialog controls; return nothing. */
  $('#add-root').addEventListener('click', () => openCreate());
  $('#empty-add').addEventListener('click', () => openCreate());
  $('#add-project-task').addEventListener('click', () => openCreate(null, state.project.id));
  $('#home-button').addEventListener('click', () => {
    if (hasChanges() && !window.confirm('未保存の変更を破棄してホームへ戻りますか？')) return;
    state.task = null;
    state.project = null;
    state.messages = [];
    render();
  });
  $('#focus-chat').addEventListener('click', () => $('#chat-input').focus());
  $('#focus-question').addEventListener('click', () => {
    const form = $('#chat-messages .choice-form');
    form?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    form?.querySelector('input[type="radio"]')?.focus({ preventScroll: true });
  });
  $('#add-child').addEventListener('click', () => openCreate(state.task.id));
  $('#rename-task').addEventListener('click', () => openRename('task', state.task?.id));
  $('#cancel-rename').addEventListener('click', () => { state.renameTarget = null; $('#rename-dialog').close(); });
  $('#rename-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = $('#rename-title').value.trim();
    const target = state.renameTarget;
    if (!target) return;
    if (!title) { toast('名前を入力してください'); return; }
    if (title === target.name) { state.renameTarget = null; $('#rename-dialog').close(); return; }
    try {
      if (target.kind === 'project') {
        const project = await api(`/api/projects/${target.id}`, { method: 'PATCH', body: JSON.stringify({ name: title }) });
        if (state.project?.id === target.id) state.project = project;
      } else {
        const task = await api(`/api/tasks/${target.id}`, { method: 'PATCH', body: JSON.stringify({ title }) });
        if (state.task?.id === target.id) state.task = task;
      }
      $('#rename-dialog').close();
      state.renameTarget = null;
      await refreshTasks();
      render();
      toast(target.kind === 'project' ? 'プロジェクト名を変更しました' : '開発項目の名前を変更しました');
    } catch (error) { toast(error.message); }
  });
  $('#delete-task').addEventListener('click', () => openDelete());
  $('#cancel-delete').addEventListener('click', () => $('#delete-dialog').close());
  $('#confirm-delete').addEventListener('click', async () => {
    const taskId = state.deleteTargetId;
    const parentId = state.tasks.find((task) => task.id === taskId)?.parent_id;
    try {
      const result = await api(`/api/tasks/${taskId}`, { method: 'DELETE' });
      $('#delete-dialog').close();
      state.deleteTargetId = null;
      const selectedDeleted = result.deleted_ids.includes(state.task?.id);
      if (selectedDeleted) {
        state.task = null;
        state.messages = [];
        state.evidence = [];
        state.drafts = { plan: '', result: '' };
      }
      await refreshTasks();
      if (selectedDeleted && parentId && state.tasks.some((task) => task.id === parentId)) await selectTask(parentId);
      else render();
      toast(`${result.deleted_ids.length} 件の開発項目を削除しました`);
    } catch (error) { toast(error.message); }
  });
  $('#cancel-create').addEventListener('click', () => $('#create-dialog').close());
  $('#create-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (hasChanges() && !window.confirm('未保存の変更を破棄して新しい開発項目を開きますか？')) return;
    try {
      let task;
      if (state.parentId) {
        task = await api(`/api/tasks/${state.parentId}/children`, { method: 'POST', body: JSON.stringify({ title: $('#new-title').value }) });
      } else if (state.createProjectId) {
        task = await api(`/api/projects/${state.createProjectId}/tasks`, { method: 'POST', body: JSON.stringify({ title: $('#new-title').value }) });
      } else {
        const project = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name: $('#new-title').value, slug: $('#new-slug').value, first_task_title: $('#first-task-title').value || $('#new-title').value }) });
        task = await api(`/api/tasks/${project.first_task_id}`);
      }
      $('#create-dialog').close();
      state.task = null;
      state.drafts = { plan: '', result: '' };
      await refreshTasks();
      await selectTask(task.id);
      toast(state.parentId ? '子タスクを作成しました' : state.createProjectId ? 'タスクを作成しました' : 'プロジェクトを作成しました');
    } catch (error) { toast(error.message); }
  });
  $('#tab-plan').addEventListener('click', () => { state.tab = 'plan'; state.messages = []; render(); loadChat().catch((error) => toast(error.message)); });
  $('#tab-result').addEventListener('click', () => { state.tab = 'result'; state.messages = []; render(); loadChat().catch((error) => toast(error.message)); });
  $('#plan-mode-toggle').addEventListener('click', () => {
    state.planEditing = !state.planEditing;
    render();
    if (state.planEditing) $('#markdown-editor').focus();
  });
  $('#chat-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = $('#chat-input').value.trim();
    if (!message) return;
    await sendChat(message);
  });
  $('#markdown-editor').addEventListener('input', (event) => {
    state.drafts.plan = event.target.value;
    const dirty = state.drafts.plan !== state.task.plan;
    $('#save-indicator').textContent = dirty ? '未保存の変更' : '保存済み';
    $('#save-indicator').classList.toggle('dirty', dirty);
    $('#save-button').disabled = !dirty;
    $('#plan-empty').classList.toggle('hidden', Boolean(state.task.plan.trim()) || Boolean(state.drafts.plan.trim()));
    renderPlanHighlights();
    $('#execute-button').disabled = true;
  });
  $('#save-button').addEventListener('click', async () => {
    try { await saveCurrent(); } catch (error) { toast(error.message); }
  });
  $('#execute-button').addEventListener('click', async () => {
    $('#execute-dialog').showModal();
  });
  $('#cancel-execute').addEventListener('click', () => $('#execute-dialog').close());
  $('#confirm-execute').addEventListener('click', async () => {
    $('#execute-dialog').close();
    try {
      await savePendingDrafts();
      const run = await api(`/api/tasks/${state.task.id}/execute`, { method: 'POST', body: '{}' });
      state.task = await api(`/api/tasks/${state.task.id}`);
      await refreshTasks();
      watchRun(run);
    } catch (error) { toast(error.message); }
  });
  $('#model-select').addEventListener('change', async (event) => {
    try {
      state.task = await api(`/api/tasks/${state.task.id}`, { method: 'PATCH', body: JSON.stringify({ model: event.target.value }) });
      await refreshTasks();
      render();
    } catch (error) { toast(error.message); render(); }
  });
  $('#provider-select').addEventListener('change', async (event) => {
    try {
      state.task = await api(`/api/tasks/${state.task.id}`, { method: 'PATCH', body: JSON.stringify({ provider: event.target.value }) });
      await refreshTasks();
      render();
    } catch (error) { toast(error.message); render(); }
  });
  $('#evidence-upload').addEventListener('change', async (event) => {
    for (const file of event.target.files) {
      try {
        const response = await fetch(`/api/tasks/${state.task.id}/evidence?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
      } catch (error) { toast(error.message); }
    }
    event.target.value = '';
    await loadEvidence();
  });
  $('#complete-button').addEventListener('click', async () => {
    try {
      const preview = await api(`/api/tasks/${state.task.id}/completion-preview`);
      const details = preview.kind === 'legacy' ? '移行前の検証記録として完了します。新しいコミットは作りません。' : preview.kind === 'commit' ? `コミット先: ${preview.branch}\nメッセージ: ${preview.message}\n対象: ${preview.paths.join(', ')}` : `マージ: ${preview.branch} → ${preview.target}\n対象: ${preview.paths.join(', ')}`;
      if (!window.confirm(`検証結果を確認し、タスクを完了しますか？\n${details}`)) return;
      state.task = await api(`/api/tasks/${state.task.id}/complete`, {
        method: 'POST', body: JSON.stringify(preview),
      });
      await refreshTasks();
      render();
      toast('タスクを完了しました');
    } catch (error) { toast(error.message); }
  });
  $('#summary-button').addEventListener('click', async () => {
    try {
      const run = await api(`/api/tasks/${state.task.id}/summary`, { method: 'POST', body: '{}' });
      state.task = await api(`/api/tasks/${state.task.id}`);
      watchRun(run);
    } catch (error) { toast(error.message); }
  });
  $('#complete-project').addEventListener('click', async () => {
    try {
      const preview = await api(`/api/projects/${state.project.id}/completion-preview`);
      if (!window.confirm(`全タスクの結果を確認し、${preview.branch} を dev にマージしますか？\n対象: ${preview.paths.join(', ')}`)) return;
      state.project = await api(`/api/projects/${state.project.id}/complete`, { method: 'POST', body: JSON.stringify(preview) });
      await refreshTasks();
      render();
      toast(preview.paths.length ? 'プロジェクトを dev にマージしました' : 'プロジェクトを完了しました');
    } catch (error) { toast(error.message); }
  });
  $('#acknowledge-migration').addEventListener('click', async () => {
    try {
      state.project = await api(`/api/projects/${state.project.id}/acknowledge-migration`, { method: 'POST', body: '{}' });
      await refreshTasks();
      render();
      toast('移行先を更新しました');
    } catch (error) { toast(error.message); }
  });
  $('#save-legacy-branch').addEventListener('click', async () => {
    try {
      state.project = await api(`/api/projects/${state.project.id}`, { method: 'PATCH', body: JSON.stringify({ slug: $('#legacy-branch-slug').value.trim() }) });
      await refreshTasks();
      render();
      toast('ブランチ名を更新しました');
    } catch (error) { toast(error.message); }
  });
  $('#delete-project').addEventListener('click', () => deleteProject(state.project.id));
  $('#details-toggle').addEventListener('click', () => {
    const content = $('#details-content');
    content.classList.toggle('hidden');
    $('#details-toggle').setAttribute('aria-expanded', String(!content.classList.contains('hidden')));
  });
  window.addEventListener('beforeunload', (event) => { if (hasChanges()) event.preventDefault(); });
}

async function main() {
  /** Initialize the local workspace; return when its first view is ready. */
  bindChatResize();
  bindEvents();
  setInterval(() => refreshUntrackedRun().catch(() => {}), 1500);
  setInterval(() => { if (state.tasks.some(isTaskBusy)) refreshTasks().catch(() => {}); }, 3000);
  $('#clock').textContent = new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  try {
    const agent = await api('/api/agent');
    state.agentAvailability = agent.providers || { codex: agent.available, claude: false };
    state.modelCatalog = agent.models || { codex: [], claude: [] };
  } catch { toast('Agentに接続できません。サーバーを再起動してください'); }
  try {
    await refreshTasks();
    if (state.tasks.length) await selectTask(state.tasks[0].id);
    else render();
  } catch (error) { toast(error.message); }
}

main();
