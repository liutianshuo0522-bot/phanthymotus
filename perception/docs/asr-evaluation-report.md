# 中文机器人 ASR Demo 技术报告

> 报告状态：开发基线（Mac 环境）  
> 数据与实验日期：2026-11-07  
> 当前分支：`codex/asr-demo`

## 1. 摘要

本项目面向具身机器人的中文语音识别场景，目标是在保留 PhanthyMotus
音频输入与文本输出接口的前提下，完成可复现的中文 ASR 演示、数据采集和
评测链路。

当前版本新增了紧凑型 `zipformer-zh-14m` 中文模型适配、本地录音采集页面、
音频格式校验、批量转写和 CER/RTF 评测工具。在 30 条自录中文机器人指令上，
Mac CPU 环境的模型直接解码基线为：整体 CER `16.41%`，RTF `0.0303`。

这些数字是开发阶段基线，只包含模型解码，不包含 VAD 端点等待、ROS2 传输和
Jetson 资源占用，不应当作榜单或 Jetson 正式结论。

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
唤醒词判断与结果整理
        ↓
ROS2 文本结果 → core 层
```

本阶段已验证“WAV 格式检查 → 整段模型解码 → 文本与评测结果”。
1024-byte 实时分块、VAD 端点、尾段 flush 和 ROS2 topic 端到端验证是下一阶段工作。

## 4. 已完成实现

| 能力 | 实现情况 |
| --- | --- |
| 紧凑中文模型 | 新增 `zipformer-zh-14m` CPU INT8 适配，保留上游 `sensevoice-small` 默认值 |
| 模型下载 | 模型保存在仓库外，支持用 `ASR_ZIPFORMER_ZH_14M_URL` 切换内网地址 |
| 本地录音 | 浏览器采集麦克风，前端重采样并编码为 16 kHz 单声道 PCM16 WAV |
| 数据清单 | 保存 WAV 时同步维护 `manifest.jsonl` 和 `records.json` |
| 数据校验 | 检查采样率、声道、采样宽度、文件是否存在和清单是否可读 |
| 批量识别 | 对固定 manifest 批量输出逐条 JSONL 预测 |
| 可复现评测 | 计算 CER、替换/删除/插入数、RTF、缺失预测、重复预测和静音误输出 |
| 提交检查 | 提供仓库大文件检查，模型、录音和结果不进 Git |

相关代码：

- `perception/plugins/asr.py`
- `perception/tools/asr_eval.py`
- `perception/tools/asr_recorder/`
- `perception/tests/test_asr_eval.py`
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

本轮不经过实时 VAD，因此句尾响应延迟为空，不用解码耗时伪装端点延迟。

## 8. Mac 模型直接解码基线

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

# 4. 检查 Git 提交是否存在超大文件
python perception/tools/asr_eval.py check-submission
```

为保证对照公平，改进版必须固定硬件、模型文件、线程数、输入音频和计时口径。

## 10. 已知限制

1. 当前只有一名说话人、30 条音频，不能代表多人、方言和真实机器人噪声环境。
2. RTF 来自 Mac CPU 热运行，不能用于推断 Jetson 性能。
3. 当前批量评测为整段 WAV 解码，未经过 VAD、1024-byte 实时分块和 ROS2。
4. 句尾延迟、尾段丢失、重复分句、队列积压和长时间稳定性尚未量化。
5. Jetson 的 GPU、CPU、内存占用和冷启动耗时尚未实测。
6. 唤醒词受限纠错、命令文本 CER 和静音 VAD 过滤尚未实施，本报告未将它们计入基线。

## 11. 下一阶段

1. 实现离线实时回放工具，按1024 bytes 向完整 VAD/ASR 链路送入音频。
2. 记录分句数、漏句、重复、尾段丢失、句尾延迟 P50/P95 和全链路 RTF。
3. 只使用开发集选择 VAD 和唤醒词参数；参数固定后才重跑测试集。
4. 同时报告原始 ASR CER、唤醒命中率和去除唤醒词后的命令 CER，不用后处理覆盖原始结果。
5. 在 Jetson Orin 8G 上验证 ROS2 topic，并用 `tegrastats` 记录资源和持续运行结果。
6. 更新本报告的基线/改进对照表，再录制最终演示视频。

## 12. 代码与实验记录

| Commit | 内容 |
| --- | --- |
| `d225625` | 增加紧凑中文 ASR Demo 和评测工具 |
| `74454bc` | 增加本地录音采集网页与数据保存 |

本报告只记录已实际运行的数据。尚未运行的 SenseVoice 对照、Jetson 性能和
ROS2 端到端结果均明确标记为待验证。
