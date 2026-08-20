const state = {
  user: null,
  view: 'dashboard',
  callbackLocks: new Set(),
};

const $ = selector => document.querySelector(selector);
const $$ = selector => document.querySelectorAll(selector);
const asArray = value => Array.isArray(value) ? value : [];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const CRM_TIME_ZONE = 'Asia/Novosibirsk';
const fmtDate = value => value
  ? new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: CRM_TIME_ZONE,
  }).format(new Date(value))
  : '—';
const money = value => new Intl.NumberFormat('ru', {
  style: 'currency', currency: 'RUB', maximumFractionDigits: 0,
}).format(value || 0);

const labels = {
  received: 'Получен',
  recorded: 'Записан',
  transcribing: 'Транскрибация',
  analyzing: 'Анализ',
  ready: 'Готово',
  failed: 'Ошибка',
  ignored: 'Пропущен',
  awaiting_recording: 'Ожидается запись',
  awaiting_transcript: 'Ожидается транскрипт',
  call_finished: 'Звонок завершён',
  pending: 'Ожидается',
  accepted: 'Соединяем с менеджером',
  calling_manager: 'Соединяем с менеджером',
  call_started: 'Звонок начался',
  unknown: 'Требует проверки',
  new: 'Новая',
  qualified: 'Квалификация',
  proposal: 'Предложение',
  negotiation: 'Переговоры',
  won: 'Успех',
  lost: 'Потеря',
};

function errorText(detail, fallback = 'Ошибка сервера') {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg || item.message || String(item)).join('; ') || fallback;
  if (detail && typeof detail === 'object') return detail.message || detail.detail || fallback;
  return fallback;
}

async function api(path, options = {}) {
  const request = { ...options };
  const headers = new Headers(options.headers || {});

  if (request.body && !(request.body instanceof FormData) && !(request.body instanceof URLSearchParams)) {
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    request.body = JSON.stringify(request.body);
  }

  const response = await fetch(path, {
    ...request,
    headers,
    credentials: 'same-origin',
  });

  if (response.status === 401) {
    showLogin();
    throw new Error('Сессия завершена. Войдите снова.');
  }

  if (response.status === 428) {
    showPasswordChange();
    throw new Error('Сначала смените стартовый пароль.');
  }

  if (!response.ok) {
    let payload = {};
    try { payload = await response.json(); } catch { /* non-JSON error is still an error */ }
    throw new Error(errorText(payload.detail || payload.message, 'Ошибка сервера'));
  }

  if (response.status === 204 || response.headers.get('content-length') === '0') return null;
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) return response.text();
  return response.json();
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 2800);
}

function showLogin() {
  state.user = null;
  $('#workspace').classList.add('hidden');
  $('#password-change').classList.add('hidden');
  $('#login').classList.remove('hidden');
}

function showPasswordChange() {
  $('#login').classList.add('hidden');
  $('#workspace').classList.add('hidden');
  $('#password-change').classList.remove('hidden');
  setTimeout(() => $('#password-change-form input[name="current_password"]')?.focus(), 0);
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
  } catch {
    // The local UI must still become locked even when a connection is interrupted.
  }
  showLogin();
}

$('#logout').onclick = logout;

$('#login-form').onsubmit = async event => {
  event.preventDefault();
  const error = $('#login-error');
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  error.textContent = '';
  submit.disabled = true;

  try {
    await api('/api/auth/token', {
      method: 'POST',
      body: new URLSearchParams(new FormData(event.currentTarget)),
    });
    await start();
  } catch (failure) {
    error.textContent = failure.message;
  } finally {
    submit.disabled = false;
  }
};

$('#password-change-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  const error = $('#password-change-error');
  const submit = form.querySelector('button[type="submit"]');
  error.textContent = '';

  if (values.new_password !== values.new_password_confirm) {
    error.textContent = 'Новый пароль и его повтор не совпадают.';
    return;
  }

  submit.disabled = true;
  try {
    await api('/api/auth/change-password', {
      method: 'POST',
      body: {
        current_password: values.current_password,
        new_password: values.new_password,
      },
    });
    form.reset();
    toast('Пароль изменён.');
    await start();
  } catch (failure) {
    error.textContent = failure.message;
  } finally {
    submit.disabled = false;
  }
};

$$('nav button[data-view]').forEach(button => {
  button.onclick = () => navigate(button.dataset.view);
});

// Dynamic workspace markup stays compatible with the strict production CSP:
// actions use data attributes and one listener from this external script, not
// inline `onclick` attributes (which CSP correctly blocks).
document.addEventListener('click', event => {
  const control = event.target.closest('[data-action]');
  if (!control) return;
  const action = control.dataset.action;
  const numericId = Number(control.dataset.callId);
  if (action === 'navigate') navigate(control.dataset.view);
  if (action === 'call-detail' && Number.isSafeInteger(numericId) && numericId > 0) callDetail(numericId);
  if (action === 'contact-detail' && control.dataset.contactId) contactDetail(control.dataset.contactId);
  if (action === 'deal-detail' && control.dataset.dealId) openDeal(control.dataset.dealId);
  if (action === 'complete-task' && control.dataset.taskId) completeTask(control.dataset.taskId);
  if (action === 'retry-call' && Number.isSafeInteger(numericId) && numericId > 0 && control.dataset.stage) {
    retryCall(numericId, control.dataset.stage);
  }
  if (action === 'draft-decision' && Number.isSafeInteger(numericId) && numericId > 0 && control.dataset.draftId && control.dataset.decision) {
    decideActionDraft(numericId, control.dataset.draftId, control.dataset.decision);
  }
});

function openDeal(id) {
  dealDetail(id).catch(error => toast(`Не удалось открыть сделку: ${error.message}`));
}

function bindDealOpeners(root = document) {
  root.querySelectorAll('[data-action="deal-detail"][data-deal-id]').forEach(control => {
    control.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      openDeal(control.dataset.dealId);
    };
  });
}

function setHead(title, eyebrow = 'РАБОЧЕЕ ПРОСТРАНСТВО') {
  $('#title').textContent = title;
  $('#eyebrow').textContent = eyebrow;
}

