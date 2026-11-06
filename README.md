# Pet3D · `merge/v3-core-actions` 合并线总说明

接手前按顺序读三份东西：本文（这个分支是什么、怎么跑）、`AGENTS.md`（工程协作约定：分支/验收/热点文件规则）、`docs/合并记录.md`（合并的来龙去脉、逐项核对与遗留问题）。

## 1. 这个分支是什么

`merge/v3-core-actions` 是把两条开发线合并后的**集成线：V3 服务框架 + 统一骨骼全量动作库**。

| 侧 | 来源提交 | 提供什么 |
|---|---|---|
| main 侧 | `fed66f6`（stage2: 合并统一骨骼与全量动作库） | 统一骨骼的**全量 65 动作库**：`app/resources/animations/source/` 下 65 个 GLB 源、`app/resources/animations/godette.tres`（32.8MB 内嵌 AnimationLibrary，65 动画）、动作清单 `app/assets_data/humanoid_core.json`（65 action）；以及 Godette/Alicia 角色契约 |
| v3 侧 | `8fd8478`（feat/v3-core） | Python 后台服务 `service/`（聊天、记忆、人格、表情、主动行为、成长、用量、诊断；settings schema v4）+ Godot 侧聊天窗口、V3 面板、`service_client.gd`（合计约 +3157 行） |

- 合并提交：`7fc92f6`（双亲 `fed66f6` + `8fd8478`，2026-11-06 06:49 +0800）。
- 其上还有两个合并后修复：`fbf0eec`（补 4 个 `.uid`——v3 侧并入的 4 个 `.gd` 原本缺 uid）与 `102aac5`（移除 `fbf0eec` 曾短暂加入的两个 `app/tools/assets/verify_recovery*.gd` 转发桩；回归脚本由 `tools/build_stage2.py` 以**绝对路径** `--script` 驱动，工程内不需要 res:// 入口。移除后已按正式方式重跑：`GODETTE_REGRESSION failures=0`、`GODETTE_RUNTIME checks=68235 failures=0`）。**HEAD 以 `git log -1` 为准；本文描述到 `102aac5`。**

归属实测（撰写本文时复核）：`git diff --stat 8fd8478 HEAD -- service/ data/` 为空（service/、data/ 与 v3 亲本零差异）；`app/scripts/chat`、`app/scripts/ui`、`app/scripts/service_client.gd` 全部来自 v3 侧；动画库与动作清单来自 main 侧。

运行形态：Godot 4.7.2 桌宠主程序（`app/`）通过 stdio 启动 Python 服务（`service/main.py`），JSON-lines 协议（`version=1`）；服务只依赖标准库。

### 在哪干活、以哪份文档为准（先读这段）

- **干活目录**：本合并线的开发在 worktree `D:\danjigame\3D-pet\wt\merge-v3-actions`（分支 `merge/v3-core-actions`）进行；仓库 `.git` 在 `D:\danjigame\3D-pet\3D-pet-gtpdo`（实测存在；旧路径 `D:\danjigame\3D-pet-gtpdo` 实测已不存在）。主仓库根的主 checkout 由集成者管理。
- **文档优先级**：本文与 `docs/合并记录.md` 描述本合并线，全部实测于 HEAD `fbf0eec`。`AGENTS.md` 的流程规范（分支命名、验收纪律、文件所有权、热点文件）仍然有效，**但其 §1 的"git 仓库根"（:9）、§2 的 worktree 示例命令（:28）与"当前阶段"描述（:12-14）已过期**——写的还是旧路径 `D:\danjigame\3D-pet-gtpdo`，且只写"阶段 2、只做 Godette 单角色"、未提本合并线。两者冲突时以本文与实测为准；修订 AGENTS.md 属全仓库协作约定，建议由集成者在主仓库单独提交（见 `docs/合并记录.md` 遗留问题 6）。
- **无 .git 的快照拷贝**：本文所有 git 证据（双亲、diff 归属、分支）都在上述 worktree 里复核。如果你拿到的是没有 `.git` 的目录（例如快照拷贝），这些证据无法在里面复核（`git status` 会报 not a git repository），请以 worktree/主仓库为准；该拷贝对应包含本文档的提交（在 worktree/主仓库里 `git log --oneline -- README.md` 第一条即可查到）。

