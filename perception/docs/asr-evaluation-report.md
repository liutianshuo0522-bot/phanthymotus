# 中文机器人 ASR Demo 技术报告

> 报告状态：Mac 基线、本地实时 Demo 与演示视频已完成；本次面试不要求 Jetson 实机<br>
> 数据集与批量实验日期：2026-11-07<br>
> 实时服务冒烟测试：2026-11-10<br>
> Demo 录制日期：2026-11-11（视频单独提交，不纳入 Git）<br>
> 当前分支：`codex/asr-demo`

## 1. 摘要

本项目面向具身机器人的中文语音识别场景，目标是在保留 PhanthyMotus
音频输入与文本输出接口的前提下，完成可复现的中文 ASR 演示、数据采集和
评测链路。

当前版本新增了紧凑型 `zipformer-zh-14m` 中文模型适配、本地录音采集页面、
本地麦克风实时识别页面、音频格式校验、批量转写，以及 1024-byte 实时分块的
VAD/ASR 全链路评测。在
30 条自录中文机器人指令上，Mac CPU 环境的模型直接解码基线为：整体 CER
`16.41%`，RTF `0.0303`；实时分块全链路为：整体 CER `16.11%`、主动处理 RTF
`0.0618`、端点延迟 P50/P95 `557/667 ms`。

实时页面进一步把 ASR 放入机器人交互上下文：未唤醒语音归入 `background`；单独说出
“小范”或“小范小范”只开启一次性任务窗口；同句任务或窗口内下一句任务才成为
`core` 候选。页面用本地确定性文字策略显示唤醒应答和任务接收确认，以便演示交互
闭环，但不调用 LLM/TTS，也不宣称实体机器人已经执行动作。

这些数字是开发阶段基线。全链路数字包含 WAV 分块、VAD、尾段 flush 和 ASR，
但仍不包含 ROS2 传输以及 Jetson 资源占用，不应当作榜单或 Jetson 正式结论。
未来正式榜单所需的真实 ROS2 回放、订阅/队列计数和 `tegrastats` 三态对比工具已经
实现；面试官已确认本次 Demo 可在 Mac 完成，不要求连接 Jetson。本报告不会在没有
真机运行记录时填入推测数字。

## 2. 场景与资源约束

榜单场景将 ASR 作为机器人 perception 能力：接收用户语音，输出文本给 core 层。
当前报告按下列约束设计：

| 项目 | 约束或目标 |
| --- | --- |
| 运行硬件 | NVIDIA Jetson Orin 8G（尚待真机验证） |
| GPU 占用 | 低于 10%，必须用 `tegrastats` 实测 |
| 模型上限 | 100 MB |
| Git 文件限制 | 不上传任何大于 1 MB 的文件 |
| 模型交付 | 模型不进 Git，运行时从指定文件服务下载 |
| 音频输入 | 16 kHz、单声道、有符号 PCM16 小端 |
| ROS2 消息 | `audio_msgs/AudioChunk` |
| `format` | `audio/pcm-16k` |
| 最小分块 | 1024 bytes（512 个采样点） |

ASR 模块只负责产生文本和必要的唤醒状态，不直接控制机器人前进、转向或抓取。

## 3. 系统边界与预期链路

```text
麦克风 / WAV 回放
        ↓
格式检查与 1024-byte 分块
        ↓
VAD 语音检测与分句
        ↓
中文 ASR 解码
        ↓
唤醒词匹配与一次性任务窗口
        ├── background：不发送任务，不回复
        ├── wake_only：本地文字应答，等待下一句
        └── core 候选：任务文本 + 本地接收确认
                              ↓
                  生产环境 ROS2 文本结果 → core 层
```

Mac 阶段已验证“WAV/浏览器麦克风 → 1024-byte 实时分块 → VAD 分句/尾段 flush →
ASR → 唤醒路由/文本与评测结果”。本地实时页面会把有唤醒词的任务标为 `core`，把
未唤醒对话标为 `background`。未来榜单工具会把完全相同的数据改为真实
`audio_msgs/AudioChunk` 发布，订阅生产 ASR 的两个文本 topic，并从 ASR `info`
读取接收、入队、队列丢弃和拒绝计数；该阶段仍需在 Jetson 上实际执行。

## 4. 已完成实现

