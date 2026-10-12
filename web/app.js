import { MotionViewer } from './viewer.js';

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 12;
const LAST_JOB_KEY = 'mia.motion-studio.last-job';
const state = {
  file: null, fileURL: null, selection: null, category: '', query: '', offset: 0,
  total: 0, catalogLoading: false, service: null, submitting: false, job: null,
  jobBusy: false, artifacts: null, pollTimer: null, pollVersion: 0,
  catalogVersion: 0, catalogController: null, viewerVersion: 0,
};
let viewer = null;
let viewerState = { time: 0, duration: 0, playing: false, hasModel: false };
let scrubbing = false;
let searchTimer = null;
let healthLoading = false;
let demoLoading = false;

function errorText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((entry) => entry.msg || JSON.stringify(entry)).join('；');
  return value ? JSON.stringify(value) : '请求失败，请重试。';
}
function localURL(value) {
  const url = new URL(value, window.location.origin);
  if (url.origin !== window.location.origin) throw new Error('服务返回了非同源地址，已停止访问。');
  return url.pathname + url.search;
}
async function requestJSON(url, options = {}) {
  const response = await fetch(localURL(url), { ...options, headers: { Accept: 'application/json', ...options.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无效响应（HTTP ${response.status}）。`); }
  if (!response.ok) {
    const error = new Error(errorText(data.detail || data.error || `HTTP ${response.status}`));
    error.status = response.status;
    throw error;
  }
  return data;
}
function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '大小未知';
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}
function formatTime(seconds) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  return `${Math.floor(safe / 60)}:${String(Math.floor(safe % 60)).padStart(2, '0')}`;
}
function updateRunState() {
  const busy = state.submitting || state.jobBusy;
  const ready = state.service?.status === 'ready';
  $('generate').disabled = busy || !ready || !state.file || !state.selection;
  $('generate').firstElementChild.textContent = state.submitting ? '正在上传模型' : state.jobBusy ? '任务正在进行' : '生成角色动画';
  $('run-hint').textContent = busy ? '当前任务结束后可再次生成；离开页面不会取消后台任务。'
    : !ready ? '等待生成服务就绪。可先浏览动作、上传和预览模型。'
    : !state.file ? '上传 GLB，或明确选择「使用示例角色」。'
    : !state.selection ? '从下方动作库选择一个具体动作。'
    : `准备就绪：${state.file.name} + ${motionName(state.selection)}`;
  $('preview-upload').disabled = !state.file || !viewer;
  $('preview-motion').disabled = !state.selection || !viewer;
}
async function refreshHealth() {
  if (healthLoading) return;
  healthLoading = true;
  $('refresh-health').disabled = true;
  try {
    const info = await requestJSON('/info');
    state.service = info;
    const labels = { ready: '生成服务就绪', loading: '模型加载中', starting: '服务启动中', failed: '服务启动失败', error: '服务异常' };
    $('service-label').textContent = labels[info.status] || `服务状态：${info.status || '未知'}`;
    $('service-health').dataset.state = info.status === 'ready' ? 'ready' : ['failed', 'error'].includes(info.status) ? 'error' : 'loading';
    $('service-health').title = info.error ? errorText(info.error) : `当前运行任务：${info.running_jobs ?? '未知'}`;
    if (Number.isFinite(info.animation_library?.count)) $('library-count').textContent = info.animation_library.count.toLocaleString('zh-CN');
  } catch (error) {
    state.service = null;
    $('service-label').textContent = '服务连接失败';
    $('service-health').dataset.state = 'error';
    $('service-health').title = error.message;
  } finally {
    healthLoading = false;
    $('refresh-health').disabled = false;
    updateRunState();
  }
}
function motionName(record) {
  return record.prompt || (record.filename ? record.filename.replace(/\.fbx$/i, '') : record.motion_id);
}
function selectMotion(record) {
  state.selection = record;
  $('selected-name').textContent = motionName(record);
  $('selected-detail').textContent = `${record.category_label || '未分类'} · ${record.frames ?? '未知'} 帧 · ID ${record.motion_id}`;
  $('selected-motion').title = record.description || record.prompt || '';
  for (const card of $('animation-grid').querySelectorAll('.animation-card')) {
    const selected = card.dataset.motionId === record.motion_id;
    card.classList.toggle('is-selected', selected);
    card.querySelector('.card-select').setAttribute('aria-pressed', String(selected));
    card.querySelector('.card-check').textContent = selected ? '已选择' : '选择 ↗';
  }
  updateRunState();
}
async function loadCategories() {
  const result = await requestJSON('/animations/categories');
  if (!Array.isArray(result.items)) throw new Error('分类响应缺少有效的 items。');
  const all = makeElement('button', 'category-button', '全部');
  all.type = 'button';
  all.dataset.category = '';
  all.setAttribute('aria-pressed', String(!state.category));
  const buttons = [all];
  for (const category of result.items) {
    const button = makeElement('button', 'category-button', category.label);
    button.type = 'button';
    button.dataset.category = category.id;
    button.setAttribute('aria-pressed', String(state.category === category.id));
    button.append(makeElement('small', '', category.count));
    buttons.push(button);
  }
  $('categories').replaceChildren(...buttons);
  if (Number.isFinite(result.total)) $('library-count').textContent = result.total.toLocaleString('zh-CN');
}
function renderCards(items) {
  const cards = items.map((record, index) => {
    const selected = state.selection?.motion_id === record.motion_id;
    const card = makeElement('article', `animation-card${selected ? ' is-selected' : ''}`);
    card.dataset.motionId = record.motion_id;
    const button = makeElement('button', 'card-select');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(selected));
    button.setAttribute('aria-label', `选择 ${motionName(record)}，动作 ID ${record.motion_id}`);
    const top = makeElement('div', 'card-top');
    top.append(makeElement('span', 'card-category', record.category_label || '未分类'), makeElement('span', 'card-ordinal', String(state.offset + index + 1).padStart(2, '0')));
    const title = makeElement('h3', 'card-title', motionName(record));
    const description = makeElement('p', 'card-description', record.description || record.prompt || '此素材没有描述。');
    description.title = description.textContent;
    const meta = makeElement('div', 'card-meta');
    meta.append(makeElement('span', '', `${record.frames ?? '未知'} 帧`), makeElement('span', 'card-check', selected ? '已选择' : '选择 ↗'));
    button.append(top, title, description, meta);
    button.addEventListener('click', () => selectMotion(record));
    const id = makeElement('div', 'card-id', `ID ${record.motion_id}`);
    id.title = record.motion_id;
    const preview = makeElement('button', 'card-preview');
    preview.type = 'button';
    preview.setAttribute('aria-label', `预览 ${motionName(record)} 的源动作骨架，ID ${record.motion_id}`);
    preview.append(makeElement('span', '', '源动作骨架预览'), makeElement('span', '', '↗'));
    preview.disabled = !viewer;
    preview.addEventListener('click', () => previewMotion(record));
    card.append(button, id, preview);
    return card;
  });
  $('animation-grid').replaceChildren(...(cards.length ? cards : [makeElement('div', 'grid-empty', '没有匹配的动作。试试其他关键词或分类。')]));
}
async function loadAnimations() {
  const version = ++state.catalogVersion;
  state.catalogController?.abort();
  state.catalogController = new AbortController();
  state.catalogLoading = true;
  $('animation-grid').setAttribute('aria-busy', 'true');
  $('library-status').textContent = '正在读取动作素材…';
  $('retry-library').hidden = true;
  $('previous-page').disabled = true;
  $('next-page').disabled = true;
  const params = new URLSearchParams({ q: state.query, limit: PAGE_SIZE, offset: state.offset });
  if (state.category) params.set('category', state.category);
  try {
    const result = await requestJSON(`/animations?${params}`, { signal: state.catalogController.signal });
    if (version !== state.catalogVersion) return;
    if (!Array.isArray(result.items) || !Number.isFinite(result.total)) throw new Error('动作库返回了无效数据。');
    state.total = result.total;
    renderCards(result.items);
    $('library-status').textContent = state.total ? `找到 ${state.total.toLocaleString('zh-CN')} 个动作 · 点击卡片选择` : '没有匹配结果';
    $('page-summary').textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + result.items.length, state.total)} / ${state.total.toLocaleString('zh-CN')}` : '0 个结果';
    $('previous-page').disabled = state.offset === 0;
    $('next-page').disabled = state.offset + PAGE_SIZE >= state.total;
  } catch (error) {
    if (version !== state.catalogVersion || error.name === 'AbortError') return;
    $('animation-grid').replaceChildren(makeElement('div', 'grid-empty', '暂时无法读取动作库。请重新加载。'));
    $('library-status').textContent = error.message;
    $('page-summary').textContent = '加载失败';
    $('retry-library').hidden = false;
  } finally {
    if (version === state.catalogVersion) {
      state.catalogLoading = false;
      $('animation-grid').setAttribute('aria-busy', 'false');
    }
  }
}
async function refreshLibrary() {
  try { await loadCategories(); }
  catch (error) { $('library-status').textContent = `分类读取失败：${error.message}`; $('retry-library').hidden = false; return; }
  await loadAnimations();
}
function setViewerMode(mode) {
  viewer?.setMode(mode);
  for (const button of document.querySelectorAll('[data-mode]')) button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
}
function updateViewer(data) {
  viewerState = data;
  const duration = Number.isFinite(data.duration) ? Math.max(0, data.duration) : 0;
  $('play-toggle').disabled = !duration;
  $('play-toggle').textContent = data.playing ? '暂停' : '播放';
  $('play-toggle').setAttribute('aria-label', data.playing ? '暂停动画' : '播放动画');
  $('timeline').disabled = !duration;
  $('timeline').max = String(duration);
  if (!scrubbing) $('timeline').value = String(data.time || 0);
  $('time-current').textContent = formatTime(data.time);
  $('time-total').textContent = formatTime(duration);
  const stats = [];
  if (data.bonesCount) stats.push(`${data.bonesCount} 骨骼`);
  if (data.verticesCount) stats.push(`${Number(data.verticesCount).toLocaleString('zh-CN')} 顶点`);
  $('viewer-stats').textContent = stats.join(' / ') || (data.hasModel ? '真实素材预览' : '真实模型 / 实时预览');
  for (const button of document.querySelectorAll('[data-mode]')) {
    button.disabled = button.dataset.mode === 'model' ? !data.hasModel : !data.bonesCount;
    if (data.mode) button.setAttribute('aria-pressed', String(button.dataset.mode === data.mode));
  }
}
async function loadView(url, { label, note, motion = false, mode = 'model' }) {
  if (!viewer) return;
  const version = ++state.viewerVersion;
  $('viewport-empty').hidden = false;
  $('viewport-empty-text').textContent = '正在加载真实 3D 素材…';
  $('viewer-message').textContent = '';
  $('preview-label').textContent = label;
  $('preview-note').textContent = note;
  try {
    if (motion) await viewer.loadAnimation(url, { label });
    else await viewer.load(url, { label });
    if (version !== state.viewerVersion) return;
    setViewerMode(mode);
    viewer.setSpeed(Number($('speed').value));
    $('viewport-empty').hidden = true;
    $('viewer-message').textContent = '';
  } catch (error) {
    if (version !== state.viewerVersion) return;
    $('viewport-empty-text').textContent = '素材预览未能加载';
    $('viewer-message').textContent = `预览失败：${error.message || error}。可重试相应预览按钮。`;
  }
}
function previewMotion(record) {
  $('viewport').scrollIntoView({
    block: 'center',
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
  });
  return loadView(`/animations/${encodeURIComponent(record.motion_id)}/file`, {
    label: `动作骨架预览 · ${motionName(record)}`,
    note: '这是源 FBX 的动作骨架，不是已绑定角色的生成结果。', motion: true, mode: 'skeleton',
  });
}
function previewUploaded() {
  if (!state.fileURL) return;
  return loadView(state.fileURL, { label: `输入模型 · ${state.file.name}`, note: '这是上传的原始角色；尚未进行本次动作绑定。' });
}
function previewDemo() {
  return loadView('/ui/demo', { label: '示例角色 · 未生成', note: '示例仅用于预览；点击「使用示例角色」才会作为输入。' });
}
function setInputFile(file) {
  if (!file || !/\.glb$/i.test(file.name)) {
    $('file-message').textContent = '请选择一个 .glb 模型文件，其他格式不支持直接上传。';
    return;
  }
  const oldURL = state.fileURL;
  state.file = file;
  state.fileURL = URL.createObjectURL(file);
  if (oldURL) URL.revokeObjectURL(oldURL);
  $('file-name').textContent = file.name;
  $('file-detail').textContent = `${formatBytes(file.size)} · 已选为生成输入`;
  $('file-message').textContent = '';
  $('model-file').value = '';
  updateRunState();
  previewUploaded();
}
async function useDemo() {
  if (demoLoading) return;
  demoLoading = true;
  $('use-demo').disabled = true;
  $('file-message').textContent = '正在读取真实示例文件…';
  try {
    const response = await fetch('/ui/demo');
    if (!response.ok) throw new Error(response.status === 404 ? '当前部署没有示例角色，请上传自己的 GLB。' : `示例读取失败（HTTP ${response.status}）。`);
    const blob = await response.blob();
    setInputFile(new File([blob], 'ninja-example.glb', { type: 'model/gltf-binary' }));
  } catch (error) { $('file-message').textContent = error.message; }
  finally { demoLoading = false; $('use-demo').disabled = false; }
}
function persistJob(id) {
  try { if (id) localStorage.setItem(LAST_JOB_KEY, id); else localStorage.removeItem(LAST_JOB_KEY); }
  catch { $('job-id').textContent += ' · 浏览器存储不可用，请保留任务 ID。'; }
}
function validJobId(id) { return typeof id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(id); }
function stageJob(stage, message) {
  const labels = { upload: '上传中', queued: '排队中', running: '生成中', succeeded: '已完成', failed: '任务失败', reconnect: '连接中断' };
  $('job-panel').hidden = false;
  $('job-state').textContent = labels[stage] || stage;
  $('job-message').textContent = message;
  const ordered = ['upload', 'queued', 'running', 'succeeded'];
  const current = ordered.indexOf(stage);
  for (const item of document.querySelectorAll('[data-stage]')) {
    const index = ordered.indexOf(item.dataset.stage);
    item.classList.toggle('active', index === current);
    item.classList.toggle('complete', current >= 0 && index < current);
  }
}
function clearArtifacts() {
  state.artifacts = null;
  $('artifact-actions').hidden = true;
  $('artifact-details').hidden = true;
  $('artifact-list').replaceChildren();
  $('download-result').removeAttribute('href');
  for (const id of ['view-result', 'view-joints', 'view-coarse']) $(id).disabled = true;
}
function uploadRequest(form) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/generate');
    xhr.responseType = 'json';
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', (event) => {
      $('upload-progress').hidden = false;
      if (event.lengthComputable) {
        const progress = Math.min(100, Math.round(event.loaded / event.total * 100));
        $('upload-progress').value = progress;
        stageJob('upload', `文件请求上传 ${progress}%（${formatBytes(event.loaded)} / ${formatBytes(event.total)}）。这不是模型生成进度。`);
      } else {
        $('upload-progress').removeAttribute('value');
        stageJob('upload', `已上传 ${formatBytes(event.loaded)}，等待服务确认。这不是模型生成进度。`);
      }
    });
    xhr.upload.addEventListener('load', () => {
      $('upload-progress').hidden = true;
      stageJob('upload', '上传完成，等待服务返回任务 ID…');
    });
    xhr.addEventListener('load', () => {
      const data = xhr.response;
      if (xhr.status < 200 || xhr.status >= 300) reject(new Error(errorText(data?.detail || data?.error || `提交失败（HTTP ${xhr.status}）`)));
      else if (!data || !validJobId(data.job_id)) reject(new Error('服务响应缺少有效的任务 ID，无法跟踪任务。'));
      else resolve(data);
    });
    xhr.addEventListener('error', () => reject(new Error('上传连接中断，未收到任务 ID。服务可能已接收，请避免立即重复提交。')));
    xhr.addEventListener('abort', () => reject(new Error('上传请求已中断。')));
    xhr.send(form);
  });
}
async function generate() {
  if ($('generate').disabled) return;
  state.submitting = true;
  ++state.pollVersion;
  clearTimeout(state.pollTimer);
  clearArtifacts();
  state.job = null;
  $('job-id').textContent = '';
  $('retry-job').hidden = true;
  $('dismiss-job').hidden = true;
  $('upload-progress').value = 0;
  $('upload-progress').hidden = false;
  stageJob('upload', '正在上传选中的 GLB 文件…');
  updateRunState();
  const form = new FormData();
  form.append('input_file', state.file, state.file.name);
  form.append('options', JSON.stringify({ animation_id: state.selection.motion_id, input_retarget: true, input_inplace: $('inplace').checked, input_reset_to_rest: true, input_no_fingers: $('no-fingers').checked }));
  try {
    const job = await uploadRequest(form);
    state.job = job;
    state.jobBusy = true;
    $('job-id').textContent = `任务 ID · ${job.job_id}`;
    persistJob(job.job_id);
    stageJob('queued', '任务已被服务接收，正在查询后台状态。推理过程中不显示虚构百分比。');
    await pollJob();
  } catch (error) {
    state.jobBusy = false;
    stageJob('failed', error.message);
    $('dismiss-job').hidden = false;
  } finally {
    state.submitting = false;
    $('upload-progress').hidden = true;
    updateRunState();
    refreshHealth();
  }
}
async function loadArtifacts(job, version, autoPreview = true) {
  const artifacts = await requestJSON(`/jobs/${encodeURIComponent(job.job_id)}/artifacts`);
  if (version !== state.pollVersion) return;
  if (artifacts.job_id !== job.job_id || !Array.isArray(artifacts.items)) throw new Error('输出文件清单与当前任务不匹配。');
  const byName = new Map();
  const links = [];
  for (const item of artifacts.items) {
    if (!item.name || typeof item.url !== 'string') continue;
    const url = localURL(item.url);
    byName.set(item.name, { ...item, url });
    const li = makeElement('li');
    const link = makeElement('a', '', `${item.name} · ${formatBytes(item.size)} ↓`);
    link.href = url;
    link.download = item.name;
    li.append(link);
    links.push(li);
  }
  state.artifacts = byName;
  $('artifact-list').replaceChildren(...links);
  $('artifact-details').hidden = !links.length;
  $('view-result').disabled = !viewer || !byName.has('animatable_preview.glb');
  $('view-joints').disabled = !viewer || !byName.has('joints.glb');
  $('view-coarse').disabled = !viewer || !byName.has('joints_coarse.glb');
  const animationName = artifacts.animation?.filename || artifacts.animation?.prompt;
  stageJob('succeeded', `生成已完成，${links.length} 个真实输出文件可下载。${animationName ? `任务动作：${animationName}。` : ''}${!byName.has('animatable_preview.glb') ? '此任务未包含可播放 GLB；可下载实际输出文件。' : '下方时间轴可播放生成角色；切换骨架或关节查看绑定。'}`);
  $('retry-job').hidden = true;
  if (autoPreview && byName.has('animatable_preview.glb')) viewArtifact('animatable_preview.glb');
}
async function pollJob() {
  if (!state.job?.job_id) return;
  clearTimeout(state.pollTimer);
  const version = ++state.pollVersion;
  const id = state.job.job_id;
  $('retry-job').hidden = true;
  try {
    const job = await requestJSON(`/jobs/${encodeURIComponent(id)}`);
    if (version !== state.pollVersion) return;
    if (job.job_id !== id || !['queued', 'running', 'succeeded', 'failed'].includes(job.status)) throw new Error('服务返回了不匹配或未知状态的任务。');
    state.job = job;
    state.jobBusy = ['queued', 'running'].includes(job.status);
    $('job-id').textContent = `任务 ID · ${id}`;
    $('dismiss-job').hidden = state.jobBusy;
    if (job.status === 'queued') stageJob('queued', '模型已上传，正在排队等待计算资源。每 2 秒查询真实状态。');
    if (job.status === 'running') stageJob('running', '后台正在预测骨架、绑定并生成动画。可继续浏览动作；任务在服务端运行。');
    if (job.status === 'failed') stageJob('failed', `任务失败：${errorText(job.error || '服务未提供错误详情')}。可保留文件和动作选择后重新生成。`);
    if (job.status === 'succeeded') {
      $('artifact-actions').hidden = false;
      $('download-result').href = localURL(job.result_url || `/jobs/${encodeURIComponent(id)}/result`);
      stageJob('succeeded', '生成完成，正在读取真实输出文件…');
      await loadArtifacts(job, version);
    }
    if (state.jobBusy && version === state.pollVersion) state.pollTimer = setTimeout(pollJob, 2000);
  } catch (error) {
    if (version !== state.pollVersion) return;
    $('retry-job').hidden = false;
    if (error.status === 404 || error.status === 410) {
      state.jobBusy = false;
      clearArtifacts();
      $('dismiss-job').hidden = false;
      stageJob('failed', `任务或结果已不可用：${error.message}。可清除记录后重新生成。`);
    } else {
      stageJob('reconnect', `查询中断：${error.message}。这不表示后台任务失败，点击「重新连接任务」获取真实状态。`);
    }
  } finally { updateRunState(); }
}
function viewArtifact(name) {
  const artifact = state.artifacts?.get(name);
  if (!artifact) return;
  const predicted = name !== 'animatable_preview.glb';
  return loadView(artifact.url, {
    label: predicted ? name === 'joints_coarse.glb' ? '生成结果 · 粗预测关节' : '生成结果 · 预测关节' : '生成结果 · 角色动画',
    note: predicted ? `当前显示任务 ${state.job.job_id} 的真实 ${name}，并非源动作骨架。` : `任务 ${state.job.job_id} 的绑定角色动画；播放时长来自实际动画片段。`,
  });
}
function dismissJob() {
  if (state.jobBusy || state.submitting) return;
  ++state.pollVersion;
  clearTimeout(state.pollTimer);
  state.job = null;
  clearArtifacts();
  persistJob(null);
  $('job-panel').hidden = true;
  updateRunState();
}
function reconnectLastJob() {
  let id;
  try { id = localStorage.getItem(LAST_JOB_KEY); } catch { return; }
  if (!validJobId(id)) return;
  state.job = { job_id: id };
  state.jobBusy = true;
  stageJob('queued', '发现上次真实任务，正在向服务确认状态。不会自动提交新的生成请求。');
  $('job-id').textContent = `任务 ID · ${id}`;
  updateRunState();
  pollJob();
}

