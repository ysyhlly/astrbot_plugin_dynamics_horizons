import {
  msg, tr, setText, feedback, confirmAction, formatScopeLabel, getLanguage,
  getSharedScope, setSharedScope,
} from './series-ui.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

const THEME_KEY = 'chat_dynamics_ui';
const PAGE_SIZE = 20;

const STATUS_COPY = {
  readingStopped: {
    zh: '本次阅读已停止',
    en: 'Reading stopped',
    ja: '今回の読書を中止',
    ru: 'Чтение остановлено',
  },
  characterChanged: {
    zh: '角色状态已变化，本次阅读已停止。等待下一次阅读检查。',
    en: 'The character’s state changed, so this reading stopped. Waiting for the next reading check.',
    ja: 'キャラクターの状態が変わったため、今回の読書を中止しました。次の読書可否の確認を待っています。',
    ru: 'Состояние персонажа изменилось, поэтому чтение остановилось. Ожидается следующая проверка готовности к чтению.',
  },
  failed: {
    zh: '失败',
    en: 'Failed',
    ja: '失敗',
    ru: 'Сбой',
  },
  healthy: {
    zh: '正常',
    en: 'OK',
    ja: '正常',
    ru: 'В норме',
  },
  diagnostics: {
    zh: '诊断详情',
    en: 'Diagnostic details',
    ja: '診断の詳細',
    ru: 'Диагностические сведения',
  },
  sharingDisabled: {
    zh: '主动分享已关闭',
    en: 'Proactive sharing is off',
    ja: '自発的共有はオフ',
    ru: 'Активный обмен выключен',
  },
  sharingAllChats: {
    zh: '主动分享范围为所有已接管的群聊和私聊',
    en: 'Proactive sharing covers all managed groups and private chats',
    ja: '自発的共有は管理対象のグループと個別チャットすべてが対象です',
    ru: 'Активный обмен доступен во всех управляемых группах и личных чатах',
  },
  sharingSelectedChats: {
    zh: '主动分享范围按会话白名单',
    en: 'Proactive sharing covers chats on the allowlist',
    ja: '自発的共有は許可リストのチャットが対象です',
    ru: 'Активный обмен доступен в чатах из списка разрешённых',
  },
};

const statusMsg = key => ({
  seriesMessage: true,
  toString() {
    const copy = STATUS_COPY[key];
    return copy[getLanguage()] || copy.zh;
  },
});

function readingCharacterChanged(health) {
  return ['ValueError:reading_character_changed', 'reading_character_changed'].includes(health?.last_error);
}

function modelStatusLabel(status, readingStopped = false) {
  if (readingStopped) return statusMsg('readingStopped');
  if (String(status).startsWith('error:')) return statusMsg('failed');
  return status === 'ok' ? statusMsg('healthy') : status || msg('就绪');
}

const KIND_LABELS = {
  bilibili: 'B站只读源',
  ebook: '电子书源',
  feed: '标准订阅',
  manual: '手动投喂',
};

const PROPOSAL_STATE_LABELS = {
  pending_review: '待审',
  pending: '待审',
  held: '人工复核',
  approved: '已通过',
  rejected: '已拒绝',
  revoked: '已撤回',
};

const REVIEW_RISK_SPECS = {
  raw_url_detected: { label: '裸链接风险', detail: '内容包含未经清洗的裸 HTTP/HTTPS 链接', tone: 'danger' },
  'injection:ignore_instructions': { label: '提示词注入', detail: '检测到疑似英文忽略规则/指令覆盖片段', tone: 'danger' },
  'injection:system_prompt_override': { label: '系统提示覆写', detail: '检测到疑似系统提示词或边界标记注入', tone: 'danger' },
  'injection:zh_prompt_injection': { label: '中文越狱/改人设注入', detail: '检测到疑似中文改人设或越狱指令片段', tone: 'danger' },
  'injection:xss_or_exec': { label: '脚本执行风险', detail: '检测到疑似脚本或 HTML 事件注入', tone: 'danger' },
  'spam:contact_spam': { label: '联系方式引流', detail: '检测到微信/QQ群等引流联系方式', tone: 'warn' },
  'spam:promo_spam': { label: '广告推广引流', detail: '检测到营销推广或领券引流话术', tone: 'warn' },
};

function describeReviewRisk(rawRisk) {
  const code = String(rawRisk || '').trim();
  if (!code) return null;
  if (REVIEW_RISK_SPECS[code]) return REVIEW_RISK_SPECS[code];
  if (code.startsWith('injection:')) {
    const sub = code.slice('injection:'.length).trim();
    return { label: '提示词注入风险', detail: sub ? `命中注入检测规则（${sub}）` : '命中提示词注入检测规则', tone: 'danger' };
  }
  if (code.startsWith('spam:')) {
    const sub = code.slice('spam:'.length).trim();
    return { label: '广告/引流风险', detail: sub ? `命中引流检测规则（${sub}）` : '命中广告或联系方式引流规则', tone: 'warn' };
  }
  if (code.startsWith('blocked_topic:')) {
    const topic = code.slice('blocked_topic:'.length).trim();
    return { label: `屏蔽话题：${topic || '未命名'}`, detail: '命中当前角色的话题黑名单', tone: 'warn' };
  }
  return { label: code, detail: '独立审查模型标记的风险提示', tone: 'warn' };
}

const BOOK_STATUS_ITEMS = [
  ['reading', '在读'],
  ['dropped', '弃书'],
  ['finished', '读完'],
];

const WATCH_MODE_BADGES = {
  native_video: { label: '原生视频', tone: 'accent' },
  keyframes_subtitle: { label: '关键帧+字幕', tone: 'info' },
  keyframes_only: { label: '关键帧', tone: 'info' },
  videoshot_subtitle: { label: '雪碧图+字幕', tone: '' },
  videoshot_only: { label: '雪碧图', tone: '' },
  subtitle_only: { label: '纯字幕', tone: '' },
  audio_transcript: { label: '音频转写', tone: '' },
  dynamic: { label: '动态', tone: '' },
};

const ORIGIN_TO_WATCH_MODE = {
  bilibili_native_video: 'native_video',
  bilibili_vision_subtitle: 'keyframes_subtitle',
  bilibili_vision_frames: 'keyframes_only',
  bilibili_videoshot_subtitle: 'videoshot_subtitle',
  bilibili_videoshot: 'videoshot_only',
  bilibili_subtitle: 'subtitle_only',
  bilibili_audio_transcript: 'audio_transcript',
  bilibili_dynamic: 'dynamic',
};

const state = {
  tab: 'overview',
  identity: '',
  sharedScope: '',
  sharedPersona: '',
  personas: [],
  overview: null,
  sources: [],
  books: [],
  logs: { items: [], page: 1, total: 0, page_size: PAGE_SIZE, source_kind: '', query: '' },
  reviews: {
    items: [],
    blocked_topics: [],
    corrections: [],
    page: 1,
    total: 0,
    page_size: PAGE_SIZE,
    state: 'pending_review',
    source_kind: '',
    query: '',
    selectedIds: new Set(),
  },
  outreachTab: 'intents',
  decayPreviewDays: null,
  editingSource: null,
  drawerKind: 'bilibili',
  drawerDirty: false,
};

const STAGE_LABELS = {
  triage: '阶段1 挑拣',
  deep_read: '阶段2 精读',
  video_deep_read: '阶段2 视频多模态',
  merged_digest: '合并挑拣+精读',
  merged_digest_review: '合并精读+自审',
  review: '阶段3 独立审查',
  feedback: '讨论回执核验',
  discussion: '交流后记',
};

const DEFAULT_PRESETS = [
  {
    id: 'conservative',
    label: '低耗省流',
    summary: '每日最多精读 2 篇、模型调用上限 20 次，模型初审后人工把关。',
    values: { daily_digest_quota: 2, daily_model_call_quota: 20, interest_half_life_days: 8, review_mode: 'llm_then_human' },
  },
  {
    id: 'balanced',
    label: '均衡推荐',
    summary: '每日最多精读 5 篇、模型调用上限 50 次，独立审查通过自动生效。',
    values: { daily_digest_quota: 5, daily_model_call_quota: 50, interest_half_life_days: 6, review_mode: 'llm' },
  },
  {
    id: 'active',
    label: '深度活跃',
    summary: '每日最多精读 10 篇、模型调用上限 100 次，更高频追踪热点与书架连载。',
    values: { daily_digest_quota: 10, daily_model_call_quota: 100, interest_half_life_days: 5, review_mode: 'llm' },
  },
];

function formatSourceErrorAdvice(error, sourceKind = '') {
  const raw = String(error || '').trim();
  if (!raw) return '';
  const low = raw.toLowerCase();
  if (raw.includes('-412') || low.includes('412') || raw.includes('风控') || low.includes('risk_control')) {
    return '命中 B 站 -412 风控：建议补充只读 Cookie (SESSDATA/buvid3)，适当拉长轮询间隔并等待退避冷却。';
  }
  if (raw.includes('-352') || low.includes('v_voucher')) {
    return '命中 B 站人机核验 (-352)：建议更换只读 Cookie，系统已自动进入退避冷却。';
  }
  if (low.includes('429') || low.includes('rate_limit') || low.includes('too many')) {
    return '目标站点限流 (429)：建议调大轮询间隔（>= 1800 秒）并等待冷却结束。';
  }
  if (low.includes('ssrf') || raw.includes('内网') || raw.includes('私有') || low.includes('localhost')) {
    return '命中 SSRF 防护：仅允许公网 HTTP/HTTPS；若为自建局域网 OPDS 请勾选 allow_private_opds。';
  }
  if (low.includes('timeout') || raw.includes('超时')) {
    return '连接超时：请检查服务器出站网络/代理或目标站点连通性。';
  }
  if (sourceKind === 'bilibili') {
    return 'B 站只读拉取异常：请核对 UID/链接有效性，必要时补充只读 Cookie 后重试。';
  }
  return '请核对地址、凭据与网络连通性后重试。';
}

const epochs = {};
let roleEpoch = 0;
const ROLE_CHANGED = Symbol('role changed');

function nextTicket(channel) {
  const role = roleEpoch;
  const id = (epochs[channel] = (epochs[channel] || 0) + 1);
  return () => role === roleEpoch && epochs[channel] === id;
}

function el(tag, cls = '', text = undefined) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) {
    if (text?.seriesMessage) setText(node, text);
    else node.textContent = String(text ?? '');
  }
  return node;
}

function actionButton(label, handler, cls = 'wb-button', isDisabled = () => false) {
  const btn = el('button', cls, label);
  const role = roleEpoch;
  btn.type = 'button';
  btn.disabled = isDisabled();
  btn.addEventListener('click', async () => {
    if (btn.disabled || !btn.isConnected || role !== roleEpoch) return;
    btn.disabled = true;
    try {
      await handler();
    } catch (err) {
      if (err !== ROLE_CHANGED && role === roleEpoch) notify(err?.message || tr('请求失败'), 'error');
    } finally {
      if (btn.isConnected) btn.disabled = isDisabled();
    }
  });
  return btn;
}

function notify(message, kind = 'info') {
  const node = $('#notice');
  if (node) feedback(node, message, kind);
}

function formatTime(ts) {
  const num = Number(ts);
  if (!Number.isFinite(num) || num <= 0) return tr('暂无记录');
  const millis = num > 1e12 ? num : num * 1000;
  const date = new Date(millis);
  if (Number.isNaN(date.getTime())) return tr('暂无记录');
  return date.toLocaleString(document.documentElement.lang,{
    year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,
  });
}

function normalizeHeatPercent(raw) {
  const num = Number(raw);
  if (!Number.isFinite(num) || num <= 0) return 0;
  if (num <= 2.0001) return Math.min(100, Math.round(num * 100));
  return Math.min(100, Math.round(num));
}

function formatRemainingDecay(expiresAt, status) {
  if (status === 'cooled') return tr('已冷却');
  if (status === 'revoked') return tr('已撤回');
  const exp = Number(expiresAt);
  if (!Number.isFinite(exp) || exp <= 0) return '';
  const diffSec = exp - (Date.now() / 1000);
  if (diffSec <= 0) return tr('渐弱中');
  if (diffSec < 3600) return tr('不足 1 小时');
  if (diffSec < 86400) return `剩 ${Math.max(1, Math.round(diffSec / 3600))} 小时`;
  return `剩 ${Math.max(1, Math.ceil(diffSec / 86400))} 天`;
}

function isMaskedSecret(val) {
  const s = String(val || '').trim();
  return Boolean(s && (s.includes('*') || s.includes('••') || s === '[REDACTED]'));
}

function uint8ToBase64(bytes) {
  const chunkSize = 0x8000;
  const parts = [];
  for (let i = 0; i < bytes.length; i += chunkSize) {
    parts.push(String.fromCharCode(...bytes.subarray(i, i + chunkSize)));
  }
  return btoa(parts.join(''));
}

/* ===== Theme Persistence ===== */
function paintTheme(mode) {
  const night = mode === 'night';
  document.documentElement.dataset.uiTheme = night ? 'night' : 'day';
  const toggle = $('#theme-toggle');
  const label = $('#theme-label');
  if (toggle) {
    toggle.setAttribute('aria-pressed', String(night));
    toggle.setAttribute('aria-label', night ? tr('切换到日间模式') : tr('切换到夜间模式'));
  }
  if (label) setText(label, msg(night ? '夜间模式' : '日间模式'));
}

function initTheme() {
  paintTheme(document.documentElement.dataset.uiTheme === 'night' ? 'night' : 'day');
  $('#theme-toggle')?.addEventListener('click', () => {
    const next = document.documentElement.dataset.uiTheme === 'night' ? 'day' : 'night';
    paintTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    try {
      const url = new URL(location.href);
      url.searchParams.set('ui', next);
      history.replaceState(history.state, '', url);
    } catch {}
  });
  window.addEventListener('storage', ev => {
    if (ev.key === THEME_KEY && (ev.newValue === 'day' || ev.newValue === 'night')) {
      paintTheme(ev.newValue);
    }
  });
}

/* ===== Bridge & API ===== */
let bridgeReadyPromise = null;
function getBridge() {
  return (window.DynamicsPanelBridge || window.AstrBotPluginPage || window.AstrBotPluginView);
}

async function ensureBridge() {
  const bridge = getBridge();
  if (!bridge) throw new Error(tr('请从 AstrBot 控制台打开'));
  if (!bridgeReadyPromise) {
    bridgeReadyPromise = Promise.resolve(bridge.ready?.()).catch(err => {
      bridgeReadyPromise = null;
      throw err;
    });
  }
  await bridgeReadyPromise;
  return bridge;
}

function unwrapResponse(raw) {
  if (!raw || typeof raw !== 'object') throw new Error(tr('空响应'));
  if (raw.status === 'error' || raw.ok === false) {
    throw new Error(raw.message || raw.error || tr('请求失败'));
  }
  return raw.data ?? raw;
}

async function apiGet(route, params = {}) {
  const bridge = await ensureBridge();
  const raw = await bridge.apiGet(route, params);
  return unwrapResponse(raw);
}

async function apiPost(route, body = {}) {
  const role = roleEpoch;
  try {
    const bridge = await ensureBridge();
    if (role !== roleEpoch) throw ROLE_CHANGED;
    const raw = await bridge.apiPost(route, body);
    if (role !== roleEpoch) throw ROLE_CHANGED;
    return unwrapResponse(raw);
  } catch (err) {
    throw role === roleEpoch ? err : ROLE_CHANGED;
  }
}

