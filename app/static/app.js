import { renderMarkdown, setEvidenceRepository } from './markdown.mjs';
import { htmlSection, renderHtmlDocument } from './html-document.mjs';
import { openFileBrowser } from './files.mjs';
import { initHome, renderApproval, showHome } from './home.mjs';

const $ = (selector) => document.querySelector(selector);
const outcomeLabels = { supported: '仮説を支持', not_supported: '仮説を支持せず', inconclusive: '判断保留' };
const chatHeightStorageKey = 'develop-agent.chat-height';
const chatHeightMin = 260;
const chatHeightMax = 1000;
const state = { repository: 'develop-agent', repositories: [], tasks: [], task: null, tab: 'plan', planEditing: false, drafts: { plan: '', result: '' }, parentId: null, deleteTargetId: null, renameTarget: null, messages: [], evidence: [], agentAvailability: { codex: false, claude: false }, modelCatalog: { codex: [], claude: [] }, activeRuns: new Map() };
let refreshingUntrackedRun = false;
let diffRequest = 0;
let parallelSelection = { parentId: null, ids: new Set() };
let chatImages = [];
let sendingChat = false;
const chatImageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const maxChatImageBytes = 10 * 1024 * 1024;
const maxChatFileBytes = 100 * 1024 * 1024;
const maxChatFiles = 10;
let chatFiles = [];

function formatBytes(size) {
  /** Format a byte count as a short KB or MB label; return text. */
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
}

function fileChip(label, detail) {
  /** Build a file chip element with a name and detail text; return it. */
  const chip = document.createElement('span');
  chip.className = 'chat-file-chip';
  const icon = document.createElement('span');
  icon.className = 'chat-file-icon';
  icon.textContent = (label.split('.').pop() || 'FILE').slice(0, 4).toUpperCase();
  const name = document.createElement('span');
  name.className = 'chat-file-name';
  name.textContent = label;
  const small = document.createElement('small');
  small.textContent = detail;
  chip.append(icon, name, small);
  return chip;
}

function renderChatAttachments() {
  /** Render staged image previews and remove controls; return nothing. */
  const container = $('#chat-attachments');
  container.replaceChildren(...chatImages.map((item, index) => {
    const preview = document.createElement('div');
    preview.className = 'chat-attachment';
    const image = document.createElement('img');
    image.src = item.url;
    image.alt = item.file.name;
    image.className = 'previewable-image';
    image.tabIndex = 0;
    image.setAttribute('role', 'button');
    image.setAttribute('aria-label', `${item.file.name}を拡大`);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.disabled = sendingChat;
    remove.setAttribute('aria-label', `${item.file.name}を削除`);
    remove.addEventListener('click', () => {
      URL.revokeObjectURL(item.url);
      chatImages.splice(index, 1);
      renderChatAttachments();
    });
    preview.append(image, remove);
    return preview;
  }), ...chatFiles.map((item, index) => {
    const chip = fileChip(item.file.name, formatBytes(item.file.size));
    chip.classList.add('chat-attachment');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.disabled = sendingChat;
    remove.setAttribute('aria-label', `${item.file.name}を削除`);
    remove.addEventListener('click', () => { chatFiles.splice(index, 1); renderChatAttachments(); });
    chip.append(remove);
    return chip;
  }));
  container.classList.toggle('hidden', !chatImages.length && !chatFiles.length);
}

function clearChatImages() {
  /** Release staged image URLs and clear the attachment preview; return nothing. */
  chatImages.forEach((item) => URL.revokeObjectURL(item.url));
  chatImages = [];
  chatFiles = [];
  $('#chat-image-input').value = '';
  renderChatAttachments();
}

function openImagePreview(image) {
  /** Show image in the preview dialog and return nothing. */
  const dialog = $('#image-preview-dialog');
  const preview = $('#image-preview');
  preview.src = image.currentSrc || image.src;
  preview.alt = image.alt;
  $('#image-preview-caption').textContent = image.alt;
  dialog.showModal();
}

function addChatImages(files) {
  /** Stage images for the Agent's image input and any other files as attachments; return nothing. */
  if (sendingChat) return;
  for (const file of files) {
    if (!chatImageTypes.has(file.type) || file.size > maxChatImageBytes) {
      if (!file.size || file.size > maxChatFileBytes) { toast('ファイルは1件100 MB以下にしてください'); continue; }
      if (chatFiles.length >= maxChatFiles) { toast(`ファイルは${maxChatFiles}件まで添付できます`); break; }
      chatFiles.push({ file, uploadedName: null });
      continue;
    }
    if (chatImages.length >= 4) { toast('画像は4枚まで添付できます'); break; }
    const suffix = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' }[file.type];
    const named = /\.(png|jpe?g|webp|gif)$/i.test(file.name) ? file : new File([file], `pasted-${Date.now()}.${suffix}`, { type: file.type });
    chatImages.push({ file: named, url: URL.createObjectURL(named), uploadedName: null });
  }
  renderChatAttachments();
}

