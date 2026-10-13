import { MotionViewer } from './viewer.js';
import { saveModel, listModels, removeModel } from './model-cache.js';

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
let cachedModels = [];
let modelListVersion = 0;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const previewObserver = new IntersectionObserver((entries) => {
  for (const { target, isIntersecting } of entries) {
    if (isIntersecting && !target.src && target.dataset.src) {
      target.src = target.dataset.src;
      target.load();
    }
    if (!isIntersecting) target.pause();
  }
}, { root: $('library') });


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
  $('generate').firstElementChild.textContent = state.submitting ? '正在上传模型' : state.jobBusy ? '任务正在进行' : '重新生成所选动作';
  $('run-hint').textContent = busy ? '当前任务结束后再点动作；离开页面不会取消后台任务。'
    : !ready ? '等待生成服务就绪。可先浏览动作、上传和预览模型。'
    : !state.file ? '先上传 GLB 或使用示例角色，再点击动画卡片直接生成。'
    : !state.selection ? '模型已选好，点击左侧动画卡片直接生成。'
    : `点击动画卡片直接生成；修改参数后可重新生成。当前：${state.file.name} + ${motionName(state.selection)}`;
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
  return record.prompt_zh || record.prompt || (record.filename ? record.filename.replace(/\.fbx$/i, '') : record.motion_id);
}
function selectMotion(record) {
  state.selection = record;
  $('selected-name').textContent = motionName(record);
  $('selected-detail').textContent = `${record.category_label || '未分类'} · ${record.frames ?? '未知'} 帧 · ID ${record.motion_id}`;
  $('selected-motion').title = record.description_zh || record.description || record.prompt || '';
  for (const card of $('animation-grid').querySelectorAll('.animation-card')) {
    const selected = card.dataset.motionId === record.motion_id;
    card.classList.toggle('is-selected', selected);
    card.querySelector('.card-select').setAttribute('aria-pressed', String(selected));
    card.querySelector('.card-check').textContent = selected ? '当前动作 · 点击生成 ↗' : '点击生成 ↗';
  }
  updateRunState();
}
function activateMotion(record) {
  if (state.submitting || state.jobBusy) {
    $('run-hint').textContent = '当前任务仍在进行，结束后再点击动作；未追加任务。';
    return;
  }
  selectMotion(record);
  if (state.file && state.service?.status === 'ready') generate();
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
function motionSymbol(category) {
  const paths = {
    dance: 'M12 9l-4 5-4-3m8-2 5-3 3 3M12 9l2 8-5 4m5-4 4 4',
    combat: 'M12 9l-5 4-3-4m8 0 5 2 3-4M12 9l-2 8-5 3m5-3 7 3',
    locomotion: 'M12 9l-4 4-4-2m8-2 4 4 4-2M12 9l-2 7-4 5m4-5 7 3',
    acrobatics: 'M12 9l-6-2-3 3m9-1 6-2 3 3M12 9l-3 7-5 1m8-8 4 7 4 1',
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const head = document.createElementNS(svg.namespaceURI, 'circle');
  for (const [key, value] of Object.entries({ cx: '12', cy: '5', r: '2' })) head.setAttribute(key, value);
  const body = document.createElementNS(svg.namespaceURI, 'path');
  body.setAttribute('d', paths[category] || 'M12 9v6m0-4-7 2m7-2 7 2m-7 2-5 6m5-6 5 6');
  svg.append(head, body);
  return svg;
}

function renderCards(items) {
  previewObserver.disconnect();
  for (const video of $('animation-grid').querySelectorAll('video')) {
    video.pause();
    video.removeAttribute('src');
    video.load();
  }
  const cards = items.map((record) => {
    const selected = state.selection?.motion_id === record.motion_id;
    const card = makeElement('article', `animation-card${selected ? ' is-selected' : ''}`);
    card.dataset.motionId = record.motion_id;
    card.dataset.category = record.category_id || 'other';
    const button = makeElement('button', 'card-select');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(selected));
    button.setAttribute('aria-label', `点击生成 ${motionName(record)}，动作 ID ${record.motion_id}`);
    button.title = `${record.description_zh || record.description || record.prompt || ''}\n${record.filename || ''}\nID ${record.motion_id}`;
    const top = makeElement('div', 'card-top');
    top.append(makeElement('span', 'card-category', record.category_label || '未分类'), makeElement('span', 'card-kind', 'FBX MOTION'));
    const title = makeElement('h3', 'card-title', motionName(record));
    const description = makeElement('p', 'card-description', record.description_zh || record.description || record.prompt || '此素材没有描述。');
    description.title = description.textContent;
    const meta = makeElement('div', 'card-meta');
    const duration = makeElement('span', 'card-duration');
    duration.hidden = true;
    meta.append(duration);
    meta.append(makeElement('span', '', `${record.frames ?? '未知'} 帧`), makeElement('span', 'card-check', selected ? '当前动作 · 点击生成 ↗' : '点击生成 ↗'));
    const lead = makeElement('div', 'card-lead');
    const body = makeElement('div', 'card-body');
    body.append(title, description);
    lead.append(body);
    const media = makeElement('div', 'card-media');
    const mediaStatus = makeElement('span', 'card-media-status', record.preview_url ? '正在读取预览…' : '未安装短视频 · 可查看骨架');
    let video = null;
    const quick = makeElement('button', 'card-preview', '播放短视频 ▷');
    quick.type = 'button';
    quick.disabled = !record.preview_url;
    quick.setAttribute('aria-label', `仅预览 ${motionName(record)}，不生成任务`);
    quick.setAttribute('aria-pressed', 'false');
    if (record.preview_url) {
      video = makeElement('video', 'card-video');
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.preload = 'metadata';
      video.dataset.src = localURL(record.preview_url);
      video.setAttribute('aria-hidden', 'true');
      const play = () => {
        if (!video.src) { video.src = video.dataset.src; video.load(); }
        video.play().catch(() => { mediaStatus.hidden = false; mediaStatus.textContent = '点击「播放短视频」预览'; });
      };
      video.addEventListener('loadedmetadata', () => {
        if (Number.isFinite(video.duration) && video.duration > 0) {
          duration.textContent = `${video.duration.toFixed(1)}s`;
          duration.hidden = false;
        }
      });
      video.addEventListener('loadeddata', () => { mediaStatus.hidden = true; });
      video.addEventListener('error', () => {
        mediaStatus.hidden = false;
        mediaStatus.textContent = '短视频不可用 · 可查看骨架';
        quick.disabled = true;
      });
      video.addEventListener('play', () => { quick.textContent = '暂停短视频 Ⅱ'; quick.setAttribute('aria-pressed', 'true'); });
      video.addEventListener('pause', () => { quick.textContent = '播放短视频 ▷'; quick.setAttribute('aria-pressed', 'false'); });
      quick.addEventListener('click', () => { if (video.paused) play(); else video.pause(); });
      button.addEventListener('pointerenter', () => { if (!reducedMotion.matches) play(); });
      button.addEventListener('pointerleave', () => video.pause());
      button.addEventListener('focus', () => { if (!reducedMotion.matches) play(); });
      button.addEventListener('blur', () => video.pause());
      media.append(video);
    } else {
      media.append(motionSymbol(record.category_id));
    }
    media.append(mediaStatus);
    const sourceName = makeElement('p', 'card-source-name', record.prompt || record.filename);
    sourceName.title = `${record.filename}\n${record.description || ''}`;
    body.append(sourceName);
    button.append(media, top, lead, meta);
    button.addEventListener('click', () => activateMotion(record));
    const id = makeElement('div', 'card-id', `ID · ${record.motion_id.slice(0, 8)}`);
    id.title = record.motion_id;
    const preview = makeElement('button', 'card-preview', '详细骨架 ↗');
    preview.type = 'button';
    preview.setAttribute('aria-label', `预览 ${motionName(record)} 的源动作骨架，ID ${record.motion_id}`);
    preview.disabled = !viewer;
    preview.addEventListener('click', () => previewMotion(record));
    const actions = makeElement('div', 'card-preview-actions');
    actions.append(quick, preview);
    card.append(button, id, actions);
    return card;
  });
  $('animation-grid').replaceChildren(...(cards.length ? cards : [makeElement('div', 'grid-empty', '没有匹配的动作。试试其他关键词或分类。')]));
  for (const video of $('animation-grid').querySelectorAll('video')) previewObserver.observe(video);
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
    $('library-status').textContent = state.total ? `找到 ${state.total.toLocaleString('zh-CN')} 个动作 · 悬停预览，点击生成` : '没有匹配结果';
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
async function setInputFile(file, { save = true, cacheId = null } = {}) {
  if (!file || !/\.glb$/i.test(file.name)) {
    $('file-message').textContent = '请选择一个 .glb 模型文件，其他格式不支持直接上传。';
    return;
  }
  const oldURL = state.fileURL;
  state.file = file;
  state.cacheId = cacheId;
  state.fileURL = URL.createObjectURL(file);
  if (oldURL) URL.revokeObjectURL(oldURL);
  $('file-name').textContent = file.name;
  $('file-detail').textContent = `${formatBytes(file.size)} · 已选为生成输入`;
  $('file-message').textContent = '';
  $('model-file').value = '';
  updateRunState();
  previewUploaded();
  renderCachedModels();
  if (save) {
    $('cache-message').textContent = '正在将 GLB 保存到浏览器…';
    try {
      const record = await saveModel(file);
      if (state.file === file) state.cacheId = record.id;
      await refreshCachedModels();
    } catch (error) {
      if (state.file === file) $('cache-message').textContent = `本机保存失败：${error.message}。当前模型仍可预览和生成。`;
    }
  }
}
function renderCachedModels() {
  $('cached-count').textContent = String(cachedModels.length);
  const cards = cachedModels.map(record => {
    const card = makeElement('div', 'cached-model');
    card.classList.toggle('is-selected', record.id === state.cacheId);
    const load = makeElement('button', 'cached-model-load');
    load.type = 'button';
    load.setAttribute('aria-pressed', String(record.id === state.cacheId));
    load.setAttribute('aria-label', `载入缓存模型 ${record.name}`);
    load.append(makeElement('span', 'model-file-icon', 'GLB'));
    const detail = makeElement('span', 'cached-model-detail');
    detail.append(makeElement('strong', '', record.name), makeElement('small', '', `${formatBytes(record.size)} · ${new Date(record.savedAt).toLocaleDateString('zh-CN')}`));
    load.append(detail);
    load.addEventListener('click', () => setInputFile(new File([record.blob], record.name, { type: 'model/gltf-binary', lastModified: record.lastModified }), { save: false, cacheId: record.id }));
    const remove = makeElement('button', 'cached-model-remove', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', `删除本机缓存 ${record.name}`);
    remove.title = '仅删除本机缓存';
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        await removeModel(record.id);
        if (state.cacheId === record.id) state.cacheId = null;
        await refreshCachedModels();
      } catch (error) {
        $('cache-message').textContent = `删除失败：${error.message}`;
        remove.disabled = false;
      }
    });
    card.append(load, remove);
    return card;
  });
  $('cached-models').replaceChildren(...(cards.length ? cards : [makeElement('p', 'cache-empty', '上传 GLB 后会自动保存在这里。')]));
}

