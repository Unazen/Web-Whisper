const $ = (sel) => document.querySelector(sel);

const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];

const state = {
  mode: "idle",
  recorder: null,
  stream: null,
  chunks: [],
  mime: "",
  startedAt: 0,
  tickId: null,
  audioCtx: null,
  analyser: null,
  buf: null,
  level: 0,
  smooth: 0,
  currentId: null,
  busy: false,
  maxDuration: 1800,
  pendingSave: null,
  saveTimer: null,
  spaceHandled: false,
};

let pollToken = 0;

const el = {
  rec: $("#rec"),
  timer: $("#timer"),
  hint: $("#hint"),
  lang: $("#lang"),
  text: $("#text"),
  state: $("#state"),
  pct: $("#pct"),
  bar: $("#bar"),
  meta: $("#meta"),
  saved: $("#saved"),
  statusline: $(".statusline"),
  history: $("#history"),
  count: $("#count"),
  chip: $("#chip"),
  chipText: $("#chip-text"),
  toast: $("#toast"),
  canvas: $("#rings"),
  copy: $("#btn-copy"),
  txt: $("#btn-txt"),
  srt: $("#btn-srt"),
  audio: $("#btn-audio"),
  mp3: $("#btn-mp3"),
  footNote: $("#foot-note"),
};

const pad = (n) => String(n).padStart(2, "0");

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function fmtDate(iso) {
  const d = new Date(iso);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

let toastTimer = null;
function toast(message, kind) {
  el.toast.textContent = message;
  el.toast.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove("show"), kind === "err" ? 6000 : 3200);
}

async function errorText(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
  } catch (_) {}
  return `Ошибка ${res.status}`;
}

function pickMime() {
  if (!window.MediaRecorder) return "";
  return MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m)) || "";
}

function extFor(mime) {
  if (mime.includes("mp4")) return "m4a";
  if (mime.includes("ogg")) return "ogg";
  return "webm";
}

function micError(err) {
  if (err && err.name === "NotAllowedError") return "Доступ к микрофону запрещён. Разреши его в настройках сайта.";
  if (err && err.name === "NotFoundError") return "Микрофон не найден.";
  if (err && err.name === "NotReadableError") return "Микрофон занят другим приложением.";
  return "Не удалось открыть микрофон.";
}

const HINTS = {
  idle: "TAP TO RECORD",
  recording: "RECORDING · TAP TO STOP",
  uploading: "UPLOADING",
};

function setMode(mode) {
  state.mode = mode;
  el.rec.classList.toggle("recording", mode === "recording");
  el.rec.classList.toggle("locked", mode === "uploading");
  el.rec.setAttribute("aria-label", mode === "recording" ? "Остановить запись" : "Начать запись");
  el.hint.textContent = HINTS[mode];
}

function setupMeter(stream) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audioCtx = new Ctx();
    const source = state.audioCtx.createMediaStreamSource(stream);
    state.analyser = state.audioCtx.createAnalyser();
    state.analyser.fftSize = 512;
    state.buf = new Uint8Array(state.analyser.fftSize);
    source.connect(state.analyser);
  } catch (_) {
    state.analyser = null;
  }
}

function teardownMeter() {
  if (state.audioCtx) state.audioCtx.close().catch(() => {});
  state.audioCtx = null;
  state.analyser = null;
  state.level = 0;
}

function releaseStream() {
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  state.stream = null;
}

function tick() {
  const elapsed = (performance.now() - state.startedAt) / 1000;
  el.timer.textContent = fmtTime(elapsed);
  if (elapsed >= state.maxDuration) {
    toast("Достигнут лимит длины записи", "warn");
    stopRecording();
  }
}

async function startRecording() {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("Микрофон работает только по HTTPS или на localhost. Открой сайт по https://", "err");
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    toast(micError(err), "err");
    return;
  }
  state.mime = pickMime();
  state.chunks = [];
  try {
    state.recorder = state.mime
      ? new MediaRecorder(state.stream, { mimeType: state.mime })
      : new MediaRecorder(state.stream);
  } catch (_) {
    releaseStream();
    toast("Браузер не поддерживает запись звука.", "err");
    return;
  }
  state.mime = state.recorder.mimeType || state.mime || "audio/webm";
  state.recorder.ondataavailable = (e) => {
    if (e.data && e.data.size) state.chunks.push(e.data);
  };
  state.recorder.onstop = onRecorderStop;
  setupMeter(state.stream);
  state.recorder.start(1000);
  state.startedAt = performance.now();
  state.tickId = setInterval(tick, 200);
  setMode("recording");
  tick();
}

