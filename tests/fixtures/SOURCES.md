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