async function refreshCachedModels() {
  const version = ++modelListVersion;
  try {
    const records = await listModels();
    if (version !== modelListVersion) return;
    cachedModels = records;
    renderCachedModels();
    $('cache-message').textContent = records.length ? '已保存实际 GLB 文件，点击模型即可切换。' : '';
  } catch (error) {
    if (version === modelListVersion) $('cache-message').textContent = `无法读取本机缓存：${error.message}`;
  }
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
function bindingOptions() {
  const restPose = $('rest-pose').value;
  return {
    animation_id: state.selection.motion_id,
    input_inplace: $('inplace').checked,
    input_no_fingers: $('no-fingers').checked,
    input_rest_pose: restPose,
    input_rest_parts: restPose === 'No' ? [] : [...document.querySelectorAll('[name="rest-parts"]:checked')].map((input) => input.value),
    input_normal: true,
    input_bw_fix: $('bw-fix').checked,
    input_bw_vis_bone: $('bw-vis-bone').value,
    input_restore_global: $('restore-global').checked,
    input_reset_to_rest: $('reset-to-rest').checked,
    input_retarget: $('retarget').checked,
  };
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
  form.append('options', JSON.stringify(bindingOptions()));
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
  stageJob('succeeded', `生成已完成，${links.length} 个真实输出文件可下载。${animationName ? `任务动作：${animationName}。` : ''}${!byName.has('animatable_preview.glb') ? '此任务未包含可播放 GLB；可下载实际输出文件。' : '视图下方时间轴可播放生成角色；切换骨架或关节查看绑定。'}`);
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

const visualizationBones = ['Hips', 'Spine', 'Spine1', 'Spine2', 'Neck', 'Head'];
for (const side of ['Left', 'Right']) {
  visualizationBones.push(...['Shoulder', 'Arm', 'ForeArm', 'Hand'].map((part) => side + part));
  for (const finger of ['Thumb', 'Index', 'Middle', 'Ring', 'Pinky']) {
    for (let joint = 1; joint <= 3; joint++) visualizationBones.push(`${side}Hand${finger}${joint}`);
  }
  visualizationBones.push(...['UpLeg', 'Leg', 'Foot', 'ToeBase'].map((part) => side + part));
}
$('bw-vis-bone').replaceChildren(...visualizationBones.map((name) => new Option(name, name, name === 'LeftArm', name === 'LeftArm')));
$('advanced-form').addEventListener('submit', (event) => event.preventDefault());

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
$('library-grid').addEventListener('click', () => setLibraryLayout(false));
$('library-list').addEventListener('click', () => setLibraryLayout(true));
function setLibraryLayout(list) {
  $('animation-grid').classList.toggle('is-list', list);
  $('library-grid').setAttribute('aria-pressed', String(!list));
  $('library-list').setAttribute('aria-pressed', String(list));
}
$('nav-models').addEventListener('click', () => {
  $('model-library').open = true;
  $('model-library').scrollIntoView({ block: 'nearest', behavior: reducedMotion.matches ? 'instant' : 'smooth' });
  $('model-library').querySelector('summary').focus({ preventScroll: true });
});
$('focus-search').addEventListener('click', () => $('animation-search').focus());
window.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    $('animation-search').focus();
  }
});
$('fullscreen-view').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $('viewer-panel').requestFullscreen();
  } catch (error) {
    $('viewer-message').textContent = `无法进入全屏：${error.message || error}`;
  }
});
document.addEventListener('fullscreenchange', () => {
  const fullscreen = document.fullscreenElement === $('viewer-panel');
  $('fullscreen-view').setAttribute('aria-pressed', String(fullscreen));
  $('fullscreen-view').setAttribute('aria-label', fullscreen ? '退出 3D 视图全屏' : '全屏显示 3D 视图');
});
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
refreshCachedModels();
reconnectLastJob();
if (viewer) previewDemo();
const healthTimer = setInterval(refreshHealth, 15000);