/* ===== Persona & World Link Header ===== */
function renderPersonaAndWorld(overview) {
  const personas = Array.isArray(overview?.personas) ? overview.personas : [];
  state.personas = personas;
  if (overview?.active_identity && (!state.identity || !personas.some(p => p.identity === state.identity))) {
    state.identity = String(overview.active_identity);
  } else if (!state.identity && personas.length > 0) {
    state.identity = String(personas[0].identity || '');
  }

  const select = $('#persona-select');
  if (select) {
    select.replaceChildren();
    if (personas.length === 0) {
      const opt = el('option', '', state.identity ? `角色实例 · ${state.identity}` : '默认角色实例');
      opt.value = state.identity || '';
      select.append(opt);
    } else {
      for (const p of personas) {
        const label = p.label || p.persona_id || p.identity || '未命名角色';
        const opt = el('option', '', label);
        opt.value = String(p.identity ?? '');
        select.append(opt);
      }
      if (state.identity && !personas.some(p => String(p.identity) === state.identity)) {
        const extra = el('option', '', state.identity);
        extra.value = state.identity;
        select.append(extra);
      }
    }
    select.value = state.identity;
  }

  const badge = $('#world-link-badge');
  const summaryNode = $('#world-link-summary');
  const wl = overview?.world_link || {};
  if (badge) {
    badge.className = 'world-badge';
    if (wl.connected) {
      if (wl.paused) {
        badge.classList.add('is-paused');
        setText(badge,msg('群间世界：生活已暂停（暂缓挑拣）'));
      } else if (wl.asleep) {
        badge.classList.add('is-asleep');
        setText(badge,msg('群间世界：熟睡休息中（暂缓挑拣）'));
      } else {
        badge.classList.add('is-awake');
        setText(badge,msg('群间世界：已联动 · 清醒可读'));
      }
    } else {
      setText(badge,msg('群间世界：独立模式（遵守静默时段）'));
    }
  }
  if (summaryNode) {
    summaryNode.textContent = wl.summary ? String(wl.summary) : '';
  }

  renderBadges(overview?.stats);
}

function renderBadges(stats = {}) {
  const setBadge = (id, count, pendingStyle = false) => {
    const node = $(id);
    if (!node) return;
    const n = Number(count) || 0;
    node.hidden = n <= 0;
    node.textContent = String(n);
    node.classList.toggle('has-pending', Boolean(pendingStyle && n > 0));
  };
  setBadge('#badge-overview', (Number(stats.active_interests) || 0) + (Number(stats.pending_questions) || 0));
  setBadge('#badge-sources', Number(stats.sources_total) || 0);
  setBadge('#badge-logs', Number(stats.logs_total) || 0);
  const pendingReviewCount = (
    stats.pending_reviews != null
      ? (Number(stats.pending_reviews) || 0)
      : (Number(stats.held_reviews) || 0)
  ) + (Number(stats.pending_corrections) || 0);
  setBadge('#badge-reviews', pendingReviewCount, true);
}

/* ===== Tab 1: Overview & Interests ===== */
function renderPresetsAndDecay(overview) {
  const box = $('#presets-decay-panel');
  if (!box) return;
  const presets = Array.isArray(overview?.presets) && overview.presets.length
    ? overview.presets
    : DEFAULT_PRESETS;
  const rc = overview?.runtime_config || {};
  const currentHalfLife = Math.max(2, Math.min(14, Number(rc.interest_half_life_days) || 6));
  if (state.decayPreviewDays == null) state.decayPreviewDays = currentHalfLife;

  const head = el('div', 'panel-card-head');
  const titleWrap = el('div', 'panel-head-title');
  titleWrap.append(
    el('h3', '', '运营预设与兴趣衰减曲线'),
    el('span', 'panel-head-sub', '一键切换配额节奏 · 预览半衰期自然冷却'),
  );
  head.append(titleWrap, el('span', 'pill info', `当前半衰期 ${currentHalfLife} 天`));

  const grid = el('div', 'presets-decay-grid');
  const presetsRow = el('div', 'presets-row');
  for (const preset of presets) {
    const vals = preset.values || {};
    const isCurrent = Number(rc.daily_digest_quota) === Number(vals.daily_digest_quota)
      && Number(rc.daily_model_call_quota) === Number(vals.daily_model_call_quota)
      && String(rc.review_mode || '') === String(vals.review_mode || '');
    const card = el('div', `preset-card${isCurrent ? ' is-current' : ''}`);
    card.append(
      el('strong', '', preset.label || preset.id),
      el('p', '', preset.summary || ''),
      actionButton(isCurrent ? '当前方案' : '一键应用', async () => {
        await apiPost('reviews', { op: 'apply_preset', identity: state.identity, preset_id: preset.id });
        state.decayPreviewDays = Number(vals.interest_half_life_days) || currentHalfLife;
        notify(`已应用「${preset.label}」运营预设。`, 'success');
        await loadOverview({ silent: true });
      }, isCurrent ? 'wb-button' : 'wb-button primary'),
    );
    presetsRow.append(card);
  }

  // Interactive half-life decay curve SVG preview
  const previewBox = el('div', 'decay-preview-box');
  const previewHead = el('div', 'decay-preview-head');
  const daysVal = Math.max(2, Math.min(14, Number(state.decayPreviewDays) || currentHalfLife));
  const labelNode = el('strong', '', `衰减曲线预览（半衰期 ${daysVal} 天）`);
  const coolDay = (daysVal * 2).toFixed(0);
  const subNode = el('span', 'panel-head-sub', `约第 ${coolDay} 天自然冷却 (<25%)`);
  previewHead.append(labelNode, subNode);

  const slider = el('input');
  slider.type = 'range';
  slider.id = 'decay-half-life-slider';
  slider.min = '2';
  slider.max = '14';
  slider.step = '1';
  slider.value = String(daysVal);
  slider.setAttribute('aria-label', '预览兴趣半衰期天数');

  const svgHost = el('div', 'decay-svg-host');
  const drawCurve = halfLife => {
    const w = 320;
    const h = 104;
    const padL = 24;
    const padR = 10;
    const padT = 10;
    const padB = 20;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;
    const maxDays = 14;
    const pts = [];
    for (let d = 0; d <= maxDays; d += 0.5) {
      const heat = Math.pow(0.5, d / halfLife);
      const x = padL + (d / maxDays) * plotW;
      const y = padT + (1 - heat) * plotH;
      pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    }
    const coolY = (padT + (1 - 0.25) * plotH).toFixed(1);
    const halfX = (padL + (Math.min(maxDays, halfLife) / maxDays) * plotW).toFixed(1);
    const halfY = (padT + 0.5 * plotH).toFixed(1);
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('class', 'decay-curve-svg');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `半衰期 ${halfLife} 天兴趣衰减曲线`);
    const mk = (tag, attrs) => {
      const n = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
      return n;
    };
    svg.append(
      mk('line', { x1: padL, y1: padT + plotH, x2: w - padR, y2: padT + plotH, stroke: 'currentColor', 'stroke-opacity': '0.2' }),
      mk('line', { x1: padL, y1: coolY, x2: w - padR, y2: coolY, stroke: '#d97706', 'stroke-dasharray': '4 3', 'stroke-opacity': '0.7' }),
      mk('polyline', { fill: 'none', stroke: 'var(--ds-accent, #2e7d5b)', 'stroke-width': '2.4', points: pts.join(' ') }),
      mk('circle', { cx: halfX, cy: halfY, r: '3.5', fill: 'var(--ds-accent, #2e7d5b)' }),
    );
    const t0 = mk('text', { x: padL, y: h - 4, 'font-size': '10', fill: 'currentColor', opacity: '0.65' });
    t0.textContent = '0天';
    const tMid = mk('text', { x: halfX, y: h - 4, 'font-size': '10', fill: 'currentColor', opacity: '0.75', 'text-anchor': 'middle' });
    tMid.textContent = `${halfLife}天(50%)`;
    const tEnd = mk('text', { x: w - padR, y: h - 4, 'font-size': '10', fill: 'currentColor', opacity: '0.65', 'text-anchor': 'end' });
    tEnd.textContent = '14天';
    const tCool = mk('text', { x: w - padR, y: Number(coolY) - 3, 'font-size': '9.5', fill: '#d97706', 'text-anchor': 'end' });
    tCool.textContent = '冷却线 25%';
    svg.append(t0, tMid, tEnd, tCool);
    svgHost.replaceChildren(svg);
  };
  drawCurve(daysVal);
  slider.addEventListener('input', () => {
    const nextDays = Math.max(2, Math.min(14, Number(slider.value) || 6));
    state.decayPreviewDays = nextDays;
    labelNode.textContent = `衰减曲线预览（半衰期 ${nextDays} 天）`;
    subNode.textContent = `约第 ${(nextDays * 2).toFixed(0)} 天自然冷却 (<25%)`;
    drawCurve(nextDays);
  });

  previewBox.append(previewHead, slider, svgHost);
  grid.append(presetsRow, previewBox);
  box.replaceChildren(head, grid);
}

function renderOutreachDiagnostics(overview) {
  const box = $('#outreach-diagnostics-panel');
  if (!box) return;
  const diag = overview?.outreach_diagnostics || {};
  const intents = Array.isArray(diag.horizon_intents) ? diag.horizon_intents : [];
  const coTopics = Array.isArray(diag.co_discussed_topics) ? diag.co_discussed_topics : [];
  const optedOut = Array.isArray(diag.opted_out_members) ? diag.opted_out_members : [];

  const disclosure = el('details', 'archive-disclosure');
  if (intents.some(i => i.status === 'pending') || optedOut.length > 0) {
    disclosure.open = true;
  }
  const summary = el('summary', 'archive-summary-bar');
  const left = el('div', 'health-summary-left');
  left.append(
    el('span', 'health-summary-title', '主动分享队列 · 共同话题 · 退订名单'),
    el('span', 'pill info', `分享队列 ${intents.length}`),
    el('span', 'pill accent', `共同话题 ${coTopics.length}`),
    el('span', `pill ${optedOut.length ? 'warn' : ''}`, `退订/屏蔽 ${optedOut.length}`),
  );
  summary.append(left, el('span', 'health-toggle-label', '展开诊断'));

  const body = el('div', 'archive-body');
  const tabs = el('div', 'segmented-group');
  tabs.setAttribute('role', 'group');
  tabs.setAttribute('aria-label', '主动分享诊断视图切换');
  const content = el('div', 'stack-list');

  const renderTabBody = () => {
    content.replaceChildren();
    for (const btn of tabs.querySelectorAll('button')) {
      btn.setAttribute('aria-pressed', String(btn.dataset.outreachTab === state.outreachTab));
    }
    if (state.outreachTab === 'intents') {
      if (!intents.length) {
        content.append(el('div', 'empty-state', '当前暂无主动分享或 /安利 回信候选队列。'));
        return;
      }
      for (const it of intents) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const tg = el('div', 'item-title-group');
        const kindLabel = it.horizon_kind === 'anli_reflection_reply' ? '安利回信' : '共聊分享';
        tg.append(
          el('span', `pill ${it.horizon_kind === 'anli_reflection_reply' ? 'accent' : 'info'}`, kindLabel),
          el('h4', 'item-title', it.topic || '见闻分享'),
          el('span', `pill ${it.status === 'pending' ? 'warm' : it.status === 'sent' ? 'accent' : ''}`, it.status),
        );
        const acts = el('div', 'item-actions');
        if (it.status === 'pending') {
          acts.append(
            actionButton('取消发送', async () => {
              await apiPost('reviews', { op: 'cancel_intent', identity: state.identity, intent_id: it.intent_id });
              notify('已取消该条待发主动分享意图。', 'info');
              await loadOverview({ silent: true });
            }, 'wb-button danger'),
          );
        }
        head.append(tg, acts);
        const meta = el('div', 'item-meta');
        meta.append(
          el('span', '', `会话：${formatScopeLabel(it.umo) || it.umo}`),
          el('span', '', `对象：${it.recipient_name ? `${it.recipient_name} (${it.recipient_user_id})` : it.recipient_user_id}`),
          el('span', '', `过期：${formatTime(it.expires_at)}`),
        );
        card.append(head);
        if (it.reason) card.append(el('p', 'item-body', it.reason));
        card.append(meta);
        content.append(card);
      }
    } else if (state.outreachTab === 'co_topics') {
      if (!coTopics.length) {
        content.append(el('div', 'empty-state', '当前暂无已核实的共同话题证据。'));
        return;
      }
      for (const ct of coTopics) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const tg = el('div', 'item-title-group');
        tg.append(
          el('span', 'pill accent', ct.topic || '未命名'),
          el('span', 'pill info', `互动 ${ct.turn_count} 轮`),
          ...(ct.opted_out ? [el('span', 'pill warn', '已退订')] : []),
        );
        head.append(tg);
        const meta = el('div', 'item-meta');
        meta.append(
          el('span', '', `会话：${formatScopeLabel(ct.umo) || ct.umo}`),
          el('span', '', `成员：${ct.member_id}`),
          el('span', '', `最近互动：${formatTime(ct.last_occurred_at)}`),
        );
        card.append(head, meta);
        content.append(card);
      }
    } else {
      if (!optedOut.length) {
        content.append(el('div', 'empty-state', '当前没有退订主动分享或屏蔽特定话题的成员。'));
        return;
      }
      for (const m of optedOut) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const tg = el('div', 'item-title-group');
        tg.append(
          el('span', 'pill warn', m.kind === 'topic' ? `屏蔽话题：${m.topic}` : '全量退订主动分享'),
          el('span', '', `${formatScopeLabel(m.umo) || m.umo} · ${m.member_id}`),
        );
        const acts = el('div', 'item-actions');
        acts.append(
          actionButton('恢复分享', async () => {
            await apiPost('reviews', {
              op: 'restore_member_outreach',
              identity: m.identity || state.identity,
              umo: m.umo,
              member_id: m.member_id,
              topic: m.topic || '',
            });
            notify('已恢复该成员的见闻分享偏好。', 'success');
            await loadOverview({ silent: true });
          }),
        );
        head.append(tg, acts);
        card.append(head);
        content.append(card);
      }
    }
  };

  for (const [key, label] of [
    ['intents', `主动分享队列 (${intents.length})`],
    ['co_topics', `共同话题 (${coTopics.length})`],
    ['opted_out', `退订名单 (${optedOut.length})`],
  ]) {
    const btn = el('button', '', label);
    btn.type = 'button';
    btn.dataset.outreachTab = key;
    btn.setAttribute('aria-pressed', String(state.outreachTab === key));
    btn.addEventListener('click', () => {
      state.outreachTab = key;
      renderTabBody();
    });
    tabs.append(btn);
  }
  renderTabBody();
  body.append(tabs, content);
  disclosure.append(summary, body);
  box.replaceChildren(disclosure);
}