function renderParallelChildren() {
  /** Render selectable ready children of the current parent; return nothing. */
  const parent = state.task;
  const panel = $('#parallel-actions');
  panel.classList.toggle('hidden', state.tab !== 'plan' || !parent.child_count || parent.status === 'completed');
  if (panel.classList.contains('hidden')) return;
  if (parallelSelection.parentId !== parent.id) parallelSelection = { parentId: parent.id, ids: new Set() };
  const children = state.tasks.filter((item) => item.parent_id === parent.id);
  const available = children.filter((item) => item.can_implement && item.implementation?.state === 'not_started' && item.status !== 'completed' && !isTaskBusy(item));
  const list = $('#parallel-children');
  list.replaceChildren(...children.map((item) => {
    const label = document.createElement('label');
    label.className = 'parallel-child';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = item.id;
    input.checked = parallelSelection.ids.has(item.id);
    input.disabled = !available.includes(item);
    input.addEventListener('change', () => {
      if (input.checked) parallelSelection.ids.add(item.id);
      else parallelSelection.ids.delete(item.id);
      $('#parallel-button').disabled = parallelSelection.ids.size < 2 || isTaskBusy(parent);
    });
    label.append(input, document.createTextNode(` ${item.title}${input.disabled ? '（計画確認または未着手が必要）' : ''}`));
    return label;
  }));
  for (const id of parallelSelection.ids) if (!available.some((item) => item.id === id)) parallelSelection.ids.delete(id);
  $('#parallel-button').disabled = parallelSelection.ids.size < 2 || isTaskBusy(parent);
}

function isTaskBusy(task) {
  /** Return whether the selected task has a running Agent request. */
  return Boolean(task && (state.activeRuns.has(task.id) || task.active || task.implementation?.state === 'running'));
}

