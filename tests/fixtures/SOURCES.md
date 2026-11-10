# 测试音频素材来源与许可

本目录 `sources/` 内的 5 段短音频均取自公开的 GitHub 仓库，用于**本地调试长音频流式管线**（VAD 分段、增量落盘、断点续跑、时间轴），不是准确率评测集，`tests/make_fixture.py` 会把它们拼接成数分钟的压力测试输入。

| 文件 | 上游来源 | 时长 | 说明与许可 |
|---|---|---|---|
| `jfk_pd.wav` | `ggerganov/whisper.cpp` → `samples/jfk.wav` | 11.00s | 肯尼迪 1961 年就职演说片段，美国政府作品，**公有领域（Public Domain）**；whisper.cpp 仓库 MIT |
| `funasr_zh.wav` | `modelscope/FunASR` → `runtime/funasr_api/asr_example.wav` | 5.55s | 中文普通话示例音频；FunASR 仓库 Apache-2.0 |
| `aishell_zh.wav` | `wenet-e2e/wenet` → `test/resources/aishell-BAC009S0724W0121.wav` | 4.28s | AISHELL-1 中文语音测试片段；wenet 仓库 Apache-2.0，原始数据集权利归 AISHELL 方，此处仅作本地测试用例 |
| `arctic_en.wav` | `mozilla/DeepSpeech` → `native_client/dotnet/DeepSpeechConsole/arctic_a0024.wav` | 3.96s | CMU ARCTIC 语料中的单句，公开可自由用于语音研究；DeepSpeech 仓库 MPL-2.0 |
| `new_home_en.wav` | `coqui-ai/STT` → `data/smoke_test/new-home-in-the-stars-16k.wav` | 3.59s | 上游仓库自带的冒烟测试音频；Coqui STT 仓库 MPL-2.0 |

全部为 **16kHz / 单声道 / 16bit PCM WAV**，与项目运行时输入规格一致，拼接时无需重采样。

## 使用边界

- 这 5 段素材只随源码仓库提供，用于自动化测试与本地调试，**不打包进发行版 EXE**。
- 拼接产物 `longform_4min.wav` 是生成物，不提交到仓库；运行 `python tests/make_fixture.py` 即可离线复现。
- 若需要评估中文网课的识别准确率，请改用自行录制或已获授权的真实课程音频；本素材是中英文短句重复拼接，不能代表真实准确率。
- 如对某段素材的再分发许可有疑虑，删除对应文件并同步修改 `tests/make_fixture.py` 的 `ORDER` 即可，管线本身不依赖具体内容。

## 随发行包附带的 VAD 模型

`ov_models/silero_vad_v4.onnx`（约 1.7 MB）取自
[`snakers4/silero-vad` v4.0 标签](https://github.com/snakers4/silero-vad/tree/v4.0/files)
的 `files/silero_vad.onnx`，上游仓库为 **MIT** 许可，允许再分发。
项目原本就在首次启动时按需下载该文件；提交到仓库是为了让 CI 构建发行包时
不依赖外网，且用户解压后 VAD 立即可用（无需联网）。

注意：这是**模型文件**，不是测试音频；IR 权重（0.6B / 1.7B INT8）体积过大，
仍保持按需下载，不随包也不入库。