| 能力 | 实现情况 |
| --- | --- |
| 紧凑中文模型 | 新增 `zipformer-zh-14m` CPU INT8 适配，保留上游 `sensevoice-small` 默认值 |
| 模型下载 | 模型保存在仓库外，支持用 `ASR_ZIPFORMER_ZH_14M_URL` 切换内网地址 |
| 本地录音 | 浏览器采集麦克风，前端重采样并编码为 16 kHz 单声道 PCM16 WAV |
| 实时麦克风 Demo | 连续收音、1024-byte 分块、状态化 VAD、ASR 和 core/background 路由可视化 |
| 唤醒状态机 | 单次/双次“小范”最长优先匹配，支持 10 秒一次性任务窗口和超时回落 |
| 文字确认层 | `wake_only` 显示唤醒应答，任务路由显示接收确认；不调用 LLM 或 TTS |
| 数据清单 | 保存 WAV 时同步维护 `manifest.jsonl` 和 `records.json` |
| 数据校验 | 检查采样率、声道、采样宽度、文件是否存在和清单是否可读 |
| 批量识别 | 对固定 manifest 批量输出逐条 JSONL 预测 |
| 可复现评测 | 计算 CER、替换/删除/插入数、RTF、缺失预测、重复预测和静音误输出 |
| 分块回放 | 严格发送 1024-byte 分块；支持快速回归和 32 ms wall-clock 实时节拍 |
| VAD/ASR 全链路 | 共用生产 VAD 构造，记录分句、尾段、端点延迟、队列/节拍和全链路 RTF |
| ROS2 验收回放 | 真实发布 `AudioChunk`、订阅生产结果 topic，记录发布/订阅差额、队列丢块与 DDS 交付延迟 |
| Jetson 资源采集 | 解析 `tegrastats`，区分系统空闲、模型已加载空闲和 ROS 回放三态 |
| 提交检查 | 提供仓库大文件检查，模型、录音和结果不进 Git |

相关代码：

- `perception/plugins/asr.py`
- `perception/tools/asr_eval.py`
- `perception/tools/asr_ros2_eval.py`
- `perception/tools/jetson_resources.py`
- `perception/tools/asr_recorder/`
- `perception/tools/asr_live/`
- `perception/tests/test_asr_eval.py`
- `perception/tests/test_asr_live.py`
- `perception/tests/test_asr_recorder.py`
- `perception/tests/test_asr_device_registry.py`

## 5. 数据集

### 5.1 采集方法

数据由一名中文说话人在同一台 Mac 上录制。采集页面在录制前已固定 30 条文本，
保存后生成与评测工具兼容的清单。数据包含：

- 移动、转向、速度、停止等基础机器人指令。
- 导航、跟随、充电、避障、拿取和放置指令。
- 普通对话、状态询问和功能介绍。
- 停顿、改口和重复词语。
- 静音和背景环境声。

### 5.2 数据划分

| 划分 | 样本数 | 用途 | 是否可用于选参 |
| --- | ---: | --- | --- |
| `dev` | 10 | 问题定位、参数选择和回归检查 | 是 |
| `test` | 20 | 固定参数后的最终对照 | 否 |
| 总计 | 30 | — | — |

当前数据总时长 `157.696` 秒，单条时长在 `3.84`–`6.66` 秒之间。

### 5.3 质量检查

- 30/30 条音频均存在，无缺失、无多余 ID。
- 全部为 16 kHz、单声道、2-byte 采样宽度 WAV。
- 未发现削波文件。
- 多数语音录音的 RMS 约为 `-40`至`-44 dBFS`，音量偏低但可解码。
- 静音样本 `test-019` 的 RMS 约为 `-71.8 dBFS`。

测试集已在首次批量识别前确定。后续不应根据模型输出反复修改测试集文本或重录测试样本。

## 6. 模型方案

### 6.1 候选模型

`zipformer-zh-14m` 是 sherpa-onnx 中文 streaming transducer。当前 CPU 推理组合为：

- INT8 encoder；
- FP32 decoder；
- INT8 joiner。

实际加载的 ONNX 权重合计约 `30 MB`。开发机模型缓存目录约 `78 MB`，因为其中
同时保留了 INT8 和 FP32 的部分备选文件。两种口径均低于 100 MB，但最终包体积
仍应在 Jetson 部署时再次核对。

### 6.2 参考基线

上游默认模型 `sensevoice-small` 保留在插件中，便于同条件对照。当前 Mac 环境
尚未下载或运行该模型，因此本报告不提供 SenseVoice 对照数字，也不猜测其在
Jetson 上的资源占用。

