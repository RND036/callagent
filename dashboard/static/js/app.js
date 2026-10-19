// Front Desk & Voice Receptionist Client Logic
(() => {
  // Any API call that comes back 401 means the session expired or was never
  // established (e.g. a direct hit on /static/index.html) -- bounce to login.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await nativeFetch(...args);
    if (res.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
    }
    return res;
  };

  let ws = null;
  let activeCalls = [];
  let allBookings = [];
  let callHistory = [];
  let messages = [];
  let services = [];
  let faqs = [];
  let timerInterval = null;

  // Header & Brand Elements
  const brandTitle = document.getElementById('brandTitle');
  const salonPhoneHeader = document.getElementById('salonPhoneHeader');
  const standbyHotline = document.getElementById('standbyHotline');
  const connectionStatusText = document.getElementById('connectionStatusText');
  const pageHeadingTitle = document.getElementById('pageHeadingTitle');

  // KPI Counters (Live Calls Tab)
  const valActiveCalls = document.getElementById('valActiveCalls');
  const valTodayCalls = document.getElementById('valTodayCalls');
  const valConfirmedBookings = document.getElementById('valConfirmedBookings');
  const valTotalBookings = document.getElementById('valTotalBookings');
  const valMessages = document.getElementById('valMessages');
  const badgeLivePulse = document.getElementById('badgeLivePulse');
  const badgeUrgentMessages = document.getElementById('badgeUrgentMessages');
  const cardActiveCalls = document.getElementById('cardActiveCalls');

  // Tab counters
  const activeCallTabCount = document.getElementById('activeCallTabCount');
  const bookingTabCount = document.getElementById('bookingTabCount');
  const messagesTabCount = document.getElementById('messagesTabCount');
  const servicesTabCount = document.getElementById('servicesTabCount');

  // Containers & Lists
  const activeCallsContainer = document.getElementById('activeCallsContainer');
  const noActiveCallsState = document.getElementById('noActiveCallsState');
  const eventStreamFeed = document.getElementById('eventStreamFeed');

  const bookingsTableBody = document.getElementById('bookingsTableBody');
  const noBookingsState = document.getElementById('noBookingsState');
  const bookingSearchInput = document.getElementById('bookingSearchInput');
  const bookingStatusFilter = document.getElementById('bookingStatusFilter');

  const servicesTableBody = document.getElementById('servicesTableBody');
  const serviceSearchInput = document.getElementById('serviceSearchInput');
  const btnOpenAddService = document.getElementById('btnOpenAddService');

  // Service Modal Elements
  const serviceModal = document.getElementById('serviceModal');
  const serviceModalTitle = document.getElementById('serviceModalTitle');
  const serviceModalForm = document.getElementById('serviceModalForm');
  const serviceOriginalName = document.getElementById('serviceOriginalName');
  const serviceModalName = document.getElementById('serviceModalName');
  const serviceModalPrice = document.getElementById('serviceModalPrice');
  const serviceModalDuration = document.getElementById('serviceModalDuration');
  const serviceModalPriceVaries = document.getElementById('serviceModalPriceVaries');
  const btnCloseServiceModal = document.getElementById('btnCloseServiceModal');
  const btnCancelServiceModal = document.getElementById('btnCancelServiceModal');

  const callHistoryTableBody = document.getElementById('callHistoryTableBody');
  const noCallsState = document.getElementById('noCallsState');

  const messagesGrid = document.getElementById('messagesGrid');
  const noMessagesState = document.getElementById('noMessagesState');

  // Settings Elements
  const settingsForm = document.getElementById('settingsForm');
  const settingSalonName = document.getElementById('settingSalonName');
  const settingSalonPhone = document.getElementById('settingSalonPhone');
  const settingTransferPhone = document.getElementById('settingTransferPhone');
  const settingSalonAddress = document.getElementById('settingSalonAddress');
  const settingAddressSinhala = document.getElementById('settingAddressSinhala');
  const settingAddressTamil = document.getElementById('settingAddressTamil');
  const settingOperatingHours = document.getElementById('settingOperatingHours');
  const settingHoursStart = document.getElementById('settingHoursStart');
  const settingHoursEnd = document.getElementById('settingHoursEnd');
  const settingGreetingSinhala = document.getElementById('settingGreetingSinhala');
  const settingGreetingTamil = document.getElementById('settingGreetingTamil');
  const settingGreetingEnglish = document.getElementById('settingGreetingEnglish');
  const settingAiVoice = document.getElementById('settingAiVoice');
  const settingAiTemperature = document.getElementById('settingAiTemperature');
  const settingAiSilenceMs = document.getElementById('settingAiSilenceMs');
  const settingBranches = document.getElementById('settingBranches');
  const settingWhatsapp = document.getElementById('settingWhatsapp');
  const btnSaveSettings = document.getElementById('btnSaveSettings');
  const saveStatusIndicator = document.getElementById('saveStatusIndicator');

  // Actions
  const btnSimulateCall = document.getElementById('btnSimulateCall');
  const btnRefresh = document.getElementById('btnRefresh');
  const btnLogout = document.getElementById('btnLogout');
  const toastContainer = document.getElementById('toastContainer');

  const tabTitles = {
    liveCallsTab: 'Live Calls & Console',
    bookingsTab: 'Appointments & Schedule',
    servicesTab: 'Services & Pricing Menu',
    faqTab: 'FAQ for the AI Receptionist',
    callHistoryTab: 'Call History & Logs',
    messagesTab: 'Customer Voicemails',
    settingsTab: 'Salon & AI Receptionist Settings',
    ivrTab: 'Language Menu (IVR)',
  };

  // Tab Navigation Handling
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
      document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      item.classList.add('active');
      const target = item.getAttribute('data-tab');
      const pane = document.getElementById(target);
      if (pane) pane.classList.add('active');
      if (pageHeadingTitle && tabTitles[target]) {
        pageHeadingTitle.textContent = tabTitles[target];
      }
    });
  });

  // Helpers
  function formatDuration(seconds) {
    if (seconds == null || isNaN(seconds)) return '00:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  }

  function formatTime(isoStr) {
    if (!isoStr) return '--:--';
    try {
      const d = new Date(isoStr);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return isoStr;
    }
  }

  function formatDate(isoStr) {
    if (!isoStr) return '';
    try {
      const d = new Date(isoStr);
      return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
    } catch {
      return isoStr;
    }
  }

  function getInitials(name) {
    if (!name) return '•';
    const parts = name.trim().split(' ');
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase();
  }

  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `app-toast toast-${type}`;
    toast.textContent = message;
    toastContainer.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(6px)';
      toast.style.transition = 'all 0.2s ease';
      setTimeout(() => toast.remove(), 250);
    }, 3500);
  }

  // Caller speech, customer names and tool output reach the DOM, so anything
  // that isn't known-safe markup must pass through here before innerHTML.
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  function appendFeedEvent(timeStr, content) {
    const item = document.createElement('div');
    item.className = 'feed-row';
    item.innerHTML = `<span class="feed-row-time">${escapeHtml(timeStr)}</span><span class="feed-row-content">${escapeHtml(content)}</span>`;

    const placeholder = eventStreamFeed.querySelector('.placeholder');
    if (placeholder) placeholder.remove();

    eventStreamFeed.insertBefore(item, eventStreamFeed.firstChild);
    while (eventStreamFeed.children.length > 30) {
      eventStreamFeed.removeChild(eventStreamFeed.lastChild);
    }
  }

  let speechEnabled = true;
  let isSpeaking = false;

  // Live Gemini voice simulation state (mic -> /ws/simulate-call -> Gemini Live -> speakers)
  let simState = null;

  function setSimSpeaking(active) {
    isSpeaking = active;
    document.querySelectorAll('.audio-waveform').forEach(w => w.classList.toggle('playing', active));
    document.querySelectorAll('.speaker-badge-text').forEach(b => {
      b.textContent = active ? '🔊 AI Speaking (Quoting LKR)' : '🎧 Listening to Caller';
    });
  }

  function downsampleTo16kMono(float32Arr, inputRate) {
    const outputRate = 16000;
    if (inputRate === outputRate) return float32Arr;
    const ratio = inputRate / outputRate;
    const newLength = Math.round(float32Arr.length / ratio);
    const result = new Float32Array(newLength);
    for (let i = 0; i < newLength; i++) {
      const srcIndex = i * ratio;
      const idx0 = Math.floor(srcIndex);
      const idx1 = Math.min(idx0 + 1, float32Arr.length - 1);
      const frac = srcIndex - idx0;
      result[i] = float32Arr[idx0] * (1 - frac) + float32Arr[idx1] * frac;
    }
    return result;
  }

  function floatTo16BitPCM(float32Arr) {
    const out = new Int16Array(float32Arr.length);
    for (let i = 0; i < float32Arr.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Arr[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  function playIncomingAudio(arrayBuffer) {
    if (!simState) return;
    const int16 = new Int16Array(arrayBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 0x8000;

    const { playCtx, outputGain } = simState;
    const buffer = playCtx.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);
    const src = playCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(outputGain);

    const startAt = Math.max(simState.nextPlayTime, playCtx.currentTime);
    src.start(startAt);
    simState.nextPlayTime = startAt + buffer.duration;
    simState.scheduledSources.push(src);
    setSimSpeaking(true);
    src.onended = () => {
      if (!simState) return;
      simState.scheduledSources = simState.scheduledSources.filter(s => s !== src);
      if (simState.scheduledSources.length === 0) setSimSpeaking(false);
    };
  }

  function handleSimControlMessage(msg) {
    if (msg.type === 'error') {
      showToast(msg.message || 'Simulated call failed to start', 'error');
      stopSimulatedCall(false);
      return;
    }
    if (!simState) return;
    if (msg.type === 'ready') {
      simState.callId = msg.call_id;
    } else if (msg.type === 'interrupted') {
      simState.scheduledSources.forEach(s => { try { s.stop(); } catch (e) { /* already stopped */ } });
      simState.scheduledSources = [];
      simState.nextPlayTime = simState.playCtx.currentTime;
      setSimSpeaking(false);
    }
  }

  async function startSimulatedCall() {
    if (simState) return;
    let micStream;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      showToast('Microphone access is required to talk to the AI receptionist', 'error');
      throw e;
    }

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const micCtx = new AudioCtx();
    const playCtx = new AudioCtx();
    const outputGain = playCtx.createGain();
    outputGain.gain.value = speechEnabled ? 1 : 0;
    outputGain.connect(playCtx.destination);

    const source = micCtx.createMediaStreamSource(micStream);
    const processor = micCtx.createScriptProcessor(4096, 1, 1);
    const silentGain = micCtx.createGain();
    silentGain.gain.value = 0;
    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(micCtx.destination);

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Stands in for the phone IVR menu: the agent speaks only this language.
    const langSelect = document.getElementById('simulateCallLanguage');
    const lang = encodeURIComponent(langSelect ? langSelect.value : 'si');
    const ws = new WebSocket(`${protocol}//${location.host}/ws/simulate-call?lang=${lang}`);
    ws.binaryType = 'arraybuffer';

    simState = {
      ws, micCtx, micStream, processor, playCtx, outputGain,
      nextPlayTime: 0, scheduledSources: [], callId: null,
    };

    processor.onaudioprocess = (e) => {
      if (!simState || ws.readyState !== WebSocket.OPEN) return;
      const input = e.inputBuffer.getChannelData(0);
      const down = downsampleTo16kMono(input, micCtx.sampleRate);
      const pcm16 = floatTo16BitPCM(down);
      ws.send(pcm16.buffer);
    };

    ws.onopen = () => showToast('Microphone connected — speak to the AI receptionist', 'info');
    ws.onmessage = (evt) => {
      if (typeof evt.data === 'string') {
        try { handleSimControlMessage(JSON.parse(evt.data)); } catch (e) { /* ignore malformed frame */ }
      } else {
        playIncomingAudio(evt.data);
      }
    };
    ws.onclose = () => stopSimulatedCall(false);
    ws.onerror = () => showToast('Simulated call connection error', 'error');
  }

  function stopSimulatedCall(sendHangup = true) {
    if (!simState) return;
    const s = simState;
    simState = null;
    setSimSpeaking(false);
    if (sendHangup) {
      try {
        if (s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: 'hangup' }));
      } catch (e) { /* socket already closing */ }
    }
    try { s.ws.close(); } catch (e) { /* already closed */ }
    try { s.processor.disconnect(); } catch (e) { /* already disconnected */ }
    try { s.micStream.getTracks().forEach(t => t.stop()); } catch (e) { /* already stopped */ }
    try { s.micCtx.close(); } catch (e) { /* already closed */ }
    try { s.playCtx.close(); } catch (e) { /* already closed */ }
    if (btnSimulateCall) {
      btnSimulateCall.disabled = false;
      const label = btnSimulateCall.querySelector('span');
      if (label) label.textContent = 'Test Inbound Call';
    }
  }

  // Render Methods
  function renderActiveCalls() {
    activeCallTabCount.textContent = activeCalls.length;
    valActiveCalls.textContent = activeCalls.length;

    if (activeCalls.length > 0) {
      badgeLivePulse.style.display = 'inline-block';
      if (cardActiveCalls) cardActiveCalls.classList.add('active-alert');
      activeCallTabCount.classList.add('live-active');
    } else {
      badgeLivePulse.style.display = 'none';
      if (cardActiveCalls) cardActiveCalls.classList.remove('active-alert');
      activeCallTabCount.classList.remove('live-active');
    }

    if (activeCalls.length === 0) {
      activeCallsContainer.innerHTML = '';
      activeCallsContainer.appendChild(noActiveCallsState);
      noActiveCallsState.style.display = 'block';
      return;
    }

    noActiveCallsState.style.display = 'none';
    activeCallsContainer.innerHTML = '';

    activeCalls.forEach(call => {
      const card = document.createElement('div');
      card.className = 'call-item-card';
      card.id = `call-card-${call.call_id}`;

      let startMs = new Date(call.started_at).getTime();
      let elapsedSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));

      const actionsHtml = (call.actions || []).slice(-6).map(act => {
        const isAi = act.action.startsWith('AI') || (act.details && act.details.speaker === 'ai');
        const isCaller = act.action.startsWith('Caller') || (act.details && act.details.speaker === 'caller');
        const roleClass = isAi ? 'ai' : (isCaller ? 'caller' : '');
        const roleLabel = isAi ? 'AI VOICE RECEPTIONIST' : (isCaller ? 'INBOUND CALLER' : 'SYSTEM LOG');

        return `
          <div class="dialogue-turn-row ${roleClass}">
            <div style="display: flex; justify-content: space-between; align-items: baseline;">
              <span class="dialogue-speaker">${roleLabel}</span>
              <span class="log-timestamp">${formatTime(act.timestamp)}</span>
            </div>
            <div class="log-msg" style="margin-top: 2px;">${escapeHtml(act.action.replace(/^(AI Receptionist:|AI Spoke:|Caller:)\s*/, ''))}</div>
          </div>
        `;
      }).join('');

      card.innerHTML = `
        <div class="call-item-top">
          <div>
            <div class="call-caller-title">
              <span>${escapeHtml(call.caller_number || 'Unknown Caller')}</span>
              <span class="call-status-badge">● Active Telephony Line</span>
            </div>
            <div class="call-sid-code">CHANNEL SID: ${escapeHtml(call.call_id)}</div>
          </div>
          <div style="display: flex; align-items: center; gap: 10px;">
            <div class="call-timer-display timer-value" data-start="${call.started_at}">
              ${formatDuration(elapsedSec)}
            </div>
            <button class="btn btn-danger-subtle btn-hangup-call" data-id="${escapeHtml(call.call_id)}">
              Hang Up
            </button>
          </div>
        </div>

        <div class="audio-visualizer-strip">
          <div class="speaker-status-pill">
            <span class="pulse-dot"></span>
            <span class="speaker-badge-text">${isSpeaking ? '🔊 AI Speaking (Quoting LKR)' : 'Live Telephony Audio'}</span>
          </div>
          <div class="audio-waveform ${isSpeaking ? 'playing' : ''}">
            <span class="waveform-bar"></span>
            <span class="waveform-bar"></span>
            <span class="waveform-bar"></span>
            <span class="waveform-bar"></span>
            <span class="waveform-bar"></span>
          </div>
          <button class="btn btn-secondary btn-toggle-voice" style="padding: 3px 8px; font-size: 11.5px;">
            ${speechEnabled ? '🔊 Audio On' : '🔇 Audio Muted'}
          </button>
        </div>

        <div class="call-activity-log">
          ${actionsHtml || '<div class="log-msg">Connecting audio stream with front desk receptionist...</div>'}
        </div>
      `;
      activeCallsContainer.appendChild(card);
    });

    // Attach hangup and audio toggle listeners
    document.querySelectorAll('.btn-hangup-call').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.getAttribute('data-id');
        if (simState && id === simState.callId) {
          stopSimulatedCall();
          showToast('Simulated call ended', 'info');
          return;
        }
        try {
          await fetch(`/api/calls/${id}/hangup`, { method: 'POST' });
          showToast('Call disconnected', 'info');
        } catch {
          showToast('Failed to hang up', 'error');
        }
      });
    });

    document.querySelectorAll('.btn-toggle-voice').forEach(btn => {
      btn.addEventListener('click', () => {
        speechEnabled = !speechEnabled;
        if (simState) simState.outputGain.gain.value = speechEnabled ? 1 : 0;
        btn.textContent = speechEnabled ? '🔊 Audio On' : '🔇 Audio Muted';
        showToast(speechEnabled ? 'Voice audio enabled' : 'Voice audio muted', 'info');
      });
    });
  }

  function startDurationTickers() {
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = setInterval(() => {
      document.querySelectorAll('.timer-value').forEach(el => {
        const startIso = el.getAttribute('data-start');
        if (startIso) {
          const startMs = new Date(startIso).getTime();
          const sec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
          el.textContent = formatDuration(sec);
        }
      });
    }, 1000);
  }

  function renderBookings() {
    const query = (bookingSearchInput.value || '').trim().toLowerCase();
    const filter = bookingStatusFilter.value;

    const filtered = allBookings.filter(b => {
      const matchesFilter = filter === 'all' || b.status === filter;
      const matchesSearch = !query ||
        (b.customer_name && b.customer_name.toLowerCase().includes(query)) ||
        (b.customer_phone && b.customer_phone.toLowerCase().includes(query)) ||
        (b.service_name && b.service_name.toLowerCase().includes(query));
      return matchesFilter && matchesSearch;
    });

    bookingTabCount.textContent = allBookings.length;
    bookingsTableBody.innerHTML = '';

    if (filtered.length === 0) {
      noBookingsState.style.display = 'block';
      return;
    }
    noBookingsState.style.display = 'none';

    const pills = {
      requested: '<span class="status-pill" style="background: rgba(245,158,11,.15); color: #b45309;">Requested</span>',
      confirmed: '<span class="status-pill status-confirmed">Confirmed</span>',
      declined: '<span class="status-pill status-cancelled">Declined</span>',
      cancelled: '<span class="status-pill status-cancelled">Cancelled</span>',
    };

    filtered.forEach(b => {
      const tr = document.createElement('tr');
      const id = escapeHtml(b.booking_id);
      const active = b.status === 'requested' || b.status === 'confirmed';
      const hasChange = active && b.requested_date;
      const statusBadge = (pills[b.status] || escapeHtml(b.status)) +
        (hasChange ? `<div style="font-size: 11px; margin-top: 4px; color: #b45309;">Change requested &rarr; ${escapeHtml(b.requested_date)} ${escapeHtml(b.requested_time)}</div>` : '');

      const btns = [];
      if (b.status === 'requested') {
        btns.push(`<button class="btn btn-primary btn-booking-action" data-act="confirm" data-id="${id}" style="padding: 4px 10px; font-size: 12px;">Confirm</button>`);
        btns.push(`<button class="btn btn-danger-subtle btn-booking-action" data-act="decline" data-id="${id}">Decline</button>`);
      }
      if (hasChange) {
        btns.push(`<button class="btn btn-primary btn-booking-action" data-act="change/approve" data-id="${id}" style="padding: 4px 10px; font-size: 12px;">Approve change</button>`);
        btns.push(`<button class="btn btn-secondary btn-booking-action" data-act="change/reject" data-id="${id}" style="padding: 4px 10px; font-size: 12px;">Reject</button>`);
      }
      if (b.status === 'confirmed') {
        btns.push(`<button class="btn btn-danger-subtle btn-cancel-booking" data-id="${id}">Cancel</button>`);
      }
      const actionBtn = btns.length ? btns.join(' ') : '<span style="color: var(--text-dim); font-size: 12px;">Archived</span>';

      tr.innerHTML = `
        <td style="font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);">${escapeHtml(b.booking_id.slice(0, 8))}</td>
        <td>
          <div class="client-meta-group">
            <div class="client-avatar-monogram">${escapeHtml(getInitials(b.customer_name))}</div>
            <span class="client-name-text">${escapeHtml(b.customer_name || 'Walk-in Client')}</span>
          </div>
        </td>
        <td style="font-family: var(--font-mono); font-size: 12.5px;">${escapeHtml(b.customer_phone || '--')}</td>
        <td>
          <span class="service-pill-tag">${escapeHtml(b.service_name)}</span>
          ${b.notes ? `<div style="font-size: 11.5px; color: var(--text-muted); margin-top: 4px;">${escapeHtml(b.notes)}</div>` : ''}
        </td>
        <td>${escapeHtml(b.branch || '--')}</td>
        <td>
          <div style="font-weight: 600; color: var(--text-primary);">${escapeHtml(b.date)}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">${escapeHtml(b.time)}</div>
        </td>
        <td>${statusBadge}</td>
        <td style="text-align: right; white-space: nowrap;">${actionBtn}</td>
      `;
      bookingsTableBody.appendChild(tr);
    });

    document.querySelectorAll('.btn-booking-action').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.getAttribute('data-id');
        const act = e.target.getAttribute('data-act');
        try {
          const res = await fetch(`/api/bookings/${id}/${act}`, { method: 'POST' });
          showToast(res.ok ? 'Appointment updated' : 'Could not update the appointment', res.ok ? 'success' : 'error');
          if (res.ok) await fetchState();
        } catch (err) {
          showToast('Network error', 'error');
        }
      });
    });

    // Attach cancellation listeners
    document.querySelectorAll('.btn-cancel-booking').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const id = e.target.getAttribute('data-id');
        if (!confirm('Are you sure you want to cancel this appointment?')) return;
        try {
          const res = await fetch(`/api/bookings/${id}/cancel`, { method: 'POST' });
          if (res.ok) {
            showToast('Appointment cancelled successfully', 'success');
            await fetchState();
          } else {
            showToast('Failed to cancel booking', 'error');
          }
        } catch (err) {
          showToast('Network error while cancelling', 'error');
        }
      });
    });
  }

  function renderServices() {
    servicesTabCount.textContent = services.length;
    const query = (serviceSearchInput.value || '').trim().toLowerCase();

    const filtered = services.filter(s => {
      return !query || (s.name && s.name.toLowerCase().includes(query));
    });

    servicesTableBody.innerHTML = '';

    filtered.forEach(s => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div style="font-weight: 600; color: var(--text-primary); font-size: 13.5px;">${s.name}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">Standard salon treatment</div>
        </td>
        <td>
          ${s.price_varies ? '<span class="price-display-text">Price varies (team confirms)</span>' : `<span class="price-display-text">LKR ${Number(s.price_usd).toLocaleString()}</span>`}
        </td>
        <td>
          <span class="duration-chip">${s.duration_minutes} mins</span>
        </td>
        <td>
          <span class="status-active-tag">AI Quotable</span>
        </td>
        <td style="text-align: right;">
          <button class="btn btn-secondary btn-edit-service" data-name="${s.name}" data-price="${s.price_usd}" data-duration="${s.duration_minutes}" data-varies="${s.price_varies ? "1" : ""}" style="padding: 4px 10px; font-size: 12px; margin-right: 6px;">Edit</button>
          <button class="btn btn-danger-subtle btn-delete-service" data-name="${s.name}">Delete</button>
        </td>
      `;
      servicesTableBody.appendChild(tr);
    });

    // Attach edit listeners
    document.querySelectorAll('.btn-edit-service').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = btn.getAttribute('data-name');
        const price = btn.getAttribute('data-price');
        const duration = btn.getAttribute('data-duration');
        openServiceModal('edit', { name, price, duration, varies: btn.getAttribute('data-varies') === '1' });
      });
    });

    // Attach delete listeners
    document.querySelectorAll('.btn-delete-service').forEach(btn => {
      btn.addEventListener('click', async () => {
        const name = btn.getAttribute('data-name');
        if (!confirm(`Are you sure you want to delete treatment "${name}"? The AI receptionist will no longer quote or book this service.`)) return;
        try {
          const res = await fetch(`/api/services/${encodeURIComponent(name)}`, { method: 'DELETE' });
          if (res.ok) {
            showToast(`Treatment "${name}" deleted`, 'success');
            await fetchState();
          } else {
            showToast(`Failed to delete service`, 'error');
          }
        } catch {
          showToast(`Network error deleting service`, 'error');
        }
      });
    });
  }

  // Service Modal Handlers
  function openServiceModal(mode = 'add', serviceData = null) {
    if (mode === 'edit' && serviceData) {
      serviceModalTitle.textContent = 'Edit Treatment & Pricing';
      serviceOriginalName.value = serviceData.name;
      serviceModalName.value = serviceData.name;
      serviceModalPrice.value = serviceData.price;
      serviceModalDuration.value = serviceData.duration;
      serviceModalPriceVaries.checked = !!serviceData.varies;
    } else {
      serviceModalTitle.textContent = 'Add New Treatment';
      serviceOriginalName.value = '';
      serviceModalName.value = '';
      serviceModalPrice.value = '3500';
      serviceModalDuration.value = '45';
      serviceModalPriceVaries.checked = false;
    }
    serviceModal.style.display = 'flex';
    serviceModalName.focus();
  }

  function closeServiceModal() {
    serviceModal.style.display = 'none';
  }

  if (btnOpenAddService) {
    btnOpenAddService.addEventListener('click', () => openServiceModal('add'));
  }
  if (btnCloseServiceModal) {
    btnCloseServiceModal.addEventListener('click', closeServiceModal);
  }
  if (btnCancelServiceModal) {
    btnCancelServiceModal.addEventListener('click', closeServiceModal);
  }

  if (serviceModalForm) {
    serviceModalForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const originalName = serviceOriginalName.value.trim();
      const name = serviceModalName.value.trim();
      const price = parseFloat(serviceModalPrice.value);
      const duration = parseInt(serviceModalDuration.value, 10);

      if (!name) {
        showToast('Please enter a service name', 'error');
        return;
      }

      try {
        let res;
        if (originalName) {
          // Update existing
          res = await fetch(`/api/services/${encodeURIComponent(originalName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, price_usd: price, duration_minutes: duration, price_varies: serviceModalPriceVaries.checked }),
          });
        } else {
          // Create new
          res = await fetch('/api/services', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, price_usd: price, duration_minutes: duration, price_varies: serviceModalPriceVaries.checked }),
          });
        }

        if (res.ok) {
          closeServiceModal();
          showToast(`Service "${name}" saved! AI voice prompt updated.`, 'success');
          await fetchState();
        } else {
          const err = await res.json();
          showToast(err.detail || 'Failed to save service', 'error');
        }
      } catch (err) {
        showToast('Network error saving service', 'error');
      }
    });
  }

  // Transcript lines carry details.speaker; everything else is a system/tool action.
  function isSpokenLine(a) {
    return !!(a.details && a.details.speaker);
  }

  // Calls the AI could not finish: the staff should call the customer back.
  const CALL_BACK_STATUSES = { dropped: 'AI Dropped – Call Back', interrupted: 'Interrupted – Call Back' };

  const CUSTOMER_LABELS = {
    name: 'Name', phone: 'Phone', service: 'Service', branch: 'Branch', date: 'Date', time: 'Time',
    booking_id: 'Booking', booking_status: 'Booking status', message: 'Message',
  };

  function customerSummary(c) {
    const customer = c.customer || {};
    return Object.keys(CUSTOMER_LABELS)
      .filter(k => customer[k])
      .map(k => `${CUSTOMER_LABELS[k]}: ${customer[k]}`)
      .join(' · ');
  }

  function callOutcome(c) {
    if (CALL_BACK_STATUSES[c.status]) return CALL_BACK_STATUSES[c.status];
    const events = (c.actions || []).filter(a => a.action && !isSpokenLine(a)).map(a => a.action.toLowerCase());
    if (events.some(e => e.startsWith('booked') || e.startsWith('modified booking'))) return 'Appointment Booked';
    if (events.some(e => e.startsWith('transfer'))) return 'Transferred to Staff';
    if (events.some(e => e.startsWith('message recorded'))) return 'Voicemail Recorded';
    return 'General Inquiry';
  }

  function renderCallHistory() {
    callHistoryTableBody.innerHTML = '';
    if (callHistory.length === 0) {
      noCallsState.style.display = 'block';
      return;
    }
    noCallsState.style.display = 'none';

    callHistory.forEach(c => {
      const tr = document.createElement('tr');
      const duration = formatDuration(c.duration_seconds);
      const started = formatTime(c.started_at);
      const date = formatDate(c.started_at);
      const summary = callOutcome(c);

      tr.innerHTML = `
        <td style="font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);">${escapeHtml(c.call_id)}</td>
        <td style="font-weight: 600; color: var(--text-primary); font-family: var(--font-mono);">${escapeHtml(c.caller_number || 'Unknown')}</td>
        <td>
          <div style="color: var(--text-primary);">${escapeHtml(date)}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">${escapeHtml(started)}</div>
        </td>
        <td style="font-family: var(--font-mono); font-weight: 600;">${escapeHtml(duration)}</td>
        <td><span class="status-pill ${CALL_BACK_STATUSES[c.status] ? 'status-cancelled' : 'status-confirmed'}">${escapeHtml(summary)}</span></td>
        <td style="font-size: 12px; color: var(--text-secondary);">${c.actions ? c.actions.length : 0} interactions</td>
        <td style="text-align: right; white-space: nowrap;">
          ${c.recording_file ? '<span title="Recording available">🎧</span> ' : ''}
          <button class="btn btn-secondary btn-view-call" data-id="${escapeHtml(c.call_id)}">View</button>
        </td>
      `;
      callHistoryTableBody.appendChild(tr);
    });

    document.querySelectorAll('.btn-view-call').forEach(btn => {
      btn.addEventListener('click', () => openCallDetail(btn.dataset.id));
    });
  }

  // Call detail modal: recording player + transcript
  const callDetailModal = document.getElementById('callDetailModal');
  const callDetailMeta = document.getElementById('callDetailMeta');
  const callDetailRecording = document.getElementById('callDetailRecording');
  const callDetailTranscript = document.getElementById('callDetailTranscript');

  function closeCallDetail() {
    // Dropping the markup also stops any audio that is still playing.
    callDetailRecording.innerHTML = '';
    callDetailModal.style.display = 'none';
  }

  // Asterisk's view of the line: how many of the agent's packets the caller's side lost. Over 2% is audible.
  function lineQualityChip(c) {
    const q = c.line_quality;
    if (!q || !q.txcount) return '';
    const lost = Math.round((1000 * (q.rlp || 0)) / q.txcount) / 10;
    const rtt = q.rtt ? ` · rtt ${Math.round(q.rtt * 1000)} ms` : '';
    const warn = lost > 2 ? ' style="color: var(--danger, #c0392b); font-weight: 600;"' : '';
    return `<span${warn} title="Packets we sent that the caller's side lost">Line: ${lost}% lost to caller${rtt}</span>`;
  }

  function openCallDetail(callId) {
    const c = callHistory.find(x => x.call_id === callId);
    if (!c) return;

    callDetailMeta.innerHTML = `
      <span><strong>${escapeHtml(c.caller_number || 'Unknown')}</strong></span>
      <span>${escapeHtml(formatDate(c.started_at))} ${escapeHtml(formatTime(c.started_at))}</span>
      <span>Duration <strong>${escapeHtml(formatDuration(c.duration_seconds))}</strong></span>
      <span>${escapeHtml(callOutcome(c))}</span>
      ${c.reconnects ? `<span>AI reconnected ${escapeHtml(String(c.reconnects))}×</span>` : ''}
      ${lineQualityChip(c)}
      ${customerSummary(c) ? `<span>${escapeHtml(customerSummary(c))}</span>` : ''}
    `;

    if (c.recording_file) {
      const url = `/api/calls/${encodeURIComponent(c.call_id)}/recording`;
      callDetailRecording.innerHTML = `
        <div class="call-detail-audio">
          <div class="call-detail-section-label">RECORDING</div>
          <audio controls preload="none" src="${url}"></audio>
          <a class="btn btn-secondary" href="${url}" download style="align-self: flex-start;">Download recording</a>
        </div>
      `;
    } else {
      callDetailRecording.innerHTML = '<div class="call-detail-nofile">No recording for this call.</div>';
    }

    callDetailTranscript.innerHTML = '';
    const lines = c.actions || [];
    lines.forEach(a => {
      const spoken = isSpokenLine(a);
      const role = spoken ? a.details.speaker : 'system';
      const text = spoken ? a.details.text : a.action;
      const who = role === 'ai' ? 'AI RECEPTIONIST' : (role === 'caller' ? 'CALLER' : '');
      const div = document.createElement('div');
      div.className = `transcript-line ${role}`;
      div.innerHTML = `
        <div class="transcript-who"><span>${escapeHtml(who)}</span><span>${escapeHtml(formatTime(a.timestamp))}</span></div>
        <div>${escapeHtml(text)}</div>
      `;
      callDetailTranscript.appendChild(div);
    });
    if (!lines.length) {
      callDetailTranscript.innerHTML = '<div class="call-detail-nofile">No transcript recorded.</div>';
    }

    callDetailModal.style.display = 'flex';
  }

  document.getElementById('btnCloseCallDetail').addEventListener('click', closeCallDetail);
  document.getElementById('btnDoneCallDetail').addEventListener('click', closeCallDetail);
  callDetailModal.addEventListener('click', e => {
    if (e.target === callDetailModal) closeCallDetail();
  });

  function renderMessages() {
    messagesTabCount.textContent = messages.length;
    messagesGrid.innerHTML = '';

    if (messages.length === 0) {
      noMessagesState.style.display = 'block';
      return;
    }
    noMessagesState.style.display = 'none';

    messages.forEach(m => {
      const card = document.createElement('div');
      const done = m.status === 'done';
      const catLabels = {complaint: 'Complaint', price_inquiry: 'Price inquiry', treatment_inquiry: 'Treatment inquiry', location_link: 'Send location link', callback: 'Callback'};
      const cat = catLabels[m.category] ? `<span class="status-pill status-confirmed">${catLabels[m.category]}</span> ` : '';
      card.className = `message-card-item ${m.urgent && !done ? 'urgent' : ''}`;
      if (done) card.style.opacity = '0.55';
      card.innerHTML = `
        <div class="msg-header-line">
          <span class="msg-caller-name">${escapeHtml(m.caller_name || 'Anonymous Caller')}</span>
          <span class="msg-phone-chip">${escapeHtml(m.caller_phone || 'No phone')}</span>
        </div>
        <div style="margin-bottom: 6px;">${cat}${m.branch ? `<span style="font-size: 12px; color: var(--text-muted);">${escapeHtml(m.branch)} branch</span>` : ''}</div>
        <div class="msg-content-text">"${escapeHtml(m.message)}"</div>
        <div class="msg-footer-line">
          <span>${formatDate(m.taken_at)} at ${formatTime(m.taken_at)}</span>
          ${m.urgent && !done ? '<span class="status-pill status-cancelled">Urgent Callback</span>' : '<span style="color: var(--text-muted); font-size: 11px;">Standard Message</span>'}
          ${m.message_id && !done ? `<button class="btn btn-secondary btn-msg-done" data-id="${escapeHtml(m.message_id)}" style="padding: 3px 10px; font-size: 12px;">Mark done</button>` : ''}
          ${done ? '<span style="font-size: 11px;">Done</span>' : ''}
        </div>
      `;
      const doneBtn = card.querySelector('.btn-msg-done');
      if (doneBtn) {
        doneBtn.addEventListener('click', async () => {
          const res = await fetch(`/api/messages/${doneBtn.getAttribute('data-id')}/done`, { method: 'POST' });
          if (res.ok) await fetchState();
        });
      }
      messagesGrid.appendChild(card);
    });
  }

  // Settings Management
  async function loadSettings() {
    try {
      const res = await fetch('/api/settings');
      if (res.ok) {
        const s = await res.json();
        if (settingSalonName) settingSalonName.value = s.salon_name || '';
        if (settingSalonPhone) settingSalonPhone.value = s.salon_phone || '';
        if (settingTransferPhone) settingTransferPhone.value = s.transfer_phone || '';
        if (settingSalonAddress) settingSalonAddress.value = s.salon_address || '';
        if (settingAddressSinhala) settingAddressSinhala.value = s.salon_address_sinhala || '';
        if (settingAddressTamil) settingAddressTamil.value = s.salon_address_tamil || '';
        if (settingOperatingHours) settingOperatingHours.value = s.operating_hours_display || '';
        if (settingHoursStart) settingHoursStart.value = s.business_hours_start || '09:00';
        if (settingHoursEnd) settingHoursEnd.value = s.business_hours_end || '19:00';
        if (settingGreetingSinhala) settingGreetingSinhala.value = s.greeting_sinhala || '';
        if (settingGreetingTamil) settingGreetingTamil.value = s.greeting_tamil || '';
        if (settingGreetingEnglish) settingGreetingEnglish.value = s.greeting_english || '';
        if (settingAiVoice) {
          const voices = s.ai_voices || [];
          if (s.ai_voice && !voices.includes(s.ai_voice)) voices.push(s.ai_voice);
          settingAiVoice.innerHTML = voices.map(v => `<option value="${v}">${v}</option>`).join('');
          settingAiVoice.value = s.ai_voice || '';
        }
        if (settingBranches) settingBranches.value = s.branches || '';
        if (settingWhatsapp) settingWhatsapp.value = s.whatsapp_number || '';
        if (settingAiTemperature) settingAiTemperature.value = s.ai_temperature || '';
        if (settingAiSilenceMs) settingAiSilenceMs.value = s.ai_vad_silence_ms || 1000;

        // Update brand in top bar & sidebar
        if (brandTitle) brandTitle.textContent = s.salon_name || 'Classy Nail Spa';
        if (salonPhoneHeader) salonPhoneHeader.textContent = s.salon_phone || '+94 77 741 2853';
        if (standbyHotline) standbyHotline.textContent = s.salon_phone || '+94 77 741 2853';
      }
    } catch (e) {
      console.error('Error loading settings:', e);
    }
  }

  if (settingsForm) {
    settingsForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      btnSaveSettings.disabled = true;
      saveStatusIndicator.textContent = 'Saving...';
      saveStatusIndicator.className = 'save-status-badge';

      const payload = {
        salon_name: settingSalonName.value.trim(),
        salon_phone: settingSalonPhone.value.trim(),
        transfer_phone: settingTransferPhone.value.trim(),
        salon_address: settingSalonAddress.value.trim(),
        salon_address_sinhala: settingAddressSinhala.value.trim(),
        salon_address_tamil: settingAddressTamil.value.trim(),
        operating_hours_display: settingOperatingHours.value.trim(),
        business_hours_start: settingHoursStart.value,
        business_hours_end: settingHoursEnd.value,
        greeting_sinhala: settingGreetingSinhala.value.trim(),
        greeting_tamil: settingGreetingTamil.value.trim(),
        greeting_english: settingGreetingEnglish.value.trim(),
        branches: settingBranches.value.trim(),
        whatsapp_number: settingWhatsapp.value.trim(),
        ai_voice: settingAiVoice.value,
        ai_temperature: settingAiTemperature.value.trim(),
        ai_vad_silence_ms: parseInt(settingAiSilenceMs.value, 10) || 1000,
      };

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          saveStatusIndicator.textContent = 'Settings saved to PBX system';
          saveStatusIndicator.className = 'save-status-badge success';
          showToast('Salon settings & AI prompts updated successfully', 'success');
          loadSettings();
        } else {
          saveStatusIndicator.textContent = 'Failed to save settings';
          saveStatusIndicator.className = 'save-status-badge error';
          let msg = 'Failed to save settings';
          try { msg = (await res.json()).detail || msg; } catch (_) {}
          saveStatusIndicator.textContent = msg;
          showToast(msg, 'error');
        }
      } catch (err) {
        saveStatusIndicator.textContent = 'Error connecting to server';
        saveStatusIndicator.className = 'save-status-badge error';
        showToast('Network error saving settings', 'error');
      } finally {
        btnSaveSettings.disabled = false;
        setTimeout(() => {
          saveStatusIndicator.textContent = '';
        }, 4000);
      }
    });
  }

  // Filter Listeners
  if (bookingSearchInput) {
    bookingSearchInput.addEventListener('input', renderBookings);
  }
  if (bookingStatusFilter) {
    bookingStatusFilter.addEventListener('change', renderBookings);
  }
  if (serviceSearchInput) {
    serviceSearchInput.addEventListener('input', renderServices);
  }

  // Data Fetching
  async function fetchState() {
    try {
      const [statsRes, activeRes, historyRes, bookingsRes, msgRes, servicesRes, faqRes] = await Promise.all([
        fetch('/api/stats').then(r => r.json()),
        fetch('/api/calls/active').then(r => r.json()),
        fetch('/api/calls/history').then(r => r.json()),
        fetch('/api/bookings').then(r => r.json()),
        fetch('/api/messages').then(r => r.json()),
        fetch('/api/services').then(r => r.json()),
        fetch('/api/faqs').then(r => r.json()),
      ]);

      activeCalls = activeRes;
      callHistory = historyRes;
      allBookings = bookingsRes;
      messages = msgRes;
      services = servicesRes;
      faqs = faqRes;

      valTodayCalls.textContent = statsRes.today_calls || 0;
      valConfirmedBookings.textContent = statsRes.confirmed_bookings || 0;
      valTotalBookings.textContent = `${statsRes.total_bookings || 0} total`;
      valMessages.textContent = statsRes.total_messages || 0;

      if (statsRes.urgent_messages > 0) {
        badgeUrgentMessages.style.display = 'inline-block';
        badgeUrgentMessages.textContent = `${statsRes.urgent_messages} Urgent`;
      } else {
        badgeUrgentMessages.style.display = 'none';
      }

      renderActiveCalls();
      renderBookings();
      renderServices();
      renderCallHistory();
      renderMessages();
      renderFaqs();
    } catch (err) {
      console.error('Error fetching state:', err);
    }
  }

  // Simulation Trigger — opens a real mic <-> Gemini Live bridge (see /ws/simulate-call)
  if (btnSimulateCall) {
    btnSimulateCall.addEventListener('click', async () => {
      btnSimulateCall.disabled = true;
      try {
        await startSimulatedCall();
        const label = btnSimulateCall.querySelector('span');
        if (label) label.textContent = 'Simulated Call Active';
      } catch (e) {
        btnSimulateCall.disabled = false;
      }
    });
  }

  if (btnLogout) {
    btnLogout.addEventListener('click', async () => {
      try {
        await nativeFetch('/api/logout', { method: 'POST' });
      } finally {
        window.location.href = '/login';
      }
    });
  }

  if (btnRefresh) {
    btnRefresh.addEventListener('click', async () => {
      btnRefresh.style.transform = 'rotate(180deg)';
      btnRefresh.style.transition = 'transform 0.4s ease';
      await fetchState();
      await loadSettings();
      setTimeout(() => {
        btnRefresh.style.transform = 'none';
      }, 400);
      showToast('Data synchronized', 'info');
    });
  }

  // WebSocket Live Sync
  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws/live`);

    ws.onopen = () => {
      if (connectionStatusText) connectionStatusText.textContent = 'Bridge Online';
      const dot = document.querySelector('.telecom-dot');
      if (dot) dot.classList.remove('offline');
      appendFeedEvent(formatTime(new Date().toISOString()), 'PBX audio stream bridge connected (Port 8766)');
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        handleWsEvent(msg);
      } catch (err) {
        console.error('Failed to parse WS payload', err);
      }
    };

    ws.onclose = () => {
      if (connectionStatusText) connectionStatusText.textContent = 'Bridge Disconnected';
      const dot = document.querySelector('.telecom-dot');
      if (dot) dot.classList.add('offline');
      setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  function handleWsEvent(msg) {
    const type = msg.type;
    const data = msg.data;
    const timeStr = formatTime(msg.timestamp || new Date().toISOString());

    if (type === 'initial_state') {
      activeCalls = data.active_calls || [];
      renderActiveCalls();
      return;
    }

    if (type === 'call_started') {
      activeCalls.push(data);
      renderActiveCalls();
      appendFeedEvent(timeStr, `Call started: ${data.caller_number} (SID: ${data.call_id})`);
      showToast(`Inbound call: ${data.caller_number}`, 'info');
    } else if (type === 'call_action') {
      const idx = activeCalls.findIndex(c => c.call_id === data.call_id);
      if (idx !== -1) {
        if (!activeCalls[idx].actions) activeCalls[idx].actions = [];
        // data.action is the whole action object ({timestamp, action, details}).
        activeCalls[idx].actions.push(data.action);
        renderActiveCalls();
      }
      appendFeedEvent(timeStr, `[${data.call_id}] ${data.action.action}`);
    } else if (type === 'call_updated') {
      const idx = activeCalls.findIndex(c => c.call_id === data.call_id);
      if (idx !== -1) {
        activeCalls[idx].customer = data.customer;
        renderActiveCalls();
      }
    } else if (type === 'call_ended') {
      if (simState && data.call_id === simState.callId) {
        stopSimulatedCall(false);
      }
      activeCalls = activeCalls.filter(c => c.call_id !== data.call_id);
      renderActiveCalls();
      appendFeedEvent(timeStr, `Call ended: ${data.caller_number} (${data.duration_seconds}s)`);
      fetchState();
    } else if (type === 'service_updated' || type === 'service_deleted') {
      fetchState();
    } else if (type === 'settings_updated') {
      loadSettings();
    } else if (type === 'ivr_updated') {
      renderIvr(data);
    } else if (type === 'booking_cancelled' || type === 'booking_created' || type === 'booking_modified' || type === 'faqs_updated' || type === 'message_updated' || type === 'message_taken') {
      fetchState();
    }
  }

  // FAQ management
  const faqList = document.getElementById('faqList');
  const noFaqState = document.getElementById('noFaqState');
  const faqTabCount = document.getElementById('faqTabCount');
  const faqModal = document.getElementById('faqModal');
  const faqModalForm = document.getElementById('faqModalForm');

  function renderFaqs() {
    if (!faqList) return;
    faqTabCount.textContent = faqs.length;
    faqList.innerHTML = '';
    noFaqState.style.display = faqs.length ? 'none' : 'block';
    faqs.forEach(f => {
      const card = document.createElement('div');
      card.className = 'message-card-item';
      const empty = !(f.answer || '').trim();
      if (!f.enabled || empty) card.style.opacity = '0.65';
      card.innerHTML = `
        <div class="msg-header-line">
          <span class="msg-caller-name">${escapeHtml(f.question)}</span>
          ${empty ? '<span class="status-pill status-cancelled">No answer yet: AI skips it</span>' : (f.enabled ? '<span class="status-pill status-confirmed">AI uses this</span>' : '<span class="status-pill status-cancelled">Off</span>')}
        </div>
        <div class="msg-content-text" style="white-space: pre-wrap;">${escapeHtml(f.answer || '')}</div>
        <div class="msg-footer-line">
          <span></span>
          <span>
            <button class="btn btn-secondary btn-faq-edit" style="padding: 4px 10px; font-size: 12px;">Edit</button>
            <button class="btn btn-danger-subtle btn-faq-delete">Delete</button>
          </span>
        </div>`;
      card.querySelector('.btn-faq-edit').addEventListener('click', () => openFaqModal(f));
      card.querySelector('.btn-faq-delete').addEventListener('click', async () => {
        if (!confirm('Delete this FAQ entry?')) return;
        const res = await fetch(`/api/faqs/${encodeURIComponent(f.faq_id)}`, { method: 'DELETE' });
        showToast(res.ok ? 'FAQ deleted' : 'Failed to delete FAQ', res.ok ? 'success' : 'error');
        if (res.ok) await fetchState();
      });
      faqList.appendChild(card);
    });
  }

  function openFaqModal(f) {
    document.getElementById('faqModalTitle').textContent = f ? 'Edit FAQ' : 'Add FAQ';
    document.getElementById('faqModalId').value = f ? f.faq_id : '';
    document.getElementById('faqModalQuestion').value = f ? f.question : '';
    document.getElementById('faqModalAnswer').value = f ? f.answer : '';
    document.getElementById('faqModalEnabled').checked = f ? !!f.enabled : true;
    faqModal.style.display = 'flex';
    document.getElementById('faqModalQuestion').focus();
  }
  function closeFaqModal() { faqModal.style.display = 'none'; }

  const btnOpenAddFaq = document.getElementById('btnOpenAddFaq');
  if (btnOpenAddFaq) btnOpenAddFaq.addEventListener('click', () => openFaqModal(null));
  ['btnCloseFaqModal', 'btnCancelFaqModal'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', closeFaqModal);
  });
  if (faqModalForm) {
    faqModalForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = document.getElementById('faqModalId').value;
      const body = {
        question: document.getElementById('faqModalQuestion').value.trim(),
        answer: document.getElementById('faqModalAnswer').value.trim(),
        enabled: document.getElementById('faqModalEnabled').checked,
      };
      try {
        const res = await fetch(id ? `/api/faqs/${encodeURIComponent(id)}` : '/api/faqs', {
          method: id ? 'PUT' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (res.ok) {
          closeFaqModal();
          showToast('FAQ saved. The AI uses it from the next call.', 'success');
          await fetchState();
        } else {
          showToast((await res.json()).detail || 'Failed to save FAQ', 'error');
        }
      } catch (err) {
        showToast('Network error saving FAQ', 'error');
      }
    });
  }

  // Language Menu (IVR)
  const IVR_KEYS = ['1','2','3','4','5','6','7','8','9','0','*','#'];
  const ivrForm = document.getElementById('ivrForm');
  let ivrState = null;
  let ivrOptions = [];

  function ivrLanguageOptions(selected) {
    const langs = (ivrState && ivrState.languages) || {};
    return Object.entries(langs).map(([code, name]) =>
      `<option value="${escapeHtml(code)}"${code === selected ? ' selected' : ''}>${escapeHtml(name)}</option>`).join('');
  }

  function renderIvrOptions() {
    const box = document.getElementById('ivrOptions');
    box.innerHTML = ivrOptions.map((o, i) => `
      <div class="ivr-option-row" data-index="${i}">
        <span>Press</span>
        <select class="form-input ivr-key">${IVR_KEYS.map(k => `<option${k === o.digit ? ' selected' : ''}>${k}</option>`).join('')}</select>
        <span>for</span>
        <select class="form-input ivr-lang">${ivrLanguageOptions(o.language)}</select>
        <button type="button" class="btn btn-secondary ivr-remove">Remove</button>
      </div>`).join('');
    box.querySelectorAll('.ivr-option-row').forEach(row => {
      const i = Number(row.dataset.index);
      row.querySelector('.ivr-key').addEventListener('change', e => { ivrOptions[i].digit = e.target.value; });
      row.querySelector('.ivr-lang').addEventListener('change', e => { ivrOptions[i].language = e.target.value; });
      row.querySelector('.ivr-remove').addEventListener('click', () => { ivrOptions.splice(i, 1); renderIvrOptions(); });
    });
  }

  function renderIvrPrompts() {
    document.querySelectorAll('.ivr-prompt').forEach(el => {
      const slot = el.dataset.slot;
      const meta = ivrState.prompts[slot];
      const body = el.querySelector('.ivr-prompt-body');
      const accept = ivrState.ffmpeg_available ? 'audio/*,.wav,.mp3,.m4a,.ogg' : '.wav,audio/wav';
      body.innerHTML = (meta ? `
        <div class="ivr-prompt-meta">${escapeHtml(meta.original_name)} &middot; ${meta.duration_s}s &middot; uploaded ${escapeHtml(new Date(meta.uploaded_at).toLocaleString())}</div>
        <audio controls preload="none" src="/api/ivr/prompts/${slot}?v=${encodeURIComponent(meta.uploaded_at)}"></audio>`
        : '<div class="ivr-prompt-meta">No recording uploaded yet.</div>') + `
        <div class="ivr-prompt-actions">
          <button type="button" class="btn btn-secondary ivr-upload">${meta ? 'Replace recording' : 'Upload recording'}</button>
          ${meta ? '<button type="button" class="btn btn-secondary ivr-delete">Delete</button>' : ''}
          <input type="file" class="ivr-file" accept="${accept}" hidden>
        </div>`;
      const fileInput = body.querySelector('.ivr-file');
      body.querySelector('.ivr-upload').addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', () => { if (fileInput.files[0]) uploadIvrPrompt(slot, fileInput.files[0]); });
      const del = body.querySelector('.ivr-delete');
      if (del) del.addEventListener('click', () => deleteIvrPrompt(slot));
    });
  }

  function renderIvr(state, keepEdits = false) {
    ivrState = state;
    document.getElementById('ivrEnabled').checked = !!state.enabled;
    if (!keepEdits) {
      ivrOptions = state.options.map(o => ({ ...o }));
      document.getElementById('ivrMaxTries').value = state.max_tries;
      document.getElementById('ivrWaitSeconds').value = state.wait_seconds;
    }
    const def = document.getElementById('ivrDefaultLanguage');
    def.innerHTML = ivrLanguageOptions(state.default_language);
    renderIvrOptions();
    renderIvrPrompts();
    const pill = document.getElementById('ivrStatusPill');
    if (state.active) { pill.textContent = 'Active'; }
    else if (state.enabled) { pill.textContent = 'Needs menu recording'; }
    else { pill.textContent = 'Off'; }
  }

  async function loadIvr() {
    try {
      const res = await fetch('/api/ivr');
      if (res.ok) renderIvr(await res.json());
    } catch (err) { /* the next refresh retries */ }
  }

  async function uploadIvrPrompt(slot, file) {
    showToast('Uploading and converting...', 'info');
    try {
      const res = await fetch(`/api/ivr/prompts/${slot}?filename=${encodeURIComponent(file.name)}`, { method: 'PUT', body: file });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(data.detail || 'Upload failed', 'error'); return; }
      renderIvr(data.ivr, true);
      showToast('Recording saved', 'success');
    } catch (err) { showToast('Network error uploading the recording', 'error'); }
  }

  async function deleteIvrPrompt(slot) {
    if (!confirm('Delete this recording?')) return;
    try {
      const res = await fetch(`/api/ivr/prompts/${slot}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(data.detail || 'Delete failed', 'error'); return; }
      renderIvr(data.ivr, true);
      showToast('Recording deleted', 'success');
    } catch (err) { showToast('Network error', 'error'); }
  }

  if (ivrForm) {
    document.getElementById('btnIvrAddKey').addEventListener('click', () => {
      const used = new Set(ivrOptions.map(o => o.digit));
      const digit = IVR_KEYS.find(k => !used.has(k));
      if (!digit) return;
      ivrOptions.push({ digit, language: Object.keys(ivrState.languages)[0] });
      renderIvrOptions();
    });
    ivrForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const badge = document.getElementById('ivrSaveStatus');
      const payload = {
        enabled: document.getElementById('ivrEnabled').checked,
        options: ivrOptions,
        default_language: document.getElementById('ivrDefaultLanguage').value,
        max_tries: Number(document.getElementById('ivrMaxTries').value),
        wait_seconds: Number(document.getElementById('ivrWaitSeconds').value),
      };
      try {
        const res = await fetch('/api/ivr', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          badge.textContent = data.detail || 'Could not save'; badge.className = 'save-status-badge error';
          showToast(data.detail || 'Could not save the language menu', 'error');
          return;
        }
        renderIvr(data.ivr);
        badge.textContent = 'Saved'; badge.className = 'save-status-badge success';
        showToast('Language menu saved', 'success');
      } catch (err) { showToast('Network error saving the language menu', 'error'); }
    });
  }

  // Init
  loadSettings();
  loadIvr();
  fetchState();
  connectWebSocket();
  startDurationTickers();
})();