try {
  viewer = new MotionViewer($('viewport'), {
    onChange: updateViewer,
    onError: (error) => { $('viewer-message').textContent = `3D 预览错误：${error.message || error}`; },
  });
} catch (error) {
  $('viewport-empty-text').textContent = '此浏览器无法初始化 3D 预览';
  $('viewer-message').textContent = `${error.message || error}。请启用 WebGL；仍可选择动作、提交任务及下载结果。`;
  for (const button of document.querySelectorAll('[data-mode]')) button.disabled = true;
  for (const id of ['speed', 'reset-view', 'preview-demo']) $(id).disabled = true;
}
$('model-file').addEventListener('change', (event) => setInputFile(event.target.files?.[0]));
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, (event) => { event.preventDefault(); $('dropzone').classList.add('drag-over'); });
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, (event) => { event.preventDefault(); $('dropzone').classList.remove('drag-over'); });
$('dropzone').addEventListener('drop', (event) => {
  const files = event.dataTransfer?.files;
  if (!files || files.length !== 1) { $('file-message').textContent = '请一次拖入一个 GLB 模型文件。'; return; }
  setInputFile(files[0]);
});
$('use-demo').addEventListener('click', useDemo);
$('preview-demo').addEventListener('click', previewDemo);
$('preview-upload').addEventListener('click', previewUploaded);
$('preview-motion').addEventListener('click', () => state.selection && previewMotion(state.selection));
$('refresh-health').addEventListener('click', refreshHealth);
$('generate').addEventListener('click', generate);
$('retry-job').addEventListener('click', pollJob);
$('dismiss-job').addEventListener('click', dismissJob);
$('view-result').addEventListener('click', () => viewArtifact('animatable_preview.glb'));
$('view-joints').addEventListener('click', () => viewArtifact('joints.glb'));
$('view-coarse').addEventListener('click', () => viewArtifact('joints_coarse.glb'));
$('categories').addEventListener('click', (event) => {
  const button = event.target.closest('[data-category]');
  if (!button) return;
  state.category = button.dataset.category;
  state.offset = 0;
  for (const item of $('categories').querySelectorAll('button')) item.setAttribute('aria-pressed', String(item === button));
  loadAnimations();
});
$('animation-search').addEventListener('input', (event) => {
  clearTimeout(searchTimer);
  state.query = event.target.value.trim();
  state.offset = 0;
  ++state.catalogVersion;
  state.catalogController?.abort();
  $('animation-grid').setAttribute('aria-busy', 'true');
  $('library-status').textContent = '正在更新搜索…';
  $('previous-page').disabled = true;
  $('next-page').disabled = true;
  searchTimer = setTimeout(loadAnimations, 300);
});
$('retry-library').addEventListener('click', refreshLibrary);
$('previous-page').addEventListener('click', () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadAnimations(); });
$('next-page').addEventListener('click', () => { state.offset += PAGE_SIZE; loadAnimations(); });
for (const button of document.querySelectorAll('[data-mode]')) button.addEventListener('click', () => setViewerMode(button.dataset.mode));
$('play-toggle').addEventListener('click', () => viewer?.setPlaying(!viewerState.playing));
$('speed').addEventListener('change', (event) => viewer?.setSpeed(Number(event.target.value)));
$('reset-view').addEventListener('click', () => viewer?.resetView());
$('timeline').addEventListener('pointerdown', () => { scrubbing = true; });
$('timeline').addEventListener('input', (event) => viewer?.seek(Number(event.target.value)));
$('timeline').addEventListener('change', () => { scrubbing = false; });
window.addEventListener('pointerup', () => { scrubbing = false; });
window.addEventListener('pointercancel', () => { scrubbing = false; });
window.addEventListener('pagehide', (event) => {
  if (event.persisted) return;
  clearTimeout(state.pollTimer);
  clearInterval(healthTimer);
  state.catalogController?.abort();
  if (state.fileURL) URL.revokeObjectURL(state.fileURL);
  viewer?.dispose();
});
updateRunState();
refreshHealth();
refreshLibrary();
reconnectLastJob();
if (viewer) previewDemo();
const healthTimer = setInterval(refreshHealth, 15000);
