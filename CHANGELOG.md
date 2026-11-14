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
- **修复 Windows 包校验误判**：`ov_models/` 是同名双用途目录——启动器旁是用户权重，
  `_internal/ov_models/` 是随包只读资源（Silero VAD、Mel filters）。G2 遗留的按名一刀切规则
  禁止它出现在 `_internal/`，与 `build_release.py`、`app.py::_find_vad_model()` 自相矛盾，
  导致首个真实 Windows 构建在打完 198 MB ZIP 后才于校验步失败。
  `tools/verify_package.py` 抽出 `check_layout()` 改为按内容判定：用户数据目录必须在启动器旁，
  纯用户数据目录不得进入 `_internal/`，`_internal/ov_models/` 只允许资源白名单；
  白名单由 `build_release.STATIC_DATA` 单一事实源派生，杜绝再次出现两份名单互相打架。
  新增 `tests/test_verify_package.py`（20/20）覆盖合法布局与 8 类越界，并接入 CI 与打包前回归。
  同时修正 `release.yml` 回归块的门禁缺陷：原写法 `{ ... }` 配 `code=$?` 只取最后一条命令的
  退出码，会吞掉中间套件的失败；中途改成 `&&` 串联后虽能拦截，却会在第一个失败处短路、
  看不到后续问题。最终改为逐套 `run_suite` 聚合，打印 PASS/FAIL 摘要，任一套失败即判构建失败。
- **修复 Windows 专属回归失败（下载清单路径分隔符）**：`build_manifest_text()` 原先直接用
  `str(Path)`，在 Windows 上输出 `ov_models\qwen3_asr_int8`，与 `tests/test_wave2.py` 断言的
  正斜杠不符；这个失败此前一直被上面的旧门禁吞掉。新增 `_manifest_path()` 统一以
  `as_posix()` 输出（aria2c / Windows 文件 API 都接受正斜杠），使清单跨平台确定性一致；
  测试补上「不得出现原生反斜杠」断言，在 Linux CI 就能提前拦住。`tests/test_wave2.py` 60/60。
- **简体优先**：`README.md` 保持简体，繁体为 `README.zh-Hant.md` 及 `.zh-Hant.md` 分册；
  回推上游前再评估是否反向。

### Wave 7 · 实机反馈修复（启动崩溃 / 简繁覆盖 / 体积）

来自用户实测第一版 Windows 包的三条反馈。第一条是致命的：程序根本起不来。

- **windowed 下 `sys.stdout` 为 `None` 导致退出崩溃**（`AttributeError: 'NoneType'
  object has no attribute 'flush'`）。`main()` 收尾无条件 flush 两个流；开发模式永远有
  终端，所以本地测试全部通过、打包版一启动即挂。修复分三层：
  `hush_stdio()` 在导入期把 `None` 的流换成写 `launcher.log` 的安全替身；
  收尾 flush 改为尽力而为，任何一步失败都不再阻断 `os._exit(0)`（否则会留下僵尸进程与
  被占用的端口）；新增 `sys.excepthook` / `threading.excepthook`，把未捕获异常写日志并弹窗
  —— 原来 windowed 版的崩溃是**完全无声**的。`tests/test_launcher_stdio.py` 14/14。
- **简繁「体验区分没做好」属实**：字典 `D`（232 键）与 `M`（128 键）本身完整，但
  `webview/js/app.js` 里有约 40 处运行时文案直接拼接繁体中文（下载中心状态、批量队列、
  进度与日志、端点页、录音页、切分预检、侧栏状态…），**只要操作一次就冒出繁体**。
  全部改走 `T()`；顺带修掉 `PHASE_TEXT` / `ST` / `VIEW_TITLES` 三处「模块载入时求值」，
  它们会让切换语言后仍显示旧语言。`tests/test_i18n_contract.py` 新增硬编码扫描
  （注释 / 正则字符类 / `T()` 回退参数豁免），当前清零。
