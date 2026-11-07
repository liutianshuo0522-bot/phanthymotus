# 中文机器人 ASR Demo

这个分支保留 PhanthyMotus 的 ROS2/MCP 接口，并增加一个严格小于榜单资源上限的
中文模型候选与可复现实验工具。面试交付不包含网站；演示方式是音频回放、终端结果、
评测报告和录屏。

2026-11-07 的自录数据、Mac 开发基线、实验边界和后续计划见
[`asr-evaluation-report.md`](asr-evaluation-report.md)。

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
冒充整链路的端点延迟。VAD/ASR 链路使用下文的 `replay` 测量；ROS2 topic、生产队列
和资源占用仍必须在 Jetson 上另外回放并记录。

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
不变。

## 1024-byte VAD/ASR 全链路回放

`replay` 命令按真实的 32 ms 节拍发送 1024-byte PCM16 分块，使用与生产 ASR 插件
共用的 Silero/sherpa VAD 配置，再把每个 VAD 分句送入真实 ASR。文件末尾如果仍有
待完成语音，就继续发送静音分块，最多 2 秒；VAD 一旦闭合便立即停止，不用固定的
尾静音人为抬高 wall-clock 指标。

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem .venv/bin/python \
  perception/tools/asr_eval.py replay \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl" \
  --output "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-e2e-realtime-predictions.jsonl" \
  --report "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-e2e-realtime-report.json" \
  --model zipformer-zh-14m \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m" \
  --num-threads 4 \
  --realtime
```

不加 `--realtime` 时，工具仍发送完全相同的分块并走相同 VAD/ASR 逻辑，但不等待
32 ms 节拍，适合快速回归。两种模式都会记录：

- 原始 CER、严格字符前缀唤醒命中率和去除唤醒前缀位置后的命令 CER；
- VAD 分句数、漏句、重复/额外分句、短分句丢弃和静音误输出；
- 尾段是否需要 flush、是否成功输出，以及分块丢弃数；
- VAD 等待、ASR 解码、句尾响应延迟 P50/P95、解码 RTF 和全链路主动处理 RTF；
- 实时回放 wall RTF、迟到分块和最大调度落后。

2026-11-07 的 Mac CPU 实时回放结果：30 条音频原始 CER `16.11%`，28 条语音
漏句 `0`、尾段丢失 `0`，2 条静音/环境声误输出 `0`；端点延迟 P50/P95 为
`557/667 ms`，全链路主动处理 RTF `0.0618`。检测到 29 个 VAD 分句，其中一个是
额外的空文本段。结果仍不包含 ROS2 topic/队列和 Jetson 资源占用；这些必须在真机
另行验证。

## Jetson ROS2 与资源验收

下一阶段工具已经就绪，但当前仓库中的报告尚无 Jetson 实测数字。模型常驻内存的
正式对照需要保持其他服务不变、仅禁用 ASR 采集一次系统空闲，再用
`zipformer-zh-14m`/CPU 配置干净重启 perception 并等待预热。ROS 验收还要设置
`trigger_mode=vad`，随后在 perception/ROS2 环境内使用隔离输入 topic 回放：

```bash
python perception/tools/asr_ros2_eval.py \
  --manifest /data/robot-asr-dataset/manifest.jsonl \
  --input-topic /asr_eval/audio \
  --output /data/robot-asr-dataset/results/jetson-ros2-predictions.jsonl \
  --report /data/robot-asr-dataset/results/jetson-ros2-report.json \
  --mcp-url http://127.0.0.1:15720 \
  --instance-id asr-eval \
  --stop-after
```

这条命令真实发布 1024-byte `audio_msgs/AudioChunk`，订阅
`/asr_eval/audio/asr` 和 `/asr_eval/audio/asr_background`，并用 MCP `info` 的前后
快照核对发布数、订阅回调数、VAD 队列入队数、队列丢弃数和拒绝数。正式验收要求
`transport_ok=true`，而不是仅凭 `publish()` 没报异常就写“零丢包”。

Jetson 主机同时用 `tegrastats` 采集三个阶段：系统空闲、模型已加载空闲、ROS 回放。
回放阶段的记录命令保持运行，回放结束后按 Ctrl-C：

```bash
python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-system-idle.log \
  --duration-s 60

python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-asr-loaded-idle.log \
  --duration-s 60

python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-ros2-replay.log

python perception/tools/jetson_resources.py compare \
  --idle-log /data/robot-asr-dataset/results/jetson-system-idle.log \
  --loaded-idle-log /data/robot-asr-dataset/results/jetson-asr-loaded-idle.log \
  --replay-log /data/robot-asr-dataset/results/jetson-ros2-replay.log \
  --gpu-limit 10 \
  --output /data/robot-asr-dataset/results/jetson-resources-report.json
```

完整 MCP 配置命令、指标解释和验收清单见
[`asr-evaluation-report.md`](asr-evaluation-report.md) 第 9 节。