function stopRecording() {
  if (state.recorder && state.recorder.state !== "inactive") state.recorder.stop();
}

async function onRecorderStop() {
  clearInterval(state.tickId);
  teardownMeter();
  releaseStream();
  const seconds = (performance.now() - state.startedAt) / 1000;
  const blob = new Blob(state.chunks, { type: state.mime });
  state.chunks = [];
  if (blob.size < 1000 || seconds < 0.4) {
    setMode("idle");
    toast("Запись слишком короткая.", "warn");
    return;
  }
  setMode("uploading");
  try {
    await upload(blob);
  } catch (err) {
    toast(err.message || "Не удалось отправить запись.", "err");
  }
  setMode("idle");
}

async function upload(blob) {
  const form = new FormData();
  form.append("file", blob, `rec.${extFor(state.mime)}`);
  form.append("language", el.lang.value);
  const res = await fetch("/api/transcribe", { method: "POST", body: form });
  if (!res.ok) throw new Error(await errorText(res));
  const { id } = await res.json();
  clearResult();
  startPolling(id);
  loadHistory();
}

function setActions(enabled) {
  [el.copy, el.txt, el.srt, el.audio, el.mp3].forEach((b) => {
    b.disabled = !enabled;
  });
}

function clearResult() {
  el.text.value = "";
  el.text.readOnly = true;
  el.bar.style.width = "0%";
  el.pct.textContent = "";
  el.meta.textContent = "";
  el.saved.textContent = "";
  setStateLabel("EMPTY", "");
  setActions(false);
}

function setStateLabel(label, kind) {
  el.state.textContent = label;
  el.statusline.className = "statusline" + (kind ? " " + kind : "");
}

function renderRecord(rec) {
  const pct = Math.round((rec.progress || 0) * 100);
  const done = rec.status === "done";
  if (done) {
    el.text.value = rec.edited_text != null ? rec.edited_text : rec.text || "";
    el.text.readOnly = false;
    el.bar.style.width = "100%";
    el.pct.textContent = "";
    setStateLabel(rec.text ? "DONE" : "NO SPEECH DETECTED", "ok");
    const parts = [];
    if (rec.language) parts.push("LANG " + rec.language);
    if (rec.duration) parts.push(fmtTime(rec.duration));
    if (rec.device) parts.push(rec.device === "cuda" ? "GPU" : "CPU");
    if (rec.processing_seconds != null) parts.push(rec.processing_seconds + "s");
    el.meta.textContent = parts.join(" · ");
    setActions(true);
    el.copy.disabled = !el.text.value;
    el.txt.disabled = !el.text.value;
    el.srt.disabled = !(rec.segments && rec.segments.length);
  } else if (rec.status === "error") {
    el.text.value = rec.text || "";
    el.text.readOnly = true;
    el.bar.style.width = "0%";
    el.pct.textContent = "";
    setStateLabel("ERROR", "err");
    el.meta.textContent = rec.error || "";
    setActions(false);
    el.audio.disabled = false;
    el.mp3.disabled = false;
  } else {
    el.text.value = rec.text || "";
    el.text.readOnly = true;
    el.text.scrollTop = el.text.scrollHeight;
    el.bar.style.width = pct + "%";
    if (rec.status === "queued") {
      setStateLabel("QUEUED", "work");
      el.pct.textContent = "";
    } else {
      setStateLabel("TRANSCRIBING", "work");
      el.pct.textContent = pct + "%";
    }
    el.meta.textContent = rec.language ? "LANG " + rec.language : "";
    setActions(false);
    el.audio.disabled = false;
  }
}

function stopPolling() {
  pollToken++;
  state.busy = false;
}

function startPolling(id) {
  const token = ++pollToken;
  state.currentId = id;
  state.busy = true;
  const step = async () => {
    if (token !== pollToken) return;
    try {
      const res = await fetch(`/api/records/${id}`);
      if (!res.ok) throw new Error(await errorText(res));
      const rec = await res.json();
      if (token !== pollToken) return;
      renderRecord(rec);
      if (rec.status === "done" || rec.status === "error") {
        state.busy = false;
        loadHistory();
        if (rec.status === "done") autoCopy(rec);
        if (rec.status === "error") toast(rec.error || "Ошибка обработки.", "err");
        return;
      }
      setTimeout(step, 800);
    } catch (err) {
      if (token !== pollToken) return;
      state.busy = false;
      toast(err.message || "Потеряна связь с сервером.", "err");
    }
  };
  step();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {}
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_) {}
  area.remove();
  return ok;
}