function renderOverview(overview) {
  renderHealth(overview?.health);
  renderPresetsAndDecay(overview);
  renderOutreachDiagnostics(overview);
  renderArchiveControls();
  const stats = overview?.stats || {};
  const wl = overview?.world_link || {};
  const kpiGrid = $('#kpi-grid');
  if (kpiGrid) {
    kpiGrid.replaceChildren();

    const quota = Math.max(1, Number(stats.daily_quota) || 5);
    const used = Math.max(0, Number(stats.daily_used) || 0);
    const quotaPct = Math.min(100, Math.round((used / quota) * 100));

    const quotaCard = el('article', 'kpi-card');
    quotaCard.append(
      el('span', 'kpi-label', msg('今日挑拣配额')),
      el('strong', 'kpi-value', `${used} / ${Number(stats.daily_quota) || 0}`),
    );
    const track = el('div', 'meter-track');
    const fill = el('div', `meter-fill${quotaPct >= 100 ? ' is-warm' : ''}`);
    fill.style.width = `${quotaPct}%`;
    track.append(fill);
    quotaCard.append(track, el('span', 'kpi-sub', `最近精读：${formatTime(stats.last_digest_at)}`));

    const poolCard = el('article', 'kpi-card');
    poolCard.append(
      el('span', 'kpi-label', msg('候选未读池')),
      el('strong', 'kpi-value', String(Number(stats.candidates_unread) || 0)),
      el('span', 'kpi-sub', `启用 ${Number(stats.sources_enabled) || 0}/${Number(stats.sources_total) || 0} 源 · 轮询 ${formatTime(stats.last_poll_at)}`),
    );

    const interestCard = el('article', 'kpi-card');
    interestCard.append(
      el('span', 'kpi-label', msg('活跃暂存兴趣')),
      el('strong', 'kpi-value', String(Number(stats.active_interests) || 0)),
      el('span', 'kpi-sub', `手账累计 ${Number(stats.logs_total) || 0} 篇 · 带半衰期不改人设`),
    );

    const pendingReviewsNum = Number(stats.pending_reviews) || 0;
    const heldReviewsNum = Number(stats.held_reviews) || 0;
    const reviewSubText = heldReviewsNum > 0
      ? `待审提案 ${pendingReviewsNum} 项（含人工复核 ${heldReviewsNum} 项）· 按群独立核销`
      : `待审提案 ${pendingReviewsNum} 项 · 按群独立核销`;
    const questionCard = el('article', 'kpi-card');
    questionCard.append(
      el('span', 'kpi-label', msg('待讨论问题')),
      el('strong', 'kpi-value', String(Number(stats.pending_questions) || 0)),
      el('span', 'kpi-sub', reviewSubText),
    );

    const currentlyBusy = overview?.health?.blockers?.includes('world_busy');
    const wakeLabel = wl.connected
      ? wl.paused ? '生活暂停' : wl.asleep ? '熟睡跳过' : currentlyBusy ? '角色正在忙碌' : '清醒可读'
      : overview?.enabled === false ? '已停用' : '独立运行';
    const readingStopped = stats.analysis_status === 'error:ValueError' && readingCharacterChanged(overview?.health);
    const modelCard = el('article', 'kpi-card');
    modelCard.append(
      el('span', 'kpi-label', msg('世界与模型状态')),
      el('strong', 'kpi-value', msg(wakeLabel)),
      el('span', 'kpi-sub', msg('分析：{0}；审查：{1}',
        modelStatusLabel(stats.analysis_status, readingStopped),
        modelStatusLabel(stats.review_status),
      )),
    );

    kpiGrid.append(quotaCard, poolCard, interestCard, questionCard, modelCard);
  }

  // Staged Interests with visual half-life decay progress bars
  const interests = Array.isArray(overview?.staged_interests) ? overview.staged_interests : [];
  const intCount = $('#interests-count-pill');
  if (intCount) intCount.textContent = String(interests.length);
  const intList = $('#interests-list');
  if (intList) {
    intList.replaceChildren();
    if (interests.length === 0) {
      intList.append(el('div', 'empty-state', '当前角色暂无活跃暂存兴趣。角色在空闲清醒时阅读后将在此沉淀阶段性兴趣。'));
    } else {
      for (const item of interests) {
        const card = el('article', 'item-card interest-card');
        const head = el('div', 'item-head');
        const titleWrap = el('div', 'item-title-group');
        titleWrap.append(el('h4', 'item-title', item.topic || '未命名话题'));
        const statusText = item.status === 'cooled' ? '已冷却' : item.status === 'revoked' ? '已撤回' : '活跃中';
        const isActive = item.status === 'active' || !item.status;
        titleWrap.append(el('span', `pill ${isActive ? 'warm' : ''}`, statusText));

        const actions = el('div', 'item-actions');
        actions.append(
          actionButton(msg('延期'), async () => {
            await apiPost('reviews', { op: 'extend_interest', identity: state.identity, interest_id: item.interest_id });
            notify(`已延长暂存兴趣「${item.topic}」保鲜期。`, 'success');
            await loadOverview({ silent: true });
          }),
        );
        if (item.status !== 'revoked') {
          actions.append(
            actionButton(msg('撤回'), async () => {
              await apiPost('reviews', { op: 'revoke_interest', identity: state.identity, interest_id: item.interest_id });
              notify(`已撤回暂存兴趣「${item.topic}」。`, 'info');
              await loadOverview({ silent: true });
            }, 'wb-button danger'),
          );
        }
        head.append(titleWrap, actions);

        const heatPct = normalizeHeatPercent(item.heat_score);
        const toneCls = heatPct >= 70 ? 'is-hot' : heatPct < 40 ? 'is-fading' : '';
        const remainText = formatRemainingDecay(item.expires_at, item.status);
        const metricParts = [`热度 ${heatPct}%`];
        if (remainText) metricParts.push(remainText);

        const decayWrap = el('div', 'decay-bar-wrap');
        const decayHeader = el('div', 'decay-header');
        decayHeader.append(
          el('span', 'decay-label', msg('半衰期衰减')),
          el('span', `decay-metric ${toneCls}`.trim(), metricParts.join(' · ')),
        );
        const track = el('div', 'decay-track');
        track.setAttribute('role', 'progressbar');
        track.setAttribute('aria-label', `${item.topic || '兴趣'} 半衰期热度`);
        track.setAttribute('aria-valuemin', '0');
        track.setAttribute('aria-valuemax', '100');
        track.setAttribute('aria-valuenow', String(heatPct));
        const fill = el('div', `decay-fill ${toneCls}`.trim());
        fill.style.width = `${heatPct}%`;
        const midpoint = el('span', 'decay-midpoint');
        midpoint.setAttribute('aria-hidden', 'true');
        track.append(fill, midpoint);
        decayWrap.append(decayHeader, track);

        card.append(head);
        if (item.summary) card.append(el('p', 'item-body', item.summary));
        card.append(decayWrap);
        if (item.expires_at) {
          const metaRow = el('div', 'item-meta');
          metaRow.append(el('span', '', `自然衰减到期：${formatTime(item.expires_at)}`));
          if (item.half_life_days) {
            metaRow.append(el('span', '', `半衰期 ${Number(item.half_life_days)} 天`));
          }
          card.append(metaRow);
        }
        intList.append(card);
      }
    }
  }

  // Pending Discussion Questions
  const questions = Array.isArray(overview?.discussion_questions) ? overview.discussion_questions : [];
  const qCount = $('#questions-count-pill');
  if (qCount) qCount.textContent = String(questions.length);
  const qList = $('#questions-list');
  if (qList) {
    qList.replaceChildren();
    if (questions.length === 0) {
      qList.append(el('div', 'empty-state', '当前没有待讨论问题。遇到合适的话题切口时角色才会自然生成问题。'));
    } else {
      for (const q of questions) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const titleWrap = el('div', 'item-title-group');
        if (q.topic) titleWrap.append(el('span', 'pill accent', q.topic));
        titleWrap.append(el('h4', 'item-title', q.question || ''));

        const actions = el('div', 'item-actions');
        actions.append(
          actionButton(msg('归档'), async () => {
            await apiPost('reviews', { op: 'archive_question', identity: state.identity, question_id: q.question_id });
            notify('已归档该待讨论问题。', 'info');
            await loadOverview({ silent: true });
          }),
        );
        head.append(titleWrap, actions);
        card.append(head);

        const umoBox = el('div', 'umo-badges');
        const umoStates = Array.isArray(q.umo_states) ? q.umo_states : [];
        if (umoStates.length === 0) {
          umoBox.append(el('span', 'pill info', '各群待聊（尚未核销）'));
        } else {
          const orderedUmos = [...umoStates].sort((a, b) => {
            const aMatch = state.sharedScope && a.umo === state.sharedScope ? 0 : 1;
            const bMatch = state.sharedScope && b.umo === state.sharedScope ? 0 : 1;
            return aMatch - bMatch;
          });
          for (const u of orderedUmos) {
            const isDone = u.status === 'discussed';
            const isActiveScope = Boolean(state.sharedScope && u.umo === state.sharedScope);
            const badge = el(
              'span',
              `pill ${isDone ? 'accent' : 'info'}${isActiveScope ? ' is-active-scope' : ''}`,
              `${formatScopeLabel(u.umo) || u.umo} · ${isDone ? tr('已聊核销') : tr('待聊')}`,
            );
            const tipParts = [];
            if (isDone && u.discussed_at) tipParts.push(`核销时间：${formatTime(u.discussed_at)}`);
            tipParts.push('点击同步为当前群聊作用域');
            badge.title = tipParts.join(' · ');
            badge.addEventListener('click', () => {
              if (u.umo) setSharedScope(u.umo, { syncUrl: true });
            });
            umoBox.append(badge);
          }
        }
        card.append(umoBox);

        const meta = el('div', 'item-meta');
        if (q.created_at) meta.append(el('span', '', `生成于 ${formatTime(q.created_at)}`));
        if (q.expires_at) meta.append(el('span', '', `保鲜期至 ${formatTime(q.expires_at)}`));
        if (meta.childNodes.length) card.append(meta);
        qList.append(card);
      }
    }
  }
}

/* ===== Tab 2: Sources & Serial Bookshelf ===== */
function summarizeSourceConfig(src) {
  const cfg = src?.config || {};
  const kind = src?.source_kind;
  const parts = [];
  if (kind === 'bilibili') {
    const mode = cfg.mode || 'popular';
    if (mode === 'up') parts.push(`UP主 UID: ${cfg.uid || '未填'}`);
    else if (mode === 'recommend') parts.push('首页个性化推荐流');
    else parts.push(`免登录榜单 (${cfg.popular_kind || cfg.category || '综合热门'})`);
    if (cfg.cookie || cfg.sessdata || cfg.has_cookie) parts.push('只读凭据：已掩码保存');
  } else if (kind === 'ebook') {
    const prov = cfg.provider || cfg.mode || 'ebook';
    parts.push(`来源: ${prov}`);
    if (cfg.page || cfg.title) parts.push(`条目: ${cfg.page || cfg.title}`);
    if (cfg.book_id || cfg.gutenberg_id) parts.push(`ID: ${cfg.book_id || cfg.gutenberg_id}`);
    if (cfg.url) parts.push(cfg.url);
    if (cfg.allow_private_opds) parts.push('允许内网 OPDS');
  } else if (kind === 'feed') {
    if (cfg.url) parts.push(cfg.url);
  } else if (kind === 'manual') {
    if (cfg.url) parts.push(cfg.url);
    else parts.push('管理员投喂案头读物');
  }
  return parts.join(' · ') || '按默认只读策略运行';
}

function renderSourcesAndBooks() {
  const sources = Array.isArray(state.sources) ? state.sources : [];
  const books = Array.isArray(state.books) ? state.books : [];

  const srcCount = $('#sources-count-pill');
  if (srcCount) srcCount.textContent = String(sources.length);

  const srcList = $('#sources-list');
  if (srcList) {
    srcList.replaceChildren();
    if (sources.length === 0) {
      srcList.append(el('div', 'empty-state', '当前角色尚未配置信息源。点击右上方「＋ 添加信息源」或「剪报投喂」开始配置。'));
    } else {
      const nowSec = Date.now() / 1000;
      for (const src of sources) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const titleGroup = el('div', 'item-title-group');
        titleGroup.append(
          el('span', 'pill accent', KIND_LABELS[src.source_kind] || src.source_kind),
          el('h4', 'item-title', src.name || '未命名源'),
          el('span', `pill ${src.enabled !== false ? 'accent' : ''}`, src.enabled !== false ? tr('已启用') : tr('已停用')),
        );
        if (src.status) {
          const isWarn = Boolean(src.last_error) || /err|fail|risk|429|backoff/i.test(String(src.status));
          const rawStatus = String(src.status).split(':')[0];
          const normalizedStatus = rawStatus.toLowerCase();
          const label = normalizedStatus === 'risk_control_backoff'
            ? 'B站风控退避'
            : normalizedStatus === 'partial_risk_control_backoff'
              ? '部分拉取后退避'
              : normalizedStatus.startsWith('partial_')
                ? '部分拉取'
                : (src.last_error ? rawStatus : String(src.status));
          titleGroup.append(el('span', `pill ${isWarn ? 'warn' : 'info'}`, label));
        }
        if (Number(src.cooldown_until) > nowSec) {
          titleGroup.append(el('span', 'pill warn', `风控/退避至 ${formatTime(src.cooldown_until)}`));
        }

        const actions = el('div', 'item-actions');
        actions.append(
          actionButton(msg('立即拉取'), async () => {
            const result = await apiPost('sources', { op: 'poll_now', identity: src.identity, source_id: src.source_id });
            if (result.outcome === 'partial' || String(result.outcome || '').startsWith('partial_')) {
              const retry = result.cooldown_until ? `；预计 ${formatTime(result.cooldown_until)} 后可重试` : '';
              const detail = result.error ? `：${result.error}` : '';
              notify(`「${src.name}」已保存 ${Number(result.added) || 0} 条候选，部分内容暂未拉取${detail}${retry}。`, 'info');
            } else if (result.polled) {
              notify(`「${src.name}」拉取完成，新增 ${result.added} 条候选。`, 'success');
            } else if (result.outcome === 'error' || result.outcome === 'cooldown') {
              const retry = result.cooldown_until ? `，可于 ${formatTime(result.cooldown_until)} 后重试` : '';
              const detail = result.error ? `（${result.error}）` : '';
              notify(`「${src.name}」拉取${result.outcome === 'cooldown' ? '退避' : '失败'}${detail}${retry}。`, result.outcome === 'error' ? 'error' : 'info');
            } else {
              const reason = {
                disabled: '信息源已停用。请启用后再拉取。',
                in_progress: '该信息源正在拉取。',
                source_changed: '信息源已更新。请刷新后再拉取。',
              }[result.status];
              notify(result.error || reason || '拉取未完成。', result.error ? 'error' : 'info');
            }
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }),
          actionButton(msg('编辑'), async () => {
            openSourceDrawer(src.source_kind || 'bilibili', src);
          }),
          actionButton(src.enabled !== false ? msg('停用') : msg('启用'), async () => {
            await apiPost('sources', {
              op: 'toggle',
              identity: src.identity,
              source_id: src.source_id,
              enabled: src.enabled === false,
            });
            notify(`已切换「${src.name}」启用状态。`, 'info');
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }),
          actionButton(msg('删除'), async () => {
            await apiPost('sources', { op: 'delete', identity: src.identity, source_id: src.source_id });
            notify(`已删除信息源「${src.name}」。`, 'info');
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }, 'wb-button danger'),
        );

        head.append(titleGroup, actions);
        const srcMeta = el('div', 'item-meta');
        srcMeta.append(el('span', '', `最近轮询：${formatTime(src.last_poll_at)}`));
        if (src.last_error) {
          srcMeta.append(el('span', 'pill warn', `最近异常：${src.last_error}`));
        }
        card.append(
          head,
          el('p', 'item-body', summarizeSourceConfig(src)),
          srcMeta,
        );
        const advice = src.error_advice || (src.last_error ? formatSourceErrorAdvice(src.last_error, src.source_kind) : '');
        if (advice) {
          card.append(el('p', 'advice-note', `风控/排查建议：${advice}`));
        }
        srcList.append(card);
      }
    }
  }

  // Serial Bookshelf
  const bookCount = $('#books-count-pill');
  if (bookCount) bookCount.textContent = String(books.length);
  const shelf = $('#bookshelf-list');
  if (shelf) {
    shelf.replaceChildren();
    if (books.length === 0) {
      const empty = el('div', 'empty-state', '角色书架暂无连载书目。可通过「公版书检索与本地上传」添加维基文库、Gutendex 或本地 EPUB/TXT。');
      empty.style.gridColumn = '1 / -1';
      shelf.append(empty);
    } else {
      for (const book of books) {
        const card = el('article', 'book-card');
        const head = el('div', 'item-head');
        const titleGroup = el('div', 'item-title-group');
        titleGroup.append(el('h4', 'item-title', book.title || '未命名书目'));
        if (book.creator) titleGroup.append(el('span', 'pill', book.creator));
        titleGroup.append(el('span','pill info',book.reading_label || (book.chapter_cursor ? '正在阅读' : '书架待读')));
        const bookActions = el('div', 'item-actions');
        bookActions.append(
          actionButton(msg('删除书籍'), async () => {
            const ok = await confirmAction(`确定要从书架删除《${book.title || '该书目'}》吗？已批准的历史阅读手账不会被删除。`);
            if (!ok) return;
            await apiPost('sources', {
              op: 'delete_book',
              identity: book.identity || state.identity,
              book_id: book.book_id,
            });
            notify(`已从书架移除《${book.title}》。`, 'info');
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }, 'wb-button danger'),
        );
        head.append(titleGroup, bookActions);

        const cursor = Math.max(0, Number(book.chapter_cursor) || 0);
        const total = Math.max(0, Number(book.total_chapters) || 0);
        const pct = total > 0 ? Math.min(100, Math.round((cursor / total) * 100)) : 0;
        const chapterLabel = total > 0 ? `第 ${cursor} / ${total} 章` : `第 ${cursor} 章`;

        const progressRow = el('div', 'heat-row');
        const track = el('div', 'meter-track');
        const fill = el('div', 'meter-fill');
        fill.style.width = `${pct}%`;
        track.append(fill);
        progressRow.append(el('span', '', chapterLabel), track, el('span', '', total > 0 ? `${pct}%` : '连载中'));

        const cursorRow = el('div', 'book-cursor-row');
        const cursorControls = el('div', 'book-cursor-controls');
        const cursorInput = el('input', 'book-cursor-input');
        cursorInput.type = 'number';
        cursorInput.min = '0';
        if (total > 0) cursorInput.max = String(total);
        cursorInput.value = String(cursor);
        cursorInput.setAttribute('aria-label', `调整《${book.title || '书目'}》章节游标`);
        cursorControls.append(
          el('span', 'page-info', '章节游标'),
          cursorInput,
          actionButton('调整进度', async () => {
            const nextCursor = Math.max(0, Number.parseInt(cursorInput.value, 10) || 0);
            await apiPost('sources', {
              op: 'update_book_status',
              identity: book.identity || state.identity,
              book_id: book.book_id,
              status: book.status || 'reading',
              chapter_cursor: nextCursor,
            });
            notify(`已将《${book.title}》阅读游标调整至第 ${nextCursor} 章。`, 'success');
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }),
        );
        cursorRow.append(cursorControls);

        const switcher = el('div', 'segmented-group');
        switcher.setAttribute('role', 'group');
        switcher.setAttribute('aria-label', `${book.title || '书目'} 阅读状态`);
        for (const [stKey, stLabel] of BOOK_STATUS_ITEMS) {
          const btn = actionButton(msg(stLabel), async () => {
            if (book.status === stKey) return;
            await apiPost('sources', {
              op: 'update_book_status',
              identity: book.identity,
              book_id: book.book_id,
              status: stKey,
            });
            notify(`已将《${book.title}》标记为「${tr(stLabel)}」。`, 'success');
            await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
          }, '');
          btn.setAttribute('aria-pressed', String((book.status || 'reading') === stKey));
          switcher.append(btn);
        }

        card.append(head, progressRow, cursorRow, switcher);
        shelf.append(card);
      }
    }
  }
}

