"use strict";

const TARGET_SAMPLE_RATE = 16000;

const ui = {
  progressCount: document.querySelector("#progress-count"),
  progressFill: document.querySelector("#progress-fill"),
  splitSummary: document.querySelector("#split-summary"),
  promptList: document.querySelector("#prompt-list"),
  promptPosition: document.querySelector("#prompt-position"),
  splitBadge: document.querySelector("#split-badge"),
  categoryBadge: document.querySelector("#category-badge"),
  savedBadge: document.querySelector("#saved-badge"),
  promptText: document.querySelector("#prompt-text"),
  promptHint: document.querySelector("#prompt-hint"),
  micStage: document.querySelector("#mic-stage"),
  recordButton: document.querySelector("#record-button"),
  recordingState: document.querySelector("#recording-state"),
  timer: document.querySelector("#timer"),
  levelFill: document.querySelector("#level-fill"),
  reviewPanel: document.querySelector("#review-panel"),
  reviewDuration: document.querySelector("#review-duration"),
  audioPreview: document.querySelector("#audio-preview"),
  previousButton: document.querySelector("#previous-button"),
  discardButton: document.querySelector("#discard-button"),
  saveButton: document.querySelector("#save-button"),
  skipButton: document.querySelector("#skip-button"),
  nextUnsaved: document.querySelector("#next-unsaved"),
  outputDirectory: document.querySelector("#output-directory"),
  toast: document.querySelector("#toast"),
};

const app = {
  prompts: [],
  saved: new Map(),
  currentIndex: 0,
  stream: null,
  audioContext: null,
  sourceNode: null,
  processorNode: null,
  chunks: [],
  inputSampleRate: 0,
  recording: false,
  startedAt: 0,
  timerHandle: null,
  pendingBlob: null,
  pendingDuration: 0,
  previewUrl: null,
};

function showToast(message, isError = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.classList.add("visible");
  window.clearTimeout(showToast.handle);
  showToast.handle = window.setTimeout(() => ui.toast.classList.remove("visible"), 2600);
}

