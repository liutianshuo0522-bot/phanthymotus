# Robot ASR Live

这个工具把浏览器麦克风接到本地 VAD/ASR 服务，用于演示机器人收到声音之后的
完整判断过程：

```text
麦克风 → 16 kHz/PCM16 → 1024-byte 分块 → VAD 分句 → ASR
      → 唤醒词匹配 → core 指令 / background 对话
```

所有请求只发到 `127.0.0.1`，音频不保存、不上传云端。浏览器可以一次批量发送多个
分块以降低 HTTP 开销，但服务端会逐个验证并处理每个 1024-byte 分块。

## 安装

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r perception/tools/asr_live/requirements.txt
```

模型保存在仓库外，不能提交到 Git。首次启动会下载 ASR 和 VAD 模型；如果 ASR 模型
已经在 Mac 缓存中，可以显式传入路径：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem .venv/bin/python \
  perception/tools/asr_live/server.py \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m"
```

然后用 Chrome 打开 `http://127.0.0.1:8766`，点击“开始实时识别”并允许麦克风权限。
终端中的服务要在整个演示期间保持运行；结束时按 `Ctrl-C`。

## 推荐演示顺序

1. 不说唤醒词：“今天天气不错。”页面应显示 `background`，不发送任务。
2. 说：“小范，请向前走一步。”页面应去掉单次唤醒词并显示 `CORE 指令`。
3. 说：“小范小范，请向前走一步。”页面应优先去掉完整双次唤醒词，不把第二个
   “小范”留在任务里。
4. 只说：“小范”或“小范小范”。页面应显示“仅唤醒”，不会发送空任务。
5. 单独说“小范”，停顿后在 10 秒内再说“请向前走”。第二句应显示
   `CORE 延迟指令`；窗口会在接收这一条任务后关闭。

页面同时显示文字回复：单独唤醒时回复“在呢，需要我做什么吗？”，收到任务后显示
“好的，已收到任务：……”。回复来自离线本地对话策略，不调用 TTS，也不把转写文本
发送到云端。它不是伪装成语言模型的模板；未来接入真正的 agent-core/LLM 时，可以用
模型输出替换事件中的 `assistant_reply`，而不改变麦克风、VAD、ASR 和唤醒状态机。

一句话在 VAD 判断句尾后才进入 ASR，所以这是连续采集、句尾出结果的实时系统，
不是边说边显示字的流式 partial transcript。默认句尾静音阈值为 400 ms。
单独的唤醒词会开启 10 秒等待任务窗口，可用 `--wake-hold-ms` 调整；窗口只接收下一条
非空任务，随后自动关闭，以免后续背景对话持续进入 core。

## Demo 与生产环境的边界

- Mac Demo 通过本地 HTTP 接收浏览器音频，不经过 ROS2。
- `core`/`background` 是可视化路由结果，不会真的控制机器人。
- `assistant_reply` 是文字层的本地确认，不代表机器人已经执行物理动作。
- Demo 先做规范化精确匹配，再用拼音模糊匹配处理“小范/小贩/小凡”等同音字；
  生产插件使用 IPA 模糊匹配。
- 配置“小范小范”时自动接受“小范”，配置“小范”时也自动接受“小范小范”；匹配
  总是从最长形式开始，防止重复唤醒词进入任务文本。
- ASR 和 VAD 都使用 CPU；页面显示分块数、分句数、模型解码耗时和估算句尾延迟。