- **体积**：OpenVINO wheel 解包 134.8 MB，其中 GPU 插件 30.9 MB、NPU 4.0 MB、
  TensorFlow/ONNX/PyTorch/Paddle/TFLite/JAX 前端合计 14 MB、导入库 `.lib` 7.7 MB、
  设备缓存 `cache.json` 8.8 MB —— 本项目只用 IR 前端 + CPU 插件，以上全是死重。
  `build_release.slim_openvino()` 在打包阶段剔除这些，**实测 openvino 目录 131 MB → 66 MB
  （省 68 MB）**。三重保护：布局认不出就整体跳过、CPU 插件缺失就跳过、`--no-slim` 可回退；
  `tools/verify_package.check_openvino_slim()` 断言 CPU 插件在位且 GPU/NPU 插件不在位。
  仍保留 `--collect-all tokenizers` 与完整 `onnxruntime`（后者是 `app.py` 无条件导入的硬依赖）。
- **Tk 界面库被误打进 WebView 包**：`app.py` 顶层 `import customtkinter`，PyInstaller 静态
  分析于是把 tkinter + customtkinter + tcl/tk 全量收进发行包 —— 一个 Tk 界面库在 HTML 界面里
  毫无用处。现在 GUI 段（含 `import tkinter`）由 `QWENASR_WEBVIEW_BUILD=1` 隔离，
  `build_release.py` 打包时设置该变量并对 tkinter/customtkinter 加 `--exclude-module` 兜底；
  `python app.py` 行为不变（已在 Xvfb 下实测窗口正常创建）。
  `tests/test_webview_bundle.py` 14/14 校验两种模式。
- **Edge --app 旗标整理**：抽出 `_app_mode_args()`，补 `--hide-crash-restore-bubble`、
  扩大 `--disable-features` 关闭 Edge 侧栏与登录提示。窗口是系统 Edge 的 --app 无痕窗口，
  标题栏无法完全去除（那是 Chromium 自绘的框架），保留以支持拖动与最小化。
- **侧栏排版**：状态文案在 88px 栏宽下被裁切/溢出侧栏。栏宽 88 → 104px，
  状态文字单行省略、品牌名换行居中；离屏渲染新增「不得溢出、不得截断」断言（三语各一次）。

### Wave 7.1 · 简繁验收补齐（第一次修复后的复检）

上面那轮修完仍不够。给离屏渲染加上「逐字扫描三语残留繁体」的断言后，暴露出四类
**上一轮没看见**的问题：

- **端点页 24 处中文、已接入 0 处**；模型页 24 处只接了 10 处。批量扫描 `index.html`
  才发现这是系统性漏接，不是零星遗漏。现已全部补上 `data-i18n`，并补 14 个三语词条。
- **执行期文案切换语言后不重绘**：进度区、自检面板、端点状态、批量 chip、麦克风下拉
  都是「渲染时就把语言定死」。`i18n.js` 新增 `onAfterLang()` 回调，`setLang` 触发后
  统一重绘，而不是只在 `app.js` 的两处调用点手动补 —— 后者漏掉任何新调用方就会退回旧语言。
- **简体自由文本在繁体界面漏出**：长音频 runner 的进度叙述走 free-form 路径，
  写的是简体。新增 `toTraditional()`（字对字反查 + 139 个「一字多繁」白名单，
  避免造出「分鈡」「埰用」这类异体字），繁体界面不再漏出简体原文。
- **英文界面整片中文**：自检 / 模型卡 / 加速选项的 label 是自由文本，`EN_MAP` 查不到就
  原样返回。根因是 mock 数据用纯字符串冒充了后端的 `{k,v,t}` 结构 —— **假数据掩盖了
  真缺陷**。mock 已改为与 `webview_backend.health_check()` 同形，英文字典补三条正则句式
  （`Processed 84.0 min · 612 segments` 等）。
- 顺带修掉：`apply()` 用 `textContent` 会抹掉元素内嵌的版本号 chip（改用
  `data-i18n-lead` 只替换文字节点）；`document.title` 不随语言变化（Edge `--app`
  窗口标题栏因此一直显示繁体名）；`status.appName` 被错放进后端消息字典 `M` 而非静态
  字典 `D`，导致品牌名根本查不到。
- 断言本身也校准过一次：黑名单曾收入「案」「理」这类**简繁同形字**造成误报；
  模型名（`TEA-ASR-1.1 台灣國語 Q8`）与语言名（`繁體中文`）按惯例不翻译，已显式豁免。
  离屏渲染 43 → **57 项**，并已确认「故意回退修复 → 断言失败」有效。

### Wave 8 · 去掉 SciPy / 打包首次自证可启动 / 那个 `No module named 'tkinter'`