/* ===== Tab 3: Reading Journal (Logs) ===== */
function formatCitationText(citation, fallbackKind) {
  const c = citation || {};
  const parts = [
    c.title || '未具名条目',
    c.creator || '',
    c.bvid || '',
    c.chapter_label || '',
  ].map(s => String(s || '').trim()).filter(Boolean);
  return parts.length ? parts.join(' · ') : (KIND_LABELS[fallbackKind] || '外部读物');
}

function formatDurationLabel(sec) {
  const total = Math.floor(Number(sec) || 0);
  if (total <= 0) return '';
  const mins = Math.floor(total / 60);
  const rem = total % 60;
  return `${String(mins).padStart(2, '0')}:${String(rem).padStart(2, '0')}`;
}

function resolveMediaMeta(item) {
  if (item?.media_meta && typeof item.media_meta === 'object') return item.media_meta;
  if (item?.evidence?.media_meta && typeof item.evidence.media_meta === 'object') return item.evidence.media_meta;
  return {};
}

function buildWatchBadges(item) {
  const meta = resolveMediaMeta(item);
  const rawMode = String(item?.watch_mode || meta.watch_mode || '').trim();
  const rawOrigin = String(item?.content_origin || meta.content_origin || item?.evidence?.content_origin || '').trim();
  const mode = rawMode || ORIGIN_TO_WATCH_MODE[rawOrigin] || '';
  const badges = [];
  const spec = WATCH_MODE_BADGES[mode];
  if (spec) {
    const cls = spec.tone ? `pill ${spec.tone} watch-badge` : 'pill watch-badge';
    const badge = el('span', cls, spec.label);
    const tips = [];
    const frames = Number(meta.frame_count) || 0;
    if (frames > 0) tips.push(`${frames} 帧`);
    const dur = formatDurationLabel(meta.duration_sec);
    if (dur) tips.push(`时长 ${dur}`);
    if (meta.provider_id) tips.push(`模型 ${String(meta.provider_id)}`);
    if (tips.length) badge.title = tips.join(' · ');
    badges.push(badge);
  }
  if (meta.has_danmaku === true) {
    badges.push(el('span', 'pill warn watch-badge', '含高能弹幕'));
  }
  return badges;
}

function extractVisualObservations(item) {
  const raw = Array.isArray(item?.visual_observations)
    ? item.visual_observations
    : (Array.isArray(item?.evidence?.visual_observations) ? item.evidence.visual_observations : []);
  const out = [];
  for (const entry of raw) {
    const text = typeof entry === 'string' ? entry.trim() : (entry == null ? '' : String(entry).trim());
    if (text) out.push(text);
  }
  return out;
}

function buildVisualObservationsBlock(item) {
  const observations = extractVisualObservations(item);
  if (!observations.length) return null;
  const meta = resolveMediaMeta(item);
  const box = el('details', 'visual-obs-box');
  box.open = true;
  const summary = el('summary', 'visual-obs-summary');
  summary.append(
    el('span', 'visual-obs-title', '画面观察'),
    el('span', 'pill info watch-badge', `${observations.length} 条`),
  );
  const frames = Number(meta.frame_count) || 0;
  if (frames > 0) {
    summary.append(el('span', 'visual-obs-meta', `${frames} 帧采样`));
  }
  const list = el('ul', 'visual-obs-list');
  for (const obs of observations) {
    list.append(el('li', 'visual-obs-item', obs));
  }
  box.append(summary, list);
  return box;
}

function renderLogs() {
  const { items, page, total, page_size, source_kind } = state.logs;
  for (const btn of $$('#logs-filter-group [data-log-kind]')) {
    btn.setAttribute('aria-pressed', String((btn.dataset.logKind || '') === source_kind));
  }

  const overviewQuestions = Array.isArray(state.overview?.discussion_questions)
    ? state.overview.discussion_questions
    : [];
  const validKinds = new Set(['bilibili', 'ebook', 'feed', 'manual']);

  const list = $('#logs-list');
  if (list) {
    list.replaceChildren();
    if (!items.length) {
      list.append(el('div', 'empty-state', '当前筛选下暂无阅读手账记录。'));
    } else {
      for (const item of items) {
        const safeKind = validKinds.has(item.source_kind) ? item.source_kind : 'manual';
        const card = el('article', `item-card journal-card kind-${safeKind}`);
        const head = el('div', 'item-head');
        const left = el('div', 'item-title-group');
        left.append(
          el('span', `pill ${safeKind === 'bilibili' ? 'warm' : safeKind === 'feed' ? 'info' : 'accent'}`, KIND_LABELS[item.source_kind] || item.source_kind || '见闻'),
          el('span', 'citation-badge', formatCitationText(item.citation, item.source_kind)),
          ...buildWatchBadges(item),
        );

        const right = el('div', 'item-meta');
        right.append(el('span', '', formatTime(item.created_at)));
        if (typeof item.raw_url === 'string' && /^https?:\/\//i.test(item.raw_url.trim())) {
          const link = el('a', 'raw-link', '管理台留档原文 ↗');
          link.href = item.raw_url.trim();
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          right.append(link);
        }
        head.append(left, right);

        const dual = el('div', 'log-dual');
        const digestPane = el('div', 'log-pane');
        digestPane.append(
          el('span', 'log-pane-label', msg('内容核对摘要')),
          el('p', '', item.digest || '（无摘要）'),
        );
        const reflectionPane = el('div', 'log-pane reflection');
        reflectionPane.append(
          el('span', 'log-pane-label', msg('角色随感')),
          el('p', '', item.reflection || '（角色未留额外随感）'),
        );
        dual.append(digestPane, reflectionPane);

        card.append(head, dual);

        // Pending discussion question chips linked to this journal entry
        const attachedQs = Array.isArray(item.questions) && item.questions.length
          ? item.questions
          : overviewQuestions.filter(q => q.log_id && item.log_id && q.log_id === item.log_id);
        if (attachedQs.length) {
          const qStrip = el('div', 'journal-questions-strip');
          for (const q of attachedQs.slice(0, 3)) {
            const qText = typeof q === 'object' ? String(q.question || q.text || '').trim() : String(q || '').trim();
            if (!qText) continue;
            const chip = el('span', 'question-chip');
            chip.append(el('span', 'question-chip-prefix', '待聊：'), el('span', '', qText));
            qStrip.append(chip);
          }
          if (qStrip.childNodes.length) card.append(qStrip);
        }

        const visualObs = buildVisualObservationsBlock(item);
        if (visualObs) card.append(visualObs);
        const discussionNotes = (item.discussion_reflections || []).slice(0, 5);
        if (discussionNotes.length) {
          const notesPane = el('div', 'log-pane reflection');
          notesPane.append(el('span', 'log-pane-label', '交流后记'));
          for (const note of discussionNotes) {
            const meta = el('div', 'item-meta');
            if (note.created_at) meta.append(el('span', '', formatTime(note.created_at)));
            if (note.topic) meta.append(el('span', '', `话题：${note.topic}`));
            if (note.umo) meta.append(el('span', '', `会话：${note.umo}`));
            if (note.member_id) meta.append(el('span', '', `成员：${note.member_id}`));
            notesPane.append(meta);
            if (note.member_quote) {
              notesPane.append(el('p', '', `对方回应：${note.member_quote}`));
            }
            if (note.reflection) {
              notesPane.append(el('p', '', `后续感想：${note.reflection}`));
            }
          }
          card.append(notesPane);
        }
        list.append(card);
      }
    }
  }

  const maxPage = Math.max(1, Math.ceil((Number(total) || 0) / (page_size || PAGE_SIZE)));
  const info = $('#logs-page-info');
  if (info) setText(info,msg('第 {0} / {1} 页 · 共 {2} 条',page,maxPage,Number(total)||0));
  const prev = $('#logs-prev');
  const next = $('#logs-next');
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= maxPage;
}

/* ===== Tab 4: Reviews & Topic Blocklist ===== */
function syncBatchSelectionUI() {
  const actionableItems = (state.reviews.items || []).filter(
    item => ['pending_review', 'pending', 'held'].includes(item.state || 'pending_review')
      && item.review_reason !== 'source_changed',
  );
  const validSet = new Set(actionableItems.map(i => i.proposal_id));
  for (const id of [...state.reviews.selectedIds]) {
    if (!validSet.has(id)) state.reviews.selectedIds.delete(id);
  }
  const count = state.reviews.selectedIds.size;
  const countNode = $('#reviews-selected-count');
  if (countNode) countNode.textContent = `已选 ${count} 项`;
  const selectAll = $('#reviews-select-all');
  if (selectAll) {
    selectAll.disabled = actionableItems.length === 0;
    selectAll.checked = actionableItems.length > 0 && count === actionableItems.length;
  }
  const btnApprove = $('#btn-batch-approve');
  const btnReject = $('#btn-batch-reject');
  if (btnApprove) btnApprove.disabled = count === 0;
  if (btnReject) btnReject.disabled = count === 0;
}

function renderCorrectionsQueue() {
  const corrections = Array.isArray(state.reviews.corrections) && state.reviews.corrections.length
    ? state.reviews.corrections
    : (Array.isArray(state.overview?.health?.diagnostics?.corrections) ? state.overview.health.diagnostics.corrections : []);
  const pill = $('#corrections-count-pill');
  if (pill) pill.textContent = String(corrections.length);
  const list = $('#corrections-list');
  if (!list) return;
  list.replaceChildren();
  if (!corrections.length) {
    list.append(el('div', 'empty-state', '当前没有待处理的群友纠错记录。'));
    return;
  }
  for (const corr of corrections) {
    const card = el('article', 'item-card');
    const head = el('div', 'item-head');
    const tg = el('div', 'item-title-group');
    tg.append(
      el('span', 'pill warn', '群友纠错'),
      el('h4', 'item-title', corr.log_title ? `关联手账：《${corr.log_title}》` : `关联手账：${corr.log_id || '未知'}`),
    );
    const acts = el('div', 'item-actions');
    acts.append(
      actionButton('确认并撤销原手记', async () => {
        await apiPost('reviews', {
          op: 'resolve_correction',
          correction_id: corr.correction_id,
          identity: corr.identity || state.identity,
          resolution: 'revoke',
        });
        notify('已确认纠错并撤销原阅读手账。', 'success');
        await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
      }, 'wb-button danger'),
      actionButton('驳回纠错', async () => {
        await apiPost('reviews', {
          op: 'resolve_correction',
          correction_id: corr.correction_id,
          identity: corr.identity || state.identity,
          resolution: 'dismiss',
        });
        notify('已驳回该条纠错请求。', 'info');
        await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
      }),
    );
    head.append(tg, acts);
    card.append(head, el('p', 'item-body', corr.statement || ''));
    const meta = el('div', 'item-meta');
    meta.append(
      el('span', '', `会话：${formatScopeLabel(corr.umo) || corr.umo}`),
      el('span', '', `成员：${corr.member_id || ''}`),
      ...(corr.created_at ? [el('span', '', formatTime(corr.created_at))] : []),
    );
    card.append(meta);
    list.append(card);
  }
}