function taskStatus(task) {
  /** Return a task's visible status, including a locally started Agent run. */
  return isTaskBusy(task)
    ? { key: 'running', label: '実行中', detail: 'Agentが作業しています', owner: 'agent', owner_label: 'Agent 作業中' }
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
  const cancelButton = $('#cancel-run');
  cancelButton.classList.toggle('hidden', !run || (!run.cancelable && !run.cancel_requested));
  cancelButton.disabled = Boolean(run?.cancel_requested);
  if (run?.kind === 'commit') {
    title.textContent = 'コミット・統合中';
    description.textContent = 'Agentが変更を意味のある単位に分けてコミットし、統合先へマージしています。';
  } else if (run?.kind === 'merge') {
    title.textContent = '統合中';
    description.textContent = 'Agentが統合先へマージし、作業ブランチを削除しています。';
  } else if (run?.kind === 'chat' && run.phase === 'plan') {
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
  if (run?.cancel_requested) {
    title.textContent = '中断中';
    description.textContent = 'Agent と起動した処理の停止を待っています。';
  } else if (run && !run.cancelable) {
    title.textContent = '結果を保存中';
    description.textContent = 'Agent の終了結果を反映しています。';
  }
  const running = [...activity].reverse().find((item) => item.state === 'running');
  if (running && run?.cancelable && !run.cancel_requested) description.textContent = `現在実行中: ${running.label}`;
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

function repositoryUrl(path, repository = state.repository) {
  /** Add repository to the API path and return its selected URL. */
  if (path === '/api/repositories' || path === '/api/repository-candidates') return path;
  return `${path}${path.includes('?') ? '&' : '?'}repo=${encodeURIComponent(repository)}`;
}

async function loadRepositoryCandidates() {
  /** Fill the registration picker from Git roots under ~/prog; return nothing. */
  const select = $('#repository-candidate');
  select.replaceChildren(new Option('選択してください', ''), new Option('その他の場所を入力', '__manual__'));
  const entries = await api('/api/repository-candidates');
  select.replaceChildren(new Option('選択してください', ''));
  for (const entry of entries) {
    const option = new Option(`${entry.name} — ${entry.path}`, entry.path);
    option.dataset.name = entry.name;
    select.add(option);
  }
  select.add(new Option('その他の場所を入力', '__manual__'));
  select.value = '';
}

async function api(path, options = {}, repository = state.repository) {
  /** Send path and options to the local API; return its parsed JSON response. */
  const response = await fetch(repositoryUrl(path, repository), {
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

function planSection(source, heading, format = 'markdown') {
  /** Read Markdown or HTML source, a section heading, and the format; return the section's plain text for the key points. */
  if (format === 'html') return htmlSection(source, heading);
  const lines = source.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/);
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
  const format = state.task?.plan_format;
  const questions = planSection(state.drafts.plan, '確認したいこと', format);
  const purpose = planSection(state.drafts.plan, '目的', format);
  $('#plan-questions-text').textContent = questions;
  $('#plan-purpose-text').textContent = purpose;
  $('#plan-questions-card').classList.toggle('hidden', !questions);
  $('#plan-purpose-card').classList.toggle('hidden', !purpose);
  $('#plan-highlights').classList.toggle('hidden', state.tab !== 'plan' || (!questions && !purpose));
}

function familyButton(task, prefix) {
  /** Build a button that opens a related task with its owner state; return it. */
  const status = taskStatus(task);
  const button = document.createElement('button');
  button.type = 'button';
  button.title = `${task.title}：${status.owner_label}・${status.label}（${status.detail}）`;
  const dot = document.createElement('i');
  dot.className = `home-owner-dot ${status.owner}`;
  button.append(dot, document.createTextNode(`${prefix}${task.title} · ${status.owner_label}`));
  button.addEventListener('click', () => selectTask(task.id));
  return button;
}

function renderFamily() {
  /** Render the selected task's parent and child links; return nothing. */
  const nav = $('#task-family');
  const task = state.task;
  nav.replaceChildren();
  if (!task) return;
  const parent = state.tasks.find((item) => item.id === task.parent_id);
  if (parent) nav.append(familyButton(parent, '↑ 親: '));
  const children = state.tasks.filter((item) => item.parent_id === task.id);
  if (children.length) {
    const label = document.createElement('span');
    label.textContent = `子タスク ${children.length}`;
    nav.append(label, ...children.map((child) => familyButton(child, '')));
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

async function loadTaskDiff() {
  /** Load changed paths for the selected task and render patches on file expansion. */
  const taskId = state.task?.id;
  if (!taskId) return;
  const request = ++diffRequest;
  const container = $('#changed-files');
  container.textContent = '変更内容を読み込み中…';
  try {
    const { files } = await api(`/api/tasks/${taskId}/diff`);
    if (request !== diffRequest || state.task?.id !== taskId) return;
    container.replaceChildren();
    if (!files.length) {
      container.textContent = 'このタスクで確認できるコード差分はまだありません。';
      return;
    }
    for (const path of files) {
      const details = document.createElement('details');
      details.className = 'diff-file';
      const summary = document.createElement('summary');
      summary.textContent = path;
      const openFile = document.createElement('button');
      openFile.type = 'button';
      openFile.className = 'subtle-button changed-file-open';
      openFile.textContent = 'ファイルを開く';
      openFile.addEventListener('click', (event) => { event.preventDefault(); openFileBrowser({ repository: state.repository, taskId, path }); });
      summary.append(openFile);
      const content = document.createElement('div');
      details.append(summary, content);
      details.addEventListener('toggle', async () => {
        if (!details.open || details.dataset.loaded) return;
        content.textContent = '差分を読み込み中…';
        try {
          const { patch } = await api(`/api/tasks/${taskId}/diff?path=${encodeURIComponent(path)}`);
          if (state.task?.id !== taskId || !details.isConnected) return;
          const code = document.createElement('pre');
          code.className = 'diff-code';
          for (const line of patch.split('\n')) {
            const row = document.createElement('span');
            row.className = line.startsWith('@@') ? 'diff-hunk' : line.startsWith('+') && !line.startsWith('+++') ? 'diff-added' : line.startsWith('-') && !line.startsWith('---') ? 'diff-removed' : 'diff-context';
            row.textContent = line || ' ';
            code.append(row);
          }
          content.replaceChildren(code);
          details.dataset.loaded = 'true';
        } catch (error) { content.textContent = error.message; }
      });
      container.append(details);
    }
  } catch (error) {
    if (request === diffRequest) container.textContent = error.message;
  }
}

function render() {
  /** Read the selected task and tab, then render the GUI; return nothing. */
  const task = state.task;
  $('#home-view').classList.toggle('hidden', Boolean(task));
  $('#task-view').classList.toggle('hidden', !task);
  renderFamily();
  if (!task) { $('#breadcrumb').textContent = 'すべてのプロジェクト'; showHome().catch((error) => toast(error.message)); return; }
  const isPlan = state.tab === 'plan';
  const planIsHtml = task.plan_format === 'html';
  const parent = state.tasks.find((item) => item.id === task.parent_id);
  const migrationPending = Boolean(task.migration_pending);
  $('#breadcrumb').textContent = `${state.repositories.find((entry) => entry.id === state.repository)?.name || state.repository} / ${task.title}`;
  $('#task-title').textContent = task.title;
  $('#task-number').textContent = `/ ${task.id.slice(0, 6).toUpperCase()}`;
  $('#task-parent').textContent = parent ? `親タスク / ${parent.title}` : task.status === 'completed' ? 'devへ統合済み' : `作業ブランチ / ${task.branch}`;
  const status = taskStatus(task);
  $('#status-label').textContent = `${status.label} · ${status.detail}`;
  $('#status-label').className = `status-pill ${status.key}`;
  const owner = document.createElement('i');
  owner.className = 'home-owner-dot';
  $('#status-owner').replaceChildren(owner, document.createTextNode(status.owner_label));
  $('#status-owner').className = `status-owner ${status.owner}`;
  $('#tab-plan').classList.toggle('active', isPlan);
  $('#tab-result').classList.toggle('active', !isPlan);
  $('#tab-plan').setAttribute('aria-selected', String(isPlan));
  $('#tab-result').setAttribute('aria-selected', String(!isPlan));
  $('#section-kicker').textContent = isPlan ? 'PLAN / HYPOTHESIS' : 'RESULT / EVIDENCE';
  $('#section-title').textContent = isPlan ? '開発の計画' : '検証の結果';
  $('#section-description').textContent = isPlan
    ? `目的から仮説、要件、実装手順までを${planIsHtml ? '図と表を含むHTML' : 'Markdown'}で整理します。`
    : '検証のたびに増えるレポートを開いて、結論と根拠を確認できます。';
  $('#file-path').textContent = `${task.storage_path}/${task.plan_file}`;
  $('#file-label').textContent = task.plan_file;
  $('#file-icon').textContent = planIsHtml ? '</>' : 'M↓';
  $('#task-path').textContent = `${task.storage_path}/`;
  $('#commit-history').textContent = task.commit_hashes?.length
    ? `このタスクのコミット: ${task.commit_hashes.map((hash) => hash.slice(0, 10)).join('、')}`
    : '';
  $('#markdown-editor').value = state.drafts.plan;
  $('#markdown-editor').placeholder = planIsHtml
    ? '<h1>開発計画</h1>\n\n<h2>目的</h2>\n<div class="callout note"><p><strong>してほしいことを一文で書く。</strong></p></div>\n\n<h2>現状と困っていること</h2>\n<ul><li>一つ目</li></ul>\n'
    : '# 開発計画\n\n## 目的\nしてほしいことを一文で書く。\n\n## 現状と困っていること\n- 一つ目\n- 二つ目\n- 三つ目\n';
  $('#markdown-editor').setAttribute('aria-label', planIsHtml ? 'HTML編集' : 'Markdown編集');
  renderPlanPreview(task, planIsHtml);
  if (!state.drafts.plan.trim()) {
    const emptyPreview = document.createElement('p');
    emptyPreview.className = 'preview-empty';
    emptyPreview.textContent = '計画を作成すると、ここに書式付きで表示されます。';
    $('#plan-preview').append(emptyPreview);
  }
  $('#plan-preview').classList.toggle('hidden', state.planEditing);
  $('#markdown-editor').classList.toggle('hidden', !state.planEditing);
  $('#save-button').classList.toggle('hidden', !state.planEditing);
  $('#plan-mode-toggle').textContent = state.planEditing ? 'プレビューに戻る' : planIsHtml ? 'HTMLを編集' : 'Markdownを編集';
  renderPlanHighlights();
  $('#plan-editor').classList.toggle('hidden', !isPlan);
  $('#save-indicator').classList.toggle('hidden', !isPlan);
  $('#report-section').classList.toggle('hidden', isPlan);
  if (!isPlan) renderReports();
  $('#plan-actions').classList.toggle('hidden', !isPlan);
  $('#result-actions').classList.toggle('hidden', isPlan);
  $('#summary-actions').classList.toggle('hidden', isPlan || !task.child_count || task.status === 'completed');
  renderParallelChildren();
  $('#evidence-section').classList.toggle('hidden', isPlan);
  $('#chat-context').textContent = isPlan ? '計画について議論' : '検証・修正について議論';
  $('#chat-input').placeholder = task.completion_waiting ? 'Agentの完了確認への回答を入力（文章のみ）' : isPlan ? 'Agentに相談する内容を入力' : task.child_count ? '子タスクの総括ボタンを使用してください' : task.implementation?.changed_files?.length ? '追加検証や修正したいことを入力' : '実装後に追加検証や修正を依頼できます';
  const busy = isTaskBusy(task);
  renderAgentProgress();
  $('#rename-task').disabled = busy;
  $('#add-child').disabled = migrationPending || busy || task.status === 'completed';
  $('#delete-task').disabled = subtreeIds(task.id).some((id) => isTaskBusy(state.tasks.find((item) => item.id === id)));
  $('#plan-empty').classList.toggle('hidden', !isPlan || Boolean(task.plan.trim()) || Boolean(state.drafts.plan.trim()));
  $('#execute-button').disabled = migrationPending || Boolean(task.child_count) || !task.can_implement || state.drafts.plan !== task.plan || !agentAvailable() || !modelSelected() || busy || task.status === 'completed' || task.implementation?.state === 'running';
  $('#execute-button').title = task.readiness?.reason || 'Agentの実装可否判定を待っています';
  $('#readiness-reason').textContent = migrationPending ? '移行前のGit変更を確認してください。' : task.can_implement ? 'Agentが計画を実装可能と判定しました。確認後に開始できます。' : task.readiness?.reason || 'Agentに計画を相談してください。';
  renderApproval(task, { busy: busy || migrationPending, dirty: state.drafts.plan !== task.plan });
  const childrenDone = state.tasks.filter((item) => item.parent_id === task.id).every((item) => item.status === 'completed');
  const summaryReady = task.reports?.at(-1)?.kind === 'summary';
  $('#summary-button').disabled = migrationPending || !childrenDone || busy || !agentAvailable() || !modelSelected();
  $('#complete-button').disabled = migrationPending || task.status === 'completed' || busy || (task.child_count ? !childrenDone || !summaryReady : task.implementation?.state !== 'succeeded' || !task.result.trim());
  $('#complete-button').textContent = task.status === 'completed' ? '完了済み ✓' : task.branch && task.commit_hash && !task.commit_pending && !task.code_owner && !task.child_count ? `${task.parent_id ? '親ブランチ' : 'dev'}へ統合 →` : 'タスクを完了 →';
  $('#complete-button').title = task.implementation?.state !== 'succeeded' ? 'CLIによる実装が確認されてから完了できます' : '';
  $('#result-actions p').textContent = task.implementation?.validation_outcome
    ? `Agentの検証結果：${outcomeLabels[task.implementation.validation_outcome]}。結果を確認し、目的を達成したと判断したら完了にしてください。`
    : '目的を達成したと判断したら、この開発項目を完了にします。';
  if (!$('#details-content').classList.contains('hidden')) loadTaskDiff();
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
  $('#proposal-section').classList.toggle('hidden', state.tab !== 'plan' || !items.length);
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
    approve.disabled = Boolean(item.stale || state.task.migration_pending);
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

function documentView(source, format, taskId, label) {
  /** Read document source, its format, task ID, and label; return a sandboxed HTML frame or rendered Markdown. */
  return format === 'html' ? renderHtmlDocument(source, taskId, label) : renderMarkdown(source, taskId);
}

function renderPlanPreview(task, planIsHtml) {
  /** Render the plan draft preview for task, reusing an unchanged HTML frame; return nothing. */
  const preview = $('#plan-preview');
  const current = preview.firstElementChild;
  const key = `${task.id}:${state.drafts.plan}`;
  if (planIsHtml && current?.dataset.documentKey === key) return;
  if (!state.drafts.plan.trim()) { preview.replaceChildren(); return; }
  const view = documentView(state.drafts.plan, task.plan_format, task.id, '計画のプレビュー');
  view.dataset.documentKey = key;
  preview.replaceChildren(view);
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
    title.textContent = planSection(report.content, '結論', report.format).replace(/\n/g, ' ') || `検証レポート ${report.number}`;
    const meta = document.createElement('small');
    meta.textContent = `${outcomeLabels[report.outcome] || '結果を確認'} · ${new Date(report.created_at).toLocaleString('ja-JP')}`;
    summary.append(number, title, meta);
    const body = document.createElement('div');
    body.className = 'report-body';
    const path = document.createElement('div');
    path.className = 'report-path';
    path.textContent = `${state.task.storage_path}/${report.file}`;
    body.append(path, documentView(report.content, report.format, state.task.id, `検証レポート ${report.number}`));
    card.append(summary, body);
    list.append(card);
  }
}

function renderModel() {
  /** Render the CLI catalog and any saved model; return nothing. */
  const select = $('#model-select');
  const choices = state.modelCatalog[state.task.provider] || [];
  select.replaceChildren();
  if (state.task.provider === 'claude') select.add(new Option('既定（CLI設定）', ''));
  else if (!state.task.model) {
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
    const url = repositoryUrl(`/api/tasks/${state.task.id}/evidence/${encodeURIComponent(entry.name)}`);
    if (entry.kind === 'image') {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      const image = document.createElement('img');
      image.src = url;
      image.alt = entry.name;
      image.className = 'previewable-image';
      image.loading = 'lazy';
      image.decoding = 'async';
      link.setAttribute('aria-label', `${entry.name}を拡大`);
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
    if (message.content) content.append(renderMarkdown(message.content, state.task.id));
    card.append(heading, content);
    if (message.images?.length) {
      const gallery = document.createElement('div');
      gallery.className = 'chat-message-images';
      for (const name of message.images) {
        const link = document.createElement('a');
        link.href = repositoryUrl(`/api/tasks/${state.task.id}/evidence/${encodeURIComponent(name)}`);
        link.target = '_blank';
        link.rel = 'noopener';
        const image = document.createElement('img');
        image.src = link.href;
        image.alt = name;
        image.className = 'previewable-image';
        image.loading = 'lazy';
        link.setAttribute('aria-label', `${name}を拡大`);
        link.append(image);
        gallery.append(link);
      }
      card.append(gallery);
    }
    if (message.files?.length) {
      const list = document.createElement('div');
      list.className = 'chat-message-files';
      for (const name of message.files) {
        const link = document.createElement('a');
        link.href = repositoryUrl(`/api/tasks/${state.task.id}/attachments/${encodeURIComponent(name)}`);
        link.download = name;
        link.title = `${name}をダウンロード`;
        link.append(fileChip(name.replace(/^[0-9a-f]{8}-/, ''), 'ダウンロード'));
        list.append(link);
      }
      card.append(list);
    }
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
  const canChat = agentAvailable() && modelSelected() && !busy && !sendingChat && state.task.status !== 'completed' && (state.tab === 'plan' || Boolean(state.task.completion_waiting) || (!state.task.child_count && Boolean(state.task.implementation?.changed_files?.length)));
  $('#chat-input').disabled = !canChat;
  $('#chat-send').disabled = !canChat;
  $('#chat-image-input').disabled = !canChat;
  $('#chat-image-input').parentElement.classList.toggle('disabled', !canChat);
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
  /** Upload staged images, send one chat message, and start polling; return nothing. */
  if (sendingChat) return;
  if (!modelSelected()) { toast('先にCodexのモデルを選択してください'); return; }
  sendingChat = true;
  const taskId = state.task.id;
  const phase = state.tab;
  const repository = state.repository;
  renderChat();
  renderChatAttachments();
  try {
    await savePendingDrafts();
    for (const item of chatImages) {
      if (item.uploadedName) continue;
      const response = await fetch(repositoryUrl(`/api/tasks/${taskId}/evidence?name=${encodeURIComponent(item.file.name)}`, repository), { method: 'POST', body: item.file });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '画像を保存できませんでした');
      item.uploadedName = result.name;
    }
    for (const item of chatFiles) {
      if (item.uploadedName) continue;
      const response = await fetch(repositoryUrl(`/api/tasks/${taskId}/attachments?name=${encodeURIComponent(item.file.name)}`, repository), { method: 'POST', body: item.file });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'ファイルを保存できませんでした');
      item.uploadedName = result.name;
    }
    if (state.task?.id !== taskId || state.tab !== phase || state.repository !== repository) return;
    const run = await api(`/api/tasks/${taskId}/chat`, {
      method: 'POST', body: JSON.stringify({ phase, message, images: chatImages.map((item) => item.uploadedName), files: chatFiles.map((item) => item.uploadedName) }),
    }, repository);
    $('#chat-input').value = '';
    clearChatImages();
    await loadChat();
    await loadEvidence();
    watchRun(run);
  } catch (error) { toast(error.message); }
  finally { sendingChat = false; renderChatAttachments(); if (state.task) renderChat(); }
}

async function pollRun(runId, taskId, repository) {
  /** Poll runId for taskId and refresh its view when finished; return nothing. */
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (state.repository !== repository) return;
    let run;
    try {
      run = await api(`/api/runs/${runId}`, {}, repository);
    } catch {
      // A restarted server forgets its runs; stop tracking once the task is idle there.
      const task = await api(`/api/tasks/${taskId}`, {}, repository).catch(() => null);
      if (!task || task.active || task.implementation?.state === 'running') continue;
      run = { state: 'failed', message: '実行状態を確認できなくなりました。会話と変更内容を確認してください' };
    }
    if (state.repository !== repository) return;
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
      if (run.state === 'needs_input') state.tab = 'result';
      await loadChat();
      await loadEvidence();
      render();
    }
    toast(run.state === 'succeeded' ? 'Agentの実行が完了しました' : run.state === 'needs_input' ? 'Agentから完了について確認があります。チャットで回答してください' : run.state === 'cancelled' ? run.message : `Agentの実行に失敗しました: ${run.message}`);
    break;
  }
}

function watchRun(run) {
  /** Track a started Agent run and return nothing. */
  state.activeRuns.set(run.task_id, run);
  render();
  pollRun(run.id, run.task_id, state.repository).catch((error) => toast(error.message));
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
  /** Fetch task metadata and update parent and child links; return nothing. */
  state.tasks = await api('/api/tasks');
  renderFamily();
}

async function selectTask(id) {
  /** Load task ID after checking unsaved edits; return nothing. */
  if (hasChanges() && !window.confirm('未保存の変更を破棄して移動しますか？')) return;
  clearChatImages();
  state.task = await api(`/api/tasks/${id}`);
  state.drafts = { plan: state.task.plan, result: state.task.result };
  state.tab = state.task.completion_waiting ? 'result' : 'plan';
  state.planEditing = false;
  state.messages = [];
  state.evidence = [];
  $('#details-content').classList.add('hidden');
  $('#details-toggle').setAttribute('aria-expanded', 'false');
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

function openCreate(parentId = null) {
  /** Open the creation form for a root or child task; return nothing. */
  state.parentId = parentId;
  $('#dialog-title').textContent = parentId ? '子タスクを作成' : 'タスクを作成';
  $('#dialog-description').textContent = parentId
    ? `「${state.tasks.find((task) => task.id === parentId)?.title || '親タスク'}」の子タスクを作成します。`
    : 'タスク名を入力してください。専用ブランチを作成します。';
  $('#new-title-label').textContent = parentId ? '子タスク名' : 'タスク名';
  $('#new-title').value = '';
  $('#create-dialog').showModal();
  $('#new-title').focus();
}

function openRename(id) {
  /** Fill the rename dialog for task ID and return nothing. */
  const item = state.tasks.find((task) => task.id === id);
  if (!item || isTaskBusy(item)) return;
  state.renameTarget = { id, name: item.title };
  $('#rename-dialog-title').textContent = 'タスク名を変更';
  $('#rename-title').value = state.renameTarget.name;
  $('#rename-dialog').showModal();
  $('#rename-title').focus();
  $('#rename-title').select();
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
  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const image = target.closest('img.previewable-image') || target.closest('a')?.querySelector(':scope > img.previewable-image');
    if (!image) return;
    event.preventDefault();
    openImagePreview(image);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (!(event.target instanceof HTMLImageElement) || !event.target.matches('img.previewable-image[role="button"]')) return;
    event.preventDefault();
    openImagePreview(event.target);
  });
  $('#close-image-preview').addEventListener('click', () => $('#image-preview-dialog').close());
  $('#image-preview-dialog').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  let pendingCommit = null;
  $('#add-repository').addEventListener('click', async () => {
    if (hasChanges() && !window.confirm('未保存の変更を破棄してリポジトリを登録しますか？')) return;
    $('#repository-form').reset();
    $('#repository-custom-path').classList.add('hidden');
    $('#repository-path').required = false;
    try { await loadRepositoryCandidates(); }
    catch (error) { toast(error.message); }
    $('#repository-dialog').showModal();
  });
  $('#cancel-repository').addEventListener('click', () => $('#repository-dialog').close());
  $('#repository-candidate').addEventListener('change', (event) => {
    const manual = event.target.value === '__manual__';
    $('#repository-custom-path').classList.toggle('hidden', !manual);
    $('#repository-path').required = manual;
    $('#repository-path').value = manual ? '' : event.target.value;
    $('#repository-name').value = manual ? '' : event.target.selectedOptions[0]?.dataset.name || '';
  });
  $('#repository-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const entry = await api('/api/repositories', { method: 'POST', body: JSON.stringify({ name: $('#repository-name').value, path: $('#repository-path').value, gpu: $('#repository-gpu').checked }) });
      state.repositories.push(entry);
      $('#repository-dialog').close();
      $('#repository-form').reset();
      $('#repository-custom-path').classList.add('hidden');
      $('#repository-path').required = false;
      await switchRepository(entry.id, false);
      await showHome(true);
    } catch (error) { toast(error.message); }
  });
  $('#open-task-files').addEventListener('click', () => openFileBrowser({ repository: state.repository, taskId: state.task?.id }));
  $('#home-button').addEventListener('click', () => {
    if (hasChanges() && !window.confirm('未保存の変更を破棄してホームへ戻りますか？')) return;
    state.task = null;
    state.messages = [];
    render();
    showHome(true).catch((error) => toast(error.message));
  });
  $('#approve-plan').addEventListener('click', async () => {
    try {
      state.task = await api(`/api/tasks/${state.task.id}/plan-approval`, { method: state.task.plan_approved ? 'DELETE' : 'POST' });
      await refreshTasks();
      render();
    } catch (error) { toast(error.message); }
  });
  $('#focus-chat').addEventListener('click', () => $('#chat-input').focus());
  $('#cancel-run').addEventListener('click', async () => {
    const run = state.activeRuns.get(state.task?.id);
    if (!run || run.cancel_requested) return;
    try {
      state.activeRuns.set(run.task_id, await api(`/api/runs/${run.id}/cancel`, { method: 'POST' }));
      renderAgentProgress();
    } catch (error) { toast(error.message); }
  });
  $('#focus-question').addEventListener('click', () => {
    const form = $('#chat-messages .choice-form');
    form?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    form?.querySelector('input[type="radio"]')?.focus({ preventScroll: true });
  });
  $('#add-child').addEventListener('click', () => openCreate(state.task.id));
  $('#rename-task').addEventListener('click', () => openRename(state.task?.id));
  $('#cancel-rename').addEventListener('click', () => { state.renameTarget = null; $('#rename-dialog').close(); });
  $('#rename-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = $('#rename-title').value.trim();
    const target = state.renameTarget;
    if (!target) return;
    if (!title) { toast('名前を入力してください'); return; }
    if (title === target.name) { state.renameTarget = null; $('#rename-dialog').close(); return; }
    try {
      const task = await api(`/api/tasks/${target.id}`, { method: 'PATCH', body: JSON.stringify({ title }) });
      if (state.task?.id === target.id) state.task = task;
      $('#rename-dialog').close();
      state.renameTarget = null;
      await refreshTasks();
      render();
      toast('タスク名を変更しました');
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
      } else {
        task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ title: $('#new-title').value }) });
      }
      $('#create-dialog').close();
      state.task = null;
      state.drafts = { plan: '', result: '' };
      await refreshTasks();
      await selectTask(task.id);
      toast(state.parentId ? '子タスクを作成しました' : 'タスクを作成しました');
    } catch (error) { toast(error.message); }
  });
  $('#tab-plan').addEventListener('click', () => { clearChatImages(); state.tab = 'plan'; state.messages = []; render(); loadChat().catch((error) => toast(error.message)); });
  $('#tab-result').addEventListener('click', () => { clearChatImages(); state.tab = 'result'; state.messages = []; render(); loadChat().catch((error) => toast(error.message)); });
  $('#plan-mode-toggle').addEventListener('click', () => {
    state.planEditing = !state.planEditing;
    render();
    if (state.planEditing) $('#markdown-editor').focus();
  });
  const chatForm = $('#chat-form');
  chatForm.addEventListener('dragover', (event) => {
    if (!event.dataTransfer?.types.includes('Files') || $('#chat-image-input').disabled) return;
    event.preventDefault();
    chatForm.classList.add('drag-over');
  });
  chatForm.addEventListener('dragleave', () => chatForm.classList.remove('drag-over'));
  chatForm.addEventListener('drop', (event) => {
    chatForm.classList.remove('drag-over');
    if (!event.dataTransfer?.files.length || $('#chat-image-input').disabled) return;
    event.preventDefault();
    addChatImages(event.dataTransfer.files);
  });
  $('#chat-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = $('#chat-input').value.trim();
    if (!message && !chatImages.length && !chatFiles.length) return;
    await sendChat(message);
  });
  $('#chat-image-input').addEventListener('change', (event) => {
    addChatImages(event.target.files);
    event.target.value = '';
  });
  $('#chat-input').addEventListener('paste', (event) => {
    const files = [...event.clipboardData.files];
    if (!files.length) return;
    event.preventDefault();
    addChatImages(files);
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
    $('#execute-instruction').focus();
  });
  $('#cancel-execute').addEventListener('click', () => $('#execute-dialog').close());
  $('#confirm-execute').addEventListener('click', async () => {
    $('#execute-dialog').close();
    try {
      await savePendingDrafts();
      const run = await api(`/api/tasks/${state.task.id}/execute`, { method: 'POST', body: JSON.stringify({ additional_instruction: $('#execute-instruction').value }) });
      $('#execute-instruction').value = '';
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
        const response = await fetch(repositoryUrl(`/api/tasks/${state.task.id}/evidence?name=${encodeURIComponent(file.name)}`), { method: 'POST', body: file });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
      } catch (error) { toast(error.message); }
    }
    event.target.value = '';
    await loadEvidence();
  });
  $('#parallel-button').addEventListener('click', () => {
    const names = state.tasks.filter((item) => parallelSelection.ids.has(item.id)).map((item) => item.title);
    if (names.length < 2) return;
    $('#parallel-selection-summary').textContent = `選択した子タスク: ${names.join('、')}`;
    $('#parallel-dialog').showModal();
    $('#parallel-instruction').focus();
  });
  $('#cancel-parallel').addEventListener('click', () => $('#parallel-dialog').close());
  $('#confirm-parallel').addEventListener('click', async () => {
    try {
      const taskIds = [...parallelSelection.ids];
      if (taskIds.length < 2) return;
      await savePendingDrafts();
      const runs = await api(`/api/tasks/${state.task.id}/parallel-execute`, {
        method: 'POST', body: JSON.stringify({ task_ids: taskIds, additional_instruction: $('#parallel-instruction').value }),
      });
      $('#parallel-dialog').close();
      $('#parallel-instruction').value = '';
      parallelSelection.ids.clear();
      await refreshTasks();
      for (const run of runs) watchRun(run);
      toast(`${runs.length} 件の子タスクを並列実行しています`);
    } catch (error) { toast(error.message); }
  });
  $('#complete-button').addEventListener('click', async () => {
    try {
      const preview = await api(`/api/tasks/${state.task.id}/completion-preview`);
      if (preview.kind === 'commit' || preview.kind === 'merge') {
        const target = state.task.parent_id ? '親ブランチ' : 'dev';
        pendingCommit = { taskId: state.task.id, preview };
        $('#commit-title').textContent = preview.kind === 'commit' ? 'タスクを完了しますか？' : `${target}へ統合しますか？`;
        $('#commit-description').textContent = preview.kind === 'commit'
          ? `変更全体を確認してください。承認後、Agent が内容に応じて一件または複数件のコミットを作成し、${target}へ統合して作業ブランチを削除します。`
          : `承認後、Agent が${target}へ統合し、競合があれば解決して作業ブランチを削除します。解決できない場合は統合を取り消します。`;
        $('#commit-branch').textContent = `${preview.branch} → ${preview.target || state.tasks.find((item) => item.id === state.task.parent_id)?.branch || 'dev'}`;
        $('#commit-paths').replaceChildren(...preview.paths.map((path) => {
          const item = document.createElement('li');
          item.textContent = path;
          return item;
        }));
        $('#commit-instruction').value = '';
        $('#commit-dialog').showModal();
        $('#commit-instruction').focus();
        return;
      }
      if (!window.confirm('検証結果を確認し、タスクを完了しますか？\n移行前の検証記録として完了します。新しいコミットは作りません。')) return;
      state.task = await api(`/api/tasks/${state.task.id}/complete`, {
        method: 'POST', body: JSON.stringify(preview),
      });
      await refreshTasks();
      render();
      toast('タスクを完了しました');
    } catch (error) {
      state.task = await api(`/api/tasks/${state.task.id}`);
      await refreshTasks();
      render();
      toast(error.message);
    }
  });
  $('#cancel-commit').addEventListener('click', () => $('#commit-dialog').close());
  $('#commit-dialog').addEventListener('close', () => { pendingCommit = null; });
  $('#commit-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!pendingCommit || $('#confirm-commit').disabled) return;
    const { taskId, preview } = pendingCommit;
    $('#confirm-commit').disabled = true;
    try {
      const run = await api(`/api/tasks/${taskId}/complete`, {
        method: 'POST', body: JSON.stringify({ ...preview, commit_instruction: $('#commit-instruction').value }),
      });
      $('#commit-dialog').close();
      state.task = await api(`/api/tasks/${taskId}`);
      watchRun(run);
    } catch (error) {
      state.task = await api(`/api/tasks/${taskId}`);
      if (state.task.commit_hash) $('#commit-dialog').close();
      await refreshTasks();
      render();
      toast(error.message);
    }
    finally { $('#confirm-commit').disabled = false; }
  });
  $('#summary-button').addEventListener('click', () => {
    $('#summary-dialog').showModal();
    $('#summary-instruction').focus();
  });
  $('#cancel-summary').addEventListener('click', () => $('#summary-dialog').close());
  $('#confirm-summary').addEventListener('click', async () => {
    $('#summary-dialog').close();
    try {
      const run = await api(`/api/tasks/${state.task.id}/summary`, { method: 'POST', body: JSON.stringify({ additional_instruction: $('#summary-instruction').value }) });
      $('#summary-instruction').value = '';
      state.task = await api(`/api/tasks/${state.task.id}`);
      watchRun(run);
    } catch (error) { toast(error.message); }
  });
  $('#details-toggle').addEventListener('click', () => {
    const content = $('#details-content');
    content.classList.toggle('hidden');
    $('#details-toggle').setAttribute('aria-expanded', String(!content.classList.contains('hidden')));
    if (!content.classList.contains('hidden')) loadTaskDiff();
  });
  window.addEventListener('beforeunload', (event) => { if (hasChanges()) event.preventDefault(); });
}