## 2. 目录结构（逐项一句话）

| 路径 | 内容 |
|---|---|
| `app/` | Godot 4.7.2 工程：`project.godot`；`scripts/`（`character_view.gd` 动作链路、`scripts/animation/action_controller.gd` 动作优先级、`scripts/chat/` 聊天会话、`scripts/ui/` 各面板、`service_client.gd` 服务客户端）；`addons/`（`vrm` VRM 导入插件、`mtoon` MToon 渲染插件——两者在 `project.godot:20` 启用，`vrm/bin/` 带 vrm_physics 各平台二进制；`pet_window` 窗口桥 GDExtension 包装）；`models/`（`godette.vrm`、`alicia.vrm`、soldier/swordsman 等模型源）；`motions/`（短动作缓存 idle/wave，导出时排除）；`assets_data/`（角色配置 + `humanoid_core.json` 动作清单）；`resources/animations/`（`godette.tres` 与 `source/` 65 个 GLB）；`tests/`（headless 引擎测试，17 个 `.gd`）；`tools/`（Godot 侧脚本）；`export_presets.cfg`（导出过滤） |
| `service/` | Python 3.13.14 后台服务（仅标准库）：`main.py` 入口（只解析 `--data-dir` 后调 `runtime.serve`）；`protocol.py` 信封；`commands.py:86` 起的 `HANDLERS` 注册 **40 条命令**；`features/`（chat / memory / state / autonomy / ai / expression / growth / usage / diag）；`persona.py`（人格卡）、`settings.py`（schema v4 + 迁移链）、`character_catalog.py`、`ai/`（本地路由 + DeepSeek 编排）、`behavior/`（主动行为预算） |
| `characters/` | 角色契约源（绑定 `animations/humanoid_core` 的 v2 契约）：`godette.json`、`alicia.json`（数据已并入、运行目录未放行）、`swordsman.json`（历史遗留） |
| `data/` | 服务的种子/配置数据（人读的仓库内文件）：`persona/godette.json` 人格卡（6 条 boundaries）、`expression/phrases.json` 台词库（10 条，care/greeting/idle/night 四类）。注意：**运行时持久化数据（设置/聊天记录/记忆）不在这里**，见 §3.4 |
| `animations/` | 动作资产管线中间产物（`asset_manifest.json`、`humanoid_core/`、`source_conversion.json` 等） |
| `native/` | GDExtension 原生模块源码（`window_bridge.cpp` 窗口桥等，构建期编译） |
| `tests/` | pytest 测试（`tests/service`、`tests/assets` 等；`conftest.py` 在 `tests/service/`） |
| `tools/` | 构建与资产工具：`build_stage1.py` / `build_stage2.py` 整包构建，`assets/verify_*.gd` 资产验收，FBX/GLB 转换脚本 |
| `docs/` | 设计与验收文档：`architecture.md`、`stage-2-plan.md`、`humanoid-skeleton-spec.md`、`humanoid-action-runbook.md`、`v3-acceptance.md`（V3 侧验收记录）、`合并记录.md`（本文档的姊妹篇）；`reference/`（V3 原始实施计划原文）、`reports/`（stage-0/1/2 历史报告，只增不改，见 AGENTS.md）、`superpowers/plans/`（历史计划存档） |
| `build/` | 构建输出（gitignore，不入库） |
| `run-dev.cmd` | 本机一键启动（Godot + 服务 Python 路径注入）——**当前其中 Python 路径过期，见 §3.2** |
| `requirements-dev.txt` | 开发依赖版本：pytest 9.1.1 / pyinstaller 6.21.0 / pillow 13.0.0（服务本体不用第三方包） |
| `AGENTS.md` | 工程协作约定：仓库根、Godot/Python 版本、分支命名、文件所有权、验收命令、硬性禁止 |
| `README-stage0/1/2.md` | 历史阶段说明（stage2 面向便携包最终用户，不是开发入口） |
| `THIRD_PARTY_NOTICES.md` | 第三方素材/依赖声明 |

