import { GlyphField, ProgressStrip, LevelStrip, COLORS, scramble } from "./glyphs.js";
import { $, esc, fmtBytes, fmtDur, fmtSpan, fmtDate, store, api, post, toast, fail } from "./util.js";
import { Recorder, extensionFor } from "./recorder.js";
import { initModels, onModels, refreshModels, renderModels, getModels, openModels, startDownload } from "./models.js";

const STATUS = {
  queued: ["в очереди", "run"],
  processing: ["обработка", "run"],
  done: ["готово", "done"],
  error: ["ошибка", "failed"],
  cancelled: ["отменено", "idle"],
};

const STAGES = { decoding: "читаю файл", loading: "загружаю модель", transcribing: "распознаю" };

const cards = new Map();
const mine = new Set();
const autoCopy = new Set();
const listEl = $("#jobs");
const modelSel = $("#model");
const langSel = $("#lang");
const noteEl = $("#modelNote");

let limits = { recording_seconds: 1800, upload_seconds: 10800, upload_bytes: 2147483648 };
let installedSig = null;
let firstSync = true;
let timer = 0;
let polling = false;

const effective = (detail) => (detail.edited_text != null ? detail.edited_text : detail.text || "");

function setText(root, selector, value) {
  const node = $(selector, root);
  if (node && node.textContent !== value) node.textContent = value;
}

function entryOf(id) {
  const data = getModels();
  return data ? data.models.find((m) => m.id === id) : null;
}

function installedModels() {
  const data = getModels();
  return data ? data.models.filter((m) => m.installed) : [];
}

function applyModel() {
  const entry = entryOf(modelSel.value);
  const english = !!entry && entry.lang === "en";
  const saved = store.get("whisper.lang") || "auto";
  langSel.disabled = english;
  langSel.value = english ? "en" : [...langSel.options].some((o) => o.value === saved) ? saved : "auto";
  if (entry) noteEl.textContent = entry.desc + (english ? " Язык зафиксирован: английский." : "");
  else noteEl.textContent = "Скачайте хотя бы одну модель в разделе «Модели Whisper» ниже.";
  $("#firstrun").hidden = installedModels().length > 0;
}

function renderSelect() {
  const data = getModels();
  if (!data) return;
  const list = installedModels();
  const saved = store.get("whisper.model");
  const pick =
    list.find((m) => m.id === modelSel.value) ||
    list.find((m) => m.id === saved) ||
    list.find((m) => m.id === data.default) ||
    list[0];
  modelSel.innerHTML = list.length
    ? list.map((m) => `<option value="${esc(m.id)}">${esc(m.title)}${m.badge ? " · " + esc(m.badge) : ""}</option>`).join("")
    : `<option value="">нет моделей</option>`;
  modelSel.disabled = !list.length;
  if (pick) modelSel.value = pick.id;
  applyModel();
  renderModels();
}

function selectModel(id) {
  if (![...modelSel.options].some((o) => o.value === id)) return;
  modelSel.value = id;
  store.set("whisper.model", id);
  applyModel();
  renderModels();
  toast(`Выбрана модель ${id}`);
}

modelSel.addEventListener("change", () => {
  store.set("whisper.model", modelSel.value);
  applyModel();
  renderModels();
});

langSel.addEventListener("change", () => {
  if (!langSel.disabled) store.set("whisper.lang", langSel.value);
});

function titleOf(rec) {
  return rec.name || `Запись ${fmtDate(rec.created)}`;
}

function factsHtml(rec) {
  const parts = [];
  if (rec.source === "upload" && rec.size) parts.push(`<span>файл <b>${fmtBytes(rec.size)}</b></span>`);
  if (rec.duration) parts.push(`<span>длина <b>${fmtDur(rec.duration)}</b></span>`);
  if (rec.language) parts.push(`<span>язык <b>${esc(rec.language)}</b></span>`);
  parts.push(`<span>модель <b>${esc(rec.model || "—")}</b></span>`);
  if (rec.device && rec.status !== "queued") {
    const note = rec.device === "cpu" && rec.device_note ? ` <span class="dim">(${esc(rec.device_note)})</span>` : "";
    parts.push(`<span>устройство <b>${rec.device === "cuda" ? "GPU" : "CPU"}</b>${note}</span>`);
  }
  if (rec.status === "done" && rec.processing_seconds != null) {
    const speed = rec.duration && rec.processing_seconds ? ` (×${(rec.duration / rec.processing_seconds).toFixed(1)})` : "";
    parts.push(`<span>заняло <b>${fmtSpan(rec.processing_seconds)}</b>${speed}</span>`);
  }
  if (rec.source === "upload") parts.push(`<span>${fmtDate(rec.created)}</span>`);
  return parts.join("");
}