function formatTime(milliseconds) {
  const totalTenths = Math.max(0, Math.floor(milliseconds / 100));
  const minutes = Math.floor(totalTenths / 600);
  const seconds = Math.floor((totalTenths % 600) / 10);
  const tenths = totalTenths % 10;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

function escapeText(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function currentPrompt() {
  return app.prompts[app.currentIndex];
}

function clearPreview() {
  app.pendingBlob = null;
  app.pendingDuration = 0;
  if (app.previewUrl) {
    URL.revokeObjectURL(app.previewUrl);
    app.previewUrl = null;
  }
  ui.audioPreview.removeAttribute("src");
  ui.audioPreview.load();
  ui.reviewPanel.classList.add("hidden");
  ui.discardButton.classList.add("hidden");
  ui.saveButton.disabled = true;
}

function loadSavedPreview(prompt) {
  if (!app.saved.has(prompt.id)) return;
  const record = app.saved.get(prompt.id);
  ui.audioPreview.src = `/api/recordings/${prompt.id}?v=${encodeURIComponent(record.recorded_at || "saved")}`;
  ui.reviewDuration.textContent = `${Number(record.duration_s).toFixed(1)} 秒 · 已保存`;
  ui.reviewPanel.classList.remove("hidden");
  ui.discardButton.classList.remove("hidden");
}

function updateProgress() {
  const total = app.prompts.length;
  const completed = app.saved.size;
  const devTotal = app.prompts.filter((item) => item.split === "dev").length;
  const testTotal = total - devTotal;
  const devSaved = [...app.saved.values()].filter((item) => item.split === "dev").length;
  const testSaved = completed - devSaved;
  ui.progressCount.textContent = `${completed} / ${total}`;
  ui.progressFill.style.width = `${total ? (completed / total) * 100 : 0}%`;
  ui.splitSummary.textContent = `调试集 ${devSaved}/${devTotal} · 测试集 ${testSaved}/${testTotal}`;
}

function renderPromptList() {
  ui.promptList.innerHTML = app.prompts.map((prompt, index) => {
    const display = prompt.display_text || prompt.text || "静音样本";
    const saved = app.saved.has(prompt.id);
    return `
      <button class="prompt-item ${index === app.currentIndex ? "active" : ""}" type="button" data-index="${index}">
        <span class="prompt-number">${String(index + 1).padStart(2, "0")}</span>
        <span class="prompt-copy">
          <strong>${escapeText(display)}</strong>
          <small>${prompt.split === "dev" ? "调试集" : "测试集"} · ${escapeText(prompt.category)}</small>
        </span>
        <span class="prompt-check">${saved ? "✓" : ""}</span>
      </button>`;
  }).join("");

  ui.promptList.querySelectorAll(".prompt-item").forEach((button) => {
    button.addEventListener("click", () => navigateTo(Number(button.dataset.index)));
  });
  ui.promptList.querySelector(".prompt-item.active")?.scrollIntoView({block: "nearest"});
}

function renderPrompt() {
  const prompt = currentPrompt();
  if (!prompt) return;
  clearPreview();
  ui.promptPosition.textContent = `${String(app.currentIndex + 1).padStart(2, "0")} / ${String(app.prompts.length).padStart(2, "0")}`;
  ui.splitBadge.textContent = prompt.split === "dev" ? "调试集" : "测试集";
  ui.splitBadge.className = `badge ${prompt.split === "dev" ? "badge-dev" : "badge-test"}`;
  ui.categoryBadge.textContent = prompt.category;
  ui.promptText.textContent = prompt.display_text || prompt.text || "保持安静，不要说话";
  ui.promptText.classList.toggle("silence-prompt", !prompt.text);
  ui.promptHint.textContent = prompt.hint;
  ui.savedBadge.classList.toggle("hidden", !app.saved.has(prompt.id));
  ui.previousButton.disabled = app.currentIndex === 0;
  ui.recordingState.textContent = app.saved.has(prompt.id)
    ? "已保存，可播放检查或点击麦克风重新录制"
    : "点击麦克风开始录音";
  ui.timer.textContent = "00:00.0";
  ui.levelFill.style.width = "0%";
  loadSavedPreview(prompt);
  updateProgress();
  renderPromptList();
}

function hasUnsavedPending() {
  return Boolean(app.pendingBlob);
}

function navigateTo(index) {
  if (app.recording) {
    showToast("请先停止当前录音", true);
    return;
  }
  if (index < 0 || index >= app.prompts.length || index === app.currentIndex) return;
  if (hasUnsavedPending() && !window.confirm("当前录音还没有保存，确定离开吗？")) return;
  app.currentIndex = index;
  renderPrompt();
}

function concatenate(chunks) {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const output = new Float32Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.length;
  }
  return output;
}

function resampleLinear(input, sourceRate, targetRate) {
  if (sourceRate === targetRate) return input;
  const ratio = sourceRate / targetRate;
  const outputLength = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(outputLength);
  for (let index = 0; index < outputLength; index += 1) {
    const sourcePosition = index * ratio;
    const left = Math.floor(sourcePosition);
    const right = Math.min(left + 1, input.length - 1);
    const fraction = sourcePosition - left;
    output[index] = input[left] * (1 - fraction) + input[right] * fraction;
  }
  return output;
}

function writeAscii(view, offset, value) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function encodePcm16Wav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (const sample of samples) {
    const clipped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clipped < 0 ? clipped * 32768 : clipped * 32767, true);
    offset += 2;
  }
  return new Blob([view], {type: "audio/wav"});
}

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    showToast("当前浏览器不支持麦克风录音，请使用 Chrome 或 Safari", true);
    return;
  }
  clearPreview();
  try {
    app.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    app.audioContext = new AudioContext();
    await app.audioContext.resume();
    app.inputSampleRate = app.audioContext.sampleRate;
    app.chunks = [];
    app.sourceNode = app.audioContext.createMediaStreamSource(app.stream);
    app.processorNode = app.audioContext.createScriptProcessor(4096, 1, 1);
    app.processorNode.onaudioprocess = (event) => {
      if (!app.recording) return;
      const input = event.inputBuffer.getChannelData(0);
      app.chunks.push(new Float32Array(input));
      const output = event.outputBuffer.getChannelData(0);
      output.fill(0);
      let energy = 0;
      for (let index = 0; index < input.length; index += 1) energy += input[index] ** 2;
      const rms = Math.sqrt(energy / input.length);
      ui.levelFill.style.width = `${Math.min(100, Math.max(2, rms * 520))}%`;
    };
    app.sourceNode.connect(app.processorNode);
    app.processorNode.connect(app.audioContext.destination);
    app.recording = true;
    app.startedAt = performance.now();
    ui.micStage.classList.add("recording");
    ui.recordButton.setAttribute("aria-label", "停止录音");
    ui.recordingState.textContent = "正在录音 · 再次点击停止";
    ui.timer.textContent = "00:00.0";
    app.timerHandle = window.setInterval(() => {
      ui.timer.textContent = formatTime(performance.now() - app.startedAt);
    }, 100);
  } catch (error) {
    console.error(error);
    showToast("无法使用麦克风，请在浏览器地址栏允许麦克风权限", true);
  }
}