三件事有同一个背景：**之前的校验都只看文件清单，而清单对得上并不代表能启动。**

- **SciPy 整体移除，省 69 MB**（`_internal` 348.1 → 171.4 MB，ZIP 133.2 → 101.8 MB）。
  它只服务两处：聚类（`linkage` + `fcluster`）与重采样（`resample_poly`）。
  换成 `cluster_core.py`（average-linkage + Lance-Williams）与
  `audio_io._resample_fft`（频域理想低通）。与 SciPy 对拍 400 组随机数据，
  Z 矩阵逐位一致、两种 `fcluster` 划分完全相同；10 kHz 降到 16 kHz 后 RMS = 0（抗混叠成立）。
  顺带必须删掉 `requirements.txt` 里的 `librosa` —— 代码里根本没 import 它，
  它却经 `scikit-learn` 把 SciPy 悄悄拖回构建环境。
- **CI 里真跑一遍打好的包**（`tools/smoke_test.py`）：解 ZIP → 真 exe `--selftest`
  （断言 `ok=true`、`server=measured`）→ 真启动 GUI → 等 `http://127.0.0.1:PORT` →
  `/health` 必须 200 → 观察存活与 `crash.log`。这条闸上线后连抓两个真缺陷。
  判定刻意只看**后果**：曾经用「日志里有没有 `No module named`」当条件，
  修好之后只要日志残留一条已处理的旧告警就会假红。
- **`No module named 'tkinter'` 的真正根因**：`app.py` 拿 `QWENASR_WEBVIEW_BUILD`
  这个**只传给 PyInstaller 分析子进程**的环境变量当运行期判据。exe 里没有它 →
  判为 Tk 版 → 真去 `import tkinter` → 包里刻意没带 → 一启动就死。
  改判据为 `环境变量 或 sys.frozen`（冻结版只有 `app_webview.py` 一个入口）。
  **它之所以躲过好几轮排查**：冒烟测试的诊断脚本和 `test_release_env` 的探针都
  主动设了那个变量，等于替 exe 补上了它不该有的东西 —— 模拟比真机宽松，必崩测成全绿。
  两处已改为 `sys.frozen = True` 且显式不设该变量，回滚修复会立刻在本地变红。
- **`--noffmpeg-variant`**：完整包（101.8 MB，随附 ffmpeg）之外另出 64.7 MB 变体。
  ZIP 内根目录名带 `-noffmpeg`，两个包解压到同一处不会互相覆盖。
- **`tools/gh_download.py`**：反代拿到的 302 指向 Azure Blob、字节不经反代，
  单连接实测 14 KB/s。Blob 支持 `Range`（206），改 16 连接 × 1.5 MB 分片后
  **0.33 MB/s，165.5 MB 用 496 秒**（约 24×）。

### Wave 9 · 首启闪退、那 10 秒、以及 `[WinError 5] 拒绝访问`

使用者报三件事，其中两件是**同一种病**：出错之后，现场没有留下任何可查的东西。

- **首启闪退查无实据，是因为日志路径本身就写错了。**
  `使用说明.txt` 让使用者去 `%LOCALAPPDATA%\QwenASR-WebView\launcher.log`，
  而 `_log_dir()` 用的是 `APP_NAME`（`聲音辨識小工具`）；`edge-profile` 也被说成
  「程序目录下的」子目录，实际在 `%LOCALAPPDATA%` 里。照说明去翻只会翻到空目录，
  于是「闪退」永远只能靠猜。改法：说明改成真实路径，并加测试钉住
  「说明里的目录名 == `APP_NAME`」，以后再改名字会立刻变红。
- **首启种子的量化值与所有读取侧的缺省值不一致。**
  `_seed_defaults()` 写 `crisp_qwen_quant = "q4"`，而 `_load_crispasr` /
  `selected_model_present` / health / `set_model` 等七处读取的缺省是 `"q8"`。
  后果是「首启下 q4 → 载入时找 q8 → 再下一遍 q8」；模型一个都没到位时，
  自动载入路径还会在启动当下静默拉起一次数百 MB 的下载 —— 这正是
  「第一次打开闪退，但 settings.json 已建立成功」的形态（种子先落盘，
  下载在后台炸掉）。改为单一常量 `_QWEN_QUANT_DEFAULT`，种子与读取共引同一个。