function statHtml(label, bind, small = false) {
  return `<div class="stat"><dt>${label}</dt><dd${small ? ' class="small"' : ""} data-${bind}>—</dd></div>`;
}

function bodyHtml(card) {
  const rec = card.rec;
  switch (rec.status) {
    case "queued":
      return `<canvas class="strip" data-strip></canvas>
        <p>Позиция в очереди: <b data-pos>—</b>. Обработка начнётся, когда освободится движок.</p>
        <div class="actions"><button type="button" class="btn danger" data-act="cancel">Отменить</button></div>`;
    case "processing":
      return `<canvas class="strip" data-strip></canvas>
        <dl class="stats">${statHtml("этап", "stage", true)}${statHtml("готово", "pct")}${statHtml("обработано", "done", true)}${statHtml("осталось", "eta", true)}${statHtml("прошло", "elapsed", true)}</dl>
        <textarea class="out" data-live readonly placeholder="Текст появится здесь по мере распознавания"></textarea>
        <div class="actions"><button type="button" class="btn danger" data-act="cancel">Отменить</button></div>`;
    case "done": {
      const hasText = !!rec.snippet;
      const view = card.open
        ? `<textarea class="out" data-text spellcheck="false" aria-label="Текст записи"></textarea><div class="saved" data-saved></div>`
        : `<p class="snip${hasText ? "" : " dim"}" data-snip></p>`;
      return `${view}
        <div class="actions">
          <button type="button" class="btn solid" data-act="copy"${hasText ? "" : " disabled"}>Копировать</button>
          <button type="button" class="btn" data-act="toggle">${card.open ? "Свернуть" : "Открыть"}</button>
          <button type="button" class="btn ghost" data-act="dl" data-kind="txt">TXT</button>
          <button type="button" class="btn ghost" data-act="dl" data-kind="srt"${rec.segments_count ? "" : " disabled"}>SRT</button>
          <button type="button" class="btn ghost" data-act="dl" data-kind="audio">${rec.source === "upload" ? "Исходник" : "Аудио"}</button>
          <button type="button" class="btn ghost" data-act="dl" data-kind="mp3">MP3</button>
          <button type="button" class="btn ghost" data-act="delete">Удалить</button>
        </div>`;
    }
    case "error":
      return `<p class="alert">${esc(rec.error || "Не удалось обработать запись.")}</p>
        <div class="actions">
          <button type="button" class="btn ghost" data-act="dl" data-kind="audio">${rec.source === "upload" ? "Исходник" : "Аудио"}</button>
          <button type="button" class="btn ghost" data-act="delete">Удалить</button>
        </div>`;
    default:
      return `<p class="dim">Обработка отменена.${rec.snippet ? " Частичный текст сохранён." : ""}</p>
        ${rec.snippet ? `<p class="snip" data-snip></p>` : ""}
        <div class="actions">
          ${rec.snippet ? `<button type="button" class="btn solid" data-act="copy">Копировать</button>` : ""}
          <button type="button" class="btn ghost" data-act="dl" data-kind="audio">${rec.source === "upload" ? "Исходник" : "Аудио"}</button>
          <button type="button" class="btn ghost" data-act="delete">Удалить</button>
        </div>`;
  }
}

function releaseBody(card) {
  if (card.strip) card.strip.destroy();
  card.strip = null;
}

function ensureDetail(card) {
  if (card.detail) return Promise.resolve(card.detail);
  if (!card.pending) {
    card.pending = api(`/api/records/${card.id}`)
      .then((detail) => (card.detail = detail))
      .finally(() => {
        card.pending = null;
      });
  }
  return card.pending;
}

function fillEditor(card) {
  const area = $("[data-text]", card.root);
  if (!area) return;
  ensureDetail(card)
    .then((detail) => {
      const again = $("[data-text]", card.root);
      if (again && !again.dataset.ready) {
        again.dataset.ready = "1";
        again.value = effective(detail);
      }
    })
    .catch(fail);
}