## 3. 怎么运行

### 3.1 引擎（Godot 4.7.2）

- Godot 可执行文件：`D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe`（**GODDOT 和 WORD 中间是两个空格**）。
- 用编辑器打开 `app/project.godot`，或命令行 `Godot.exe --path app`。
- worktree 首次打开会重新生成 `app/.godot` 导入缓存（约 1 分钟，不入库）；跑 headless 测试前先执行一次导入：
  ```bat
  "D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --editor --import --quit
  ```

### 3.2 一键启动 `run-dev.cmd`（注意：当前有一处过期路径）

`run-dev.cmd` 设置 `PET3D_PYTHON` 后用 Godot 启动 `app/`。但其中写死的是旧仓库根：

```bat
set "PET3D_PYTHON=D:\danjigame\3D-pet-gtpdo\.venv\Scripts\python.exe"   ← 旧路径，已不存在
```

仓库根已迁移到 `D:\danjigame\3D-pet\3D-pet-gtpdo`（`.venv` 在其下，实测存在）。所以现在双击 `run-dev.cmd` 会被 "Missing Python venv" 守卫拦截退出。处理：把这一行改成新仓库根路径（一行改动）。

为什么需要它：服务由 app 拉起，查找顺序在 `app/scripts/main.gd:88-91`——启动参数 `python` → 环境变量 `PET3D_PYTHON` → `res://../.venv/Scripts/python.exe`（即 worktree 根下的 `.venv`，本 worktree 没有）。所以从 worktree 跑完整 app，必须让前两条之一生效。服务本体只用标准库，任何 Python 3.13.14 都可以；`.venv` 主要给构建/测试用。

### 3.3 服务单独冒烟（不需要 Godot）

```bat
python -I service\main.py --data-dir %TEMP%\pet3d-data
```

往 stdin 写 JSON 行即可对话，例如 hello：

```json
{"version":1,"id":"1","type":"hello","payload":{"protocol_version":1}}
```

服务回 `ready` 帧（含 settings 全量、features 能力表、personas）。40 条命令清单可枚举：

```bat
python -I -c "import sys; sys.path.insert(0,'.'); from service.commands import HANDLERS; print(sorted(HANDLERS))"
```

### 3.4 数据目录（运行时数据落在哪里）

服务的持久化数据（设置、聊天记录、记忆数据库）不在仓库里，在用户目录，且**随启动方式不同而不同**：

| 启动方式 | 数据目录 |
|---|---|
| `run-dev.cmd`（app + 服务开发启动） | `%LOCALAPPDATA%\Pet3D-v3-dev`（run-dev.cmd 写死 `--data-dir` 传入） |
| Godot app 不带参数启动 | `%LOCALAPPDATA%\Pet3D`（`app/scripts/main.gd:33`），并把这个目录传给它拉起的服务 |
| 裸跑 `python -I service\main.py` 不带 `--data-dir` | `%LOCALAPPDATA%\Pet3D`（`service/main.py:32-34`） |

数据目录里实测是三件套：`settings.json`（设置，schema v4）、`pet.db`（SQLite，聊天记录/记忆）、`service.lock`（单实例锁）；本机 `%LOCALAPPDATA%\Pet3D` 已存在（含 2026-11-03 的 settings.json）。

指向一个新的 `--data-dir` 就是全新状态：`settings.json` 缺失时 `SettingsStore` 直接写默认值（`service/settings.py:496-522`），`pet.db` 重新建库，**不会自动迁移**旧目录的数据。排查"设置没生效/聊天记录丢了"时，先确认这次是哪种启动方式、数据落在了哪个目录。

### 3.5 测试