async function stopRecording() {
  if (!app.recording) return;
  app.recording = false;
  window.clearInterval(app.timerHandle);
  app.processorNode?.disconnect();
  app.sourceNode?.disconnect();
  app.stream?.getTracks().forEach((track) => track.stop());
  await app.audioContext?.close();
  ui.micStage.classList.remove("recording");
  ui.recordButton.setAttribute("aria-label", "开始录音");
  ui.recordingState.textContent = "录音完成，请播放检查后保存";
  ui.levelFill.style.width = "0%";

  const input = concatenate(app.chunks);
  const samples = resampleLinear(input, app.inputSampleRate, TARGET_SAMPLE_RATE);
  app.pendingBlob = encodePcm16Wav(samples, TARGET_SAMPLE_RATE);
  app.pendingDuration = samples.length / TARGET_SAMPLE_RATE;
  app.previewUrl = URL.createObjectURL(app.pendingBlob);
  ui.audioPreview.src = app.previewUrl;
  ui.reviewDuration.textContent = `${app.pendingDuration.toFixed(1)} 秒 · 待保存`;
  ui.reviewPanel.classList.remove("hidden");
  ui.discardButton.classList.remove("hidden");
  ui.saveButton.disabled = app.pendingDuration < 0.2;
  if (app.pendingDuration < 0.2) showToast("录音太短，请重新录制", true);
}

async function saveRecording() {
  const prompt = currentPrompt();
  if (!app.pendingBlob || !prompt) return;
  ui.saveButton.disabled = true;
  ui.saveButton.textContent = "正在保存…";
  try {
    const response = await fetch(`/api/recordings/${prompt.id}`, {
      method: "POST",
      headers: {"Content-Type": "audio/wav"},
      body: app.pendingBlob,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    app.saved.set(prompt.id, result.record);
    showToast(`已保存 ${prompt.id} · ${result.record.duration_s.toFixed(1)} 秒`);
    const next = app.prompts.findIndex((item, index) => index > app.currentIndex && !app.saved.has(item.id));
    const fallback = app.prompts.findIndex((item) => !app.saved.has(item.id));
    app.currentIndex = next >= 0 ? next : (fallback >= 0 ? fallback : app.currentIndex);
    renderPrompt();
    if (app.saved.size === app.prompts.length) showToast("30 条录音已全部完成！");
  } catch (error) {
    console.error(error);
    showToast(error.message || "保存失败", true);
    ui.saveButton.disabled = false;
  } finally {
    ui.saveButton.textContent = "保存并下一条 →";
  }
}

function findNextUnsaved() {
  const next = app.prompts.findIndex((item, index) => index > app.currentIndex && !app.saved.has(item.id));
  const fallback = app.prompts.findIndex((item) => !app.saved.has(item.id));
  if (next >= 0 || fallback >= 0) navigateTo(next >= 0 ? next : fallback);
  else showToast("所有句子都已经录制完成");
}

async function initialize() {
  try {
    const response = await fetch("/api/state", {cache: "no-store"});
    if (!response.ok) throw new Error("无法连接本地录音服务");
    const state = await response.json();
    app.prompts = state.prompts;
    app.saved = new Map(state.saved.map((record) => [record.id, record]));
    ui.outputDirectory.textContent = state.output_dir;
    const firstUnsaved = app.prompts.findIndex((prompt) => !app.saved.has(prompt.id));
    app.currentIndex = firstUnsaved >= 0 ? firstUnsaved : 0;
    renderPrompt();
  } catch (error) {
    console.error(error);
    ui.promptText.textContent = "无法连接本地录音服务";
    ui.promptHint.textContent = "请回到终端确认 Python 服务仍在运行。";
    showToast(error.message, true);
  }
}

ui.recordButton.addEventListener("click", () => app.recording ? stopRecording() : startRecording());
ui.saveButton.addEventListener("click", saveRecording);
ui.discardButton.addEventListener("click", () => {
  clearPreview();
  ui.recordingState.textContent = "录音已清除，点击麦克风重新录制";
});
ui.previousButton.addEventListener("click", () => navigateTo(app.currentIndex - 1));
ui.skipButton.addEventListener("click", () => navigateTo(Math.min(app.prompts.length - 1, app.currentIndex + 1)));
ui.nextUnsaved.addEventListener("click", findNextUnsaved);

window.addEventListener("keydown", (event) => {
  if (event.code === "Space" && !["BUTTON", "AUDIO"].includes(document.activeElement?.tagName)) {
    event.preventDefault();
    app.recording ? stopRecording() : startRecording();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (app.recording || hasUnsavedPending()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

initialize();