function buildBody(card) {
  releaseBody(card);
  const body = $(".body", card.root);
  body.innerHTML = bodyHtml(card);
  const strip = $("[data-strip]", body);
  if (strip) card.strip = new ProgressStrip(strip, { cell: 14 });
  if (card.rec.status === "done" && card.open) fillEditor(card);
}

function updateBody(card) {
  const rec = card.rec;
  const body = $(".body", card.root);
  if (rec.status === "queued") {
    if (card.strip) card.strip.set(0, "queued");
    setText(body, "[data-pos]", rec.position != null ? String(rec.position) : "—");
  } else if (rec.status === "processing") {
    const transcribing = rec.stage === "transcribing";
    const p = rec.progress || 0;
    if (card.strip) card.strip.set(transcribing ? p : 0, "running");
    setText(body, "[data-stage]", STAGES[rec.stage] || "подготовка");
    setText(body, "[data-pct]", transcribing ? `${Math.floor(p * 100)}%` : "—");
    setText(body, "[data-done]", transcribing && rec.duration ? `${fmtDur(rec.duration * p)} из ${fmtDur(rec.duration)}` : "—");
    const eta = transcribing && p > 0.03 && rec.elapsed_seconds ? fmtSpan((rec.elapsed_seconds * (1 - p)) / p) : "—";
    setText(body, "[data-eta]", eta);
    setText(body, "[data-elapsed]", rec.elapsed_seconds != null ? fmtSpan(rec.elapsed_seconds) : "—");
    const area = $("[data-live]", body);
    const text = rec.text || "";
    if (area && area.value !== text) {
      const stick = area.scrollHeight - area.scrollTop - area.clientHeight < 40;
      area.value = text;
      if (stick) area.scrollTop = area.scrollHeight;
    }
    const cancel = $("[data-act=cancel]", body);
    if (cancel) {
      cancel.disabled = !!card.cancelling;
      cancel.textContent = card.cancelling ? "Останавливаю…" : "Отменить";
    }
  } else if (rec.status === "done") {
    const snip = $("[data-snip]", body);
    if (snip) snip.textContent = (card.detail ? effective(card.detail) : rec.snippet) || "Речь не обнаружена";
  } else if (rec.status === "cancelled") {
    const snip = $("[data-snip]", body);
    if (snip) snip.textContent = rec.snippet;
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {}
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {}
  area.remove();
  return ok;
}

async function copyCard(card, auto = false) {
  const button = $("[data-act=copy]", card.root);
  let text;
  try {
    text = effective(await ensureDetail(card));
  } catch (error) {
    fail(error);
    return;
  }
  if (!text) {
    toast("В этой записи нет текста", true);
    return;
  }
  if (await copyText(text)) {
    toast("Текст скопирован в буфер обмена");
  } else if (auto) {
    toast("Браузер не дал скопировать автоматически. Нажмите «Копировать».", true);
    if (button) {
      button.classList.add("flash");
      setTimeout(() => button.classList.remove("flash"), 3000);
    }
  } else {
    toast("Не удалось скопировать. Откройте запись, выделите текст и нажмите Ctrl+C.", true);
  }
}

function scheduleSave(card) {
  card.dirty = true;
  setText(card.root, "[data-saved]", "правка…");
  clearTimeout(card.saveTimer);
  card.saveTimer = setTimeout(() => flushSave(card), 700);
}

async function flushSave(card) {
  clearTimeout(card.saveTimer);
  if (!card.dirty || !card.detail) return;
  card.dirty = false;
  try {
    await api(`/api/records/${card.id}/text`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: effective(card.detail) }),
    });
    setText(card.root, "[data-saved]", "сохранено");
  } catch (error) {
    card.dirty = true;
    setText(card.root, "[data-saved]", "не сохранено");
    fail(error);
  }
}

