const MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

function pickMime() {
  if (!window.MediaRecorder) return "";
  return MIMES.find((m) => MediaRecorder.isTypeSupported(m)) || "";
}

function micMessage(err) {
  if (err && err.name === "NotAllowedError") return "Доступ к микрофону запрещён. Разрешите его в настройках сайта.";
  if (err && err.name === "NotFoundError") return "Микрофон не найден.";
  if (err && err.name === "NotReadableError") return "Микрофон занят другим приложением.";
  return "Не удалось открыть микрофон.";
}

export function extensionFor(mime) {
  if (mime.includes("mp4")) return "m4a";
  if (mime.includes("ogg")) return "ogg";
  return "webm";
}

export class Recorder {
  constructor() {
    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.mime = "";
    this.startedAt = 0;
    this.ctx = null;
    this.analyser = null;
    this.buf = null;
    this.active = false;
  }

  async start() {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("Микрофон работает только по HTTPS или на localhost. Откройте сайт по адресу https://");
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      throw new Error(micMessage(err));
    }
    this.mime = pickMime();
    this.chunks = [];
    try {
      this.recorder = this.mime ? new MediaRecorder(this.stream, { mimeType: this.mime }) : new MediaRecorder(this.stream);
    } catch {
      this.release();
      throw new Error("Браузер не поддерживает запись звука.");
    }
    this.mime = this.recorder.mimeType || this.mime || "audio/webm";
    this.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) this.chunks.push(event.data);
    };
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new Ctx();
      const source = this.ctx.createMediaStreamSource(this.stream);
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.buf = new Uint8Array(this.analyser.fftSize);
      source.connect(this.analyser);
    } catch {
      this.analyser = null;
    }
    this.recorder.start(1000);
    this.startedAt = performance.now();
    this.active = true;
  }

  elapsed() {
    return this.active ? (performance.now() - this.startedAt) / 1000 : 0;
  }

  level() {
    if (!this.analyser) return 0;
    this.analyser.getByteTimeDomainData(this.buf);
    let sum = 0;
    for (let i = 0; i < this.buf.length; i++) {
      const v = (this.buf[i] - 128) / 128;
      sum += v * v;
    }
    return Math.min(1, Math.sqrt(sum / this.buf.length) * 4.5);
  }

  stop() {
    return new Promise((resolve) => {
      const seconds = this.elapsed();
      const finish = () => {
        const blob = new Blob(this.chunks, { type: this.mime });
        const mime = this.mime;
        this.chunks = [];
        this.release();
        resolve({ blob, mime, seconds });
      };
      if (!this.recorder || this.recorder.state === "inactive") {
        finish();
        return;
      }
      this.recorder.onstop = finish;
      this.recorder.stop();
    });
  }

  release() {
    this.active = false;
    if (this.ctx) this.ctx.close().catch(() => {});
    this.ctx = null;
    this.analyser = null;
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.recorder = null;
  }
}