function navigate(view, argument) {
  state.view = view;
  $$('nav button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  ({ dashboard, calls, contacts, deals, tasks, pipeline, admin }[view] || dashboard)(argument);
}

async function start() {
  try {
    const user = await api('/api/me');
    state.user = user;

    if (user.must_change_password) {
      showPasswordChange();
      return;
    }

    $('#login').classList.add('hidden');
    $('#password-change').classList.add('hidden');
    $('#workspace').classList.remove('hidden');
    $('#user-name').textContent = user.display_name;
    $('#user-role').textContent = user.role === 'admin' ? 'Администратор' : 'Менеджер';
    $('#avatar').textContent = (user.display_name || 'М')[0].toUpperCase();
    $('#admin-link').classList.toggle('hidden', user.role !== 'admin');
    const requestedDealId = new URLSearchParams(window.location.search).get('deal');
    if (requestedDealId) {
      history.replaceState(null, '', window.location.pathname);
      try {
        await dealDetail(requestedDealId);
      } catch (error) {
        toast(`Не удалось открыть сделку: ${error.message}`);
        navigate('dashboard');
      }
      return;
    }
    navigate('dashboard');
  } catch {
    showLogin();
  }
}

function callStatus(call) {
  return call.processing_status || call.status || call.call_status || 'received';
}

function recordingStatus(call) {
  return call.recording_status || call.provider_recording_status || call.recording_state || '';
}

function isNovofon(value) {
  return String(value?.source || value?.provider || value?.telephony_source || '').toLowerCase() === 'novofon';
}

function sourceName(value) {
  if (isNovofon(value)) return 'Novofon';
  if (String(value?.source || '').toLowerCase() === 'asterisk') return 'Asterisk';
  return value?.source || value?.provider || '';
}

function statusPill(status) {
  return `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
}

function sourcePill(call) {
  const name = sourceName(call);
  return name ? `<span class="source-pill ${isNovofon(call) ? 'novofon' : ''}">${esc(name)}</span>` : '';
}

async function dashboard() {
  setHead('Обзор');
  const [dashboardData, seasonData, recentCalls, taskList] = await Promise.all([
    api('/api/dashboard'),
    api('/api/dashboard/season'),
    api('/api/calls?limit=5'),
    api('/api/tasks'),
  ]);

  $('#content').innerHTML = `<section class="hero"><div><h2>Добрый день, ${esc(state.user.display_name)}.</h2><p>Главное на сегодня — не оставить клиента без следующего шага.</p></div><div class="date">${new Intl.DateTimeFormat('ru-RU', { dateStyle: 'full', timeZone: CRM_TIME_ZONE }).format(new Date())}</div></section>
    <section class="stats"><div class="stat"><small>Заработано владельцем</small><strong>${money(seasonData.earned_owner_income)}</strong><em>цель ${money(seasonData.goal_owner_income)}</em></div><div class="stat"><small>Прогноз дохода</small><strong>${money(seasonData.projected_owner_income)}</strong><em>по текущим сделкам</em></div><div class="stat"><small>Безопасные деньги</small><strong>${money(seasonData.safe_cash)}</strong><em>подтверждённый cashflow</em></div><div class="stat"><small>Осталось до цели</small><strong>${money(seasonData.remaining_to_goal)}</strong><em>по earned income</em></div></section>
    <section class="stats"><div class="stat"><small>Подтверждённые деньги</small><strong>${money(seasonData.net_confirmed_customer_cash)}</strong><em>после возвратов</em></div><div class="stat"><small>Фактические расходы</small><strong>${money(seasonData.realized_cost_outflows)}</strong><em>оплаченная себестоимость</em></div><div class="stat"><small>Открытые обязательства</small><strong>${money(seasonData.open_reserved_obligations)}</strong><em>зарезервировано</em></div><div class="stat"><small>Безопасные деньги</small><strong>${money(seasonData.safe_cash)}</strong><em>факт P0</em></div></section>
    <section class="grid-2"><div class="panel"><div class="panel-head"><h2>Последние звонки</h2><button class="link" data-action="navigate" data-view="calls">Все звонки →</button></div>${callRows(asArray(recentCalls))}</div><div class="panel"><div class="panel-head"><h2>Ближайшие действия</h2><button class="link" data-action="navigate" data-view="tasks">Все задачи →</button></div>${taskRows(asArray(taskList).slice(0, 6))}</div></section>`;
}

function callRows(items) {
  return asArray(items).length
    ? asArray(items).map(call => `<div class="call-row" role="button" tabindex="0" data-action="call-detail" data-call-id="${Number(call.id)}"><div><b>${esc(call.contact_name || call.phone_normalized || 'Неизвестный клиент')}</b><small>${call.direction === 'in' ? 'Входящий' : 'Исходящий'} · ${fmtDate(call.started_at)} · ${call.duration_sec || 0} сек. ${sourceName(call) ? `· ${esc(sourceName(call))}` : ''}</small></div>${statusPill(callStatus(call))}</div>`).join('')
    : '<p class="muted">Звонков пока нет</p>';
}

function taskRows(items) {
  return asArray(items).length
    ? asArray(items).map(task => `<div class="task-row"><div><b>${esc(task.title)}</b><small>${esc(task.contact_name || task.phone_normalized || 'Без контакта')} · ${fmtDate(task.due_at)}</small></div><button class="secondary" type="button" data-action="complete-task" data-task-id="${esc(task.id)}" aria-label="Завершить задачу">✓</button></div>`).join('')
    : '<p class="muted">Задач пока нет</p>';
}

async function calls() {
  setHead('Звонки', 'РАЗГОВОРЫ С КЛИЕНТАМИ');
  $('#content').innerHTML = `<div class="toolbar"><input id="call-q" placeholder="Имя, телефон или ID звонка"><select id="call-status"><option value="">Все статусы</option>${['received', 'awaiting_recording', 'awaiting_transcript', 'ready', 'recorded', 'transcribing', 'analyzing', 'failed'].map(status => `<option value="${status}">${labels[status]}</option>`).join('')}</select><button class="primary" id="call-search">Найти</button></div><div id="calls-table"></div>`;

  async function load() {
    const query = encodeURIComponent($('#call-q').value);
    const status = $('#call-status').value;
    const rows = await api(`/api/calls?q=${query}&status=${status}&limit=100`);
    $('#calls-table').innerHTML = `<table class="table"><thead><tr><th>Клиент</th><th>Направление</th><th>Дата</th><th>Длительность</th><th>Источник</th><th>Статус</th><th>Следующий шаг</th></tr></thead><tbody>${asArray(rows).map(call => `<tr class="clickable-row" data-action="call-detail" data-call-id="${Number(call.id)}"><td><span class="phone">${esc(call.contact_name || call.phone_normalized || 'Неизвестный')}</span><br><small class="muted">${esc(call.phone_normalized || '')}</small></td><td>${call.direction === 'in' ? 'Входящий' : 'Исходящий'}</td><td>${fmtDate(call.started_at)}</td><td>${call.duration_sec || 0} сек.</td><td>${sourcePill(call) || '—'}</td><td>${statusPill(callStatus(call))}</td><td>${call.has_open_task ? 'Назначен' : '—'}</td></tr>`).join('')}</tbody></table>`;
  }

  $('#call-search').onclick = load;
  await load();
}

function providerRecordings(call) {
  const explicit = asArray(call.provider_recordings);
  const fromRecordings = asArray(call.recordings).filter(recording => {
    const source = String(recording.provider || recording.source || '').toLowerCase();
    return source === 'novofon' || recording.is_provider_recording === true || recording.external === true;
  });
  const seen = new Set();
  return [...explicit, ...fromRecordings].filter(recording => {
    const key = String(recording.id || recording.recording_id || '');
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function localRecordings(call) {
  return asArray(call.recordings).filter(recording => !providerRecordings(call).some(provider => String(provider.id || provider.recording_id) === String(recording.id || recording.recording_id)));
}

function recordingPanel(call) {
  const provider = providerRecordings(call);
  const local = localRecordings(call);
  const status = recordingStatus(call);
  const parts = [];

  if (local[0]?.id) {
    parts.push(`<div class="audio"><small>ЗАПИСЬ РАЗГОВОРА</small><audio id="call-audio" controls></audio></div>`);
  }

  if (isNovofon(call) || provider.length || status) {
    const stateText = labels[status] || status || (provider.length ? 'Запись доступна' : 'Ожидается запись');
    const links = provider.map(recording => {
      const id = recording.id || recording.recording_id;
      if (!id) return '';
      return `<a class="provider-recording-link" href="/api/recordings/${encodeURIComponent(id)}/open" target="_blank" rel="noopener noreferrer">Открыть запись в Novofon</a>`;
    }).join('');
    parts.push(`<div class="provider-recording"><div><small>ЗАПИСЬ NOVOFON</small><b>${esc(stateText)}</b><p>Файл хранится у Novofon. В CRM не показывается и не передаётся его внешняя ссылка.</p></div><div class="provider-recording-actions">${links || '<span class="muted">Ссылка появится после уведомления от Novofon.</span>'}</div></div>`);
  }

  return parts.join('');
}

function transcriptText(call) {
  if (call.transcript?.text) return call.transcript.text;
  if (call.processing_error) return call.processing_error;
  if (isNovofon(call)) return 'Транскрипт в пилоте не подключён. CRM не создаёт ИИ-выводы без подтверждённого текста разговора.';
  return 'Транскрипт ещё не готов';
}

function insightMarkup(call, insightData) {
  if (isNovofon(call) && !call.transcript?.text && !insightData.summary) {
    return '<p class="muted">ИИ-анализ недоступен: для пилота речевая аналитика Novofon не подключена. Здесь не будет сгенерированных предположений без транскрипта.</p>';
  }
  return `<div class="insight-list">${insight('Резюме', insightData.summary)}${insight('Потребность', insightData.customer_need)}${insight('Продукт', insightData.product)}${insight('Бюджет', insightData.budget_amount)}${insight('Стадия', insightData.lead_stage)}${insight('Возражения', asArray(insightData.objections).join('; '))}${insight('Следующий шаг', insightData.next_step)}${insight('Риск потери', insightData.loss_risk)}${asArray(insightData.recommendations).length ? `<div class="recommend"><b>Что улучшить</b><p>${esc(asArray(insightData.recommendations).join(' · '))}</p></div>` : ''}${asArray(insightData.evidence).slice(0, 3).map(quote => `<div class="quote">«${esc(quote.quote)}»</div>`).join('')}</div>`;
}

function previewValue(value) {
  if (value == null || value === '') return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function evidenceMarkup(items) {
  return asArray(items).map(item => {
    const start = item.segment_start_ms == null ? '?' : item.segment_start_ms;
    const end = item.segment_end_ms == null ? '?' : item.segment_end_ms;
    return `<small class="draft-evidence">${start}–${end} мс · «${esc(item.quote || '')}»</small>`;
  }).join('') || '<small class="muted">Нет подтверждения</small>';
}

function actionDraftMarkup(drafts, previews, callId) {
  if (!drafts.length) return '';
  return `<section class="panel"><h2>Предложения ИИ</h2>${drafts.map(draft => {
    const preview = previews[draft.id];
    const pending = draft.status === 'pending';
    const stale = Boolean(preview?.stale);
    const diff = draft.kind === 'deal_update' && preview
      ? `<div class="draft-diff"><div class="draft-diff-head"><small>Поле</small><small>Текущее</small><small>Предложенное</small><small>Confidence / evidence</small></div>${asArray(preview.diff).map(item => `<div class="draft-diff-row ${item.conflict ? 'conflict' : ''}"><b>${esc(item.field)}</b><span>${esc(previewValue(item.current_value))}</span><span>${esc(previewValue(item.proposed_value))}</span><span>${item.confidence == null ? '—' : `${Math.round(Number(item.confidence) * 100)}%`} · ${esc(item.inference_status || 'unknown')}${evidenceMarkup(item.evidence)}</span></div>`).join('')}</div>${stale ? `<p class="error">Черновик устарел: ${esc(asArray(preview.conflicts).join(', '))}. Обновление не будет применено.</p>` : ''}`
      : `<p class="muted">${esc(draft.kind)} · ${esc(previewValue(draft.payload))}</p>${evidenceMarkup(draft.evidence)}`;
    const actions = pending ? `<div class="actions"><button class="primary" type="button" data-action="draft-decision" data-call-id="${Number(callId)}" data-draft-id="${esc(draft.id)}" data-decision="approve" ${stale ? 'disabled' : ''}>Применить черновик</button><button class="secondary" type="button" data-action="draft-decision" data-call-id="${Number(callId)}" data-draft-id="${esc(draft.id)}" data-decision="reject">Отклонить</button></div>` : `<span class="badge ${esc(draft.status)}">${esc(draft.status)}</span>`;
    return `<article class="draft-card"><div class="panel-head"><b>${esc(draft.kind)}</b><span class="badge">${esc(draft.status)}</span></div>${diff}${actions}</article>`;
  }).join('')}</section>`;
}

async function decideActionDraft(callId, draftId, decision) {
  try {
    await api(`/api/action-drafts/${encodeURIComponent(draftId)}/decision`, { method: 'POST', body: { action: decision } });
    toast(decision === 'approve' ? 'Черновик применён' : 'Черновик отклонён');
    await callDetail(callId);
  } catch (failure) {
    toast(failure.message);
  }
}

async function callDetail(id) {
  setHead('Карточка звонка', 'РАЗБОР РАЗГОВОРА');
  const call = await api(`/api/calls/${id}`);
  const insightData = call.insight?.data || {};
  const contact = call.contact || {};
  const localRecording = localRecordings(call)[0];
  const showRetryActions = !isNovofon(call) || call.transcription_available === true || call.analysis_available === true;
  const drafts = asArray(call.action_drafts);
  const previewPairs = await Promise.all(drafts.filter(draft => draft.kind === 'deal_update').map(async draft => {
    try { return [draft.id, await api(`/api/action-drafts/${encodeURIComponent(draft.id)}/preview`)]; } catch (_error) { return [draft.id, null]; }
  }));
  const draftPreviews = Object.fromEntries(previewPairs);

  $('#content').innerHTML = `<div class="call-layout"><section><div class="panel"><div class="panel-head"><div><h2>${esc(contact.full_name || contact.phone_normalized || 'Неизвестный клиент')}</h2><div class="detail-meta">${sourcePill(call)} ${recordingStatus(call) ? `<span class="muted">${esc(labels[recordingStatus(call)] || recordingStatus(call))}</span>` : ''}</div></div>${statusPill(callStatus(call))}</div>${recordingPanel(call)}<h2>Транскрипт</h2><div class="transcript">${esc(transcriptText(call))}</div></div></section><aside-detail><div class="panel"><div class="panel-head"><h2>Выводы ИИ</h2><small class="muted">${insightData.confidence != null ? `${Math.round(insightData.confidence * 100)}% уверенность` : ''}</small></div>${insightMarkup(call, insightData)}</div><div class="panel"><h2>Данные клиента</h2><form class="form" id="contact-form"><label>Имя<input name="contact_name" value="${esc(contact.full_name || '')}"></label><label>Email<input name="contact_email" value="${esc(contact.email || '')}"></label><label>Заметка<textarea name="contact_notes">${esc(contact.notes || '')}</textarea></label><button class="primary">Сохранить</button></form></div>${contact.id ? `<div class="panel"><h2>Новая сделка</h2><form class="form" id="deal-form"><label>Название<input name="title" required value="${esc(insightData.product || 'Сделка по звонку')}"></label><label>Сумма<input name="amount" type="number" min="0" value="${esc(insightData.budget_amount || '')}"></label><label>Этап<select name="stage">${['new', 'qualified', 'proposal', 'negotiation'].map(stage => `<option value="${stage}" ${insightData.lead_stage === stage ? 'selected' : ''}>${labels[stage]}</option>`).join('')}</select></label><button class="primary">Создать сделку</button></form></div>` : ''}<div class="panel"><div class="panel-head"><h2>Следующий шаг</h2></div><form class="form" id="task-form"><label>Что сделать<input name="title" required value="${esc(insightData.next_step || '')}"></label><label>Когда<input name="due_at" type="datetime-local"></label><button class="primary">Создать задачу</button></form></div>${showRetryActions ? `<div class="actions"><button class="secondary" type="button" data-action="retry-call" data-call-id="${Number(id)}" data-stage="transcribe">Повторить ASR</button><button class="secondary" type="button" data-action="retry-call" data-call-id="${Number(id)}" data-stage="analyze">Повторить анализ</button></div>` : ''}</aside-detail></div>`;
  if (drafts.length) $('aside-detail').insertAdjacentHTML('beforeend', actionDraftMarkup(drafts, draftPreviews, id));

  if (localRecording?.id) await loadAudio(localRecording.id);

  $('#contact-form').onsubmit = async event => {
    event.preventDefault();
    await api(`/api/calls/${id}`, { method: 'PATCH', body: Object.fromEntries(new FormData(event.currentTarget)) });
    toast('Карточка сохранена');
  };

  if ($('#deal-form')) {
    $('#deal-form').onsubmit = async event => {
      event.preventDefault();
      const payload = Object.fromEntries(new FormData(event.currentTarget));
      payload.call_id = id;
      payload.contact_id = contact.id;
      payload.amount = payload.amount ? Number(payload.amount) : null;
      await api('/api/deals', { method: 'POST', body: payload });
      toast('Сделка создана');
      await callDetail(id);
    };
  }

  $('#task-form').onsubmit = async event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    payload.call_id = id;
    payload.contact_id = contact.id || null;
    payload.due_at = payload.due_at ? new Date(payload.due_at).toISOString() : null;
    await api('/api/tasks', { method: 'POST', body: payload });
    toast('Задача создана');
    await callDetail(id);
  };
}

function insight(label, value) {
  if (value == null || value === '') return '';
  return `<div class="insight-item"><label>${esc(label)}</label><p>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</p></div>`;
}

async function loadAudio(recordingId) {
  const response = await fetch(`/api/recordings/${encodeURIComponent(recordingId)}`, { credentials: 'same-origin' });
  if (response.ok && $('#call-audio')) $('#call-audio').src = URL.createObjectURL(await response.blob());
}

async function retryCall(id, stage) {
  await api(`/api/calls/${id}/retry?stage=${encodeURIComponent(stage)}`, { method: 'POST' });
  toast('Обработка поставлена в очередь');
}

async function contacts() {
  setHead('Клиенты', 'ЕДИНАЯ ИСТОРИЯ КОНТАКТОВ');
  const rows = await api('/api/contacts');
  $('#content').innerHTML = `<table class="table"><thead><tr><th>Клиент</th><th>Телефон</th><th>Email</th><th>Звонков</th><th>Последний контакт</th></tr></thead><tbody>${asArray(rows).map(contact => `<tr class="clickable-row" data-action="contact-detail" data-contact-id="${esc(contact.id)}"><td class="phone">${esc(contact.full_name || 'Без имени')}</td><td>${esc(contact.phone_normalized)}</td><td>${esc(contact.email || '—')}</td><td>${contact.calls_count || 0}</td><td>${fmtDate(contact.last_call_at)}</td></tr>`).join('')}</tbody></table>`;
}

function callbackText(status) {
  const normalized = String(status || '').toLowerCase();
  return labels[normalized] || (normalized ? `Статус: ${status}` : 'Соединяем с менеджером…');
}

function idempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `callback-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function callbackCallId(result) {
  return result?.call_id || result?.call?.id || result?.crm_call_id || result?.data?.call_id || null;
}

function setCallbackStatus(message, kind = '') {
  const element = $('#callback-status');
  if (!element) return;
  element.className = `callback-status ${kind}`;
  element.textContent = message;
}

function showCallbackCardLink(callId) {
  const numericId = Number(callId);
  if (!Number.isSafeInteger(numericId) || numericId <= 0) return;
  const link = document.createElement('button');
  link.className = 'secondary callback-call-link';
  link.type = 'button';
  link.dataset.action = 'call-detail';
  link.dataset.callId = String(numericId);
  link.textContent = 'Открыть карточку звонка';
  $('#callback-result')?.replaceChildren(link);
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function observeCallback(requestId, contactId) {
  // Call API acknowledges the request before Novofon has a completed call
  // session. Poll only the CRM's own protected endpoint; never expose or call
  // provider URLs from the mobile browser.
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (attempt) await wait(3000);
    let current;
    try {
      current = await api(`/api/calls/initiations/${encodeURIComponent(requestId)}`);
    } catch (failure) {
      // Keep the original callback locked. A temporary CRM network error must
      // not invite a second Call API request while Novofon may still be calling.
      setCallbackStatus('Связь с CRM временно недоступна. Не нажимайте повторно: проверяем данные вызова.', 'pending');
      continue;
    }
    const callId = callbackCallId(current);
    if (callId) {
      setCallbackStatus('Звонок завершён и добавлен в CRM.', 'accepted');
      showCallbackCardLink(callId);
      state.callbackLocks.delete(contactId);
      return;
    }
    const status = String(current?.status || '').toLowerCase();
    if (status === 'unknown') {
      setCallbackStatus('Novofon ещё не подтвердил callback. Не нажимайте повторно: CRM продолжает ждать событие.', 'pending');
    } else if (status === 'accepted' || status === 'requested') {
      setCallbackStatus('Novofon принял запрос. Ожидайте обычный звонок на мобильный телефон.', 'accepted');
    } else {
      setCallbackStatus(callbackText(status), 'pending');
    }
  }
  setCallbackStatus('Карточка ещё не пришла от Novofon. Повторно не звоните: проверьте журнал позже или обратитесь к администратору.', 'pending');
}

async function initiateCall(contact) {
  const contactId = String(contact.id || '');
  const button = $('#callback-button');
  if (!contactId || state.callbackLocks.has(contactId)) return;

  const key = idempotencyKey();
  state.callbackLocks.add(contactId);
  button.disabled = true;
  setCallbackStatus('Соединяем с менеджером…', 'pending');

  try {
    const result = await api('/api/calls/initiate', {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
      body: {
        contact_id: contact.id,
        phone: contact.phone_normalized,
        idempotency_key: key,
      },
    });
    const callId = callbackCallId(result);
    const stateText = callbackText(result?.status || result?.call?.status || result?.request?.status);
    setCallbackStatus(stateText, 'accepted');
    button.textContent = 'Вызов отправлен';
    toast('Novofon звонит менеджеру. Ответьте на обычный звонок телефона.');

    if (callId) {
      showCallbackCardLink(callId);
      state.callbackLocks.delete(contactId);
    } else if (result?.id) {
      observeCallback(result.id, contactId);
    }
  } catch (failure) {
    state.callbackLocks.delete(contactId);
    button.disabled = false;
    button.textContent = 'Позвонить через Novofon';
    setCallbackStatus(`Не удалось начать звонок: ${failure.message}`, 'failed');
  }
}

async function contactDetail(id) {
  const contact = await api(`/api/contacts/${encodeURIComponent(id)}`);
  setHead(contact.full_name || contact.phone_normalized, 'КАРТОЧКА КЛИЕНТА');
  const phone = contact.phone_normalized || contact.phone || '';
  $('#content').innerHTML = `<div class="grid-2"><div class="panel"><h2>История звонков</h2>${callRows(asArray(contact.calls))}</div><div><div class="panel"><h2>Контакт</h2><p class="phone">${esc(phone)}</p></div><div class="panel"><h2>Сделки</h2>${asArray(contact.deals).map(deal => `<a class="task-row deal-link" href="/?deal=${encodeURIComponent(deal.id)}"><div><b>${esc(deal.title)}</b><small>${money(deal.amount)}</small></div><span class="badge">${esc(deal.stage)}</span></a>`).join('') || '<p class="muted">Сделок нет</p>'}</div></div></div>`;

  const callbackButton = $('#callback-button');
  if (phone && callbackButton) callbackButton.onclick = () => initiateCall(contact);
}

async function deals() {
  setHead('Сделки', 'РАБОЧИЙ СПИСОК');
  const rows = asArray(await api('/api/deals'));
  $('#content').innerHTML = rows.length
    ? `<table class="table deal-list"><thead><tr><th>Сделка</th><th>Клиент</th><th>Этап</th><th>Сегмент</th><th>Цена</th><th>Прогноз</th></tr></thead><tbody>${rows.map(deal => `<tr><td><a class="deal-link" href="/?deal=${encodeURIComponent(deal.id)}"><b>${esc(deal.title)}</b></a></td><td>${esc(deal.contact_name || deal.phone_normalized || '—')}</td><td><span class="badge">${esc(deal.stage)}</span></td><td>${esc(deal.qualification_segment || 'unknown')}</td><td>${money(deal.final_contract_price || deal.quoted_price || deal.amount)}</td><td>${money(deal.projected_owner_income)}</td></tr>`).join('')}</tbody></table>`
    : '<section class="panel"><p class="muted">Сделок пока нет.</p></section>';
}

async function dealDetail(id) {
  const [deal, cashflow] = await Promise.all([
    api(`/api/deals/${encodeURIComponent(id)}`),
    api(`/api/deals/${encodeURIComponent(id)}/cashflow`),
  ]);
  const rev = asArray(deal.economics_revisions)[0] || {};
  const movements = asArray(cashflow.movements);
  const obligations = asArray(cashflow.obligations);
  const movementLabels = {
    customer_incoming: 'Платёж клиента', customer_refund: 'Возврат клиенту',
    realized_cost_outflow: 'Фактическая себестоимость',
    realized_cost_outflow_reversal: 'Компенсация себестоимости',
    other_reserved_cash: 'Прочий резерв', other_reserved_cash_release: 'Высвобождение резерва',
  };
  const obligationRows = obligations.map(o => {
    const settled = o.status === 'settled';
    const status = settled ? '<span class="badge completed">settled</span>' : `<span class="badge">${esc(o.status)}</span>`;
    const settlement = settled
      ? `<small>Урегулировано ${fmtDate(o.settled_at)} · фактический расход #${esc(o.settled_movement_id || '—')} остаётся в safe cash.</small>`
      : '<small>Открытый резерв уменьшает safe cash до settlement.</small>';
    const editor = o.status === 'open' ? `<form class="form compact-form" data-obligation-form="${esc(o.id)}"><label>Сумма<input name="amount" type="number" min="0.01" step="0.01" required value="${esc(o.amount)}"></label><label>Описание<input name="description" value="${esc(o.description || '')}"></label><label>Срок<input name="due_date" type="date" value="${esc(o.due_date ? String(o.due_date).slice(0, 10) : '')}"></label><div class="actions"><button class="secondary" type="submit">Изменить резерв</button><button class="primary" type="button" data-settle-obligation="${esc(o.id)}">Урегулировать</button></div></form>` : '';
    return `<article class="cash-history"><div><b>${esc(o.description || 'Плановая себестоимость')} · ${money(o.amount)}</b>${status}${settlement}</div>${editor}</article>`;
  }).join('') || '<p class="muted">Плановых обязательств пока нет.</p>';
  const movementRows = movements.map(m => `<article class="cash-history"><div><b>${esc(movementLabels[m.kind] || m.kind)} · ${money(m.amount)}</b><span class="badge completed">confirmed</span><small>${fmtDate(m.confirmed_at || m.occurred_at)}${m.note ? ` · ${esc(m.note)}` : ''}${m.reversal_of_movement_id ? ` · компенсация #${esc(m.reversal_of_movement_id)}` : ''}</small></div><button class="secondary" type="button" data-reverse-movement="${esc(m.id)}">Компенсировать</button></article>`).join('') || '<p class="muted">Подтверждённых движений пока нет.</p>';
  setHead(deal.title, 'СДЕЛКА · МОБИЛЬНАЯ КАРТОЧКА');
  const below = deal.quoted_price != null && rev.price_floor_ae_8 != null && Number(deal.quoted_price) < Number(rev.price_floor_ae_8);
  $('#content').innerHTML = `<section class="panel deal-hero"><b>${esc(deal.qualification_segment)} | ${money(deal.quoted_price || deal.final_contract_price)} | AE ${rev.ae_percent == null ? '—' : `${(Number(rev.ae_percent)*100).toFixed(1)}%`} | ${esc(rev.economics_status || 'unknown')}</b><small>${esc(deal.stage)} · ${esc(deal.next_contact_at ? fmtDate(deal.next_contact_at) : 'следующий контакт не задан')}</small></section>${below ? '<div class="error">Цена ниже финансового пола AE 8%. Укажите причину override.</div>' : ''}<form class="panel form" id="deal-workspace"><h2>Квалификация</h2><label>Сегмент<select name="qualification_segment"><option>unknown</option><option value="under_80k">&lt;80k</option><option value="over_80k">80k+</option></select></label><label>Бюджет от<input name="estimated_budget_min" type="number" value="${esc(deal.estimated_budget_min || '')}"></label><label>Бюджет до<input name="estimated_budget_max" type="number" value="${esc(deal.estimated_budget_max || '')}"></label><label>Боль<textarea name="pain_primary">${esc(deal.pain_primary || '')}</textarea></label><label>ЛПР<textarea name="decision_makers">${esc(JSON.stringify(deal.decision_makers || []))}</textarea></label><label>Альтернатива<input name="alternative_considered" value="${esc(deal.alternative_considered || '')}"></label><label>Период монтажа<input name="desired_install_period" value="${esc(deal.desired_install_period || '')}"></label><label>Причина override<input name="price_floor_override_reason"></label><button class="primary">Сохранить</button></form><section class="panel"><h2>История экономики</h2>${asArray(deal.economics_revisions).map(r => `<p>R${r.revision} · AE ${money(r.ae_amount)} / ${(Number(r.ae_percent || 0)*100).toFixed(1)}% · floor 8/10/12: ${money(r.price_floor_ae_8)} / ${money(r.price_floor_ae_10)} / ${money(r.price_floor_ae_12)} · solo/partner ${money(r.owner_income_solo)} / ${money(r.owner_income_with_partner)} · ${esc(r.economics_status)} · settings ${r.settings_version}</p>`).join('') || 'Расчётов пока нет'}</section>`;
  $('#content').insertAdjacentHTML('afterbegin', '<div class="actions deal-actions"><button class="secondary danger" type="button" id="delete-deal">Удалить сделку</button></div>');
  $('#deal-workspace').qualification_segment.value = deal.qualification_segment || 'unknown';
  $('#content').insertAdjacentHTML('beforeend', `<section class="panel"><h2>Cashflow · только подтверждённые факты</h2><div class="cashflow-summary"><p><small>Подтверждённые платежи клиентов</small><b>${money(cashflow.confirmed_customer_cash)}</b></p><p><small>Возвраты клиентам</small><b>${money(cashflow.refunds)}</b></p><p><small>Чистые деньги клиентов</small><b>${money(cashflow.net_confirmed_customer_cash)}</b></p><p><small>Фактическая себестоимость</small><b>${money(cashflow.realized_costs)}</b></p><p><small>Открытые обязательства</small><b>${money(cashflow.open_obligations)}</b></p><p><small>Прочие резервы</small><b>${money(cashflow.other_reserved_cash)}</b></p><p class="safe-cash"><small>Safe cash</small><b>${money(cashflow.safe_cash)}</b></p></div><form class="form compact-form" id="cash-movement-form"><h3>Подтвердить новое движение</h3><label>Тип<select name="kind"><option value="customer_incoming">Платёж клиента</option><option value="customer_refund">Возврат клиенту</option><option value="realized_cost_outflow">Фактическая себестоимость</option><option value="other_reserved_cash">Прочий резерв</option><option value="other_reserved_cash_release">Высвобождение прочего резерва</option></select></label><label>Сумма<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Когда произошло<input name="occurred_at" type="datetime-local"></label><label>Комментарий<textarea name="note"></textarea></label><button class="primary">Подтвердить движение</button><small>Подтверждённые строки append-only: редактирование и удаление недоступны. Для исправления используйте «Компенсировать» в истории.</small></form><h3>История движений</h3>${movementRows}</section><section class="panel"><h2>Плановые обязательства</h2><form class="form compact-form" id="obligation-create-form"><label>Сумма<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Описание<input name="description"></label><label>Срок<input name="due_date" type="date"></label><button class="primary">Добавить открытый резерв</button><small>Открытый резерв уменьшает safe cash. После settlement он исчезнет из резервов, а связанный фактический расход останется учтённым.</small></form><h3>История обязательств</h3>${obligationRows}</section>`);
  $('#deal-workspace').onsubmit = async e => { e.preventDefault(); const v=Object.fromEntries(new FormData(e.currentTarget)); ['estimated_budget_min','estimated_budget_max'].forEach(k=>v[k]=v[k]?Number(v[k]):null); if (!v.price_floor_override_reason) delete v.price_floor_override_reason; try { v.decision_makers=JSON.parse(v.decision_makers||'[]'); await api(`/api/deals/${id}`,{method:'PATCH',body:v}); toast('Сделка сохранена'); await dealDetail(id); } catch(err) { toast(err.message); } };
  $('#cash-movement-form').onsubmit = async e => { e.preventDefault(); const v=Object.fromEntries(new FormData(e.currentTarget)); v.amount=Number(v.amount); if (!v.occurred_at) v.occurred_at=null; if (!v.note) v.note=null; try { await api(`/api/deals/${id}/cash-movements`, {method:'POST',body:v}); toast('Подтверждённое движение добавлено'); await dealDetail(id); } catch(err) { toast(err.message); } };
  $('#obligation-create-form').onsubmit = async e => { e.preventDefault(); const v=Object.fromEntries(new FormData(e.currentTarget)); v.amount=Number(v.amount); if (!v.description) v.description=null; if (!v.due_date) v.due_date=null; try { await api(`/api/deals/${id}/cost-obligations`, {method:'POST',body:v}); toast('Открытый резерв добавлен'); await dealDetail(id); } catch(err) { toast(err.message); } };
  $$('[data-obligation-form]').forEach(form => form.onsubmit = async e => { e.preventDefault(); const v=Object.fromEntries(new FormData(e.currentTarget)); v.amount=Number(v.amount); if (!v.description) v.description=null; if (!v.due_date) v.due_date=null; try { await api(`/api/deals/${id}/cost-obligations/${form.dataset.obligationForm}`, {method:'PATCH',body:v}); toast('Открытый резерв изменён'); await dealDetail(id); } catch(err) { toast(err.message); } });
  $$('[data-settle-obligation]').forEach(button => button.onclick = async () => { if (!window.confirm('Урегулировать обязательство и создать фактический расход?')) return; try { await api(`/api/deals/${id}/cost-obligations/${button.dataset.settleObligation}/settle`, {method:'POST'}); toast('Обязательство урегулировано; фактический расход добавлен'); await dealDetail(id); } catch(err) { toast(err.message); } });
  $$('[data-reverse-movement]').forEach(button => button.onclick = async () => { const reason=window.prompt('Причина компенсирующего движения'); if (!reason) return; try { await api(`/api/deals/${id}/cash-movements/${button.dataset.reverseMovement}/reverse`, {method:'POST',body:{reason}}); toast('Компенсирующее движение добавлено'); await dealDetail(id); } catch(err) { toast(err.message); } });
  $('#delete-deal').onclick = async () => {
    if (!window.confirm(`Удалить «${deal.title}»? Это действие нельзя отменить.`)) return;
    try {
      await api(`/api/deals/${id}`, {method:'DELETE', body:{confirmation:'DELETE'}});
      toast('Сделка удалена');
      navigate('deals');
    } catch (error) {
      toast(error.message);
    }
  };
}

async function tasks() {
  setHead('Задачи', 'ПЛАН СЛЕДУЮЩИХ ДЕЙСТВИЙ');
  const rows = await api('/api/tasks');
  $('#content').innerHTML = `<div class="panel">${taskRows(asArray(rows))}</div>`;
}

async function completeTask(id) {
  await api(`/api/tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: { status: 'completed' } });
  toast('Задача выполнена');
  navigate(state.view);
}

async function pipeline() {
  setHead('Воронка', 'ПОДТВЕРЖДЁННЫЕ СДЕЛКИ');
  const [summary, cards] = await Promise.all([api('/api/pipeline'), api('/api/pipeline/deals')]);
  const rows = asArray(summary);
  const dealCards = asArray(cards);
  const render = segment => {
    const data = rows.filter(row => segment === 'all' || row.qualification_segment === segment);
    const stages = [...new Set(data.map(row => row.stage))];
    $('#pipeline-body').innerHTML = stages.length
      ? `<div class="pipeline">${stages.map(stage => {
        const aggregate = data.filter(row => row.stage === stage).reduce((total, row) => ({
          count: total.count + Number(row.count || 0),
          projected: total.projected + Number(row.projected_owner_income || 0),
        }), {count: 0, projected: 0});
        const stageCards = dealCards.filter(deal => deal.stage === stage && (segment === 'all' || deal.qualification_segment === segment));
        return `<section class="stage"><h3>${esc(stage)} · ${aggregate.count}</h3><small>Прогноз ${money(aggregate.projected)}</small>${stageCards.map(deal => `<a class="deal-card deal-link" href="/?deal=${encodeURIComponent(deal.id)}"><b>${esc(deal.title)}</b><small>${esc(deal.contact_name || deal.phone_normalized || '—')} · ${money(deal.commercial_value)}</small></a>`).join('')}</section>`;
      }).join('')}</div>`
      : '<section class="panel"><p class="muted">В выбранном сегменте сделок нет.</p></section>';
  };
  $('#content').innerHTML = `<div class="toolbar"><button class="secondary" data-segment="all">ALL</button><button class="secondary" data-segment="under_80k">&lt;80k</button><button class="secondary" data-segment="over_80k">80k+</button></div><div id="pipeline-body"></div>`; $$('#content [data-segment]').forEach(b=>b.onclick=()=>render(b.dataset.segment)); render('all');
}

async function admin() {
  setHead('Обработка', 'ТЕХНИЧЕСКОЕ СОСТОЯНИЕ');
  const [jobs, mappingsResponse, reasonsResponse] = await Promise.all([
    api('/api/admin/jobs'),
    api('/api/admin/novofon/employee-mappings').catch(() => []),
    api('/api/admin/deal-reasons').catch(() => []),
  ]);
  const mappings = asArray(mappingsResponse?.items || mappingsResponse);
  const reasons = asArray(reasonsResponse);
  const ownMapping = mappings.find(mapping => String(mapping.crm_user_id || mapping.user_id) === String(state.user.id)) || {};
  const mappingRows = mappings.length
    ? `<table class="table mapping-table"><thead><tr><th>Пользователь CRM</th><th>Сотрудник Novofon</th><th>Мобильный</th><th>Внутренний</th></tr></thead><tbody>${mappings.map(mapping => `<tr><td>${esc(mapping.crm_user_name || mapping.user_display_name || mapping.crm_user_id || mapping.user_id)}</td><td>${esc(mapping.provider_employee_id || mapping.employee_id)}</td><td>${esc(mapping.mobile_phone || '—')}</td><td>${esc(mapping.provider_extension || mapping.extension_phone_number || '—')}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">Пока нет сопоставлений. Добавьте сотрудника, который будет принимать callback-звонки.</p>';

  $('#content').innerHTML = `<div class="admin-stack"><section class="panel"><div class="panel-head"><div><h2>Сотрудники Novofon</h2><small class="muted">Нужны только для callback-звонка. Ключи API здесь не хранятся.</small></div>${sourcePill({ source: 'novofon' })}</div><form class="form mapping-form" id="novofon-mapping-form"><label>Пользователь CRM<input name="crm_user_id" value="${esc(ownMapping.crm_user_id || ownMapping.user_id || state.user.id)}" readonly></label><label>ID сотрудника Novofon<input name="provider_employee_id" inputmode="numeric" required value="${esc(ownMapping.provider_employee_id || ownMapping.employee_id || '')}" placeholder="Например, 12345"></label><label>Мобильный сотрудника<input name="mobile_phone" inputmode="tel" required value="${esc(ownMapping.mobile_phone || '')}" placeholder="79001234567"></label><label>Внутренний номер (необязательно)<input name="provider_extension" inputmode="numeric" value="${esc(ownMapping.provider_extension || ownMapping.extension_phone_number || '')}"></label><label>Имя для CRM (необязательно)<input name="display_name" value="${esc(ownMapping.display_name || ownMapping.employee_full_name || '')}"></label><button class="primary" type="submit">Сохранить сотрудника</button></form><div id="mapping-error" class="error"></div></section><section class="panel"><h2>Настроенные сопоставления</h2>${mappingRows}</section><section class="panel"><h2>Очередь обработки</h2><table class="table"><thead><tr><th>Звонок</th><th>Этап</th><th>Статус</th><th>Попытки</th><th>Ошибка</th><th>Обновлено</th></tr></thead><tbody>${asArray(jobs).map(job => `<tr class="clickable-row" data-action="call-detail" data-call-id="${Number(job.call_id)}"><td>${esc(job.call_id)}</td><td>${esc(job.kind)}</td><td><span class="badge ${esc(job.status)}">${esc(job.status)}</span></td><td>${job.attempts || 0}/${job.max_attempts || 0}</td><td>${esc(job.last_error || '—')}</td><td>${fmtDate(job.updated_at)}</td></tr>`).join('')}</tbody></table></section></div>`;

  $('#content').insertAdjacentHTML('beforeend', `<section class="panel"><h2>Причины потери и дисквалификации</h2><form class="form compact-form" id="reason-create-form"><label>Тип<select name="kind"><option value="lost">Потеря</option><option value="disqualified">Дисквалификация</option></select></label><label>Код<input name="code" required pattern="[a-z0-9_]+" placeholder="price_too_high"></label><label>Название<input name="label" required></label><button class="primary">Добавить причину</button></form><div class="reason-catalog">${reasons.map(reason => `<form class="reason-row" data-reason-form="${esc(reason.id)}"><span class="badge ${reason.active ? 'completed' : ''}">${esc(reason.kind)}</span><label>Код<small>${esc(reason.code)}</small></label><label>Название<input name="label" value="${esc(reason.label)}" required></label><label class="reason-active"><input name="active" type="checkbox" ${reason.active ? 'checked' : ''}> Активна</label><button class="secondary">Сохранить</button></form>`).join('') || '<p class="muted">Причин пока нет.</p>'}</div></section>`);

  $('#novofon-mapping-form').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = $('#mapping-error');
    const submit = form.querySelector('button[type="submit"]');
    const payload = Object.fromEntries(new FormData(form));
    ['provider_extension', 'display_name'].forEach(key => {
      if (!payload[key]) payload[key] = null;
    });
    error.textContent = '';
    submit.disabled = true;
    try {
      await api('/api/admin/novofon/employee-mappings', { method: 'PUT', body: payload });
      toast('Сотрудник Novofon сохранён.');
      await admin();
    } catch (failure) {
      error.textContent = failure.message;
    } finally {
      submit.disabled = false;
    }
  };
  $('#reason-create-form').onsubmit = async event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    try {
      await api('/api/admin/deal-reasons', { method: 'POST', body: payload });
      toast('Причина добавлена');
      await admin();
    } catch (failure) { toast(failure.message); }
  };
  $$('[data-reason-form]').forEach(form => form.onsubmit = async event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form));
    payload.active = form.elements.active.checked;
    try {
      await api(`/api/admin/deal-reasons/${encodeURIComponent(form.dataset.reasonForm)}`, { method: 'PATCH', body: payload });
      toast(payload.active ? 'Причина сохранена' : 'Причина отключена');
      await admin();
    } catch (failure) { toast(failure.message); }
  });
}

start();
