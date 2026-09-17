// Front Desk & Voice Receptionist Client Logic
(() => {
  let ws = null;
  let activeCalls = [];
  let allBookings = [];
  let callHistory = [];
  let messages = [];
  let services = [];
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
  const settingOperatingHours = document.getElementById('settingOperatingHours');
  const settingHoursStart = document.getElementById('settingHoursStart');
  const settingHoursEnd = document.getElementById('settingHoursEnd');
  const settingGreetingSinhala = document.getElementById('settingGreetingSinhala');
  const btnSaveSettings = document.getElementById('btnSaveSettings');
  const saveStatusIndicator = document.getElementById('saveStatusIndicator');

  // Actions
  const btnSimulateCall = document.getElementById('btnSimulateCall');
  const btnRefresh = document.getElementById('btnRefresh');
  const toastContainer = document.getElementById('toastContainer');

  const tabTitles = {
    liveCallsTab: 'Live Calls & Console',
    bookingsTab: 'Appointments & Schedule',
    servicesTab: 'Services & Pricing Menu',
    callHistoryTab: 'Call History & Logs',
    messagesTab: 'Customer Voicemails',
    settingsTab: 'Salon & AI Receptionist Settings',
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

  function appendFeedEvent(timeStr, content) {
    const item = document.createElement('div');
    item.className = 'feed-row';
    item.innerHTML = `<span class="feed-row-time">${timeStr}</span><span class="feed-row-content">${content}</span>`;

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
    const ws = new WebSocket(`${protocol}//${location.host}/ws/simulate-call`);
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
            <div class="log-msg" style="margin-top: 2px;">${act.action.replace(/^(AI Receptionist:|AI Spoke:|Caller:)\s*/, '')}</div>
          </div>
        `;
      }).join('');

      card.innerHTML = `
        <div class="call-item-top">
          <div>
            <div class="call-caller-title">
              <span>${call.caller_number || 'Unknown Caller'}</span>
              <span class="call-status-badge">● Active Telephony Line</span>
            </div>
            <div class="call-sid-code">CHANNEL SID: ${call.call_id}</div>
          </div>
          <div style="display: flex; align-items: center; gap: 10px;">
            <div class="call-timer-display timer-value" data-start="${call.started_at}">
              ${formatDuration(elapsedSec)}
            </div>
            <button class="btn btn-danger-subtle btn-hangup-call" data-id="${call.call_id}">
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

    filtered.forEach(b => {
      const tr = document.createElement('tr');
      const isConfirmed = b.status === 'confirmed';
      const statusBadge = isConfirmed
        ? '<span class="status-pill status-confirmed">Confirmed</span>'
        : '<span class="status-pill status-cancelled">Cancelled</span>';

      const actionBtn = isConfirmed
        ? `<button class="btn btn-danger-subtle btn-cancel-booking" data-id="${b.booking_id}">Cancel</button>`
        : '<span style="color: var(--text-dim); font-size: 12px;">Archived</span>';

      tr.innerHTML = `
        <td style="font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);">${b.booking_id.slice(0, 8)}</td>
        <td>
          <div class="client-meta-group">
            <div class="client-avatar-monogram">${getInitials(b.customer_name)}</div>
            <span class="client-name-text">${b.customer_name || 'Walk-in Client'}</span>
          </div>
        </td>
        <td style="font-family: var(--font-mono); font-size: 12.5px;">${b.customer_phone || '--'}</td>
        <td><span class="service-pill-tag">${b.service_name}</span></td>
        <td>
          <div style="font-weight: 600; color: var(--text-primary);">${b.date}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">${b.time}</div>
        </td>
        <td>${statusBadge}</td>
        <td style="text-align: right;">${actionBtn}</td>
      `;
      bookingsTableBody.appendChild(tr);
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
          <span class="price-display-text">LKR ${Number(s.price_usd).toLocaleString()}</span>
        </td>
        <td>
          <span class="duration-chip">${s.duration_minutes} mins</span>
        </td>
        <td>
          <span class="status-active-tag">AI Quotable</span>
        </td>
        <td style="text-align: right;">
          <button class="btn btn-secondary btn-edit-service" data-name="${s.name}" data-price="${s.price_usd}" data-duration="${s.duration_minutes}" style="padding: 4px 10px; font-size: 12px; margin-right: 6px;">Edit</button>
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
        openServiceModal('edit', { name, price, duration });
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
    } else {
      serviceModalTitle.textContent = 'Add New Treatment';
      serviceOriginalName.value = '';
      serviceModalName.value = '';
      serviceModalPrice.value = '3500';
      serviceModalDuration.value = '45';
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
            body: JSON.stringify({ name, price_usd: price, duration_minutes: duration }),
          });
        } else {
          // Create new
          res = await fetch('/api/services', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, price_usd: price, duration_minutes: duration }),
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

      let summary = 'General Inquiry';
      if ((c.actions || []).some(a => a.action && a.action.toLowerCase().includes('booking'))) {
        summary = 'Appointment Booked';
      } else if ((c.actions || []).some(a => a.action && a.action.toLowerCase().includes('transfer'))) {
        summary = 'Transferred to Staff';
      } else if ((c.actions || []).some(a => a.action && a.action.toLowerCase().includes('message'))) {
        summary = 'Voicemail Recorded';
      }

      tr.innerHTML = `
        <td style="font-family: var(--font-mono); font-size: 12px; color: var(--text-muted);">${c.call_id}</td>
        <td style="font-weight: 600; color: var(--text-primary); font-family: var(--font-mono);">${c.caller_number || 'Unknown'}</td>
        <td>
          <div style="color: var(--text-primary);">${date}</div>
          <div style="font-size: 11.5px; color: var(--text-muted);">${started}</div>
        </td>
        <td style="font-family: var(--font-mono); font-weight: 600;">${duration}</td>
        <td><span class="status-pill status-confirmed">${summary}</span></td>
        <td style="font-size: 12px; color: var(--text-secondary);">${c.actions ? c.actions.length : 0} interactions</td>
      `;
      callHistoryTableBody.appendChild(tr);
    });
  }

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
      card.className = `message-card-item ${m.urgent ? 'urgent' : ''}`;
      card.innerHTML = `
        <div class="msg-header-line">
          <span class="msg-caller-name">${m.caller_name || 'Anonymous Caller'}</span>
          <span class="msg-phone-chip">${m.caller_phone || 'No phone'}</span>
        </div>
        <div class="msg-content-text">"${m.message}"</div>
        <div class="msg-footer-line">
          <span>${formatDate(m.taken_at)} at ${formatTime(m.taken_at)}</span>
          ${m.urgent ? '<span class="status-pill status-cancelled">Urgent Callback</span>' : '<span style="color: var(--text-muted); font-size: 11px;">Standard Message</span>'}
        </div>
      `;
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
        if (settingOperatingHours) settingOperatingHours.value = s.operating_hours_display || '';
        if (settingHoursStart) settingHoursStart.value = s.business_hours_start || '09:00';
        if (settingHoursEnd) settingHoursEnd.value = s.business_hours_end || '19:00';
        if (settingGreetingSinhala) settingGreetingSinhala.value = s.greeting_sinhala || '';

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
        operating_hours_display: settingOperatingHours.value.trim(),
        business_hours_start: settingHoursStart.value,
        business_hours_end: settingHoursEnd.value,
        greeting_sinhala: settingGreetingSinhala.value.trim(),
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
          showToast('Failed to save settings', 'error');
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
      const [statsRes, activeRes, historyRes, bookingsRes, msgRes, servicesRes] = await Promise.all([
        fetch('/api/stats').then(r => r.json()),
        fetch('/api/calls/active').then(r => r.json()),
        fetch('/api/calls/history').then(r => r.json()),
        fetch('/api/bookings').then(r => r.json()),
        fetch('/api/messages').then(r => r.json()),
        fetch('/api/services').then(r => r.json()),
      ]);

      activeCalls = activeRes;
      callHistory = historyRes;
      allBookings = bookingsRes;
      messages = msgRes;
      services = servicesRes;

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
        activeCalls[idx].actions.push({ timestamp: msg.timestamp, action: data.action, details: data.details });
        renderActiveCalls();
      }
      appendFeedEvent(timeStr, `[${data.call_id}] ${data.action}`);
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
    } else if (type === 'booking_cancelled') {
      fetchState();
    }
  }

  // Init
  loadSettings();
  fetchState();
  connectWebSocket();
  startDurationTickers();
})();