- **`[WinError 5] 拒绝访问` 没有任何上下文，因为 ffmpeg 的 stderr 被丢进了 DEVNULL。**
  ffmpeg 打不开输入文件的真正原因（`Permission denied` / `No such file` /
  `moov atom not found` / `Is a directory`）**只在 stderr 里**，而
  `ffmpeg_pcm_stream()` 写的是 `stderr=subprocess.DEVNULL`，失败时只抛一句
  `ffmpeg 退出码 N`。于是使用者拿到的就只有自己 OSError 的那半句。
  改为管道收集 + 独立线程抽干（不抽干会因管道写满把 ffmpeg 堵死，表现成
  「转换卡住」），失败时把 ffmpeg 原话连同中文提示一起抛出来。
- **临时目录清理 `ignore_errors=True` 等于每次转写都在 `%TEMP%` 留一份音频。**
  子程序刚被 kill、句柄还没释放时 rmtree 会撞 WinError 5，被 `ignore_errors`
  静默吞掉，跑几次就能把系统盘塞满且毫无痕迹。改为退让重试 4 次，
  仍失败则写进 run log 并告诉使用者去哪删。
- **那 10 秒**：本地实测 Python 侧全链路 **57 ms**（建 server 51 / 监听 3 /
  判定 3），所以不在后端。真正的问题是**这段时间完全没有反馈**：模型加载在
  `srv.start()` 之后立刻开跑，而 `_EventHub` 只广播给当前订阅者、**没有回放**，
  Edge 还没连上 SSE 时推的「載入 VAD 模型…」「編譯 ASR 模型…」全被丢掉，
  用户看到的就是白屏卡住再突然「已就绪」。改为 hub 记住最近的 `status` /
  `warning`，新连接先补发再接实时流（瞬时 `progress` 刻意不重放，重放一条
  过期的 47% 只会误导）。同时在启动器里加了分段计时，每段写进 `launcher.log`
  （`[启动计时] … +51ms（累计 51ms）`），下次再有「开得慢」可以直接指名。
- **日志分不清哪一次启动**：`launcher.log` 是 append 的，跑三次以后三段内容
  首尾相接，光看正文分不出哪段属于崩掉那次。改为**每行盖时间戳**
  （`[11-11 20:35:21.183]`）、每次启动写一条
  `===== 聲音辨識小工具 啟動 <日期> PID <n> =====` 分界，并且**每次运行另留一份
  快照** `launcher-<年月日-时分秒>-p<PID>.log`（保留最近 12 份，自动清理）。
  主文件超过 1 MB 时轮换为 `launcher.log.1` 而不是直接删除 —— 前一次崩溃
  往往正是要对照的那一次。`stdout` / `stderr` 现在共享同一个 fanout，只有一份
  文件句柄，两个流交错时顺序不再乱。

### Wave 10 · 切分预检说人话 / Edge 脱离 Job Object

用户反馈的两个问题在同一份 `launcher.log` 里同时暴露：

- **`[21:06:36] ✕ 切分预检失败：vad_sess is required for VAD preview`**
  模型未下载时点「切分预检」，`vad_preview()` 需要 Silero VAD 的 ONNX session，
  但引擎是空壳（`vad_sess=None`），直接抛了内部技术词。改为明确提示
  「模型尚未載入，無法進行切分預檢。請先到「模型」頁下載並載入模型，再重新嘗試。」，
  同样的提示也用在「开始转换」上。
- **首次启动依旧"闪退"**：`launcher.log` 证明 Python 侧两次都成功（34ms 拉起 Edge），
  "闪退"是 Edge 窗口消失。根因是 `proc_guard` 的 Job Object（`KILL_ON_JOB_CLOSE`）
  把 Edge 也圈在内 —— Python 退出时 Job handle 关闭 → Edge 被连杀。
  第一次启动 Edge profile 是空的，冷启动慢（3-5 秒白屏），用户以为闪退就关了。
  修法：Edge 启动加 `CREATE_BREAKAWAY_FROM_JOB`（`0x01000000`），脱离 Job；
  同时记录 Edge PID、退出码、存活时间，下次「闪退」时日志能直接看出 Edge
  是正常关闭还是异常退出。

### Wave 11 · 段长：固定补零改动态、等长硬切改低谷切分、上限 10 → 20 秒

