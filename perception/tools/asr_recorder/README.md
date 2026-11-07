# Robot ASR 本地录音采集器

本工具通过浏览器采集 30 条中文机器人语音，所有数据只写入本机，不上传云端。
网页在浏览器中把麦克风数据转换为 16 kHz、单声道、PCM16 WAV；Python 服务在落盘前
再次校验格式，并同步生成 `manifest.jsonl`。

## 启动

从仓库根目录执行：

```bash
python3 perception/tools/asr_recorder/server.py
```

然后打开终端输出的 `http://127.0.0.1:8765`，第一次录音时允许浏览器使用麦克风。
Chrome 或 Safari 均可；`localhost` 属于浏览器允许麦克风的安全上下文。

默认数据目录为：

```text
~/Documents/robot-asr-dataset/
├── audio/
│   ├── dev-001.wav
│   └── ...
├── manifest.jsonl
└── records.json
```

数据目录位于 Git 仓库外。可通过参数覆盖：

```bash
python3 perception/tools/asr_recorder/server.py \
  --output-dir /absolute/path/to/robot-asr-dataset \
  --port 8765
```

## 数据划分

- `dev-001` 到 `dev-010`：调试集，可用于定位问题和调整参数。
- `test-001` 到 `test-020`：最终测试集，确定后不要根据识别结果反复重录或调参。

完成后验证音频和清单：

```bash
python perception/tools/asr_eval.py validate \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl"
```
