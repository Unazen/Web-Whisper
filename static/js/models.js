import { $, esc, fmtBytes, api, post, toast, fail } from "./util.js";

const panel = $("#models");
const body = $("#modelsBody");
const listeners = new Set();

let data = null;
let timer = 0;
let sig = "";
let hooks = { getSelected: () => null, select: () => {} };

export const getModels = () => data;
export const onModels = (fn) => listeners.add(fn);

const meter = (n) => "■".repeat(n) + "□".repeat(5 - n);
const sizeLabel = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} ГБ` : `${mb} МБ`);

function busy(model) {
  return model.download && (model.download.state === "queued" || model.download.state === "downloading");
}

function cardHtml(model, selected) {
  const id = esc(model.id);
  let action;
  if (model.installed) {
    const chosen = model.id === selected;
    action = `<div class="actions">${
      chosen
        ? `<span class="tag t-done"><i></i><span>выбрана</span></span>`
        : `<button type="button" class="btn" data-m="use" data-id="${id}">Выбрать</button>`
    }<button type="button" class="btn ghost" data-m="remove" data-id="${id}">Удалить</button></div>`;
  } else if (busy(model)) {
    action = `<div class="dl"><i data-dlbar="${id}"></i></div><span class="dim" data-dltext="${id}"></span>`;
  } else {
    const error = model.download && model.download.state === "failed" ? `<p class="alert">${esc(model.download.error || "Не удалось скачать")}</p>` : "";
    action = `${error}<div class="actions"><button type="button" class="btn solid" data-m="get" data-id="${id}">Скачать ${sizeLabel(model.size_mb)}</button></div>`;
  }
  const badge = model.badge ? `<span class="tag t-idle"><span>${esc(model.badge)}</span></span>` : "";
  const loaded = model.loaded ? `<span class="tag t-run"><i></i><span>в памяти</span></span>` : "";
  return `<div class="cat${model.installed && model.id === selected ? " sel" : ""}">
    <div class="cat-head"><b>${esc(model.title)}</b>${badge}${loaded}</div>
    <p class="dim">${esc(model.desc)}</p>
    <div class="facts"><span>диск <b>${sizeLabel(model.size_mb)}</b></span><span>видеопамять <b>~${(model.vram_mb / 1024).toFixed(1)} ГБ</b></span><span>языки <b>${model.lang === "en" ? "English" : "99"}</b></span></div>
    <div class="meters"><span>скорость <b>${meter(model.speed)}</b></span><span>точность <b>${meter(model.quality)}</b></span></div>
    ${action}
  </div>`;
}

function patchProgress() {
  for (const model of data.models) {
    if (!busy(model)) continue;
    const bar = $(`[data-dlbar="${CSS.escape(model.id)}"]`, body);
    const text = $(`[data-dltext="${CSS.escape(model.id)}"]`, body);
    if (!bar || !text) continue;
    const d = model.download;
    const share = d.total ? Math.min(100, ((d.downloaded || 0) / d.total) * 100) : 0;
    bar.style.width = `${share}%`;
    text.textContent =
      d.state === "queued" ? "ждёт очереди на скачивание" : `${fmtBytes(d.downloaded || 0)} из ${fmtBytes(d.total)}`;
  }
}

export function renderModels() {
  if (!data) return;
  const selected = hooks.getSelected();
  const next = JSON.stringify([
    selected,
    data.models.map((m) => [m.id, m.installed, m.loaded, m.download ? [m.download.state, m.download.error] : null]),
  ]);
  if (next !== sig) {
    sig = next;
    const minutes = Math.round(data.idle_unload_seconds / 60);
    body.innerHTML = `<p class="dim">Папка с моделями: ${esc(data.dir)}. Выбранная модель загружается в память при первой расшифровке и выгружается после ${minutes} мин простоя. Оценки скорости и точности относительные, для русской речи лучше всего работают large-v3 и large-v3-turbo.</p>
      <div class="catalog models">${data.models.map((m) => cardHtml(m, selected)).join("")}</div>`;
  }
  patchProgress();
}

export async function refreshModels() {
  clearTimeout(timer);
  try {
    data = await api("/api/models");
    renderModels();
    listeners.forEach((fn) => fn(data));
  } catch (error) {
    if (panel.open) body.innerHTML = `<p class="alert">${esc(error.message)}</p>`;
    sig = "";
  }
  const downloading = data && data.models.some(busy);
  if (panel.open || downloading) timer = setTimeout(refreshModels, 1500);
  return data;
}

export function openModels() {
  panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

export async function startDownload(id) {
  await post(`/api/models/${encodeURIComponent(id)}/download`);
  toast(`Скачиваю модель ${id}`);
  document.dispatchEvent(new Event("whisper:poll"));
  await refreshModels();
}

export function initModels(options) {
  hooks = options;
  panel.addEventListener("toggle", () => {
    if (panel.open) {
      if (!body.innerHTML) body.innerHTML = `<p class="dim">Загружаю список моделей…</p>`;
      refreshModels();
    } else {
      clearTimeout(timer);
    }
  });
  body.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-m]");
    if (!button) return;
    const id = button.dataset.id;
    try {
      if (button.dataset.m === "use") {
        hooks.select(id);
      } else if (button.dataset.m === "get") {
        button.disabled = true;
        await startDownload(id);
      } else if (button.dataset.m === "remove") {
        if (!confirm(`Удалить модель ${id} с диска?`)) return;
        button.disabled = true;
        await api(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast(`Модель ${id} удалена`);
        sig = "";
        await refreshModels();
      }
    } catch (error) {
      fail(error);
      sig = "";
      refreshModels();
    }
  });
}