起因：`_extract_mel` 每段一律补到模板长度（10 秒），4 秒的段也要付 10 秒的算力；
而超过 10 秒的连续讲话由 `_enforce_chunk_limit` 按固定步长硬切，刀口落在字中间。

- **`processor_numpy._layout()`**：补零长度改为 `ceil(实际秒数)`，三个常量
  （`n_samples` / `nb_frames` / `n_audio_tokens`）由模板换算出的每秒系数推导。
  天花板经 `LightProcessor(max_secs=…)` 由引擎传入，单点驱动。600 秒语音实测
  固定补零 391.9 秒 → 动态 381.2 秒（**−2.7%**），段数与切点不变。省得不多是因为
  这段素材的 VAD 组本来就偏长（平均 8 秒）；按单段耗时曲线估算的 −19.9% 高估了。
- **`chunk_split.py`（新）**：超长段在 `[上限−1.5s, 上限]` 内找 20ms 窗的
  最低 RMS 处下刀，替代 `chunk[pos:pos+limit]` 的等长硬切。顺带修掉残片
  不足 1 秒被 `break` 丢弃（吞掉最后半句）的问题。`app.py` 与
  `chatllm_engine.py` 两份重复实现合并为这一份。
- **上限 10 → 20 秒**（`ASREngine1p7B.max_chunk_secs`）。600 秒真实课程录音：
  硬切刀口 **33 → 12**（−64%），最终段数 68 → 40（−43%），端到端 **391.9 → 396.9 秒
  （+1.3%）** —— 短段省下的空转基本抵掉了长段多付的部分。单段耗时 4.11 → 12.48 秒
  （RTF 0.41 → 0.62）。300 秒样本另测一次为 +3.1%（195.5 → 201.6 秒）。
  文本：600 秒相似度 0.9877、300 秒 0.9831；逐处比对 cap=20 在 6 处更准
  （「開始析思想」→「開始新思想」、「很多實證」→「很多時政」）、3 处更差，
  cap=10 另有一整句「我看大家討論的很火熱」丢失。
- **实时模式锁死 10 秒**：`rt_max_buf` 不再跟随 `max_chunk_secs`，否则实时出字
  延迟会变成 20 秒。
- 界面：`capabilities` 到达时把段长滑杆的 `max` 夹到模型天花板，i18n 文案与
  README / 文档同步到 20 秒。断点指纹本就含 `chunk=`，旧断点会被识别为不匹配
  并重新开始，不会接出半新半旧的 SRT。
- **修复切分预检在「点击选档」路径下必崩**：`picked` 是 `{path, name}`
  普通对象、没有 `File`，旧代码仍把它塞进 `FormData.append("file", …)`，
  浏览器抛 `parameter 2 is not of type 'Blob'`，请求根本没发出。现在预检与
  转写完全对称：原生对话框走 JSON `{path}` 零拷贝引用，拖放的真 `File` 仍走
  multipart；`bridge.js` 用 `instanceof Blob` 做类型判断，避免同类误传再次
  变成浏览器异常。新增 `tests/test_vad_preview_path.py`（15 项，含真实 HTTP
  端到端验证两条路径与临时副本清理）。
- 回归：新增 `tests/test_chunk_split.py`（17 项）、`tests/test_dynamic_pad.py`
  （30 项）、`tests/test_vad_preview_path.py`（15 项）。**动态 pad 与固定 pad 不是逐字节相同** —— 批量形状不同会让
  `np.fft.rfft` 走不同 SIMD 分组，实测 151/400 帧各差 5.96e-08（1 个 float32 ulp），
  所以不能靠 SRT 对拍验证，改看端到端文本。

### Wave 12 · 词级对齐：逐段同步改自适应窗口攒批 + 后台执行

起因：诊断（`02_交付文档/管线效率诊断_20261114.md`）量出 3x → 1.x 的回退里
约 87% 来自词级 FA。当时的写法是「每段 ASR 之后当场跑一次 FA，收完才落盘」：
`pending` 是 `emit_group` 的局部变量，组一结束就清空，于是「提交完立刻收」，
`fa_pipeline` 再快也没机会重叠。实测三组独立进程的 ASR/FA 重叠恒等于 **0**。