Godot headless（`--path` 指向 `app`，输出形如 `STAGE2_LIBRARY_TESTS checks=27 failures=0`）。**下面的清单只是合并相关的子集（`app/tests/` 共 17 个 `.gd`）；整包门禁实际跑什么以 `tools/build_stage2.py:117-129` 为准**：pytest（:117）+ :120 列出的 11 个 godot 用例（比下表多 `test_stage2_controllers`、`test_godette_performance`、`test_godette_lifecycle`）+ `test_godette_preview`（:124，带渲染）+ `test_godette_review_regressions`（:125）+ baseline/runtime 资产回归与 poses 渲染验收（:118-119、:129）。其中 `test_v3_core` / `test_v3_panels` 目前**不在** stage2 门禁内（遗留问题 4）。

```bat
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_protocol.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_app_settings.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_windows.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_service_client.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_chat_framework.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_stage2_libraries.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_stage2_controllers.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_stage2_character_view.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_stage2_character_catalog.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_godette_performance.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_godette_lifecycle.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_v3_core.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_v3_panels.gd
```

Python（3.13.14）：

```bat
python -m pytest tests/service tests/assets -q
```

整包构建（慢，含原生编译、PyInstaller、Godot 导出、整包验收）：

```bat
python -B tools/build_stage2.py
```

纪律（AGENTS.md §5）：局部改动只跑最相关的聚焦用例；不要拿"整包构建通过"代替聚焦测试，也不要拿聚焦测试冒充整包验收。

## 4. 本次合并验证状态（逐条）

标注含义：**✅** = 撰写文档时在 HEAD `fbf0eec` 上亲自重跑/复核；**📋** = 合并运行时的实测记录（当时在合并提交 `7fc92f6` 上完成，撰写时未重跑）；**❌** = 未验证。

| # | 项目 | 状态 | 结果 |
|---|---|---|---|
| 1 | Godot 引擎测试 6 项 | ✅ | `STAGE2_LIBRARY_TESTS` 27/0、`STAGE2_CHARACTER_VIEW_TESTS` 25/0（`[character] loaded id=godette bones=69 springs=1`）、`STAGE2_CATALOG_TESTS` 3/0、`CHAT_FRAMEWORK_TESTS` 18/0、`V3_CORE_TESTS` 105/0、`V3_PANELS_TESTS` 87/0 |
| 2 | pytest `tests/service tests/assets` | ✅ | **319 passed, 2 skipped**（skip 原因：`tests/service/test_ai_chat.py:907` 非 Windows 行为、`tests/service/test_build.py:16` 需 `--build-service` 才跑真实构建） |
| 3 | 服务 stdio 冒烟 | ✅ | hello→ready 153 ms（features 含 autonomy/chat/diag/expression/growth/memory/state/usage，personas=[godette]）；settings.get / state.get / expression.phrases.list / autonomy.get / chat.history 全部 result；chat.send「你好」→ accepted{request_id, conversation_id} → chat.done 0.11 s（本地路由，无需 API key）→ chat.history 2 条（user+assistant）；shutdown 后干净退出（exit 0） |
| 4 | 命令注册 | ✅ | `HANDLERS` 枚举 **40 条**，任务点名的 ai / memory / expression / behavior(经 autonomy 消费) / state / features 聚合全部接入 |
| 5 | 65 动作三方一致 | ✅ | `godette.tres` 65 个 resource_name ↔ `source/` 65 个 GLB ↔ `humanoid_core.json` 65 个 action，双向差集全空、无重复；`godette.json` 的 65 个动画全部存在于 tres |
| 6 | 人格/台词加载 | ✅ | `load_card('godette')` → source=card、6 条 boundaries；`public_catalog()`=[godette]；`load_phrases()` → 10 条 4 类 |
| 7 | 协议对齐抽查 | ✅ | 信封 version==1 双侧一致；chat 超时 40s（`service_client.gd:73-74` = `chat_session.gd:19`）；文本上限 8000；`chat.done` 负载键（`orchestrator.py:494-508`）与消费端 `_finish`（`chat_session.gd:203-223`）逐键一致；`autonomy.bubble` 双侧处理（`service_client.gd:184`、`chat_session.gd:135`） |
| 8 | Alicia 数据合格性 | ✅（数据层面） | `characters/alicia.json` 与 `app/assets_data/alicia.json` 均为合格 v2 契约（schema_version=2、65 animations）；但 `alicia.tres` 不存在、目录未放行（见 §5.1） |
| 9 | 设置 v1/v2/v3→v4 迁移 | 📋 | 合并运行时实测三种旧文档全部迁移成功、警告文案正确；迁移代码 `settings.py:440` 起撰写时核对过 |
| 10 | action_preview 引擎级 65/65 | 📋 | 合并运行时以临时脚本实测（count=65 labels=65，全部存在于 tres）；静态代码 `_load_actions()`（`action_preview.gd:135-148`）撰写时核对过 |
| 11 | shutdown 的 0xC0000005 | ✅（复现） | 发 shutdown 后**不关 stdin**：stderr 打 `Fatal Python error: _enter_buffered_busy`，退出码 3221225477；立即关 stdin 则干净退 0。详见合并记录遗留问题 2 |
| 12 | DeepSeek 真实链路（流式/取消） | ❌ | 需要 API key，合并线未跑；仅 v3 侧 `docs/v3-acceptance.md` 有当时记录 |
| 13 | 整包构建 `tools/build_stage2.py` | ❌ | 本次未跑（含原生编译与导出，耗时长） |
| 14 | `run-dev.cmd` 一键启动 | ❌（已知坏） | 原因与修法见 §3.2 与合并记录遗留问题 5 |