function renderReviews() {
  const { items, blocked_topics, page, total, page_size, state: filterState } = state.reviews;
  for (const btn of $$('#reviews-filter-group [data-review-state]')) {
    btn.setAttribute('aria-pressed', String((btn.dataset.reviewState || '') === filterState));
  }
  const kindSel = $('#reviews-source-kind');
  if (kindSel) kindSel.value = state.reviews.source_kind || '';

  const revCount = $('#reviews-count-pill');
  if (revCount) revCount.textContent = String(Number(total) || items.length);

  const list = $('#reviews-list');
  if (list) {
    list.replaceChildren();
    if (!items.length) {
      list.append(el('div', 'empty-state', '当前筛选下没有匹配的审查提案。'));
    } else {
      for (const item of items) {
        const card = el('article', 'item-card');
        const head = el('div', 'item-head');
        const titleGroup = el('div', 'item-title-group');
        const st = item.state || 'pending_review';
        const actionable = ['pending_review', 'pending', 'held'].includes(st)
          && item.review_reason !== 'source_changed';
        if (actionable) {
          const cb = el('input', 'review-select-cb');
          cb.type = 'checkbox';
          cb.checked = state.reviews.selectedIds.has(item.proposal_id);
          cb.setAttribute('aria-label', `选择提案 ${item.title || item.proposal_id}`);
          cb.addEventListener('change', () => {
            if (cb.checked) state.reviews.selectedIds.add(item.proposal_id);
            else state.reviews.selectedIds.delete(item.proposal_id);
            syncBatchSelectionUI();
          });
          titleGroup.append(cb);
        }
        if (item.source_kind) {
          titleGroup.append(el('span', 'pill', KIND_LABELS[item.source_kind] || item.source_kind));
        }
        titleGroup.append(el('h4', 'item-title', item.title || '未命名阅读提案'));

        const stTone = st === 'approved' ? 'accent' : st === 'rejected' ? 'danger' : 'warn';
        titleGroup.append(
          el('span', `pill ${stTone}`, PROPOSAL_STATE_LABELS[st] || st),
          ...buildWatchBadges(item),
        );

        const actions = el('div', 'item-actions');
        const target = {
          identity: item.identity || state.identity,
          proposal_id: item.proposal_id,
          revision: item.revision,
        };
        let editorBox = null;
        if (actionable) {
          const editBtn = el('button', 'wb-button', '微调编辑');
          editBtn.type = 'button';
          editBtn.addEventListener('click', () => {
            if (editorBox) editorBox.hidden = !editorBox.hidden;
          });
          actions.append(
            editBtn,
            actionButton(msg('批准'), async () => {
              await apiPost('reviews', {
                op: 'approve',
                ...target,
              });
              notify(`已批准提案「${item.title || item.proposal_id}」。`, 'success');
              await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
            }, 'wb-button primary'),
            actionButton(msg('拒绝'), async () => {
              await apiPost('reviews', {
                op: 'reject',
                ...target,
              });
              notify(`已拒绝提案「${item.title || item.proposal_id}」。`, 'info');
              await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
            }, 'wb-button danger'),
          );
        }
        if (st === 'approved') actions.append(actionButton('撤销生效', async () => {
          await apiPost('reviews', { op: 'revoke_proposal', ...target });
          await Promise.all([loadReviews({silent:true}), loadOverview({silent:true})]);
        }, 'wb-button danger'));
        if (actionable) actions.append(actionButton(msg('重试审核'), async () => {
          await apiPost('reviews', { op: 'retry_review', ...target });
          notify('已将提案加入审核队列。', 'success');
          await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
        }));
        head.append(titleGroup, actions);
        card.append(head);

        if (actionable) {
          editorBox = el('div', 'proposal-editor');
          editorBox.hidden = true;
          const digestInput = el('textarea');
          digestInput.rows = 2;
          digestInput.value = item.digest || '';
          const reflInput = el('textarea');
          reflInput.rows = 2;
          reflInput.value = item.reflection || '';
          const topicInput = el('input');
          topicInput.type = 'text';
          topicInput.value = (item.staged_interest && typeof item.staged_interest === 'object')
            ? (item.staged_interest.topic || '')
            : '';
          const qsInput = el('textarea');
          qsInput.rows = 2;
          qsInput.placeholder = '每行一条待讨论问题，留空则不生成待讨论问题';
          qsInput.value = (Array.isArray(item.questions) ? item.questions : [])
            .map(q => (typeof q === 'object' ? (q.question || q.text || '') : String(q || '')))
            .filter(Boolean)
            .join('\n');

          const f1 = el('label', 'form-field');
          f1.append(el('span', '', '核对摘要（<=200字）'), digestInput);
          const f2 = el('label', 'form-field');
          f2.append(el('span', '', '角色随感（<=150字）'), reflInput);
          const f3 = el('label', 'form-field');
          f3.append(el('span', '', '拟更新兴趣话题'), topicInput);
          const f4 = el('label', 'form-field');
          f4.append(el('span', '', '待讨论问题（可润色，每行 1 条）'), qsInput);
          const edActions = el('div', 'item-actions');
          edActions.append(
            actionButton('保存微调', async () => {
              const nextTopic = topicInput.value.trim();
              const nextQs = qsInput.value
                .split(/\r?\n/)
                .map(s => s.trim())
                .filter(Boolean)
                .map(qText => ({ topic: nextTopic || item.title || '近期见闻', question: qText }));
              await apiPost('reviews', {
                op: 'edit_proposal',
                ...target,
                digest: digestInput.value.trim(),
                reflection: reflInput.value.trim(),
                staged_interest: nextTopic
                  ? {
                      topic: nextTopic,
                      summary: (item.staged_interest && item.staged_interest.summary) || reflInput.value.trim(),
                    }
                  : {},
                questions: nextQs,
              });
              notify('已保存提案微调内容。', 'success');
              await loadReviews({ silent: true });
            }, 'wb-button primary'),
            actionButton('收起', async () => { editorBox.hidden = true; }),
          );
          editorBox.append(f1, f2, f3, f4, edActions);
          card.append(editorBox);
        }
        const evidence = el('details','log-pane');
        evidence.append(el('summary','','阅读证据与审核历史'));
        evidence.append(el('p','',`内容来源：${item.evidence?.content_origin || item.content_origin || '旧记录，原文未留档'} · 抓取：${formatTime(item.evidence?.fetched_at)}`));
        evidence.append(el('p','item-body',item.evidence?.text || '没有可核验的原文快照'));
        for (const [key,label] of [['summary','累计摘要'],['characters','人物关系'],['open_questions','未解问题']]) if (item.continuity?.proposed?.[key]) evidence.append(el('p','item-body',`${label}：${item.continuity.proposed[key]}`));
        for (const entry of item.audit || []) evidence.append(el('p','item-meta',`${formatTime(entry.created_at)} ${entry.actor}：${entry.from_state} → ${entry.to_state} ${entry.reason}`));
        card.append(evidence);

        // Dual-LLM Review Verdict, Reason & Risk Tags
        const verdictBox = el('div', 'verdict-box');
        const verdictText = item.review_verdict || '待复检';
        const verdictTone = /approve|pass|ok|通过/i.test(String(verdictText))
          ? 'accent'
          : /reject|block|fail|拒绝/i.test(String(verdictText))
            ? 'danger'
            : 'warn';
        const verdictHeader = el('div', 'item-title-group');
        verdictHeader.append(
          el('strong', '', '独立审查模型结论：'),
          el('span', `pill ${verdictTone}`, String(verdictText)),
        );
        verdictBox.append(verdictHeader);
        if (item.review_reason) {
          verdictBox.append(el('span', '', `审查依据：${item.review_reason}`));
        }
        const reviewRisks = Array.isArray(item.review_risks)
          ? item.review_risks.map(describeReviewRisk).filter(Boolean)
          : [];
        if (reviewRisks.length > 0) {
          const risksRow = el('div', 'review-risks-row');
          risksRow.setAttribute('aria-label', tr('审查风险标记'));
          risksRow.append(el('strong', 'review-risks-label', '风险标记：'));
          for (const rk of reviewRisks) {
            const text = rk.detail && rk.detail !== '独立审查模型标记的风险提示'
              ? `${rk.label} · ${rk.detail}`
              : rk.label;
            const tag = el('span', `pill ${rk.tone} risk-tag`, text);
            tag.title = rk.detail || rk.label;
            risksRow.append(tag);
          }
          verdictBox.append(risksRow);
        }
        card.append(verdictBox);

        const dual = el('div', 'log-dual');
        const dPane = el('div', 'log-pane');
        dPane.append(el('span', 'log-pane-label', msg('内容核对摘要')), el('p', '', item.digest || '—'));
        const rPane = el('div', 'log-pane reflection');
        rPane.append(el('span', 'log-pane-label', msg('角色随感')), el('p', '', item.reflection || '—'));
        dual.append(dPane, rPane);
        card.append(dual);

        const visualObs = buildVisualObservationsBlock(item);
        if (visualObs) card.append(visualObs);

        const metaLines = el('div', 'item-meta');
        if (item.staged_interest) {
          const topic = typeof item.staged_interest === 'object'
            ? (item.staged_interest.topic || item.staged_interest.summary || '')
            : String(item.staged_interest);
          if (topic) metaLines.append(el('span', 'pill accent', `拟更新兴趣：${topic}`));
        }
        const qs = Array.isArray(item.questions) ? item.questions : [];
        for (const q of qs) {
          const qText = typeof q === 'object' ? (q.question || q.text || '') : String(q || '');
          if (qText) metaLines.append(el('span', 'pill info', `拟讨论：${qText}`));
        }
        if (item.created_at) metaLines.append(el('span', '', formatTime(item.created_at)));
        if (metaLines.childNodes.length) card.append(metaLines);

        list.append(card);
      }
    }
  }

  const maxPage = Math.max(1, Math.ceil((Number(total) || 0) / (page_size || PAGE_SIZE)));
  const info = $('#reviews-page-info');
  if (info) setText(info,msg('第 {0} / {1} 页 · 共 {2} 条',page,maxPage,Number(total)||0));
  const prev = $('#reviews-prev');
  const next = $('#reviews-next');
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= maxPage;

  // Blocked Topics & Corrections Queue
  syncBatchSelectionUI();
  renderCorrectionsQueue();
  const topics = Array.isArray(blocked_topics) ? blocked_topics : [];
  const blkCount = $('#blocked-count-pill');
  if (blkCount) blkCount.textContent = String(topics.length);
  const blkList = $('#blocked-topics-list');
  if (blkList) {
    blkList.replaceChildren();
    if (topics.length === 0) {
      blkList.append(el('span', 'world-summary-note', '当前未设置屏蔽话题词。'));
    } else {
      for (const entry of topics) {
        const topicStr = typeof entry === 'object' ? String(entry.topic || entry.keyword || '') : String(entry || '');
        if (!topicStr) continue;
        const chip = el('span', 'blocked-tag');
        chip.append(
          el('span', '', topicStr),
          actionButton(msg('移除'), async () => {
            await apiPost('reviews', {
              op: 'unblock_topic',
              identity: state.identity,
              topic: topicStr,
            });
            notify(`已移除屏蔽词「${topicStr}」。`, 'info');
            await loadReviews({ silent: true });
          }, 'wb-button danger'),
        );
        blkList.append(chip);
      }
    }
  }
}

/* ===== Data Loaders ===== */
async function readRoleData(channel, params, busyNode, accept) {
  const valid = nextTicket(channel);
  busyNode?.setAttribute('aria-busy', 'true');
  try {
    const data = await apiGet(channel, { identity: state.identity, ...params });
    if (valid()) accept(data);
  } catch (err) {
    if (valid()) throw err;
  } finally {
    if (valid()) busyNode?.setAttribute('aria-busy', 'false');
  }
}

async function loadOverview({ silent = false } = {}) {
  if (!silent) notify('正在同步角色见闻总览…', 'info');
  const params = state.sharedScope ? { scope: state.sharedScope } : {};
  await readRoleData('overview', params, $('#panel-overview'), data => {
    state.overview = data;
    if (Array.isArray(data?.books) && state.books.length === 0) {
      state.books = data.books;
    }
    renderPersonaAndWorld(data);
    renderOverview(data);
    if (!silent) notify('已同步当前角色见闻状态。', 'info');
  });
}

async function loadSources({ silent = false } = {}) {
  if (!silent) notify('正在读取信息源与角色书架…', 'info');
  await readRoleData('sources', {}, $('#panel-sources'), data => {
    state.sources = Array.isArray(data?.sources) ? data.sources : [];
    state.books = Array.isArray(data?.books) ? data.books : (state.overview?.books || []);
    renderSourcesAndBooks();
    if (!silent) notify('已读取信息源与角色连载书架。', 'info');
  });
}

async function loadLogs({ silent = false } = {}) {
  if (!silent) notify('正在读取阅读手账…', 'info');
  await readRoleData('logs', {
    page: state.logs.page,
    source_kind: state.logs.source_kind,
    ...(state.logs.query ? { query: state.logs.query } : {}),
  }, $('#logs-list'), data => {
    state.logs.items = Array.isArray(data?.items) ? data.items : [];
    state.logs.page = Number(data?.page) || state.logs.page;
    state.logs.total = Number(data?.total ?? state.logs.items.length);
    if (data?.page_size) state.logs.page_size = Number(data.page_size);
    renderLogs();
    if (!silent) notify('已加载阅读手账。', 'info');
  });
}

async function loadReviews({ silent = false } = {}) {
  if (!silent) notify('正在读取审查队列与屏蔽词…', 'info');
  await readRoleData('reviews', {
    state: state.reviews.state,
    page: state.reviews.page,
    ...(state.reviews.source_kind ? { source_kind: state.reviews.source_kind } : {}),
    ...(state.reviews.query ? { query: state.reviews.query } : {}),
  }, $('#reviews-list'), data => {
    if (data?.identity && state.identity && data.identity !== state.identity) {
      throw new Error('审查队列属于其他角色，请刷新后重试。');
    }
    const identity = state.identity || data?.identity;
    state.reviews.items = Array.isArray(data?.items)
      ? data.items.filter(item => !item.identity || item.identity === identity)
      : [];
    state.reviews.blocked_topics = Array.isArray(data?.blocked_topics) ? data.blocked_topics : [];
    state.reviews.corrections = Array.isArray(data?.corrections) ? data.corrections : [];
    state.reviews.selectedIds.clear();
    state.reviews.page = Number(data?.page) || state.reviews.page;
    state.reviews.total = Number(data?.total ?? state.reviews.items.length);
    if (data?.page_size) state.reviews.page_size = Number(data.page_size);
    renderReviews();
    if (!silent) notify('已更新审查队列与屏蔽词。', 'info');
  });
}

async function refreshActiveTab({ silent = false } = {}) {
  const role = roleEpoch;
  await loadOverview({ silent });
  if (role !== roleEpoch) return;
  if (state.tab === 'sources') await loadSources({ silent: true });
  else if (state.tab === 'logs') await loadLogs({ silent: true });
  else if (state.tab === 'reviews') await loadReviews({ silent: true });
}

function readSharedPersona() {
  try {
    const uiVal = window.DynamicsUI?.getSharedPersona?.();
    if (typeof uiVal === 'string' && uiVal.trim()) return uiVal.trim();
  } catch {}
  try {
    const ctx = getBridge()?.getContext?.() || {};
    const ctxVal = ctx.persona || ctx.sharedPersona || ctx.persona_id || '';
    if (typeof ctxVal === 'string' && ctxVal.trim()) return ctxVal.trim();
  } catch {}
  try {
    const q = new URLSearchParams(location.search).get('persona');
    if (q && q.trim()) return q.trim();
  } catch {}
  return '';
}

function resolvePersonaIdFromIdentity(identity) {
  const matched = state.personas.find(p => String(p.identity) === String(identity));
  if (matched?.persona_id) return String(matched.persona_id).trim();
  const raw = String(identity || '').trim();
  if (raw.startsWith('[')) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed[0]) return String(parsed[0]).trim();
    } catch {}
  }
  return raw;
}

