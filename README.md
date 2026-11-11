# QwenASRGUI

简体中文 · [繁體中文](README.zh-Hant.md)

> 本地语音识别转字幕工具。纯 CPU + OpenVINO INT8，不需要 NVIDIA 显卡，不依赖 PyTorch。
> 本项目是 [`dseditor/QwenASRMiniTool`](https://github.com/dseditor/QwenASRMiniTool) 的 fork。

**先说边界**（读完这一段再决定要不要用）：

- 主场景是 **3–5 小时中文网课 / 会议录音转字幕**，追求准确率，不追求实时。
- 模型权重**不内嵌**：首次使用要在「模型」页下载 0.6B（约 1.2 GB）或 1.7B（约 4.4 GB）。
- 1.7B 段长上限 **10 秒**，来自 IR 导出时固定的 `T=1000`，放宽会静默丢字，界面会显示实际生效值。
- 所有回归测试使用 **mock ASR**。真实 INT8 权重、ForcedAligner、声纹 ONNX 从未在 CI 中加载。
  Windows 实机的窗口、冷启动、3–5 小时完整任务**未复验**。详见 [验证边界](docs/验证边界.md)。

---

## 这是什么

一条统一的流式管道：边读音频、边按 VAD 分段、边增量写 SRT、边通过 SSE 推进度。
不再按音频长短分两套逻辑，也不再把整段波形读进内存。取消与崩溃都保留已完成字幕，
同一文件再次开始可从断点续跑，结果逐字节一致。

| 能力 | 状态 |
|---|---|
| 长音频流式转写 / 断点续跑 / 取消保留 | 已实现，mock 回归覆盖 |
| 5 分钟试跑 / 切分预检（p50/p90/p99） | 已实现 |
| 模型下载中心（多源测速、SHA256、离线清单、导入验证） | 已实现 |
| 服务端持久批量队列（刷新页面不丢、失败隔离、重试） | 已实现 |
| 词级时间轴 / 说话人分离 | 可选增强，另存 `*.rich.json`，失败不影响 SRT |
| 跨段标点修正 / 导出讲义 | 转录完成时自动修正正式 SRT；讲义另存 `*.handout.md` |
| CTk 桌面版 | 遗留，不再更新；WebView 版是唯一主线 |

完整矩阵见 [功能矩阵](docs/功能矩阵.md)。

## 快速开始

**发行包（Windows）**：到 [Releases](https://github.com/1-1-2/QwenASRGUI/releases/latest) 下载 ZIP，
解压整个文件夹，双击 `QwenASR-WebView.exe`。无需安装 Python。

> **Important · SmartScreen**：分层目录版的启动器是未签名的 EXE。首次运行 Windows
> SmartScreen 可能提示「Windows 已保护你的电脑」，点「更多信息」→「仍要运行」。
> 启动器内容稳定、更新不再整包替换，随着下载量累积该提示会减少；正式签名需要代码签名证书，
> 目前**未签名**。请勿只拷走 EXE —— `_internal/` 是程序本体，必须与 EXE 一起移动。

**源码**：

```bash
python -m pip install -r requirements.txt
python app_webview.py            # 起本地服务 + Edge --app 窗口
python app_webview.py --selftest # 无窗口冷启动基准（输出 JSON）
python app_webview.py transcribe 课程.mp4 --no-resume --json
```

## 目录布局（onedir 分层）

```text
QwenASR-WebView.exe    启动器（内容稳定，可累积 SmartScreen 信誉）
_internal/             程序本体、依赖、前端资源（随更新替换）
manifest.json          内容寻址清单（SHA256，用于校验 / 差量 / 回滚）
ov_models/ subtitles/ longform_state/ ffmpeg/   用户数据（更新时不动）
settings.json          设置（更新时不动）
```

更新只替换前三项；`app.prev/` 保存上一版本以便回滚。详见 [打包与升级](docs/打包与升级.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 全部文档的阅读入口 |
| [架构说明](docs/架构说明.md) | 窗口策略、打包形态、路径规则、SSE 契约 |
| [长音频流式方案](docs/长音频流式方案.md) | 流式 Runner 原始技术说明 |
| [打包与升级](docs/打包与升级.md) | manifest、差量、回滚、签名与 SmartScreen |
| [功能矩阵](docs/功能矩阵.md) | 当前支持 / 遗留未迁移 |
| [验证边界](docs/验证边界.md) | 已验证与未验证，逐项列证据 |
| [模型下载清单](docs/模型下载清单.md) | 由 `models.json` 生成的产物（不翻译） |
| [风格规范](docs/风格规范.md) | 文档与代码的语言、语气约定 |
| [变更记录](CHANGELOG.md) | 版本历史 |
| [NOTICE](NOTICE) | 第三方资产与许可 |

## 构建与验证

```bash
python build_release.py --version 2.0.0 --sha <short-sha>   # 需 PyInstaller + Windows
python release_manifest.py verify --root release_pkg/QwenASR-WebView
python -m compileall -q *.py tests/*.py tools/*.py
for t in tests/test_*.py; do python "$t"; done
python tools/offscreen_render.py --out ./_offscreen_out      # 需 Playwright + Chromium
```

CI 只跑不依赖权重的调度层回归；发行包由手动触发的 `release.yml` 在 Windows runner 上构建。

## 回推上游与许可

上游仓库**没有 License**。本站是 fork，最终目标是拆成原子化 PR 回推上游。
`NOTICE` 逐项列出第三方资产（OpenVINO Runtime、Silero VAD、dseditor IR、CrispASR 等）的来源与许可状态；
在上游明确许可之前，这是 PR 回推的硬门槛。

上游原始 README（繁体，未改动）归档于 [docs/legacy/README_upstream.md](docs/legacy/README_upstream.md)。