合并运行时（`7fc92f6` 上）以上同一组 Godot 测试同样全绿，数字与本表一致。

## 5. 已知边界

1. **Alicia 数据已并入，但运行目录只放行 godette**。契约与角色配置均合格（见上表 #8），但 `service/character_catalog.py:2` 的 `ALLOWED_CHARACTER_IDS` 只有 `'godette'`；`app/assets_data/characters.json` 只列 godette；`app/resources/animations/alicia.tres` 不存在（`app/assets_data/alicia.json` 指向它；模型文件 `app/models/alicia.vrm` 本身存在，缺的只是这个动画库）；`app/export_presets.cfg` 明确排除 alicia 资产；存量设置里 `character_id=alicia` 会被静默迁移为 godette（`service/settings.py:120` `LEGACY_CHARACTERS`），`settings.update` 直接传 alicia 会被 validate 拒绝。启用 Alicia 需要三件事：烘焙 `alicia.tres`、放开目录、更新 characters.json 与导出过滤——具体顺序见合并记录"下一步"。
2. **向量检索需要支持 embeddings 的端点**。`ai.embedding` 默认 `enabled=false`、model 为空（ready 帧实测）；记忆索引相关命令（`memory.index.*`）存在，但未配置 embeddings 端点时向量检索不可用。
3. **主动行为 / 成长 / 记忆压缩默认关闭**。ready 帧实测默认值：`autonomy.enabled=false`、`growth.enabled=false`、`memory.compaction.enabled=false`（台词库 `expression.phrases_enabled` 默认开）。
4. **聊天线最多触发旧 7 动作**。运行时库有 65 动作，但 AI 管线的动作枚举、本地路由和前端消费仍限旧集合——不会报错，但 65 动作能力对聊天链路没接通。这是本次合并最大的"看起来合了、其实没打通"，详见 `docs/合并记录.md` 遗留问题 1。
5. **仓库根已迁移**。主仓库 `.git` 现位于 `D:\danjigame\3D-pet\3D-pet-gtpdo`；本分支是其 worktree（`D:\danjigame\3D-pet\wt\merge-v3-actions`）。旧路径 `D:\danjigame\3D-pet-gtpdo` 已不存在，`run-dev.cmd` 和 `AGENTS.md` 都还没跟上（§3.2、§1"以哪份文档为准"、合并记录遗留问题 5/6）。