function matchPersonaIdentity(rawPersona) {
  const target = String(rawPersona || '').trim();
  if (!target) return '';
  const exact = state.personas.find(
    p => String(p.identity) === target || String(p.persona_id) === target || String(p.label) === target,
  );
  return exact ? String(exact.identity) : '';
}

function publishSharedPersona(identity) {
  const personaId = resolvePersonaIdFromIdentity(identity);
  state.sharedPersona = personaId;
  try {
    window.DynamicsUI?.setSharedPersona?.(personaId, { syncUrl: false, emit: true });
  } catch {}
}

function applyExternalPersona(rawPersona) {
  const clean = String(rawPersona || '').trim();
  if (!clean) return false;
  state.sharedPersona = clean;
  const matchedIdentity = matchPersonaIdentity(clean);
  if (matchedIdentity && matchedIdentity !== state.identity) {
    changePersona(matchedIdentity, { syncShared: false });
    return true;
  }
  return false;
}

function changePersona(identity, { syncShared = true } = {}) {
  if (identity === state.identity) return;
  roleEpoch += 1;
  state.identity = identity;
  if (syncShared) publishSharedPersona(identity);
  state.overview = null;
  state.sources = [];
  state.books = [];
  Object.assign(state.logs, { items: [], page: 1, total: 0 });
  Object.assign(state.reviews, { items: [], blocked_topics: [], corrections: [], page: 1, total: 0 });
  state.reviews.selectedIds.clear();
  state.decayPreviewDays = null;
  state.editingSource = null;
  const ebookInput = $('#ebook-file-input');
  if (ebookInput) ebookInput.value = '';
  state.drawerDirty = false;
  archivePage = 1;
  const archive = $('#archive-panel');
  if (archive) delete archive.dataset.ready;
  for (const id of [
    'kpi-grid', 'interests-list', 'questions-list', 'health-panel', 'presets-decay-panel',
    'outreach-diagnostics-panel', 'archive-panel', 'sources-list', 'bookshelf-list',
    'logs-list', 'reviews-list', 'corrections-list', 'blocked-topics-list', 'pub-book-results',
  ]) {
    const node = $(`#${id}`);
    node?.replaceChildren();
    node?.setAttribute('aria-busy', 'false');
  }
  for (const id of ['panel-overview', 'panel-sources']) $(`#${id}`)?.setAttribute('aria-busy', 'false');
  for (const id of ['source-drawer', 'book-modal']) {
    const dialog = $(`#${id}`);
    if (dialog?.open) dialog.close();
  }
  if ($('#block-topic-input')) $('#block-topic-input').value = '';
  for (const id of ['reviews-prev', 'reviews-next', 'logs-prev', 'logs-next']) {
    const button = $(`#${id}`);
    if (button) button.disabled = true;
  }
  for (const id of ['world-link-summary', 'reviews-page-info', 'logs-page-info']) {
    const node = $(`#${id}`);
    if (node) node.textContent = '';
  }
  for (const id of ['reviews', 'blocked', 'corrections', 'interests', 'questions', 'sources', 'books']) {
    const count = $(`#${id}-count-pill`);
    if (count) count.textContent = '0';
  }
  renderBadges();
  refreshActiveTab().catch(err => notify(err?.message || tr('读取失败'), 'error'));
}