async function autoCopy(rec) {
  const text = rec.edited_text != null ? rec.edited_text : rec.text;
  if (!text) return;
  const ok = await copyText(text);
  if (ok) {
    toast("Текст скопирован в буфер обмена");
  } else {
    toast("Браузер не дал скопировать автоматически. Нажми COPY.", "warn");
    el.copy.classList.add("flash");
    setTimeout(() => el.copy.classList.remove("flash"), 2000);
  }
}

function scheduleSave() {
  if (!state.currentId || el.text.readOnly) return;
  state.pendingSave = { id: state.currentId, text: el.text.value };
  el.saved.textContent = "EDITING…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSave, 700);
}

async function flushSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const job = state.pendingSave;
  state.pendingSave = null;
  if (!job) return;
  try {
    const res = await fetch(`/api/records/${job.id}/text`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: job.text }),
    });
    if (!res.ok) throw new Error(await errorText(res));
    if (job.id === state.currentId) el.saved.textContent = "SAVED";
  } catch (err) {
    if (job.id === state.currentId) el.saved.textContent = "SAVE FAILED";
    toast(err.message || "Не удалось сохранить правки.", "err");
  }
}

async function download(kind) {
  if (!state.currentId) return;
  await flushSave();
  const res = await fetch(`/api/records/${state.currentId}/download/${kind}`);
  if (!res.ok) {
    toast(await errorText(res), "err");
    return;
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const name = match ? match[1] : `whisper-${state.currentId}.${kind}`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function selectRecord(id) {
  await flushSave();
  stopPolling();
  try {
    const res = await fetch(`/api/records/${id}`);
    if (!res.ok) throw new Error(await errorText(res));
    const rec = await res.json();
    state.currentId = id;
    renderRecord(rec);
    el.saved.textContent = "";
    if (rec.status === "queued" || rec.status === "processing") startPolling(id);
    markActive();
    el.text.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (err) {
    toast(err.message, "err");
    loadHistory();
  }
}

async function deleteRecord(id) {
  if (!confirm("Удалить запись вместе с аудио?")) return;
  try {
    const res = await fetch(`/api/records/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await errorText(res));
    if (id === state.currentId) {
      stopPolling();
      state.currentId = null;
      clearResult();
    }
    loadHistory();
  } catch (err) {
    toast(err.message, "err");
  }
}

function markActive() {
  el.history.querySelectorAll(".item").forEach((row) => {
    row.classList.toggle("active", row.dataset.id === state.currentId);
  });
}

function renderHistory(items) {
  el.history.replaceChildren();
  el.count.textContent = pad(items.length);
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Пока пусто. Первая запись появится здесь.";
    el.history.appendChild(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "item";
    row.dataset.id = item.id;
    row.tabIndex = 0;
    row.setAttribute("role", "button");

    const when = document.createElement("div");
    when.className = "when";
    when.textContent = fmtDate(item.created);

    const snip = document.createElement("div");
    snip.className = "snip";
    if (item.status === "done") {
      snip.textContent = item.snippet || "(речь не обнаружена)";
      if (!item.snippet) snip.classList.add("muted");
    } else if (item.status === "error") {
      snip.textContent = "Ошибка обработки";
      snip.classList.add("muted");
    } else {
      snip.textContent = item.snippet || "Обрабатывается…";
      snip.classList.add("muted");
    }

    const badges = document.createElement("div");
    badges.className = "badges";
    if (item.status === "done") {
      if (item.language) badges.append(spanText(item.language));
      if (item.duration) badges.append(spanText(fmtTime(item.duration)));
    } else if (item.status === "error") {
      badges.append(spanText("ERROR", "err"));
    } else {
      badges.append(spanText(item.status === "queued" ? "QUEUED" : Math.round((item.progress || 0) * 100) + "%", "work"));
    }

    const del = document.createElement("button");
    del.className = "del";
    del.type = "button";
    del.textContent = "×";
    del.setAttribute("aria-label", "Удалить запись");
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteRecord(item.id);
    });

    row.append(when, snip, badges, del);
    row.addEventListener("click", () => selectRecord(item.id));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target === row) selectRecord(item.id);
    });
    if (item.id === state.currentId) row.classList.add("active");
    el.history.appendChild(row);
  }
}

function spanText(text, cls) {
  const s = document.createElement("span");
  s.textContent = text;
  if (cls) s.className = cls;
  return s;
}

async function loadHistory() {
  try {
    const res = await fetch("/api/records");
    if (!res.ok) return;
    renderHistory(await res.json());
  } catch (_) {}
}

async function pollStatus() {
  let label;
  let cls;
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    state.maxDuration = data.max_duration || state.maxDuration;
    el.footNote.textContent = `RETENTION ${data.retention_days}D`;
    const engine = data.engine;
    if (!engine.model_present) {
      label = "MODEL MISSING";
      cls = "bad";
    } else if (engine.loaded) {
      label = "ENGINE " + (engine.device === "cuda" ? "GPU" : "CPU");
      cls = "on";
    } else {
      label = "ENGINE STANDBY";
      cls = "";
    }
    if (data.pending) label += ` · QUEUE ${data.pending}`;
  } catch (_) {
    label = "OFFLINE";
    cls = "bad";
  }
  el.chipText.textContent = label;
  el.chip.className = "chip" + (cls ? " " + cls : "");
}

function toggleRecording() {
  if (state.mode === "recording") stopRecording();
  else if (state.mode === "idle") startRecording();
}

el.rec.addEventListener("click", toggleRecording);
el.text.addEventListener("input", scheduleSave);
el.copy.addEventListener("click", async () => {
  const ok = await copyText(el.text.value);
  toast(ok ? "Скопировано" : "Не удалось скопировать. Выдели текст и нажми Ctrl+C.", ok ? "" : "err");
});
el.txt.addEventListener("click", () => download("txt"));
el.srt.addEventListener("click", () => download("srt"));
el.audio.addEventListener("click", () => download("audio"));
el.mp3.addEventListener("click", () => download("mp3"));

function typingTarget(target) {
  const tag = target && target.tagName;
  return tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT";
}

document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
  if (typingTarget(e.target)) return;
  e.preventDefault();
  state.spaceHandled = true;
  toggleRecording();
});

document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && state.spaceHandled) {
    e.preventDefault();
    state.spaceHandled = false;
  }
});

window.addEventListener("beforeunload", () => {
  if (state.pendingSave) flushSave();
});

const ctx = el.canvas.getContext("2d");
let dpr = 1;

function resizeCanvas() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = el.canvas.getBoundingClientRect();
  el.canvas.width = Math.round(rect.width * dpr);
  el.canvas.height = Math.round(rect.height * dpr);
}

function readLevel() {
  if (!state.analyser) return 0;
  state.analyser.getByteTimeDomainData(state.buf);
  let sum = 0;
  for (let i = 0; i < state.buf.length; i++) {
    const v = (state.buf[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / state.buf.length) * 4.5);
}

function drawCross(x, y, size) {
  ctx.beginPath();
  ctx.moveTo(x - size, y);
  ctx.lineTo(x + size, y);
  ctx.moveTo(x, y - size);
  ctx.lineTo(x, y + size);
  ctx.stroke();
}

function frame(t) {
  const w = el.canvas.width;
  const h = el.canvas.height;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2;
  const cy = h / 2;
  const base = Math.min(w, h) / 2;
  const recording = state.mode === "recording";
  state.level = recording ? readLevel() : 0;
  state.smooth = state.smooth * 0.82 + state.level * 0.18;
  const energy = state.smooth;
  const spin = state.busy ? 4 : 1;
  const rings = 11;

  for (let i = 0; i < rings; i++) {
    const k = i / (rings - 1);
    const wobble = Math.sin(t * 0.004 + i * 1.3);
    const r = base * (0.38 + k * 0.6) + energy * base * 0.1 * (0.3 + k) * wobble;
    const thick = (1 + (Math.sin(t * 0.0015 + i * 0.8) + 1) * 1.3 * (1 - k * 0.4) + energy * 6 * k) * dpr;
    const alpha = (recording ? 0.95 : state.busy ? 0.7 : 0.42) * (1 - k * 0.78);
    ctx.lineWidth = thick;
    ctx.strokeStyle = `rgba(200,255,0,${alpha.toFixed(3)})`;
    if (i % 3 === 1) {
      ctx.setLineDash([base * 0.05, base * 0.04]);
      ctx.lineDashOffset = -t * 0.02 * (i % 2 ? 1 : -1) * spin;
    } else {
      ctx.setLineDash([]);
    }
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(1, r), 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.setLineDash([]);
  ctx.lineWidth = Math.max(1, dpr);
  ctx.strokeStyle = "rgba(200,255,0,0.55)";
  const edge = base * 0.985;
  const size = base * 0.035;
  drawCross(cx, cy - edge, size);
  drawCross(cx, cy + edge, size);
  drawCross(cx - edge, cy, size);
  drawCross(cx + edge, cy, size);

  requestAnimationFrame(frame);
}

window.addEventListener("resize", resizeCanvas);
resizeCanvas();
requestAnimationFrame(frame);
setMode("idle");
clearResult();
loadHistory();
pollStatus();
setInterval(pollStatus, 4000);
setInterval(() => {
  if (!state.busy) loadHistory();
}, 15000);