async function switchRepository(repository, openFirst = true) {
  /** Load repository's task and Agent state, optionally opening its first task; return nothing. */
  const agent = await api('/api/agent', {}, repository);
  const tasks = await api('/api/tasks', {}, repository);
  clearChatImages();
  state.repository = repository;
  setEvidenceRepository(repository);
  window.localStorage.setItem('develop-agent.repository', repository);
  state.agentAvailability = agent.providers || { codex: agent.available, claude: false };
  state.modelCatalog = agent.models || { codex: [], claude: [] };
  state.tasks = tasks;
  state.task = null;
  state.messages = [];
  state.evidence = [];
  state.activeRuns.clear();
  if (openFirst && tasks.length) await selectTask(tasks[0].id);
  else render();
}

async function openHomeTask(repository, taskId) {
  /** Switch to repository when needed and open taskId from the home view; return nothing. */
  if (repository !== state.repository) await switchRepository(repository, false);
  await selectTask(taskId);
}

async function createHomeTask(repository) {
  /** Switch to repository when needed and open the new task dialog; return nothing. */
  if (repository !== state.repository) await switchRepository(repository, false);
  openCreate();
}

async function main() {
  /** Initialize the local workspace; return when its first view is ready. */
  bindChatResize();
  bindEvents();
  initHome({ api, toast, openTask: openHomeTask, createTask: createHomeTask, models: () => state.modelCatalog, providers: () => state.agentAvailability });
  setInterval(() => refreshUntrackedRun().catch(() => {}), 1500);
  setInterval(() => { if (state.tasks.some(isTaskBusy)) refreshTasks().catch(() => {}); }, 3000);
  $('#clock').textContent = new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  try {
    const repositories = await api('/api/repositories');
    state.repositories = repositories;
    const stored = window.localStorage.getItem('develop-agent.repository');
    await switchRepository(repositories.some((entry) => entry.id === stored) ? stored : 'develop-agent', false);
  } catch (error) { toast(error.message); }
}

main();