/* ===== Tab Navigation ===== */
async function switchTab(tabName) {
  state.tab = tabName;
  for (const btn of $$('.side-nav [data-tab]')) {
    const active = btn.dataset.tab === tabName;
    btn.classList.toggle('selected', active);
    btn.setAttribute('aria-selected', String(active));
    if (active) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  for (const name of ['overview', 'sources', 'logs', 'reviews']) {
    const panel = $(`#panel-${name}`);
    if (panel) panel.hidden = name !== tabName;
  }
  try {
    if (tabName === 'overview') await loadOverview({ silent: true });
    else if (tabName === 'sources') await loadSources();
    else if (tabName === 'logs') await loadLogs();
    else if (tabName === 'reviews') await loadReviews();
  } catch (err) {
    notify(err?.message || tr('读取失败'), 'error');
  }
}

/* ===== Source / Manual Drawer Logic ===== */
function syncDrawerVisibility() {
  const kind = state.drawerKind;
  for (const btn of $$('#source-kind-tabs [data-src-kind]')) {
    btn.setAttribute('aria-pressed', String(btn.dataset.srcKind === kind));
    btn.disabled = Boolean(state.editingSource && btn.dataset.srcKind !== kind);
  }
  const isManualNew = kind === 'manual' && !state.editingSource;
  $('#fields-common').hidden = isManualNew;
  $('#fields-bilibili').hidden = kind !== 'bilibili';
  $('#fields-ebook').hidden = kind !== 'ebook';
  $('#fields-feed').hidden = kind !== 'feed';
  $('#fields-manual').hidden = kind !== 'manual';

  const biliMode = $('#bili-mode')?.value || 'up';
  $('#bili-uid-wrap').hidden = biliMode !== 'up';
  $('#bili-popular-wrap').hidden = biliMode !== 'popular';

  const ebookProv = $('#ebook-provider')?.value || 'wikisource';
  $('#ebook-page-wrap').hidden = ebookProv !== 'wikisource';
  $('#ebook-gutendex-wrap').hidden = ebookProv !== 'gutendex';
  $('#ebook-opds-wrap').hidden = ebookProv !== 'opds';

  setText($('#btn-submit-source'), msg(isManualNew ? '立即投喂' : '保存'));
}

function openSourceDrawer(kind = 'bilibili', existing = null) {
  state.editingSource = existing;
  state.drawerKind = existing?.source_kind || kind || 'bilibili';
  state.drawerDirty = false;
  $('#source-drawer-error').textContent = '';
  const testBox = $('#source-test-result');
  if (testBox) {
    testBox.hidden = true;
    testBox.className = 'test-result-box';
    testBox.replaceChildren();
  }

  const cfg = existing?.config || {};
  $('#source-drawer-title').textContent = existing
    ? `编辑信息源 · ${existing.name || ''}`
    : (state.drawerKind === 'manual' ? '手动剪报投喂' : '添加信息源');

  $('#src-name').value = existing?.name || '';
  $('#src-enabled').checked = existing ? existing.enabled !== false : true;

  $('#bili-mode').value = cfg.mode || 'up';
  $('#bili-uid').value = cfg.uid || '';
  $('#bili-popular-kind').value = cfg.popular_kind || cfg.category || 'hot';
  $('#bili-cookie').value = '';
  $('#bili-cookie').placeholder = (cfg.cookie || cfg.sessdata || cfg.has_cookie)
    ? '已保存掩码凭据，留空保持不变'
    : '仅刷推荐流或读取 AI 总结时需要，留空免登录运行';

  $('#ebook-provider').value = cfg.provider || cfg.mode || 'wikisource';
  $('#ebook-page').value = cfg.page || cfg.title || '';
  $('#ebook-book-id').value = cfg.book_id || cfg.gutenberg_id || cfg.download_url || cfg.query || '';
  $('#ebook-opds-url').value = cfg.url || '';
  $('#ebook-credential-origins').value = (cfg.credential_origins || []).join('\n');
  $('#ebook-allow-private').checked = Boolean(cfg.allow_private_opds);
  $('#ebook-opds-user').value = cfg.username || '';
  $('#ebook-opds-pass').value = '';
  $('#ebook-opds-pass').placeholder = (cfg.password || cfg.auth_token || cfg.has_password)
    ? '已保存掩码凭据，留空保持不变'
    : '留空不启用认证';

  $('#feed-url').value = cfg.url || '';

  $('#manual-title').value = cfg.title || '';
  $('#manual-creator').value = cfg.creator || '';
  $('#manual-url').value = cfg.url || '';
  $('#manual-text').value = cfg.text || '';

  syncDrawerVisibility();
  const dialog = $('#source-drawer');
  if (dialog && !dialog.open) dialog.showModal();
}

function buildSourceConfigFromDrawer(kind) {
  const prevCfg = state.editingSource?.config || {};
  let config = {};
  if (kind === 'bilibili') {
    const mode = $('#bili-mode').value;
    const uid = $('#bili-uid').value.trim();
    if (mode === 'up' && !uid) throw new Error('指定关注 UP 主模式需要填写 UID。');
    config = { mode };
    if (mode === 'up') config.uid = uid;
    if (mode === 'popular') config.popular_kind = $('#bili-popular-kind').value;
    const cookieInput = $('#bili-cookie').value.trim();
    if (cookieInput && !isMaskedSecret(cookieInput)) {
      config.cookie = cookieInput;
    } else if (state.editingSource && prevCfg.cookie) {
      config.cookie = '******';
    }
  } else if (kind === 'ebook') {
    const provider = prevCfg.provider === 'local' && state.editingSource ? 'local' : $('#ebook-provider').value;
    config = provider === 'local' ? { ...prevCfg, provider: 'local' } : { provider };
    if (provider === 'wikisource') {
      const page = $('#ebook-page').value.trim();
      if (!page) throw new Error('请填写维基文库页面标题。');
      config.page = page;
      config.title = page;
    } else if (provider === 'gutendex') {
      const bookRef = $('#ebook-book-id').value.trim();
      if (!bookRef) throw new Error('请填写 Gutendex 书籍正整数 ID 或下载链接。');
      if (/^https?:\/\//i.test(bookRef)) config.download_url = bookRef;
      else config.book_id = bookRef;
    } else if (provider === 'opds') {
      const url = $('#ebook-opds-url').value.trim();
      if (!url) throw new Error('请填写 OPDS 书库链接。');
      config.url = url;
      config.allow_private_opds = $('#ebook-allow-private').checked;
      config.credential_origins = $('#ebook-credential-origins').value.split(/\n/).map(v => v.trim()).filter(Boolean);
      const username = $('#ebook-opds-user').value.trim();
      const password = $('#ebook-opds-pass').value.trim();
      if (username) config.username = username;
      if (password && !isMaskedSecret(password)) {
        config.password = password;
      } else if (state.editingSource && prevCfg.password) {
        config.password = '******';
      }
    }
  } else if (kind === 'feed') {
    const url = $('#feed-url').value.trim();
    if (!url) throw new Error('请填写 RSS / Atom / JSON Feed 订阅地址。');
    config = { url };
  } else if (kind === 'manual') {
    config = {
      title: $('#manual-title').value.trim(),
      creator: $('#manual-creator').value.trim(),
      url: $('#manual-url').value.trim(),
      text: $('#manual-text').value.trim(),
    };
  }
  return config;
}

async function runSourceConnectionTest() {
  const errNode = $('#source-drawer-error');
  const box = $('#source-test-result');
  const btn = $('#btn-test-source');
  if (!box || !btn) return;
  errNode.textContent = '';
  btn.disabled = true;
  box.hidden = false;
  box.className = 'test-result-box';
  box.replaceChildren(el('p', '', '正在执行连接测试并试抓 1 条样本…'));
  try {
    const kind = state.drawerKind;
    const config = buildSourceConfigFromDrawer(kind);
    const res = await apiPost('sources', {
      op: 'test_source',
      identity: state.identity,
      source_kind: kind,
      config,
    });
    box.replaceChildren();
    if (res?.ok) {
      box.className = 'test-result-box is-ok';
      const previews = Array.isArray(res.preview_items) ? res.preview_items : [];
      box.append(el('strong', '', `连接成功（试抓到 ${Number(res.count ?? previews.length)} 条候选）`));
      for (const item of previews.slice(0, 2)) {
        const line = [item.title, item.creator, item.bvid].filter(Boolean).join(' · ');
        box.append(el('p', '', `样本：${line}`));
      }
      if (res.advice) box.append(el('p', 'advice-note', `提示：${res.advice}`));
    } else {
      box.className = 'test-result-box is-err';
      box.append(el('strong', '', `连接测试未通过：${res?.error || res?.status || '未知异常'}`));
      const advice = res?.advice || formatSourceErrorAdvice(res?.error || res?.status, kind);
      if (advice) box.append(el('p', 'advice-note', `处置建议：${advice}`));
    }
  } catch (err) {
    if (err !== ROLE_CHANGED) {
      box.className = 'test-result-box is-err';
      const errMsg = err?.message || tr('请求失败');
      box.replaceChildren(
        el('strong', '', `连接测试失败：${errMsg}`),
        el('p', 'advice-note', `处置建议：${formatSourceErrorAdvice(errMsg, state.drawerKind)}`),
      );
    }
  } finally {
    btn.disabled = false;
  }
}

async function submitSourceForm(ev) {
  ev.preventDefault();
  const errNode = $('#source-drawer-error');
  errNode.textContent = '';
  const submitBtn = $('#btn-submit-source');
  submitBtn.disabled = true;

  try {
    const kind = state.drawerKind;
    if (kind === 'manual' && !state.editingSource) {
      const title = $('#manual-title').value.trim();
      const creator = $('#manual-creator').value.trim();
      const url = $('#manual-url').value.trim();
      const text = $('#manual-text').value.trim();
      if (!text && !url) throw new Error('请填写剪报正文内容或公开文章 URL。');
      await apiPost('sources', {
        op: 'manual_feed',
        identity: state.identity,
        title: title || '管理员案头剪报',
        creator,
        text,
        url,
      });
      state.drawerDirty = false;
      $('#source-drawer').close();
      notify('已投喂案头读物并送入沙箱清洗候选池。', 'success');
      await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
      return;
    }

    const name = $('#src-name').value.trim();
    if (!name) throw new Error('请填写信息源名称。');
    const enabled = $('#src-enabled').checked;
    const config = buildSourceConfigFromDrawer(kind);

    const payload = state.editingSource
      ? {
          op: 'update',
          source_id: state.editingSource.source_id,
          identity: state.editingSource.identity,
          source_kind: kind,
          name,
          enabled,
          config,
        }
      : {
          op: 'create',
          identity: state.identity,
          source_kind: kind,
          name,
          enabled,
          config,
        };

    await apiPost('sources', payload);
    state.drawerDirty = false;
    $('#source-drawer').close();
    notify(state.editingSource ? `已更新信息源「${name}」。` : `已创建信息源「${name}」。`, 'success');
    await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
  } catch (err) {
    if (err !== ROLE_CHANGED) errNode.textContent = err?.message || tr('请求失败');
  } finally {
    submitBtn.disabled = false;
  }
}

/* ===== Public Ebook Search & Local Upload Modal ===== */
function buildPublicBookSourceConfig(provider, book) {
  if (book?.config && typeof book.config === 'object') {
    return { ...book.config, provider: book.config.provider || provider };
  }
  const cfg = {
    provider,
    mode: provider,
    title: String(book.title || ''),
    creator: String(book.creator || book.author || ''),
  };
  if (provider === 'wikisource') {
    cfg.page = String(book.page || book.title || '');
    if (book.language) cfg.language = String(book.language);
    if (book.url || book.raw_url) cfg.url = String(book.url || book.raw_url);
  } else {
    const rawId = String(book.id ?? book.gutenberg_id ?? book.book_id ?? '').replace(/^gutendex:/i, '');
    if (rawId) cfg.book_id = rawId;
    if (book.language) cfg.language = String(book.language);
    if (book.download_url || book.text_url || book.url) {
      cfg.download_url = String(book.download_url || book.text_url || book.url);
    }
  }
  return cfg;
}

async function runPublicBookSearch() {
  const provider = $('#pub-book-provider').value;
  const language = $('#pub-book-lang').value;
  const query = $('#pub-book-query').value.trim();
  const fb = $('#book-modal-feedback');
  const resultsBox = $('#pub-book-results');
  if (!query) {
    feedback(fb, '请输入书名或作者关键词后再搜索。', 'warning');
    return;
  }
  feedback(fb, '正在检索公版书目…', 'info');
  resultsBox.replaceChildren();

  const res = await apiPost('sources', {
    op: 'search_public_books',
    identity: state.identity,
    provider,
    query,
    language,
  });
  const books = Array.isArray(res?.books) ? res.books : [];
  if (books.length === 0) {
    feedback(fb, '未找到匹配的公版书，请尝试其他关键词或语种。', 'info');
    return;
  }
  feedback(fb, `检索到 ${books.length} 本公版书目，可一键加入当前角色书架。`, 'success');
  for (const book of books) {
    const row = el('article', 'item-card');
    const head = el('div', 'item-head');
    const info = el('div', 'item-title-group');
    info.append(el('h4', 'item-title', book.title || '未命名书目'));
    const author = book.creator || (Array.isArray(book.authors) ? book.authors.join(', ') : '');
    if (author) info.append(el('span', 'pill', String(author)));
    if (book.language) info.append(el('span', 'pill info', String(book.language)));

    const addBtn = actionButton(msg('加入角色书架'), async () => {
      await apiPost('sources', {
        op: 'create',
        identity: state.identity,
        source_kind: 'ebook',
        name: book.title || '公版书连载',
        enabled: true,
        config: buildPublicBookSourceConfig(provider, book),
      });
      feedback(fb, `已将《${book.title}》加入当前角色书架！`, 'success');
      await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
    }, 'wb-button primary');

    head.append(info, addBtn);
    row.append(head);
    if (book.summary || book.snippet) {
      row.append(el('p', 'item-body', String(book.summary || book.snippet)));
    }
    resultsBox.append(row);
  }
}

async function handleEbookUpload() {
  const input = $('#ebook-file-input');
  const fb = $('#book-modal-feedback');
  const role = roleEpoch;
  const identity = state.identity;
  const file = input?.files?.[0];
  if (!file) {
    feedback(fb, '请先选择本地 .epub 或 .txt 文件。', 'warning');
    return;
  }
  const lower = file.name.toLowerCase();
  if (!lower.endsWith('.epub') && !lower.endsWith('.txt')) {
    feedback(fb, '仅支持上传 .epub 或 .txt 电子书文件。', 'error');
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    feedback(fb, '电子书体积超过 10MB 限制。', 'error');
    return;
  }
  feedback(fb, `正在编码并上传《${file.name}》…`, 'info');
  let buffer;
  try {
    buffer = await file.arrayBuffer();
  } catch (err) {
    if (role !== roleEpoch) throw ROLE_CHANGED;
    throw err;
  }
  if (role !== roleEpoch) throw ROLE_CHANGED;
  const bytes = new Uint8Array(buffer);
  const content_base64 = uint8ToBase64(bytes);
  await apiPost('sources', {
    op: 'upload_ebook',
    identity,
    filename: file.name,
    content_base64,
  });
  input.value = '';
  feedback(fb, `《${file.name}》已切片入库并加入角色连载书架！`, 'success');
  notify(`已上传本地电子书《${file.name}》。`, 'success');
  await Promise.all([loadSources({ silent: true }), loadOverview({ silent: true })]);
}

const HEALTH_LABELS = {disabled:'插件已停用',analysis_provider_missing:'请配置分析模型',review_provider_missing:'请配置审查模型',independent_review_provider_required:'分析与审查需要不同模型',chat_dynamics_missing:'未接入群间',chat_dynamics_bridge_missing:'群间见闻联动模块缺失',world_state_unavailable:'世界状态暂不可用',world_paused:'角色生活已暂停',world_asleep:'角色正在睡眠',world_busy:'角色正在忙碌',world_in_transit:'角色正在旅途中',quiet_hours:'当前为静默时段',character_profile_missing:'缺少有效角色设定',world_bookmark_capability_missing:'世界书签接口缺失'};
function renderHealth(h) {
  const box = $('#health-panel');
  if (!box) return;
  if (!h) {
    const emptyWrap = el('div', 'health-summary-bar');
    const left = el('div', 'health-summary-left');
    left.append(
      el('span', 'health-summary-title', msg('运行健康')),
      el('span', 'health-summary-meta', msg('等待状态信息')),
    );
    emptyWrap.append(left);
    box.replaceChildren(emptyWrap);
    return;
  }

  const d = h.diagnostics || {};
  const blockers = Array.isArray(h.blockers) ? h.blockers : [];
  const activeReviewJobs = (d.review_jobs || []).filter(j => j.kind !== 'done');
  const pendingCorrections = Array.isArray(d.corrections) ? d.corrections : [];
  const storageMb = (Number(d.storage_bytes || 0) / 1048576).toFixed(1);
  const quota = Number(h.model_call_quota) || 50;
  const callsToday = Number(d.model_calls_today) || 0;
  const storageOver = Number(d.storage_bytes || 0) > (h.storage_warning_mb || 512) * 1048576;

  const disclosure = el('details', 'health-disclosure');
  if (h.last_error || pendingCorrections.length > 0 || activeReviewJobs.length > 0 || storageOver) {
    disclosure.open = true;
  }

  // Compact summary row (matches prototype!)
  const summary = el('summary', 'health-summary-bar');
  const left = el('div', 'health-summary-left');
  left.append(el('span', 'health-summary-title', msg('运行健康')));
  left.append(
    el(
      'span',
      `pill ${callsToday >= quota ? 'warn' : 'accent'}`,
      `今日模型 ${callsToday}/${quota}`,
    ),
  );
  for (const reason of blockers) {
    left.append(el('span', 'pill warn', msg(HEALTH_LABELS[reason] || reason)));
  }

  const policyShort = h.review_context_policy === 'merged_self_review'
    ? '同模型合并自审'
    : h.review_context_policy === 'independent_calls'
      ? '同模型独立双调用'
      : '双模型独立审查';
  const metaParts = [`存储 ${storageMb} MB`, policyShort];
  if (Number(d.archived_candidates) > 0) metaParts.push(`已归档 ${d.archived_candidates}`);
  if (h.next_digest_at) metaParts.push(`下次检查 ${formatTime(h.next_digest_at)}`);
  left.append(el('span', 'health-summary-meta', metaParts.join(' · ')));

  const toggle = el('span', 'health-toggle-label', msg('展开诊断详情'));
  summary.append(left, toggle);

  // Collapsible diagnostic details body
  const body = el('div', 'health-details-body');
  const grid = el('div', 'health-details-grid');

  const modelBox = el('div', 'health-detail-box');
  modelBox.append(
    el('strong', '', msg('模型与审查策略')),
    el('p', '', msg('分析：{0}；审查：{1}', h.analysis_provider || msg('未配置'), h.review_provider || msg('未配置'))),
  );
  if ('video_provider' in h || 'video_provider_explicit' in h || 'video_provider_fallback' in h) {
    const explicit = String(h.video_provider_explicit || '').trim();
    const effective = String(h.video_provider || '').trim();
    const isFallback = h.video_provider_fallback === true || (!explicit && h.video_provider_fallback !== false);
    let videoLabel;
    if (explicit) {
      videoLabel = explicit;
    } else if (effective) {
      videoLabel = isFallback ? `${effective}（未单独配置，回退分析模型）` : effective;
    } else {
      videoLabel = '未配置（回退分析模型）';
    }
    const stt = String(h.video_audio_stt_provider || '').trim();
    const lineText = stt ? `视频精读：${videoLabel}；音频转写：${stt}` : `视频精读：${videoLabel}`;
    modelBox.append(el('p', 'health-meta-line', lineText));
  }
  if (h.review_context_policy === 'independent_calls') {
    modelBox.append(el('p', '', msg('审查采用独立调用，不继承分析对话的历史上下文。')));
  } else {
    modelBox.append(el('p', '', msg('分析与审查模型相互隔离，不继承群聊历史上下文。')));
  }

  const schedBox = el('div', 'health-detail-box');
  const sharingSummary = h.proactive_enabled === false || h.proactive_scope === 'disabled'
    ? statusMsg('sharingDisabled')
    : h.proactive_scope === 'explicit'
      ? statusMsg('sharingSelectedChats')
      : statusMsg('sharingAllChats');
  schedBox.append(
    el('strong', '', msg('调度与主动分享')),
    el('p', '', msg('下次采集：{0}；下次阅读检查：{1}', formatTime(h.next_poll_at), formatTime(h.next_digest_at))),
    el('p', '', msg('今日模型调用 {0}/{1} · 已归档候选 {2} · 存储 {3} MB', callsToday, quota, d.archived_candidates || 0, storageMb)),
    el('p', '', sharingSummary),
  );
  if (Number(d.anli_today_total) > 0) {
    schedBox.append(el('p', 'health-meta-line', `今日群友 /安利 投喂：${Number(d.anli_today_total)} 条`));
  }

  grid.append(modelBox, schedBox);

  // Stage-split Token/Call Cost & Video Channel Hit/Fallback Stats
  const costRows = Array.isArray(d.cost_by_stage) ? d.cost_by_stage : [];
  const vStats = d.video_channel_stats && typeof d.video_channel_stats === 'object' ? d.video_channel_stats : null;
  if (costRows.length > 0 || (vStats && Number(vStats.total) > 0)) {
    const costBox = el('div', 'health-detail-box');
    costBox.append(el('strong', '', '分阶段调用与 Token 成本（近 7 日）'));
    if (costRows.length > 0) {
      const table = el('table', 'cost-table');
      const thead = el('thead');
      const hr = el('tr');
      for (const htxt of ['阶段', '调用', 'Prompt', 'Completion', '总 Token']) {
        hr.append(el('th', '', htxt));
      }
      thead.append(hr);
      const tbody = el('tbody');
      for (const row of costRows) {
        const trNode = el('tr');
        trNode.append(
          el('td', '', STAGE_LABELS[row.stage] || row.stage),
          el('td', '', `${row.calls} 次`),
          el('td', '', String(Number(row.prompt_tokens) || 0)),
          el('td', '', String(Number(row.completion_tokens) || 0)),
          el('td', '', String(Number(row.total_tokens) || 0)),
        );
        tbody.append(trNode);
      }
      table.append(thead, tbody);
      costBox.append(table);
    } else {
      costBox.append(el('p', 'health-meta-line', '近 7 日暂无分阶段模型调用记录。'));
    }

    const videoBox = el('div', 'health-detail-box');
    videoBox.append(el('strong', '', '视频通道命中与降级率'));
    if (vStats && Number(vStats.total) > 0) {
      const hitPct = Math.round((Number(vStats.native_hit_rate) || 0) * 100);
      const fbPct = Math.round((Number(vStats.fallback_rate) || 0) * 100);
      videoBox.append(
        el('p', '', `原生直传命中率 ${hitPct}% · 降级率 ${fbPct}%（共 ${vStats.total} 条视频手账）`),
      );
      const bars = el('div', 'video-channel-bars');
      bars.append(
        el('span', 'pill accent', `原生直传 ${Number(vStats.native_video) || 0}`),
        el('span', 'pill info', `关键帧抽帧 ${Number(vStats.keyframes) || 0}`),
        el('span', 'pill', `雪碧图 ${Number(vStats.videoshot) || 0}`),
        el('span', 'pill warn', `纯字幕/转写 ${(Number(vStats.subtitle_only) || 0) + (Number(vStats.audio_transcript) || 0)}`),
      );
      videoBox.append(bars);
    } else {
      videoBox.append(el('p', 'health-meta-line', '暂无视频精读通道统计样本。'));
    }
    grid.append(costBox, videoBox);
  }

  body.append(grid);

  if (h.last_error) {
    if (readingCharacterChanged(h)) body.append(el('p', 'health-meta-line', statusMsg('characterChanged')));
    const errDetails = el('details', 'item-card');
    errDetails.append(
      el('summary', '', statusMsg('diagnostics')),
      el('code', '', h.last_error),
    );
    body.append(errDetails);
  }
  if (storageOver) {
    body.append(el('p', 'pill warn', '存储超过告警阈值；只归档，不自动删除，可导出后人工管理。'));
  }
  if (d.feedback_jobs?.length) {
    const reasons = {
      verified: '已核实讨论', not_recorded: '未形成共同话题', insufficient_evidence: '原话依据不足',
      unrelated: '未讨论候选话题', no_verified_opening: '缺少已核实的分享', model_failure: '模型调用失败',
      invalid_result: '模型结果格式错误', storage_failure: '写入失败', processing_failure: '处理失败', timeout: '超时',
      quota_wait: '等待额度', legacy: '未记录原因',
    };
    const statuses = { held: '已挂起', done: '已完成', pending: '等待处理', running: '处理中' };
    const section = el('details', 'item-card');
    section.append(el('summary', '', msg('讨论反馈')));
    for (const group of d.feedback_jobs) {
      section.append(el('p', 'item-meta', [
        msg(statuses[group.status] || '未知状态'),
        msg(reasons[group.outcome] || '未记录原因'),
        String(group.count),
      ].join(' · ')));
    }
    body.append(section);
  }
  for (const job of activeReviewJobs) {
    const line = el('div', 'item-actions', `${job.kind} · 重试 ${job.attempts} · ${formatTime(job.next_at)} ${job.last_error || ''}`);
    if (job.review_reason !== 'source_changed') {
      line.append(actionButton(msg('重试审核'), async () => {
        await apiPost('reviews', {
          op: 'retry_review',
          proposal_id: job.proposal_id,
          identity: job.identity || state.identity,
          revision: job.revision,
        });
        notify('已将提案加入审核队列。', 'success');
        await loadOverview({ silent: true });
      }));
    }
    body.append(line);
  }
  for (const job of d.world_pending || []) {
    body.append(el('p', 'item-meta', `书签同步 ${job.operation} · ${job.attempts} 次 · ${job.last_error || '待处理'}`));
  }
  if (pendingCorrections.length > 0) {
    const corrHint = el('div', 'item-actions');
    corrHint.append(
      el('span', 'pill warn', `待处理群友纠错 ${pendingCorrections.length} 项`),
      actionButton('前往「审查与黑名单」处理', async () => {
        await switchTab('reviews');
      }),
    );
    body.append(corrHint);
  }

  disclosure.append(summary, body);
  box.replaceChildren(disclosure);
}
let archivePage = 1, archiveMode = true;
function renderArchiveControls() {
  const box = $('#archive-panel');
  if (!box || box.dataset.ready === state.identity) return;
  box.dataset.ready = state.identity;

  const disclosure = el('details', 'archive-disclosure');
  const summary = el('summary', 'archive-summary-bar');
  const summaryLeft = el('div', 'health-summary-left');
  summaryLeft.append(
    el('span', 'health-summary-title', msg('候选与归档')),
    el('span', 'health-summary-meta', msg('检索活跃候选、历史归档或导出 JSON')),
  );
  summary.append(summaryLeft, el('span', 'health-toggle-label', msg('展开检索')));

  const body = el('div', 'archive-body');
  const input = el('input');
  setText(input, msg('按标题或摘要查询'), 'placeholder');
  setText(input, msg('候选查询'), 'aria-label');
  const results = el('div', 'stack-list');
  const actions = el('div', 'item-actions');
  const identity = state.identity;
  let maxPage = 1;
  async function load(page = archivePage) {
    const valid = nextTicket('archive');
    const data = await apiGet('sources', { identity, candidates: '1', archived: archiveMode ? '1' : '0', page, query: input.value });
    if (!valid()) return;
    archivePage = Number(data.page) || page;
    maxPage = Math.max(1, Math.ceil(data.total / data.page_size));
    previousPage.disabled = archivePage <= 1;
    nextPage.disabled = archivePage >= maxPage;
    results.replaceChildren(el('p', 'page-info', msg('第 {0} / {1} 页 · 共 {2} 条', archivePage, maxPage, data.total)));
    for (const item of data.items || []) {
      const row = el('details', 'item-card');
      row.append(el('summary', '', item.title));
      row.append(el('p', 'item-body', item.full_text || item.summary));
      const rowActions = el('div', 'item-actions');
      rowActions.append(
        actionButton(item.pinned ? '取消置顶' : '置顶', async () => {
          await apiPost('reviews', { op: 'candidate_update', identity, candidate_id: item.candidate_id, pinned: !item.pinned });
          await load();
        }),
        actionButton(item.archived ? '恢复至活跃池' : '归档', async () => {
          await apiPost('reviews', { op: 'candidate_update', identity, candidate_id: item.candidate_id, archived: !item.archived });
          await load();
        }),
      );
      row.append(rowActions);
      results.append(row);
    }
    return data;
  }
  const previousPage = actionButton(msg('上一页'), () => load(archivePage - 1), 'wb-button', () => archivePage <= 1);
  const nextPage = actionButton(msg('下一页'), () => load(archivePage + 1), 'wb-button', () => archivePage >= maxPage);
  actions.append(
    actionButton(msg('查询归档'), async () => { archiveMode = true; archivePage = 1; await load(); }),
    actionButton(msg('查看活跃候选'), async () => { archiveMode = false; archivePage = 1; await load(); }),
    previousPage,
    nextPage,
    actionButton(msg('导出查询结果'), async () => {
      const valid = nextTicket('archiveExport');
      const archived = archiveMode ? '1' : '0';
      const query = input.value;
      const items = new Map();
      for (let page = 1; ; page += 1) {
        const data = await apiGet('sources', { identity, candidates: '1', archived, page, query });
        if (!valid()) return;
        if (Number(data.page) !== page || !data.items.length) break;
        for (const item of data.items) items.set(item.candidate_id, item);
        if (page >= Math.ceil(data.total / data.page_size)) break;
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify([...items.values()], null, 2)], { type: 'application/json' }));
      const a = el('a');
      a.href = url;
      a.download = 'horizons-candidates.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }),
  );
  body.append(input, actions, results);
  disclosure.append(summary, body);
  box.replaceChildren(disclosure);
}

/* ===== Event Wiring & Bootstrap ===== */
function bindEvents() {
  // Guard dirty edits on cross-plugin navigation
  window.ChatDynamicsBeforeNavigate = async () => {
    if ($('#source-drawer')?.open && state.drawerDirty) {
      return await confirmAction(msg('此页有编辑操作。请确认已保存，再打开其他面板。'));
    }
    return true;
  };

  // Tabs + Arrow key support
  const tabs = $$('.side-nav [data-tab]');
  for (const btn of tabs) {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    btn.addEventListener('keydown', ev => {
      if (!['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(ev.key)) return;
      ev.preventDefault();
      const idx = tabs.indexOf(btn);
      const delta = (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') ? 1 : -1;
      const next = tabs[(idx + delta + tabs.length) % tabs.length];
      next.focus();
      switchTab(next.dataset.tab);
    });
  }

  // Refresh & Persona Switch
  $('#btn-refresh')?.addEventListener('click', () => {
    refreshActiveTab().catch(err => notify(err?.message || tr('读取失败'), 'error'));
  });

  $('#persona-select')?.addEventListener('change', ev => changePersona(ev.target.value));

  // Trigger Digest
  $('#btn-trigger-digest')?.addEventListener('click', async () => {
    const btn = $('#btn-trigger-digest');
    btn.disabled = true;
    try {
      await apiPost('reviews', { op: 'trigger_digest', identity: state.identity });
      notify('已触发一次挑拣与精读流水线。', 'success');
      await loadOverview({ silent: true });
    } catch (err) {
      if (err !== ROLE_CHANGED) notify(err?.message || tr('请求失败'), 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // Source Drawer Controls
  $('#btn-open-source-drawer')?.addEventListener('click', () => openSourceDrawer('bilibili', null));
  $('#btn-open-manual-drawer')?.addEventListener('click', () => openSourceDrawer('manual', null));
  $('#btn-close-source-drawer')?.addEventListener('click', () => $('#source-drawer')?.close());
  $('#btn-cancel-source')?.addEventListener('click', () => $('#source-drawer')?.close());
  $('#btn-test-source')?.addEventListener('click', runSourceConnectionTest);
  $('#source-form')?.addEventListener('input', () => { state.drawerDirty = true; });
  $('#source-form')?.addEventListener('submit', submitSourceForm);

  for (const btn of $$('#source-kind-tabs [data-src-kind]')) {
    btn.addEventListener('click', () => {
      if (state.editingSource) return;
      state.drawerKind = btn.dataset.srcKind;
      syncDrawerVisibility();
    });
  }
  $('#bili-mode')?.addEventListener('change', syncDrawerVisibility);
  $('#ebook-provider')?.addEventListener('change', syncDrawerVisibility);

  // Public Book Search & Upload Modal Controls
  $('#btn-open-book-modal')?.addEventListener('click', () => {
    $('#book-modal-feedback').textContent = '';
    const modal = $('#book-modal');
    if (modal && !modal.open) modal.showModal();
  });
  $('#btn-close-book-modal')?.addEventListener('click', () => $('#book-modal')?.close());
  $('#btn-search-books')?.addEventListener('click', async () => {
    const btn = $('#btn-search-books');
    btn.disabled = true;
    try {
      await runPublicBookSearch();
    } catch (err) {
      if (err !== ROLE_CHANGED) feedback($('#book-modal-feedback'), err?.message || tr('请求失败'), 'error');
    } finally {
      btn.disabled = false;
    }
  });
  $('#pub-book-query')?.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      $('#btn-search-books')?.click();
    }
  });
  $('#btn-upload-ebook')?.addEventListener('click', async () => {
    const btn = $('#btn-upload-ebook');
    btn.disabled = true;
    try {
      await handleEbookUpload();
    } catch (err) {
      if (err !== ROLE_CHANGED) feedback($('#book-modal-feedback'), err?.message || tr('请求失败'), 'error');
    } finally {
      btn.disabled = false;
    }
  });

  // Logs Search, Filter & Pagination
  const triggerLogsSearch = () => {
    state.logs.query = $('#logs-search-input')?.value.trim() || '';
    state.logs.page = 1;
    loadLogs().catch(err => notify(err?.message || tr('读取失败'), 'error'));
  };
  $('#btn-logs-search')?.addEventListener('click', triggerLogsSearch);
  $('#logs-search-input')?.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      triggerLogsSearch();
    }
  });
  for (const btn of $$('#logs-filter-group [data-log-kind]')) {
    btn.addEventListener('click', () => {
      state.logs.source_kind = btn.dataset.logKind || '';
      state.logs.page = 1;
      loadLogs().catch(err => notify(err?.message || tr('读取失败'), 'error'));
    });
  }
  $('#logs-prev')?.addEventListener('click', () => {
    if (state.logs.page > 1) {
      state.logs.page -= 1;
      loadLogs().catch(err => notify(err?.message || tr('读取失败'), 'error'));
    }
  });
  $('#logs-next')?.addEventListener('click', () => {
    state.logs.page += 1;
    loadLogs().catch(err => notify(err?.message || tr('读取失败'), 'error'));
  });

  // Reviews Search, Source Filter, Batch Actions, Pagination & Blocklist
  const triggerReviewsSearch = () => {
    state.reviews.query = $('#reviews-search-input')?.value.trim() || '';
    state.reviews.source_kind = $('#reviews-source-kind')?.value || '';
    state.reviews.page = 1;
    loadReviews().catch(err => notify(err?.message || tr('读取失败'), 'error'));
  };
  $('#btn-reviews-search')?.addEventListener('click', triggerReviewsSearch);
  $('#reviews-source-kind')?.addEventListener('change', triggerReviewsSearch);
  $('#reviews-search-input')?.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      triggerReviewsSearch();
    }
  });
  $('#reviews-select-all')?.addEventListener('change', ev => {
    const checked = Boolean(ev.target.checked);
    state.reviews.selectedIds.clear();
    if (checked) {
      for (const item of state.reviews.items || []) {
        const st = item.state || 'pending_review';
        if (['pending_review', 'pending', 'held'].includes(st) && item.review_reason !== 'source_changed') {
          state.reviews.selectedIds.add(item.proposal_id);
        }
      }
    }
    for (const cb of $$('#reviews-list .review-select-cb')) cb.checked = checked;
    syncBatchSelectionUI();
  });
  const runBatchReview = async opName => {
    const ids = [...state.reviews.selectedIds];
    if (!ids.length) return;
    const itemMap = new Map((state.reviews.items || []).map(i => [i.proposal_id, i]));
    const items = ids.map(pid => ({ proposal_id: pid, revision: itemMap.get(pid)?.revision }));
    const btn = opName === 'batch_approve' ? $('#btn-batch-approve') : $('#btn-batch-reject');
    if (btn) btn.disabled = true;
    try {
      const res = await apiPost('reviews', {
        op: opName,
        identity: state.identity,
        items,
      });
      notify(
        `${opName === 'batch_approve' ? '批量批准' : '批量拒绝'}完成：成功 ${Number(res?.succeeded) || 0} 项${Number(res?.failed) ? `，失败 ${res.failed} 项` : ''}。`,
        Number(res?.failed) ? 'warning' : 'success',
      );
      await Promise.all([loadReviews({ silent: true }), loadOverview({ silent: true })]);
    } catch (err) {
      if (err !== ROLE_CHANGED) notify(err?.message || tr('请求失败'), 'error');
    } finally {
      syncBatchSelectionUI();
    }
  };
  $('#btn-batch-approve')?.addEventListener('click', () => runBatchReview('batch_approve'));
  $('#btn-batch-reject')?.addEventListener('click', () => runBatchReview('batch_reject'));

  for (const btn of $$('#reviews-filter-group [data-review-state]')) {
    btn.addEventListener('click', () => {
      state.reviews.state = btn.dataset.reviewState || '';
      state.reviews.page = 1;
      loadReviews().catch(err => notify(err?.message || tr('读取失败'), 'error'));
    });
  }
  $('#reviews-prev')?.addEventListener('click', () => {
    if (state.reviews.page > 1) {
      state.reviews.page -= 1;
      loadReviews().catch(err => notify(err?.message || tr('读取失败'), 'error'));
    }
  });
  $('#reviews-next')?.addEventListener('click', () => {
    state.reviews.page += 1;
    loadReviews().catch(err => notify(err?.message || tr('读取失败'), 'error'));
  });

  const submitBlockTopic = async () => {
    const btn = $('#btn-add-block-topic');
    if (!btn || btn.disabled) return;
    const input = $('#block-topic-input');
    const topic = input?.value.trim() || '';
    if (!topic) {
      notify('请输入要屏蔽的话题词。', 'warning');
      return;
    }
    btn.disabled = true;
    try {
      await apiPost('reviews', { op: 'block_topic', identity: state.identity, topic });
      input.value = '';
      notify(`已添加屏蔽话题词「${topic}」。`, 'success');
      await loadReviews({ silent: true });
    } catch (err) {
      if (err !== ROLE_CHANGED) notify(err?.message || tr('请求失败'), 'error');
    } finally {
      btn.disabled = false;
    }
  };
  $('#btn-add-block-topic')?.addEventListener('click', submitBlockTopic);
  $('#block-topic-input')?.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      submitBlockTopic();
    }
  });

  // Close compact (i) info popovers on outside click or Escape
  document.addEventListener('click', ev => {
    for (const pop of $$('.info-popover[open]')) {
      if (!pop.contains(ev.target)) pop.open = false;
    }
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') {
      for (const pop of $$('.info-popover[open]')) pop.open = false;
    }
  });

  // Handle same-plugin Ctrl+K federated search deep links from series-panels.js v2
  window.addEventListener('dynamics-cmdk-deeplink', ev => {
    const item = ev?.detail || {};
    const dl = item.deep_link;
    const validTabs = new Set(['overview', 'sources', 'logs', 'reviews']);
    let targetTab = '';
    let targetIdentity = '';
    if (dl && typeof dl === 'object') {
      if (typeof dl.tab === 'string' && validTabs.has(dl.tab)) targetTab = dl.tab;
      if (typeof dl.identity === 'string' && dl.identity.trim()) targetIdentity = dl.identity.trim();
    } else if (typeof dl === 'string') {
      const prefix = dl.split(':', 1)[0];
      if (validTabs.has(prefix)) targetTab = prefix;
    }
    if (!targetTab && typeof item.target_id === 'string') {
      const prefix = item.target_id.split(':', 1)[0];
      if (validTabs.has(prefix)) targetTab = prefix;
    }
    if (targetIdentity && targetIdentity !== state.identity) {
      changePersona(targetIdentity);
    }
    if (targetTab && targetTab !== state.tab) {
      switchTab(targetTab);
    }
  });

  // Series shared scope synchronization (getSharedScope / setSharedScope / dynamics-scopechange)
  window.addEventListener('dynamics-scopechange', ev => {
    const nextScope = String(ev?.detail?.umo ?? getSharedScope() ?? '').trim();
    if (nextScope === state.sharedScope) return;
    state.sharedScope = nextScope;
    if (state.overview) renderOverview(state.overview);
    loadOverview({ silent: true }).catch(() => {});
  });

  // Series sharedPersona compatibility listeners
  const onSharedPersonaEvent = ev => {
    const detail = ev?.detail;
    const raw = typeof detail === 'string'
      ? detail
      : (detail?.persona ?? detail?.persona_id ?? detail?.sharedPersona ?? detail?.identity ?? '');
    applyExternalPersona(raw);
  };
  window.addEventListener('dynamics-personachange', onSharedPersonaEvent);
  window.addEventListener('dynamics-persona-change', onSharedPersonaEvent);
  try {
    getBridge()?.onContext?.(ctx => {
      if (typeof ctx?.scope === 'string' && ctx.scope.trim() !== state.sharedScope) {
        state.sharedScope = ctx.scope.trim();
        if (state.overview) renderOverview(state.overview);
      }
      const nextPersona = ctx?.persona || ctx?.sharedPersona || ctx?.persona_id || '';
      if (typeof nextPersona === 'string' && nextPersona.trim()) {
        applyExternalPersona(nextPersona.trim());
      }
    });
  } catch {}
}

async function init() {
  initTheme();
  const params = new URLSearchParams(location.search);
  state.sharedScope = String(getSharedScope() || params.get('umo') || '').trim();
  state.sharedPersona = readSharedPersona();
  bindEvents();
  const reqIdentity = params.get('identity');
  if (reqIdentity) state.identity = reqIdentity;
  const reqTab = params.get('tab');
  const validTabs = new Set(['overview', 'sources', 'logs', 'reviews']);
  try {
    await loadOverview();
    if (!reqIdentity && state.sharedPersona) {
      applyExternalPersona(state.sharedPersona);
    }
    if (reqTab && validTabs.has(reqTab) && reqTab !== 'overview') {
      await switchTab(reqTab);
    }
  } catch (err) {
    notify(err?.message || tr('请从 AstrBot 控制台打开'), 'error');
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