- **`fa_batch.py`（新）**：自适应窗口的 FA 队列。ASR 主线程只 `submit`（立即
  返回），累计音频到窗口 W 才合并成一次 FA，并派发到后台单线程；`settle` 只在
  真正要写字幕时取结果。W 由实测 `(L, T)` 在线拟合 `T = a + bL + cL²`，取
  `T/L` 最小的 L，夹在 [15, 40] 秒（实测甜点 20~30 秒）。整窗降级条件：
  返回空 / 时间覆盖 < 50%（防 `f128731` 那种「固定 32 项、只覆盖 2 秒」的假结果）。
  FA 吞标点属正常（60 字只回 54 词），不再当成失败 —— 词按**局部时间**归属到段，
  段内再按字符偏移或中点法切给行。
- **`longform_runner`**：`pending` 从 `emit_group` 局部提到 `run()`，跨 VAD 组
  累积；队列深度按 `ceil(窗口/段长)+4` 起步，收取时若真的等了 FA 就再加深一段
  （上限 10）。**断点不再 flush**：改成回卷到最早未落盘那一段（VAD 状态清空，
  与取消时的回卷同一条路径）—— 否则每 16 秒一次的断点会变成 FA 同步屏障，
  把攒批彻底废掉。取消/结束/暂停三处仍统一 flush，SRT 序号与单调性不变。
- **`webview_backend`**：`_fa_align_raw` 提供整窗对齐；攒批默认开，`faBatch=0`
  一键回到逐段路径；内存不足时仍按 `sysmem.can_run_parallel_with_asr()` 降级并
  提示。每次任务结束把「窗口数 / FA 累计秒 / 音频秒」写进运行日志，便于回看。
- **实测（180 秒真实课程录音，1.7B INT8，VAD 0.8，同机）**：

  | 配置 | 墙钟 | 倍速 |
  |---|---:|---:|
  | cap10 无词级（ASR 地板） | 53.73 s | 3.35x |
  | cap20 无词级（ASR 地板） | 61.03 s | 2.95x |
  | cap20 逐段 FA（旧） | 82.04 s | 2.19x |
  | **cap20 攒批（新）** | **74.95 s** | **2.40x** |
  | cap10 攒批（新） | 65.35 s | 2.75x |

  FA 本身在这段音频上要 24.8 秒（0.21 秒/秒音频，与 ASR 并发时比独占慢约 1.5 倍），
  攒批把其中约 **45%** 藏进了 ASR 时间；剩下的由 CPU 争抢决定，不可能为 0。
  60 秒样本 A/B：攒批与逐段的**行文本完全一致**、词数 163 = 163。
- 回归：`tests/test_fa_batch.py`（44 项，纯逻辑、不加载模型）、
  `test_parallel_policy.py` 新增「窗口大小不改变 SRT」整跑用例；
  `test_release_env.py` 的打包模块清单补 `fa_batch`。

## 验证口径

本地回归全绿（25 套，断言数见 `tests/test_*.py`）、离屏渲染 57/57、远端 CI 全绿；
Windows 发行构建**每次都会解包真跑**（`tools/smoke_test.py`），两个变体实测
端口绑定成功、`/health` 200、无 `crash.log`。
**全部使用 mock ASR**，真实权重未加载；人工实机验收与 3–5 小时完整任务未做。
详见 `docs/验证边界.md`。

Wave 9 的额外实测：ffmpeg 失败三条路径（不存在 / 伪造扩展名 / 传目录）都拿到了
原文与中文提示；正常路径 60 秒音频 960000 样本、耗时 0.03 s；造 2.5 MB
stderr 噪声验证抽干线程有效（1.36 s 读完、无阻塞）。
日志侧：同一目录连跑两次，`launcher.log` 里两段各带横幅与毫秒时间戳、
两份快照分别对应一个 PID；连开 15 次只保留最近 12 份；主文件 1 MB 处
正确轮换为 `.1`。
「那 10 秒」在 Windows 真机上仍未复现（本机无桌面，Edge 冷启动那一段测不到），
但分段计时已落地，下次可从 `launcher.log` 直接读出归属。
Wave 10：vad_preview 错误提示含「模型尚未載入」+「下載」动作指引 + 不是 ValueError；
Edge 脱离 Job Object（`CREATE_BREAKAWAY_FROM_JOB`）+ 退出码/存活时间日志。