## 7. 评测方法

### 7.1 文字准确率

评测对中文字符做确定性规范化，再使用 Levenshtein 距离计算：

```text
CER = (替换数 + 删除数 + 插入数) / 参考文本字符数
```

原始参考文本、原始识别文本和规范化后文本全部保留在评测结果中。

### 7.2 性能

```text
RTF = 模型解码耗时 / 音频时长
```

模型首次加载和预热不计入批量热运行 RTF。本轮使用 Mac CPU、4 个推理线程；
整段 WAV 直接送入模型，未使用实时 sleep 模拟，未经 VAD 和 ROS2。

### 7.3 稳定性记录

评测同时记录：

- 缺失预测数；
- 多余预测数；
- 重复 ID；
- 静音误输出次数；
- 可选的句尾响应延迟 P50/P95。

模型直接解码基线不经过实时 VAD，因此句尾响应延迟为空，不用解码耗时伪装端点
延迟。新增的全链路回放按 VAD 报告的语音结束采样点计时：端点延迟包含 VAD 等待、
调度落后和 ASR 解码；`full_chain_rtf` 只累计分块转换、VAD 和 ASR 的主动处理时间，
不包含刻意等待 32 ms 节拍的 sleep。`replay_wall_rtf` 另行保留完整实际耗时。

ROS2 阶段使用发送块的可信 `header.stamp` 与生产结果中的 `audio_end_ts` 计算端点
延迟，因此额外包含输入 DDS、生产多进程队列、输出 DDS 和评测订阅回调。生产结果的
`asr_complete_ts` 到评测订阅时间记为 `ros_delivery_ms`。发布端成功调用次数并不等于
订阅端收到次数，所以 ASR 节点另行累计 `received`、`enqueued`、`queue_dropped` 和
`rejected`；只有隔离输入 topic 下发布数、接收数和入队数对得上，才能报告零丢块。

## 8. Mac 实验结果

### 8.1 分组结果

| 数据 | 样本 | 参考字符 | 编辑错误 | CER | 音频时长 | 解码耗时 | RTF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 开发集 | 10 | 105 | 19 | 18.10% | 47.104 s | 1.424 s | 0.0302 |
| 测试集 | 20 | 224 | 35 | 15.63% | 110.592 s | 3.354 s | 0.0303 |
| 整体 | 30 | 329 | 54 | 16.41% | 157.696 s | 4.778 s | 0.0303 |

整体 54 个编辑错误包含：

| 错误类型 | 数量 |
| --- | ---: |
| 替换 | 45 |
| 删除 | 5 |
| 插入 | 4 |

其他完整性指标：

| 指标 | 结果 |
| --- | ---: |
| 缺失预测 | 0 |
| 多余预测 | 0 |
| 重复预测 | 0 |
| 静音误输出 | 1 |
| 句尾延迟 P50/P95 | 未测量 |

### 8.2 典型现象

错误主要集中在句首固定唤醒词“小范小范”，常见同音形式包括“小贩”、
“小方”、“小分”和“小翻”。示例：

```text
参考：小范小范请向前走一步
识别：小范小范抢相前走一步
```

```text
参考：小范小范请回到充电桩
识别：小范小范请回到充电桩
```

静音样本 `test-019` 误输出“我”；环境声样本 `test-020` 正确输出空文本。
在完整机器人链路中，这类静音应优先由 VAD 阻止，不应依赖删除特定识别词。

### 8.3 1024-byte 实时 VAD/ASR 全链路

同一批音频使用生产配置 `vad_threshold=0.5`、`vad_silence_ms=400`、
`vad_pre_roll_ms=500`。每个输入块固定为 1024 bytes/512 samples/32 ms；`--realtime`
按 wall clock 发送。WAV 到达 EOF 时，只有仍在说话的文件才继续补静音块，最大
flush 窗口 2 秒，VAD 闭合后立即停止。

| 数据 | 样本 | 预期语音 | VAD 分句 | 原始 CER | 解码 RTF | 全链路 RTF | 端点 P50/P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 开发集 | 10 | 10 | 10 | 18.10% | 0.0280 | 0.0622 | 532/593 ms |
| 测试集 | 20 | 18 | 19 | 15.18% | 0.0302 | 0.0616 | 565/895 ms |
| 整体 | 30 | 28 | 29 | 16.11% | 0.0295 | 0.0618 | 557/667 ms |

