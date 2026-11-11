# 变更记录

本文件只记**已合入 main** 的变更。版本语义见 `version.py`。
性能相关的宣称必须附实测数字；没有就写「未实测」。

## 2.0.0（当前主线）

以 `dseditor/QwenASRMiniTool` 为起点的 fork。WebView 版成为唯一主线，CTk 桌面版停在 1.0.9。

### Wave 1 · 正确性与 CI 治理

- `concurrency` 分组、docs-only `paths` 过滤、`[skip ci]`、push 只守 main，保留两个 workflow 的手动触发。
- 消黑框（`CREATE_NO_WINDOW` + `STARTUPINFO`）、探测信息进 SSE、只读输入头解析时长。
- 任务指纹（模型 / 段长 / VAD / 源文件 / 语言 / 简繁 / 词汇 / 提示词），不一致从头开始。
- 取消改为语音组边界生效，续跑与完整跑逐字节一致；附带修复连续长语音无静音时丢段。
- 核实 `app.py` 两处简繁转换是独立路径，无重复，不改动。

### Wave 1.5 · 统一流式与收尾 OOM

起因：3–5 小时任务**跑完之后** WebView2 渲染进程崩溃。根因在收尾链路，不在推理。

- 上传落盘后立即释放内存副本 + `gc.collect()`。
- 后端流式维护 200 个峰值桶；前端删除整文件 `decodeAudioData()`；结果只返回前 200 条字幕。
- **删除长短分支**：所有入口统一走 `LongformRunner`，`--longform` 留作无效果兼容旗标。
- 试跑与切分预检一律只看前 5 分钟；切分参数全部集中在识别页。
- 新增 `capabilities` SSE，明确词级 / 说话人在流式路径下的可用性。

### Wave 2 · 下载与数据地基

- `models.json` 成为单一事实源；`model_catalog.py` + 下载中心 UI。
- L1：多源测速、错误分类、重试退避、大小兜底、ETag / LFS / HTML 探测、SHA256、磁盘预检。
- L2：手动下载清单 `.txt`（含 aria2c 命令）+ 导入本地模型 `full_verify()`。
- **明确否决 SDK**：不引入 `huggingface_hub` / `modelscope` / `hf_transfer`。

### Wave 3 · 打包分层

- G1：去 pythonnet，默认 Edge `--app`，从根上绕开 MOTW。
- G2：`--onedir` 分层；路径规则收敛到 `app_paths.py`（用户数据在启动器旁、只读资源在 `_internal/`）。
- G3：内容寻址 `manifest.json`、SHA256 校验、差量计算、`app.prev/` 回滚。
- G4：平滑升级测试（旧设置 / 模型 / 字幕 / 断点逐项断言不动）。
- G5：首次启动引导。G6：`--selftest` 冷启动基准。G7：CI 校验 manifest。G8/G9：签名与 SmartScreen 说明。
- **G2 明确未做**：剔除 CTk/Tk 盲端（约 15 MB，需拆上游 `app.py` 约 2200 行 GUI 段，与原子化 PR 冲突）。

### Wave 4 · UI 整合

- B3 控件持久化、B8 双进度 + 实时字幕 SSE（前端列表限 200 条、上滚暂停跟随）。
- B4：把 `/api/batch` 空桩换成**服务端持久队列**（`batch_queue.py`）：上传一次、单 worker 串行、
  状态持久化、SSE 推送、失败隔离、取消 / 重试 / 清除、刷新页面不丢。
- B5.2：UI 语言稳定枚举 `hant/hans/en`，旧中文设置值自动迁移。
- H1：后端结构化运行日志（环形缓冲 + SSE `log` + `GET /api/logs`）、前端可折叠面板。
- H2：统一按钮状态机（`syncActionButtons`），消除散落的 `disabled` 赋值。
- H4：多实例保护（回环端口锁 + URL 交接，崩溃不留 stale lock）。
- H6/H7：后端动态文案本地化（`I18N.L()`，简繁转换 + 英文短语字典；专有名词与诊断原文保留）。**本轮已重构为结构化 `key + vars` 契约**，见下方「用户裁决后的收尾」。
- 词级时间轴与说话人分离接回流式路径：`StreamEnhancer` 挂现成 PCM chunk，另存 `*.rich.json`，
  失败只回退为行级字幕，`tests/test_postprocess.py` 断言 SRT 字节不变。

### Wave 5 · 输出与收尾

- B6 / B7：语言下拉 i18n 错挂、检查更新指向本 fork。
- C1：跨段标点修正，另存 `*.punct.srt`，不改原 SRT、不动断点契约。**本轮已改为默认写回正式 SRT**，见下方「用户裁决后的收尾」。
- C2：导出讲义 `*.handout.md` / `.txt`，按停顿分段。
- J5：TLS 证书验证降级不再静默，发 `warning` 事件 + 持久横幅 + `status.sslInsecure`。

### Wave 6 · 文档与 PR 物料

- README 简繁双份重构（边界前置、Important 块、功能矩阵）；上游繁体原版归档到 `docs/legacy/`。
- `docs/` 分册：索引、打包与升级、功能矩阵、验证边界、风格规范，简繁各一份 + 页顶互链。
- `CHANGELOG.md`、`NOTICE`、明暗双主题架构图、由 `models.json` 生成的模型下载清单。

### 用户裁决后的收尾（本轮）

- **H6/H7 重构**：废弃「逐字转简体 + 英文整串字典」的半成品方案。后端新增 `ui_text.py`
  统一发 `{k, v, t}` 结构化消息，前端 `webview/js/i18n.js` 维护三语**人工**文案字典 `M`
  （繁体为单一事实源；简体用词独立撰写，如「内存／默认／通过／网络／文件」）；
  自由文本（异常、路径、用户提示词、运行日志）走词汇级 + 字符级兜底，英文保留原文。
  修复了旧逐字表因 3 字符片段导致配对整体错位的 bug（「記憶體」曾被转成「建續」）。
  `tests/test_i18n_contract.py` 用 Node 加载真实前端字典，校验键集合、占位符、繁体一致性、
  英文无 CJK、简繁用词差异，并以独立子进程驱动真实后端 API 验证 JSON 序列化与键注册（39/39）。
- **C1 默认写回**：`LongformRunner` 在任务 `done` 时对完整 SRT 做一次原子重写，
  `paused` 不重写、`done` 短路不重复执行，因此续跑与完整跑仍逐字节一致（A2）。
  `tests/test_punct_contract.py` 16/16 覆盖。手动按钮保留另存 `*.punct.srt` 的能力，
  用于对旧文件或用户手工改过的字幕生成副本，不覆盖原档。
- **自动更新采用 A 方案**：维持「检查更新 = 打开发行页，手动下载覆盖」，
  不投入差量自动更新器；`docs/打包与升级.md` 已把「自动更新未实现」改成准确描述。
- **简体优先**：`README.md` 保持简体，繁体为 `README.zh-Hant.md` 及 `.zh-Hant.md` 分册；
  回推上游前再评估是否反向。

## 验证口径

本地回归全绿（各套断言数见 `tests/test_*.py`）、离屏渲染 33/33、远端 CI 全绿。
**全部使用 mock ASR**，真实权重未加载；Windows 实机与 3–5 小时完整任务未复验。
详见 `docs/验证边界.md`。
