"use strict";

const TARGET_SAMPLE_RATE = 16000;
const CHUNK_BYTES = 1024;
const MAX_BATCH_CHUNKS = 8;

const ui = {
  serverStatus: document.querySelector("#server-status"),
  wakeWord: document.querySelector("#wake-word"),
  startButton: document.querySelector("#start-button"),
  stopButton: document.querySelector("#stop-button"),
  liveState: document.querySelector("#live-state"),
  liveHint: document.querySelector("#live-hint"),
  timer: document.querySelector("#timer"),
  levelFill: document.querySelector("#level-fill"),
  metricChunks: document.querySelector("#metric-chunks"),
  metricDuration: document.querySelector("#metric-duration"),
  metricSegments: document.querySelector("#metric-segments"),
  metricRoute: document.querySelector("#metric-route"),
  rawText: document.querySelector("#raw-text"),
  wakeResult: document.querySelector("#wake-result"),
  commandText: document.querySelector("#command-text"),
  assistantReply: document.querySelector("#assistant-reply"),
  decodeMs: document.querySelector("#decode-ms"),
  endpointMs: document.querySelector("#endpoint-ms"),
  utteranceS: document.querySelector("#utterance-s"),
  routeBadge: document.querySelector("#route-badge"),
  history: document.querySelector("#history"),
  clearButton: document.querySelector("#clear-button"),
  matcherNote: document.querySelector("#matcher-note"),
  toast: document.querySelector("#toast"),
  stages: {
    input: document.querySelector("#stage-input"),
    vad: document.querySelector("#stage-vad"),
    asr: document.querySelector("#stage-asr"),
    route: document.querySelector("#stage-route"),
  },
};

const app = {
  running: false,
  stream: null,
  audioContext: null,
  sourceNode: null,
  processorNode: null,
  startedAt: 0,
  timerHandle: null,
  pcmRemainder: new Uint8Array(0),
  sendQueue: [],
  sending: false,
  sendError: null,
  history: [],
  resampler: null,
};

function showToast(message, isError = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.classList.add("visible");
  window.clearTimeout(showToast.handle);
  showToast.handle = window.setTimeout(() => ui.toast.classList.remove("visible"), 2800);
}