整体 53 个原始 ASR 编辑错误包含 42 个替换、9 个删除和 2 个插入。全链路稳定性
结果如下：

| 指标 | 结果 |
| --- | ---: |
| 语音文件漏句 | 0/28 |
| 额外 VAD 分句 | 1（空文本） |
| 静音/环境声误输出 | 0/2 |
| 需要尾段 flush 的文件 | 26 |
| 尾段丢失 | 0 |
| 丢弃的短 VAD 分句 | 1 |
| 输入/flush 分块 | 4939/622 |
| 离线回放丢块 | 0 |

测试集 P95 被 `test-013` 的一次 `502 ms` ASR 解码尖峰拉到 `895 ms`；其余最高
端点延迟为 `667 ms`。`replay_wall_rtf=1.1574` 包含真实节拍和每个独立 WAV
结束后的必要尾静音，不是模型吞吐指标，所以与主动处理的 `full_chain_rtf=0.0618`
分开报告。

另按严格字符前缀统计，“小范小范”命中 10/28（35.71%）；去除参考/预测中对应的
四字符唤醒前缀位置后，命令 CER 为 10.60%（开发集 16.92%，测试集 7.89%）。这个
严格字符指标用于暴露同音字问题，不等同于生产 `asr_kws` 的 IPA 模糊匹配命中率，
也没有覆盖原始 CER。

### 8.4 ROS2/Jetson 验收状态

本轮开发机没有 Jetson、`rclpy` 和板端 perception 运行环境，因此没有伪造 ROS2 或
资源数字。当前状态为：

| 验收项 | 工具状态 | 实测状态 |
| --- | --- | --- |
| 1024-byte `AudioChunk` 实时发布 | 已实现 | 待 Jetson |
| 生产 `/asr`、`/asr_background` 订阅 | 已实现 | 待 Jetson |
| 发布→订阅→VAD 队列丢块核对 | 已实现 | 待 Jetson |
| ROS2 端点延迟与输出 DDS 延迟 | 已实现 | 待 Jetson |
| 系统空闲/模型加载/回放三态资源 | 已实现 | 待 Jetson |
| GPU P95 与最大值是否低于 10% | 自动判定 | 待 Jetson |

### 8.5 本地麦克风实时 Demo

实时页面持续采集浏览器麦克风，必要时重采样为 16 kHz 单声道 PCM16，再按
1024 bytes/32 ms 切块。后端保持一个状态化 VAD 会话，并在句尾调用同一个
`zipformer-zh-14m` 适配器。页面展示原始 ASR、唤醒匹配方式、去除唤醒词后的任务、
core/background 路由、分块数、分句数、解码耗时和估算句尾延迟。

2026-11-10 的服务级冒烟测试把自录 `dev-001` 按真实时间节奏送入该接口，VAD 产生
1 个分句，ASR 输出“小范小范抢相前走一步”，精确命中唤醒词并路由到 core，模型
解码约 `189 ms`。这项记录验证实时服务接口、VAD、ASR 和路由能够连通。2026-11-11
已完成浏览器麦克风现场演示视频录制；视频作为独立交付物发送，不放入 Git 仓库，也不
替代上面的固定数据集指标。

Demo 使用本地 HTTP，不冒充 ROS2；`core` 是路由结果展示，不会直接控制机器人。
拼音模糊匹配用于容忍“小范/小贩/小凡”等同音识别，生产插件则使用 IPA 匹配。
本地路由同时接受单次“小范”和双次“小范小范”，按最长形式优先剥离唤醒词。
仅说唤醒词时会保留 10 秒的一次性任务窗口；窗口内下一条非空语句进入 core，消费后
自动关闭，超时语句回到 background。
Demo 另有不依赖 TTS 的本地文字确认层，通过 `assistant_reply` 展示唤醒应答和任务接收
结果。该层明确记录为 `local_dialogue_policy`，不冒充真实 LLM 或机器人动作执行。

| 用户语音与状态 | 页面路由 | 页面文字回复 | 含义 |
| --- | --- | --- | --- |
| 未包含唤醒词 | `background` | 无 | 背景语音不进入 core |
| 只说“小范”或“小范小范” | `wake_only` | 在呢，需要我做什么吗？ | 只确认唤醒，开启 10 秒任务窗口 |
| 唤醒词与任务在同一句 | `core` | 好的，已收到任务：…… | 形成 core 候选任务 |
| 唤醒后 10 秒内说下一句任务 | `core`（reason=`deferred_command`） | 好的，已收到任务：…… | 一次性消费任务窗口，之后自动关闭 |
| 唤醒超时后再说任务 | `background` | 无 | 防止旧唤醒状态持续接收背景指令 |

