# 中文机器人 ASR Demo

这个分支保留 PhanthyMotus 的 ROS2/MCP 接口，并增加一个严格小于榜单资源上限的
中文模型候选与可复现实验工具。面试交付不包含网站；演示方式是音频回放、终端结果、
评测报告和录屏。

## 模型策略

- 上游基线：`sensevoice-small`。它继续作为默认值，便于做同条件对照，但不作为满足
  100M 限制的最终方案。
- Demo 候选：`zipformer-zh-14m`，仅中文、CPU INT8。模型约 14M 参数；实际加载的
  encoder INT8、decoder FP32 和 joiner INT8 合计约 30MB。
- ASR 不占用 CUDA，目标是在 Jetson 上把 ASR GPU 使用控制在接近空闲基线。是否低于
  10% 必须用真机 `tegrastats` 实测，不能由 CPU 配置直接推断。

模型来自 sherpa-onnx 的
[官方预训练模型页面](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html)。
模型文件不得提交进 Git。正式内网环境可用 `ASR_ZIPFORMER_ZH_14M_URL` 把下载地址切换
到指定的内部文件服务。

## 数据清单

使用 JSON Lines；音频路径相对于清单文件。音频必须是 16kHz、单声道、PCM16 WAV。

```json
{"id":"cmd-001","audio":"audio/cmd-001.wav","text":"小范小范向前走一步"}
{"id":"silence-001","audio":"audio/silence-001.wav","text":""}
```

录音、模型、预测文件和视频都不要放进仓库。可以把它们放到仓库外的工作目录。

## 本地录音采集网页

项目提供一个只在本机运行的录音网页，内置 30 条机器人指令、普通对话、停顿、改口、
静音和环境声文本。网页把录音直接编码为 16 kHz 单声道 PCM16 WAV，Python 服务保存
音频并同步维护评测清单；不上传云端，也不会把音频写入 Git 仓库。

```bash
python3 perception/tools/asr_recorder/server.py
```

打开 `http://127.0.0.1:8765`，允许麦克风权限，按页面顺序录制。默认输出位置是：

```text
~/Documents/robot-asr-dataset/
```

前 10 条是调试集，后 20 条是固定测试集。详细说明见
`perception/tools/asr_recorder/README.md`。

## 本机可做的检查

```bash
python perception/tools/asr_eval.py validate \
  --manifest /path/to/dataset/manifest.jsonl

python perception/tools/asr_eval.py check-submission
```

第二条命令会检查已跟踪文件和未被 `.gitignore` 排除的文件，严格拒绝大于
1,000,000 字节的内容。

## Jetson 模型直测

`zipformer-zh-14m` 的单音频演示也可以在 macOS/Linux 开发机运行；完整插件和其他模型
仍需 perception/Jetson 运行环境。先创建隔离环境，不要修改系统 Python：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install sherpa-onnx==1.13.6
```

然后完成最短演示。模型默认下载到仓库外的
`~/.cache/robot-asr/models/zipformer-zh-14m`，不会被提交：


```bash
python perception/tools/asr_eval.py demo \
  --audio /path/to/demo.wav \
  --model zipformer-zh-14m \
  --repeat 3 \
  --output /path/to/results/demo.json
```

输出包含转写文本、模型加载耗时、三次解码耗时中位数和 RTF。首次运行会下载模型；
正式榜单内网环境通过 `ASR_ZIPFORMER_ZH_14M_URL` 把下载地址切换到内部文件服务。

如果 macOS 的新虚拟环境报 `CERTIFICATE_VERIFY_FAILED`，显式使用系统 CA 文件，
不要关闭证书校验：

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem python perception/tools/asr_eval.py demo \
  --audio /path/to/demo.wav \
  --model zipformer-zh-14m \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m"
```

2026-11-07 在 macOS arm64 上的合成语音冒烟测试：3.312 秒音频识别为
“小范小范机器人向前走一步”，三次输出一致，中位解码 95.3 ms，RTF 0.0288。
这只证明安装和模型调用正常，不属于真实录音测试集或 Jetson 性能结论。

这条命令走真实模型，但不经过 VAD/ROS，所以用于演示模型转写、模型推理 RTF，不能
冒充整链路的端点延迟。完整链路必须在 Jetson 上另外回放并记录。

再用固定测试集做批量对照：

```bash
python perception/tools/asr_eval.py transcribe \
  --manifest /path/to/dataset/manifest.jsonl \
  --model zipformer-zh-14m \
  --num-threads 2 \
  --output /path/to/results/zipformer.jsonl

python perception/tools/asr_eval.py score \
  --manifest /path/to/dataset/manifest.jsonl \
  --predictions /path/to/results/zipformer.jsonl \
  --output /path/to/results/zipformer-report.json
```

基线只需把 `--model` 改为 `sensevoice-small`，其余硬件、音频、线程数和计时口径保持
不变。VAD 端点延迟、尾段丢失、队列丢包和 GPU 使用需要在下一阶段通过完整 ROS2
回放链路单独记录。