function formatTime(milliseconds) {
  const tenths = Math.max(0, Math.floor(milliseconds / 100));
  const minutes = Math.floor(tenths / 600);
  const seconds = Math.floor((tenths % 600) / 10);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${tenths % 10}`;
}

function setStage(name, state, detail) {
  const stage = ui.stages[name];
  stage.classList.remove("active", "done", "background");
  if (state) stage.classList.add(state);
  stage.querySelector("small").textContent = detail;
}

function resetResampler(sourceRate) {
  app.resampler = {
    sourceRate,
    inputSamples: 0,
    nextOutputPosition: 0,
    previousSample: 0,
    hasPreviousSample: false,
  };
}

function resampleStreaming(input, sourceRate, targetRate) {
  if (!app.resampler || app.resampler.sourceRate !== sourceRate) {
    resetResampler(sourceRate);
  }
  const state = app.resampler;
  if (sourceRate === targetRate) {
    state.inputSamples += input.length;
    state.previousSample = input[input.length - 1] || 0;
    state.hasPreviousSample = input.length > 0;
    return new Float32Array(input);
  }

  // Keep one continuous source timeline across AudioContext callbacks. Doing
  // the rounding separately for every callback slowly changes the sample rate
  // (for example 48 kHz -> 16 kHz) and introduces a seam at every block.
  const start = state.inputSamples;
  const end = start + input.length;
  const step = sourceRate / targetRate;
  const output = [];
  while (state.nextOutputPosition <= end - 1) {
    const left = Math.floor(state.nextOutputPosition);
    const right = Math.ceil(state.nextOutputPosition);
    if (right >= end) break;
    let leftValue;
    if (left < start) {
      if (!state.hasPreviousSample) break;
      leftValue = state.previousSample;
    } else {
      leftValue = input[left - start];
    }
    const rightValue = right < start
      ? state.previousSample
      : input[right - start];
    const fraction = state.nextOutputPosition - left;
    output.push(leftValue * (1 - fraction) + rightValue * fraction);
    state.nextOutputPosition += step;
  }
  state.inputSamples = end;
  if (input.length) {
    state.previousSample = input[input.length - 1];
    state.hasPreviousSample = true;
  }
  return Float32Array.from(output);
}

function pcm16Bytes(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < samples.length; index += 1) {
    const clipped = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(index * 2, clipped < 0 ? clipped * 32768 : clipped * 32767, true);
  }
  return new Uint8Array(buffer);
}

function appendPcm(bytes) {
  const combined = new Uint8Array(app.pcmRemainder.length + bytes.length);
  combined.set(app.pcmRemainder, 0);
  combined.set(bytes, app.pcmRemainder.length);
  let offset = 0;
  while (combined.length - offset >= CHUNK_BYTES) {
    app.sendQueue.push(combined.slice(offset, offset + CHUNK_BYTES));
    offset += CHUNK_BYTES;
  }
  app.pcmRemainder = combined.slice(offset);
  void pumpChunks();
}

async function readJson(response) {
  let result;
  try {
    result = await response.json();
  } catch (_) {
    throw new Error(`本地服务返回了无效响应（HTTP ${response.status}）`);
  }
  if (!response.ok || result.ok === false) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

async function postJson(path, value = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(value),
  });
  return readJson(response);
}

async function pumpChunks() {
  if (app.sending || app.sendQueue.length === 0 || app.sendError) return;
  app.sending = true;
  const chunks = app.sendQueue.splice(0, MAX_BATCH_CHUNKS);
  const body = new Uint8Array(chunks.length * CHUNK_BYTES);
  chunks.forEach((chunk, index) => body.set(chunk, index * CHUNK_BYTES));
  try {
    const response = await fetch("/api/live/chunks", {
      method: "POST",
      headers: {"Content-Type": "application/octet-stream"},
      body,
    });
    renderResponse(await readJson(response));
  } catch (error) {
    console.error(error);
    app.sendError = error;
    app.running = false;
    cleanupAudio();
    ui.startButton.disabled = false;
    ui.startButton.textContent = "重新开始";
    ui.stopButton.disabled = true;
    ui.wakeWord.disabled = false;
    ui.liveState.textContent = "音频传输失败";
    ui.liveHint.textContent = error.message;
    showToast(error.message, true);
  } finally {
    app.sending = false;
    if (app.sendQueue.length) void pumpChunks();
  }
}

async function waitForQueue(timeoutMs = 10000) {
  const deadline = performance.now() + timeoutMs;
  while (!app.sendError && (app.sending || app.sendQueue.length)
      && performance.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 25));
  }
  if (app.sendError) throw app.sendError;
  if (app.sending || app.sendQueue.length) throw new Error("等待音频发送完成超时");
}

function renderSession(session) {
  if (!session) return;
  ui.metricChunks.textContent = String(session.chunks_received || 0);
  ui.metricDuration.textContent = `${Number(session.audio_duration_s || 0).toFixed(1)}s`;
  ui.metricSegments.textContent = String(session.segment_count || 0);
  if (session.wake_pending && !session.speech_active) {
    ui.liveState.textContent = "已唤醒，等待任务…";
    ui.liveHint.textContent = `请在 ${(Number(session.wake_remaining_ms) / 1000).toFixed(1)} 秒内说出任务，无需重复唤醒词`;
    setStage("route", "active", "等待任务语句");
  } else if (session.speech_active) {
    ui.liveState.textContent = "检测到语音…";
    setStage("vad", "active", "正在收集语音段");
  } else if (app.running) {
    ui.liveState.textContent = "正在聆听";
    setStage("vad", "", "等待语音");
  }
}

function routeLabel(event) {
  if (event.route === "core" && event.route_reason === "deferred_command") {
    return "CORE 延迟指令";
  }
  if (event.route === "core") return "CORE 指令";
  if (event.route === "wake_only") return "仅唤醒";
  if (event.route === "background") return "背景语音";
  return "无输出";
}

function renderTranscript(event) {
  ui.rawText.textContent = event.raw_text || "（空文本）";
  ui.wakeResult.textContent = event.wake_matched
    ? `已匹配 ${event.wake_variant || event.wake_word} · ${event.wake_match_method}`
    : event.wake_session_active
      ? "已在唤醒窗口内 · 无需重复唤醒"
      : "未匹配";
  ui.commandText.textContent = event.command_text || "不发送任务";
  ui.assistantReply.textContent = event.assistant_reply || "—";
  ui.decodeMs.textContent = `${Number(event.decode_ms).toFixed(0)}ms`;
  ui.endpointMs.textContent = `${Number(event.endpoint_latency_ms).toFixed(0)}ms`;
  ui.utteranceS.textContent = `${Number(event.utterance_duration_s).toFixed(1)}s`;
  ui.metricRoute.textContent = event.route;
  ui.routeBadge.textContent = routeLabel(event);
  ui.routeBadge.className = `route-badge ${event.route}`;

  setStage("asr", "done", `识别 ${event.raw_text?.length || 0} 字`);
  setStage(
    "route",
    event.route === "core" ? "done" : "background",
    routeLabel(event),
  );

  app.history.unshift(event);
  app.history = app.history.slice(0, 12);
  ui.history.innerHTML = app.history.map((item) => `
    <article class="history-item ${item.route}">
      <div><span>${String(item.index).padStart(2, "0")}</span><strong>${escapeHtml(item.raw_text || "（空文本）")}</strong></div>
      <p>${item.send_to_core ? `任务：${escapeHtml(item.command_text)}` : routeLabel(item)}</p>
      ${item.assistant_reply ? `<p class="assistant-line">机器人：${escapeHtml(item.assistant_reply)}</p>` : ""}
      <small>${Number(item.endpoint_latency_ms).toFixed(0)}ms · ${escapeHtml(item.wake_match_method || "none")}</small>
    </article>
  `).join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderResponse(response) {
  renderSession(response.session);
  for (const event of response.events || []) {
    if (event.type === "speech_start") {
      setStage("vad", "active", "检测到语音");
      setStage("asr", "active", "等待句尾");
    }
    if (event.type === "transcript") renderTranscript(event);
  }
}

function cleanupAudio() {
  window.clearInterval(app.timerHandle);
  app.processorNode?.disconnect();
  app.sourceNode?.disconnect();
  app.stream?.getTracks().forEach((track) => track.stop());
  const context = app.audioContext;
  app.processorNode = null;
  app.sourceNode = null;
  app.stream = null;
  app.audioContext = null;
  if (context && context.state !== "closed") void context.close();
  ui.levelFill.style.width = "0%";
}

async function startLive() {
  if (app.running) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    showToast("当前浏览器不支持麦克风，请使用 Chrome 或 Safari", true);
    return;
  }
  ui.startButton.disabled = true;
  ui.wakeWord.disabled = true;
  app.sendError = null;
  app.sendQueue = [];
  app.pcmRemainder = new Uint8Array(0);
  app.resampler = null;
  try {
    app.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    try {
      app.audioContext = new AudioContextClass({sampleRate: TARGET_SAMPLE_RATE});
    } catch (_) {
      app.audioContext = new AudioContextClass();
    }
    await app.audioContext.resume();
    app.sourceNode = app.audioContext.createMediaStreamSource(app.stream);
    app.processorNode = app.audioContext.createScriptProcessor(1024, 1, 1);

    const captureStartedAtMs = Date.now();
    await postJson("/api/live/start", {
      wake_word: ui.wakeWord.value.trim(),
      capture_started_at_ms: captureStartedAtMs,
    });

    app.running = true;
    app.startedAt = performance.now();
    app.processorNode.onaudioprocess = (event) => {
      const output = event.outputBuffer.getChannelData(0);
      output.fill(0);
      if (!app.running) return;
      const input = event.inputBuffer.getChannelData(0);
      let energy = 0;
      for (let index = 0; index < input.length; index += 1) energy += input[index] ** 2;
      const rms = Math.sqrt(energy / input.length);
      ui.levelFill.style.width = `${Math.min(100, Math.max(1, rms * 520))}%`;
      const samples = resampleStreaming(
        input, app.audioContext.sampleRate, TARGET_SAMPLE_RATE);
      appendPcm(pcm16Bytes(samples));
    };
    app.sourceNode.connect(app.processorNode);
    app.processorNode.connect(app.audioContext.destination);

    ui.startButton.textContent = "实时识别中";
    ui.stopButton.disabled = false;
    ui.liveState.textContent = "正在聆听";
    ui.liveHint.textContent = `输入采样率 ${app.audioContext.sampleRate} Hz · 请自然说出唤醒词和任务`;
    setStage("input", "active", "实时采集中");
    setStage("vad", "", "等待语音");
    setStage("asr", "", "等待分句");
    setStage("route", "", "等待结果");
    app.timerHandle = window.setInterval(() => {
      ui.timer.textContent = formatTime(performance.now() - app.startedAt);
    }, 100);
  } catch (error) {
    console.error(error);
    cleanupAudio();
    ui.startButton.disabled = false;
    ui.wakeWord.disabled = false;
    showToast(error.message || "无法启动麦克风", true);
    ui.liveHint.textContent = "请检查浏览器麦克风权限和本地服务。";
  }
}

async function stopLive() {
  if (!app.running) return;
  app.running = false;
  ui.stopButton.disabled = true;
  ui.liveState.textContent = "正在完成最后一句…";
  ui.liveHint.textContent = "等待剩余音频和 VAD 尾段处理完成";
  cleanupAudio();

  if (app.pcmRemainder.length) {
    const padded = new Uint8Array(CHUNK_BYTES);
    padded.set(app.pcmRemainder);
    app.sendQueue.push(padded);
    app.pcmRemainder = new Uint8Array(0);
    void pumpChunks();
  }
  try {
    await waitForQueue();
    renderResponse(await postJson("/api/live/stop"));
    ui.liveState.textContent = "已停止";
    ui.liveHint.textContent = "可以修改唤醒词后重新开始。";
  } catch (error) {
    console.error(error);
    showToast(error.message, true);
    ui.liveState.textContent = "停止时发生错误";
  } finally {
    ui.startButton.disabled = false;
    ui.startButton.textContent = "开始实时识别";
    ui.wakeWord.disabled = false;
    setStage("input", "done", "采集已停止");
  }
}

async function initialize() {
  try {
    const response = await fetch("/api/state", {cache: "no-store"});
    const state = await readJson(response);
    ui.serverStatus.classList.add("ready");
    ui.serverStatus.innerHTML = "<span></span>模型已就绪";
    ui.wakeWord.value = state.wake_word || "小范小范";
    const aliases = (state.wake_aliases || [state.wake_word]).join(" / ");
    const holdSeconds = Number(state.wake_hold_ms || 0) / 1000;
    ui.matcherNote.textContent = state.pinyin_available
      ? `可用唤醒：${aliases}（最长优先）· 单独唤醒后等待任务 ${holdSeconds} 秒。`
      : `可用唤醒：${aliases}（最长优先）· 等待任务 ${holdSeconds} 秒；当前仅精确匹配。`;
  } catch (error) {
    console.error(error);
    ui.serverStatus.classList.add("error");
    ui.serverStatus.innerHTML = "<span></span>模型服务不可用";
    ui.startButton.disabled = true;
    ui.liveHint.textContent = error.message;
  }
}

ui.startButton.addEventListener("click", startLive);
ui.stopButton.addEventListener("click", stopLive);
ui.clearButton.addEventListener("click", () => {
  app.history = [];
  ui.history.innerHTML = '<p class="empty">识别结果会按时间显示在这里。</p>';
});
window.addEventListener("beforeunload", () => cleanupAudio());

initialize();