async function download(card, kind, button) {
  try {
    await flushSave(card);
    if (kind === "mp3") {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Конвертирую…";
      try {
        await post(`/api/records/${card.id}/prepare/mp3`);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    }
    const link = document.createElement("a");
    link.href = `/api/records/${card.id}/download/${kind}`;
    document.body.append(link);
    link.click();
    link.remove();
  } catch (error) {
    fail(error);
  }
}

function createCard(rec) {
  const root = document.createElement("article");
  root.className = "job";
  root.dataset.id = rec.id;
  root.innerHTML = `<i class="c1">+</i><i class="c2">+</i>
    <div class="job-top"><h3></h3><span class="tag"></span></div>
    <div class="facts"></div><div class="body"></div>`;
  const card = {
    id: rec.id,
    root,
    rec,
    open: mine.has(rec.id),
    sig: null,
    tag: null,
    facts: null,
    strip: null,
    detail: null,
    pending: null,
    dirty: false,
    saveTimer: 0,
    cancelling: false,
  };
  const title = $("h3", root);
  const text = titleOf(rec);
  title.textContent = text;
  scramble(title, text, 600);
  root.addEventListener("click", (event) => onClick(card, event));
  root.addEventListener("input", (event) => {
    if (event.target.matches("[data-text]") && card.detail) {
      card.detail.edited_text = event.target.value;
      scheduleSave(card);
    }
  });
  return card;
}

function updateCard(card, rec) {
  const previous = card.rec.status;
  card.rec = rec;
  const root = card.root;
  const changed = previous !== rec.status;
  if (changed) {
    card.detail = null;
    card.cancelling = false;
  }
  root.classList.toggle("is-failed", rec.status === "error");
  const [label, kind] = STATUS[rec.status] || [rec.status, "idle"];
  if (card.tag !== rec.status) {
    card.tag = rec.status;
    const tag = $(".tag", root);
    tag.className = `tag t-${kind}`;
    tag.innerHTML = `<i></i><span></span>`;
    scramble($("span", tag), label, 420);
  }
  const facts = factsHtml(rec);
  if (facts !== card.facts) {
    card.facts = facts;
    $(".facts", root).innerHTML = facts;
  }
  const sig = [rec.status, card.open ? 1 : 0, rec.status === "error" ? rec.error : "", rec.snippet ? 1 : 0, rec.segments_count ? 1 : 0].join("|");
  if (sig !== card.sig) {
    card.sig = sig;
    buildBody(card);
  }
  updateBody(card);
  if (changed && rec.status === "done" && autoCopy.delete(rec.id)) copyCard(card, true);
}

function removeCard(card) {
  releaseBody(card);
  clearTimeout(card.saveTimer);
  card.root.remove();
  cards.delete(card.id);
}

async function onClick(card, event) {
  const button = event.target.closest("[data-act]");
  if (!button) return;
  const act = button.dataset.act;
  try {
    if (act === "copy") {
      await copyCard(card);
    } else if (act === "toggle") {
      await flushSave(card);
      card.open = !card.open;
      if (card.open) await ensureDetail(card);
      updateCard(card, card.rec);
    } else if (act === "dl") {
      await download(card, button.dataset.kind, button);
    } else if (act === "cancel") {
      card.cancelling = true;
      button.disabled = true;
      await post(`/api/records/${card.id}/cancel`);
      tick(true);
    } else if (act === "delete") {
      if (!confirm(`Удалить «${titleOf(card.rec)}» вместе с аудио?`)) return;
      await api(`/api/records/${card.id}`, { method: "DELETE" });
      removeCard(card);
      toast("Запись удалена");
      syncEmpty();
    }
  } catch (error) {
    card.cancelling = false;
    fail(error);
    tick(true);
  }
}

function syncEmpty() {
  let empty = $(".empty", listEl);
  const total = cards.size;
  if (!total && !empty) {
    empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Записей пока нет. Нажмите «Запись» или загрузите файл выше.";
    listEl.append(empty);
  } else if (total && empty) {
    empty.remove();
  }
  $("#count").textContent = total ? String(total) : "";
}

function sync(records) {
  const seen = new Set();
  records.forEach((rec, index) => {
    seen.add(rec.id);
    let card = cards.get(rec.id);
    if (!card) {
      card = createCard(rec);
      if (firstSync && index === 0 && rec.status === "done") card.open = true;
      cards.set(rec.id, card);
    }
    updateCard(card, rec);
    const at = listEl.children[index];
    if (at !== card.root) listEl.insertBefore(card.root, at || null);
  });
  for (const card of [...cards.values()]) if (!seen.has(card.id)) removeCard(card);
  firstSync = false;
  syncEmpty();
}

const bar = $("#bar");
let barSig = "";

function renderBar(status) {
  let html;
  if (!status) {
    html = `<span class="alert"><i class="live"></i>Нет связи с сервером</span>`;
  } else {
    const engine = status.engine;
    html = status.current
      ? `<span><i class="live on"></i><b>Расшифровываю:</b> ${esc(status.current.name)}</span>`
      : `<span><i class="live"></i><b>Движок свободен</b></span>`;
    html += `<span class="dim">в очереди: <b>${status.queued}</b></span>`;
    html += `<span class="dim">в памяти: <b>${engine.loaded ? `${esc(engine.model)} · ${engine.device === "cuda" ? "GPU" : "CPU"}` : "ничего"}</b></span>`;
    if (engine.gpu) html += `<span class="dim">видеопамять свободна: <b>${fmtBytes(engine.gpu.free_mb * 1048576)}</b> из ${fmtBytes(engine.gpu.total_mb * 1048576)}</span>`;
    if (engine.reason) html += `<span class="alert">на CPU: ${esc(engine.reason)}</span>`;
    if (status.download) {
      const d = status.download;
      const share = d.total ? Math.floor(((d.downloaded || 0) / d.total) * 100) : 0;
      html += `<span><b>Скачиваю модель</b> ${esc(d.id)} ${d.state === "queued" ? "(в очереди)" : share + "%"}</span>`;
    }
    html += `<span class="dim">свободно на диске: <b>${fmtBytes(status.disk_free)}</b></span>`;
  }
  if (html !== barSig) {
    barSig = html;
    bar.innerHTML = html;
  }
}

async function tick() {
  if (polling) return;
  polling = true;
  clearTimeout(timer);
  let active = false;
  try {
    const [records, status] = await Promise.all([api("/api/records"), api("/api/status")]);
    limits = status.limits;
    sync(records);
    renderBar(status);
    $("#foot").textContent = `Записи и загруженные файлы удаляются через ${status.retention_days} дней.`;
    const sig = status.models_installed.join(",");
    if (sig !== installedSig) {
      installedSig = sig;
      await refreshModels();
    }
    active = records.some((r) => r.status === "queued" || r.status === "processing") || !!status.download;
  } catch {
    renderBar(null);
  } finally {
    polling = false;
    timer = setTimeout(tick, document.hidden ? 6000 : active ? 1200 : 4000);
  }
}

function sendAudio(blob, name, { source, onProgress }) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", blob, name);
    form.append("language", langSel.value || "auto");
    form.append("model", modelSel.value);
    form.append("source", source);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/transcribe");
    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded, event.total);
      };
    }
    xhr.onload = () => {
      let data = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {}
      if (xhr.status === 201) resolve(data);
      else reject(new Error(typeof data.detail === "string" ? data.detail : `Ошибка ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Нет связи с сервером"));
    xhr.send(form);
  });
}

function requireModel() {
  if (modelSel.value) return true;
  toast("Сначала скачайте модель Whisper", true);
  openModels();
  return false;
}

const recorder = new Recorder();
const level = new LevelStrip($("#level"), { cell: 14, color: COLORS.void });
const recBox = $("#rec");
const recBtn = $("#recBtn");
const recLabel = $("#recLabel");
const timerEl = $("#timer");
let recMode = "idle";
let recTimer = 0;

function setRecMode(mode) {
  recMode = mode;
  recBox.classList.toggle("live", mode === "recording");
  recBtn.disabled = mode === "sending";
  recLabel.textContent = mode === "recording" ? "Стоп" : mode === "sending" ? "Отправляю…" : "Запись";
  level.setActive(mode === "recording", mode === "recording" ? COLORS.acid : COLORS.void);
}

async function startRec() {
  if (recMode !== "idle" || !requireModel()) return;
  try {
    await recorder.start();
  } catch (error) {
    fail(error);
    return;
  }
  setRecMode("recording");
  timerEl.textContent = "00:00";
  recTimer = setInterval(() => {
    level.level = recorder.level();
    const elapsed = recorder.elapsed();
    timerEl.textContent = fmtDur(elapsed);
    if (elapsed >= limits.recording_seconds) {
      toast("Достигнут лимит длины записи", true);
      stopRec();
    }
  }, 80);
}

async function stopRec() {
  if (recMode !== "recording") return;
  clearInterval(recTimer);
  setRecMode("sending");
  const { blob, mime, seconds } = await recorder.stop();
  if (seconds < 0.5 || blob.size < 1000) {
    toast("Запись слишком короткая", true);
    setRecMode("idle");
    return;
  }
  try {
    const { id } = await sendAudio(blob, `rec.${extensionFor(mime)}`, { source: "recording" });
    mine.add(id);
    autoCopy.add(id);
    tick();
  } catch (error) {
    fail(error);
  }
  setRecMode("idle");
}

function toggleRec() {
  if (recMode === "recording") stopRec();
  else startRec();
}

recBtn.addEventListener("click", toggleRec);

document.addEventListener("keydown", (event) => {
  if (event.code !== "Space" || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("textarea, input, select, button, a, summary, [contenteditable]")) return;
  event.preventDefault();
  toggleRec();
});

const drop = $("#drop");
const dropText = $("#dropText");
const upbar = $("#upbar");
const defaultDropText = dropText.textContent;
let uploading = false;

function resetDrop() {
  uploading = false;
  drop.classList.remove("busy", "over");
  dropText.textContent = defaultDropText;
  upbar.style.width = "0";
}

function looksLikeMedia(file) {
  return /^(audio|video)\//.test(file.type) || /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|webm|weba|mp4|m4v|mov|mkv|avi|wma|amr|3gp|aif|aiff|caf|mpeg|mpg|mpga|ts|wmv|flv)$/i.test(file.name);
}

async function uploadFiles(files) {
  if (uploading) {
    toast("Дождитесь окончания текущей загрузки", true);
    return;
  }
  if (!files.length || !requireModel()) return;
  uploading = true;
  drop.classList.add("busy");
  let sent = 0;
  for (let n = 0; n < files.length; n++) {
    const file = files[n];
    if (!looksLikeMedia(file)) {
      toast(`«${file.name}» не похож на аудио или видео`, true);
      continue;
    }
    if (file.size > limits.upload_bytes) {
      toast(`«${file.name}» больше ${fmtBytes(limits.upload_bytes)}`, true);
      continue;
    }
    const prefix = files.length > 1 ? `${n + 1} из ${files.length}: ` : "";
    dropText.textContent = `Загружаю ${prefix}${file.name}`;
    upbar.style.width = "0";
    try {
      const { id } = await sendAudio(file, file.name, {
        source: "upload",
        onProgress: (loaded, total) => {
          upbar.style.width = `${(loaded / total) * 100}%`;
          dropText.textContent = `Загружаю ${prefix}${file.name}: ${fmtBytes(loaded)} из ${fmtBytes(total)}`;
        },
      });
      mine.add(id);
      sent++;
      await tick();
    } catch (error) {
      toast(`«${file.name}»: ${error.message}`, true);
    }
  }
  resetDrop();
  if (sent) toast(sent === 1 ? "Файл загружен, расшифровка в очереди" : `Загружено файлов: ${sent}`);
}

$("#file").addEventListener("change", (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  uploadFiles(files);
});

for (const name of ["dragover", "drop"]) window.addEventListener(name, (event) => event.preventDefault());
drop.addEventListener("dragover", () => drop.classList.add("over"));
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (event) => {
  drop.classList.remove("over");
  uploadFiles([...event.dataTransfer.files]);
});

$("#firstrun").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-fr]");
  if (!button) return;
  try {
    if (button.dataset.fr === "get") {
      button.disabled = true;
      const data = getModels();
      await startDownload(data ? data.default : "large-v3");
    }
    openModels();
  } catch (error) {
    button.disabled = false;
    fail(error);
  }
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-open=models]");
  if (link) {
    $("#models").open = true;
  }
});

document.addEventListener("whisper:poll", () => tick());

window.addEventListener("pagehide", () => {
  for (const card of cards.values()) {
    if (!card.dirty || !card.detail) continue;
    fetch(`/api/records/${card.id}/text`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: effective(card.detail) }),
      keepalive: true,
    });
  }
});

initModels({ getSelected: () => modelSel.value || null, select: selectModel });
onModels(renderSelect);

new GlyphField($("#field"), { cell: 14, seed: 5 });
const heading = $(".hero h1");
scramble(heading, heading.textContent, 800);
setRecMode("idle");
tick();
