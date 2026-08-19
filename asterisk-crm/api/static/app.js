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
  if (action === 'complete-task' && control.dataset.taskId) completeTask(control.dataset.taskId);
  if (action === 'retry-call' && Number.isSafeInteger(numericId) && numericId > 0 && control.dataset.stage) {
    retryCall(numericId, control.dataset.stage);
  }
});

function setHead(title, eyebrow = 'РАБОЧЕЕ ПРОСТРАНСТВО') {
  $('#title').textContent = title;
  $('#eyebrow').textContent = eyebrow;
}

function navigate(view, argument) {
  state.view = view;
  $$('nav button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  ({ dashboard, calls, contacts, tasks, pipeline, admin }[view] || dashboard)(argument);
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
  const outcomeCount = (dashboardData.won_deals || 0) + (dashboardData.lost_deals || 0);
  const conversion = outcomeCount ? Math.round((dashboardData.won_deals || 0) / outcomeCount * 100) : 0;

  $('#content').innerHTML = `<section class="hero"><div><h2>Добрый день, ${esc(state.user.display_name)}.</h2><p>Главное на сегодня — не оставить клиента без следующего шага.</p></div><div class="date">${new Intl.DateTimeFormat('ru-RU', { dateStyle: 'full', timeZone: CRM_TIME_ZONE }).format(new Date())}</div></section>
    <section class="stats"><div class="stat"><small>Заработано владельцем</small><strong>${money(seasonData.earned_owner_income)}</strong><em>цель ${money(seasonData.goal_owner_income)}</em></div><div class="stat"><small>Прогноз дохода</small><strong>${money(seasonData.projected_owner_income)}</strong><em>по текущим сделкам</em></div><div class="stat"><small>Безопасные деньги</small><strong>${money(seasonData.safe_cash)}</strong><em>подтверждённый cashflow</em></div><div class="stat"><small>Осталось до цели</small><strong>${money(seasonData.remaining_to_goal)}</strong><em>по earned income</em></div></section>
    <section class="stats"><div class="stat"><small>Звонков сегодня</small><strong>${dashboardData.calls_today || 0}</strong><em>${dashboardData.ready_today || 0} обработано</em></div><div class="stat"><small>Задач сегодня</small><strong>${dashboardData.tasks_today || 0}</strong><em>в работе</em></div><div class="stat"><small>Просрочено</small><strong>${dashboardData.overdue_tasks || 0}</strong><em class="${dashboardData.overdue_tasks ? 'badge failed' : ''}">требует внимания</em></div><div class="stat"><small>Подтверждённая конверсия</small><strong>${conversion}%</strong><em>${outcomeCount} исходов</em></div></section>
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

async function callDetail(id) {
  setHead('Карточка звонка', 'РАЗБОР РАЗГОВОРА');
  const call = await api(`/api/calls/${id}`);
  const insightData = call.insight?.data || {};
  const contact = call.contact || {};
  const localRecording = localRecordings(call)[0];
  const showRetryActions = !isNovofon(call) || call.transcription_available === true || call.analysis_available === true;

  $('#content').innerHTML = `<div class="call-layout"><section><div class="panel"><div class="panel-head"><div><h2>${esc(contact.full_name || contact.phone_normalized || 'Неизвестный клиент')}</h2><div class="detail-meta">${sourcePill(call)} ${recordingStatus(call) ? `<span class="muted">${esc(labels[recordingStatus(call)] || recordingStatus(call))}</span>` : ''}</div></div>${statusPill(callStatus(call))}</div>${recordingPanel(call)}<h2>Транскрипт</h2><div class="transcript">${esc(transcriptText(call))}</div></div></section><aside-detail><div class="panel"><div class="panel-head"><h2>Выводы ИИ</h2><small class="muted">${insightData.confidence != null ? `${Math.round(insightData.confidence * 100)}% уверенность` : ''}</small></div>${insightMarkup(call, insightData)}</div><div class="panel"><h2>Данные клиента</h2><form class="form" id="contact-form"><label>Имя<input name="contact_name" value="${esc(contact.full_name || '')}"></label><label>Email<input name="contact_email" value="${esc(contact.email || '')}"></label><label>Заметка<textarea name="contact_notes">${esc(contact.notes || '')}</textarea></label><button class="primary">Сохранить</button></form></div>${contact.id ? `<div class="panel"><h2>Новая сделка</h2><form class="form" id="deal-form"><label>Название<input name="title" required value="${esc(insightData.product || 'Сделка по звонку')}"></label><label>Сумма<input name="amount" type="number" min="0" value="${esc(insightData.budget_amount || '')}"></label><label>Этап<select name="stage">${['new', 'qualified', 'proposal', 'negotiation'].map(stage => `<option value="${stage}" ${insightData.lead_stage === stage ? 'selected' : ''}>${labels[stage]}</option>`).join('')}</select></label><button class="primary">Создать сделку</button></form></div>` : ''}<div class="panel"><div class="panel-head"><h2>Следующий шаг</h2></div><form class="form" id="task-form"><label>Что сделать<input name="title" required value="${esc(insightData.next_step || '')}"></label><label>Когда<input name="due_at" type="datetime-local"></label><button class="primary">Создать задачу</button></form></div>${showRetryActions ? `<div class="actions"><button class="secondary" type="button" data-action="retry-call" data-call-id="${Number(id)}" data-stage="transcribe">Повторить ASR</button><button class="secondary" type="button" data-action="retry-call" data-call-id="${Number(id)}" data-stage="analyze">Повторить анализ</button></div>` : ''}</aside-detail></div>`;

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
  $('#content').innerHTML = `<div class="grid-2"><div class="panel"><h2>История звонков</h2>${callRows(asArray(contact.calls))}</div><div><div class="panel"><h2>Контакт</h2><p class="phone">${esc(phone)}</p><p>${esc(contact.email || 'Email не указан')}</p><p class="muted">${esc(contact.notes || '')}</p></div><div class="panel callback-panel"><div class="panel-head"><h2>Исходящий звонок</h2>${sourcePill({ source: 'novofon' })}</div><p>Novofon сначала позвонит на ваш мобильный. После ответа он соединит вас с клиентом.</p>${phone ? `<button class="primary" id="callback-button">Позвонить через Novofon</button><div id="callback-status" class="callback-status">Готово к звонку</div><div id="callback-result"></div>` : '<p class="muted">У контакта нет номера телефона.</p>'}</div><div class="panel"><h2>Сделки</h2>${asArray(contact.deals).map(deal => `<div class="task-row"><div><b>${esc(deal.title)}</b><small>${money(deal.amount)}</small></div><span class="badge ${esc(deal.stage)}">${esc(labels[deal.stage] || deal.stage)}</span></div>`).join('') || '<p class="muted">Сделок нет</p>'}</div><div class="panel"><h2>Задачи</h2>${taskRows(asArray(contact.tasks))}</div></div></div>`;

  if (phone) $('#callback-button').onclick = () => initiateCall(contact);
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
  const rows = asArray(await api('/api/deals'));
  const stages = ['new', 'qualified', 'proposal', 'negotiation', 'won', 'lost'];
  $('#content').innerHTML = `<div class="pipeline">${stages.map(stage => `<section class="stage"><h3>${labels[stage]} · ${rows.filter(deal => deal.stage === stage).length}</h3>${rows.filter(deal => deal.stage === stage).map(deal => `<div class="deal-card"><b>${esc(deal.title)}</b><small>${esc(deal.contact_name || deal.phone_normalized || '')}<br>${money(deal.amount)}</small></div>`).join('')}</section>`).join('')}</div>`;
}

async function admin() {
  setHead('Обработка', 'ТЕХНИЧЕСКОЕ СОСТОЯНИЕ');
  const [jobs, mappingsResponse] = await Promise.all([
    api('/api/admin/jobs'),
    api('/api/admin/novofon/employee-mappings').catch(() => []),
  ]);
  const mappings = asArray(mappingsResponse?.items || mappingsResponse);
  const ownMapping = mappings.find(mapping => String(mapping.crm_user_id || mapping.user_id) === String(state.user.id)) || {};
  const mappingRows = mappings.length
    ? `<table class="table mapping-table"><thead><tr><th>Пользователь CRM</th><th>Сотрудник Novofon</th><th>Мобильный</th><th>Внутренний</th></tr></thead><tbody>${mappings.map(mapping => `<tr><td>${esc(mapping.crm_user_name || mapping.user_display_name || mapping.crm_user_id || mapping.user_id)}</td><td>${esc(mapping.provider_employee_id || mapping.employee_id)}</td><td>${esc(mapping.mobile_phone || '—')}</td><td>${esc(mapping.provider_extension || mapping.extension_phone_number || '—')}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">Пока нет сопоставлений. Добавьте сотрудника, который будет принимать callback-звонки.</p>';

  $('#content').innerHTML = `<div class="admin-stack"><section class="panel"><div class="panel-head"><div><h2>Сотрудники Novofon</h2><small class="muted">Нужны только для callback-звонка. Ключи API здесь не хранятся.</small></div>${sourcePill({ source: 'novofon' })}</div><form class="form mapping-form" id="novofon-mapping-form"><label>Пользователь CRM<input name="crm_user_id" value="${esc(ownMapping.crm_user_id || ownMapping.user_id || state.user.id)}" readonly></label><label>ID сотрудника Novofon<input name="provider_employee_id" inputmode="numeric" required value="${esc(ownMapping.provider_employee_id || ownMapping.employee_id || '')}" placeholder="Например, 12345"></label><label>Мобильный сотрудника<input name="mobile_phone" inputmode="tel" required value="${esc(ownMapping.mobile_phone || '')}" placeholder="79001234567"></label><label>Внутренний номер (необязательно)<input name="provider_extension" inputmode="numeric" value="${esc(ownMapping.provider_extension || ownMapping.extension_phone_number || '')}"></label><label>Имя для CRM (необязательно)<input name="display_name" value="${esc(ownMapping.display_name || ownMapping.employee_full_name || '')}"></label><button class="primary" type="submit">Сохранить сотрудника</button></form><div id="mapping-error" class="error"></div></section><section class="panel"><h2>Настроенные сопоставления</h2>${mappingRows}</section><section class="panel"><h2>Очередь обработки</h2><table class="table"><thead><tr><th>Звонок</th><th>Этап</th><th>Статус</th><th>Попытки</th><th>Ошибка</th><th>Обновлено</th></tr></thead><tbody>${asArray(jobs).map(job => `<tr class="clickable-row" data-action="call-detail" data-call-id="${Number(job.call_id)}"><td>${esc(job.call_id)}</td><td>${esc(job.kind)}</td><td><span class="badge ${esc(job.status)}">${esc(job.status)}</span></td><td>${job.attempts || 0}/${job.max_attempts || 0}</td><td>${esc(job.last_error || '—')}</td><td>${fmtDate(job.updated_at)}</td></tr>`).join('')}</tbody></table></section></div>`;

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
}

start();