“已收到任务”只确认 perception 层完成了识别和路由判断；Mac Demo 并没有调用真实
机器人 core，因此不能解释为动作执行成功。当前策略会移除任务开头的“请”，再生成
确定性的确认文字。未来接入 agent-core/LLM 时，可以替换 `assistant_reply` 的来源，
而不改动麦克风、1024-byte 分块、VAD、ASR 和唤醒状态机。

## 9. 复现步骤

录音、模型和预测文件默认保存在仓库外。以本地默认数据目录为例：

```bash
# 1. 校验数据与 WAV 合同
python perception/tools/asr_eval.py validate \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl"

# 2. 批量转写（当前 Mac 基线用 4 线程）
.venv/bin/python perception/tools/asr_eval.py transcribe \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl" \
  --output "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-predictions.jsonl" \
  --model zipformer-zh-14m \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m" \
  --num-threads 4

# 3. 生成 CER/RTF 报告
.venv/bin/python perception/tools/asr_eval.py score \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl" \
  --predictions "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-predictions.jsonl" \
  --output "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-score.json"

# 4. 1024-byte 实时 VAD/ASR 全链路评测
SSL_CERT_FILE=/etc/ssl/cert.pem .venv/bin/python \
  perception/tools/asr_eval.py replay \
  --manifest "$HOME/Documents/robot-asr-dataset/manifest.jsonl" \
  --output "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-e2e-realtime-predictions.jsonl" \
  --report "$HOME/Documents/robot-asr-dataset/results/zipformer-zh-14m-e2e-realtime-report.json" \
  --model zipformer-zh-14m \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m" \
  --num-threads 4 \
  --realtime

# 5. 启动本地麦克风实时 Demo，再用 Chrome 打开 http://127.0.0.1:8766
python -m pip install -r perception/tools/asr_live/requirements.txt
SSL_CERT_FILE=/etc/ssl/cert.pem .venv/bin/python \
  perception/tools/asr_live/server.py \
  --model-dir "$HOME/Library/Caches/robot-asr/models/zipformer-zh-14m"

# 6. 检查 Git 提交是否存在超大文件
python perception/tools/asr_eval.py check-submission
```

### 9.1 Jetson 真实 ROS2 回放

以下命令必须在 perception/ROS2 环境中运行，数据目录也必须挂载到容器内。先把生产
插件固定为 CPU 小模型和纯 VAD 模式；纯 VAD 模式会发布完整转写，才能与原始 CER
直接比较。配置调用可能异步加载模型，应轮询 `info` 直到不再是 `loading`。

```bash
curl -s http://127.0.0.1:15720 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asr","arguments":{"action":"config","asr_model":"zipformer-zh-14m","device":"cpu","trigger_mode":"vad","publish_background":false,"save_vad_segments":false}}}'

curl -s http://127.0.0.1:15720 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"asr","arguments":{"action":"info","instance_id":"asr-eval","input_topic":"/asr_eval/audio"}}}'
```

模型就绪后运行回放。`--mcp-url` 让工具在需要时启动 ASR 实例，并在回放前后读取
生产传输计数；`--stop-after` 只停止由本工具启动的实例。`/asr_eval/audio` 必须是
隔离 topic，不能同时有麦克风或第二个回放器发布，否则发布/接收差额没有意义。

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

验收时至少检查报告中的 `publisher_errors=0`、
`publisher_to_subscriber_gap_chunks=0`、`queue_dropped_chunks=0`、
`rejected_chunks=0` 和 `transport_ok=true`。还应对照 Mac 基线检查 CER、漏句、静音
误输出、端点 P50/P95 和 `ros_delivery_ms_p50/p95`，不能只看“有文本输出”。

### 9.2 Jetson `tegrastats` 三态资源采集

资源日志从 Jetson 主机采集；三个阶段必须保持功耗模式、时钟策略和其余机器人卡片
一致。每个阶段至少 60 秒，回放阶段覆盖整个数据集。正式测模型常驻内存时，A 使用
相同服务配置但禁用 ASR 插件，B 用 `zipformer-zh-14m`/CPU 配置干净重启 perception
并等待预热完成；不要先加载默认 SenseVoice 再热切换后就把差值称为 Zipformer 的
常驻成本。若无法构造这个对照，把已加载空闲日志作为 `--idle-log` 并省略
`--loaded-idle-log`，只报告回放增量，不得报告模型常驻增量。

```bash
# A. 相同服务和功耗模式，仅禁用 ASR 插件
python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-system-idle.log \
  --duration-s 60

# B. 干净启动 zipformer，预热完成且没有音频输入
python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-asr-loaded-idle.log \
  --duration-s 60

# C. 另一个终端运行 ROS2 回放；本命令保持运行，回放结束后 Ctrl-C
python perception/tools/jetson_resources.py record \
  --output /data/robot-asr-dataset/results/jetson-ros2-replay.log

python perception/tools/jetson_resources.py compare \
  --idle-log /data/robot-asr-dataset/results/jetson-system-idle.log \
  --loaded-idle-log /data/robot-asr-dataset/results/jetson-asr-loaded-idle.log \
  --replay-log /data/robot-asr-dataset/results/jetson-ros2-replay.log \
  --gpu-limit 10 \
  --output /data/robot-asr-dataset/results/jetson-resources-report.json
```

GPU 约束同时报告 P95 和最大值；任一项达到或超过 10% 时命令都以非零状态退出，
两项分开保留以区分持续超限与短尖峰。RAM 分成“模型常驻相对系统空闲”和“回放相对
模型已加载空闲”两个增量，避免把模型加载成本误算成每次识别的临时开销。

为保证对照公平，改进版必须固定硬件、模型文件、线程数、输入音频和计时口径。

## 10. 已知限制

1. 当前只有一名说话人、30 条音频，不能代表多人、方言和真实机器人噪声环境。
2. RTF 来自 Mac CPU 热运行，不能用于推断 Jetson 性能。
3. ROS2 与生产队列的验收工具已实现，但尚未在 Jetson 上生成实测报告；当前表格仍只
   能引用 Mac 单进程基线。
4. 当前只有 157.7 秒的逐文件回放；长时间队列积压、重启恢复和持续运行尚未量化。
5. Jetson 的 GPU、CPU、内存占用和冷启动耗时尚未实测；资源工具已覆盖稳态三态，
   冷启动耗时仍需结合 perception 日志单独记录。
6. 唤醒命中率是严格字符前缀诊断，不是生产 IPA 模糊匹配的端到端命中率。
7. 本地实时 Demo 在 VAD 句尾后输出完整转写，目前不提供边说边显示的 partial 文本；
   本地 `core` 路由也不经过 ROS2 或真实机器人 core。
8. 页面回复来自本地确定性策略，不具备开放域对话、任务规划或执行结果查询能力；当前
   版本也没有 TTS 音频输出。

## 11. 交付与下一阶段

1. 面试交付包含公开代码仓库、固定 commit ID、技术报告和独立 Demo 视频；模型、录音、
   预测结果及视频均不提交到 Git。
2. 提交前运行测试和大文件检查，并确认邮件中的仓库地址、分支与 commit ID 指向同一
   份代码。
3. 后续调优只使用开发集选择 VAD 和唤醒词参数；参数固定后再重跑测试集。
4. 扩大说话人、方言、距离和机器人本体噪声数据，并单独评测唤醒命中率与任务 CER。
5. 如果未来参加正式榜单，再在 Jetson 上运行现有 ROS2 回放与 `tegrastats` 三态采集。
6. 如果需要完整人机对话，再接入 agent-core/LLM 与 TTS；该扩展不应改变 ASR 原始
   CER、端点延迟和资源占用的独立评测口径。

## 12. 代码与实验记录

| Commit | 内容 |
| --- | --- |
| `d225625` | 增加紧凑中文 ASR Demo 和评测工具 |
| `74454bc` | 增加本地录音采集网页与数据保存 |
| `49f5cc2` | 增加 Mac 基线评测报告 |
| `5aa2ad5` | 增加 1024-byte VAD/ASR 全链路与 ROS2/Jetson 验收工具 |

本报告只记录已实际运行的数据。尚未运行的 SenseVoice 对照、Jetson 性能和
ROS2 端到端结果均明确标记为待验证。本地实时页面及本次文档修改应在交付前形成新的
固定 commit，并以该 commit 作为邮件中的最终版本。
