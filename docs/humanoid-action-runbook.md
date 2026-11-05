# humanoid_core 动作管线操作手册（AI 执行版）

> 面向读者：任何接手本仓库的 AI agent 或开发者。
> 目标：**照抄命令即可完成任务**。本文所有路径均为绝对或仓库相对路径，所有命令都写明期望输出。
> 如果你只读一节，读 [§3 任务配方](#3-任务配方) 和 [§5 不变量](#5-不变量必须始终成立)。

最后更新：2026-11-05（对应动作库 65 个动作、2 个角色契约）。

---

## 0. 环境常量

执行任何命令前，把下面这些值当作常量，**不要猜**。

| 名称 | 值 |
|---|---|
| 仓库根 | `D:\danjigame\3D-pet\wt\unified-skeleton`（`feat/unified-skeleton-contract` 分支） |
| 上游仓库 | `D:\danjigame\3D-pet-gtpdo`（`main`，**只读参照，不要改**） |
| Godot 控制台 | `D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe`（注意 `GODDOT` 与 `WORD` 之间是**两个空格**） |
| Godot 版本 | 4.7.2 stable |
| Blender | `D:\steam\steamapps\common\Blender\blender.exe`（5.2.3 LTS） |
| Python | `<仓库根>\.venv\Scripts\python.exe`（3.13.14，`build_stage2.py` 会校验版本） |
| 动作源目录 | `D:\danjigame\模型all\pet动作模型`（`convert_stage2.py` 的 `SOURCE_ROOT` 常量） |
| 动态库输出 | `<仓库根>\build\stage2\`（git 忽略） |
| 运行库输出 | `<仓库根>\app\resources\animations\`（入库） |

下文用 `<ROOT>` 代表仓库根。所有命令都在 `<ROOT>` 下执行。

---

## 1. 单一事实源与数据流

```
D:\danjigame\模型all\pet动作模型\*.fbx          ← 素材（不入库）
        │
        │  ①convert_stage2.py（Blender，增量）
        ▼
app/resources/animations/source/<id>.glb        ← 归一化后的源骨架动作（入库）
        │
        │  ②bake_stage2.gd（Godot headless，全量）
        ▼
build/stage2/<character>.tres                   ← 每个角色一份烘焙库（git 忽略）
        │
        │  ③发布 = 复制到 app/resources/animations/<character>.tres
        ▼
运行时 AnimationLibrary
        ▲
        │  ④prepare_stage2.py
characters/<id>.json  ──────────────────────►  app/assets_data/<id>.json
（开发契约，含 rest/表情等）                     （运行契约，剥离 DEV_FIELDS）
```

**关键事实（照此判断改哪里）：**

| 数据 | 唯一来源 | 谁读它 |
|---|---|---|
| 动作清单与顺序 | `animations/humanoid_core/manifest.json` 的 `actions` | convert、bake、prepare、预览 UI、构建脚本、包校验 |
| 动作显示名（中文） | 同上，每条动作的 `label` 字段 | `app/scripts/ui/action_preview.gd` |
| 源动作参数（取哪个 FBX、怎么剥根运动） | 同上，`source` / `root_motion` | convert |
| 角色骨骼映射 | `characters/<id>.json` 的 `bone_map` / `source_bone_map` / `source_to_target_yaw_degrees` | bake |
| 运行时角色契约 | `app/assets_data/<id>.json`（**生成物，不要手改**） | `character_view.gd` |
| 交付哪些角色 | `PACKAGE_CHARACTER_IDS`（**三处**，见 §6） | prepare / render / build |
| 转换缓存凭据 | `animations/humanoid_core/source_manifest.json`（生成物，入库） | convert 的增量判断 |

> **生成物白名单**（只允许脚本写，禁止手改）：
> `app/assets_data/*.json`、`animations/humanoid_core/source_manifest.json`、`build/**`、`*.glb`、`*.tres`。

---

## 2. 数据结构规范

### 2.1 manifest 动作条目

文件：`animations/humanoid_core/manifest.json`（`schema_version: 1`，`id: "humanoid_core"`）。

```json
"dancing": {
  "mode": "once",
  "source": "Dancing.fbx",
  "blend_seconds": 0.15,
  "root_motion": "none",
  "semantic_status": "verified",
  "label": "跳舞"
}
```

| 字段 | 必需 | 取值 | 语义 |
|---|---|---|---|
| `mode` | ✓ | `loop` / `once` / `hold` | 循环播放 / 播完回待机 / 保持到显式释放 |
| `source` | ✓（派生条目除外） | FBX 文件名 | 必须在 `D:\danjigame\模型all\pet动作模型` 中存在，**大小写与空格必须完全一致** |
| `root_motion` | ✓ | `none` / `vertical_only` / `preserve` | 转换时 Hips 位移的剥离策略：冻结 XYZ / 只保留 Y / 全保留 |
| `blend_seconds` | ✓ | 秒（浮点） | 切入过渡时长；新增动作统一 `0.15` |
| `semantic_status` | ✓ | `verified` / `candidate` / `authored` | 语义核对状态；FBX 直转填 `verified`，派生动作填 `authored` |
| `label` | 建议 | 中文显示名 | 缺失不报错，但预览界面会显示英文 id |
| `derived` | 仅派生 | `turn_step` / `suspended` / `landing_cushion` | 由脚本程序化生成，不对应独立 FBX |
| `note` | 可选 | 字符串 | id 冲突或源文件名非 ASCII 时的说明 |

**id 命名规则**（id 同时是文件名和动画轨道名，违规会产生难查的错误）：

1. 取 `source` 文件名主干；
2. 转 ASCII 小写；
3. 非 `[a-z0-9]` 字符转 `_`，连续 `_` 合并，首尾 `_` 去掉；
4. 与已有 id 冲突且源文件不同 → 加 `_2` 后缀；
5. 主干全是中文等非 ASCII → 用 `motion_<序号>`，原名写进 `note`。

**顺序约定**：`idle, walk, turn, wave, pickup, fall, recover` 这 7 条永远排在最前且**逐字不改**；新增条目按 `source` 文件名排序追加在后。顺序决定预览下拉框顺序和 `action_bounds` 生成顺序。

### 2.2 角色契约

文件：`characters/<id>.json`，`schema_version >= 2` 且 `motion_library` 以 `humanoid_core` 开头才被识别为契约。

必需的骨骼相关字段：

| 字段 | 说明 |
|---|---|
| `bone_map` | canonical 45 键 → 模型实际骨名 |
| `source_bone_map` | canonical 45 键 → 源 FBX 骨名（`mixamorig1_*`，冒号被 Godot 转成下划线） |
| `source_to_target_yaw_degrees` | 源到目标朝向偏角；VRM 角色为 `180` |
| `model_path` / `skeleton_path` | 例：`res://models/godette.vrm` / `Skeleton3D` |

**不要手改** `animations` / `action_map` / `action_bounds` —— `prepare_stage2.py` 每次都会按 manifest 全量重写这三个字段（含 `action_bounds` 中缺失项继承该契约首个包围盒）。

### 2.3 运行时契约（生成物）

`app/assets_data/<id>.json` = 开发契约减去 `DEV_FIELDS`：
```
DEV_FIELDS = {rest, inspected_meshes, source, import_options}
```
另外 `expressions` 会被重算：仅当 `capabilities.expressions == true` 且 `source_format == "vrm0"` 时解析 VRM 内嵌 blendshape，否则原样透传。

---

## 3. 任务配方

每个配方都给出：**改什么 → 跑什么 → 期望看到什么**。命令全部在 `<ROOT>` 下、用 `<ROOT>\.venv\Scripts\python.exe` 执行。

### R1. 改动作中文名（最轻）

**改**：`animations/humanoid_core/manifest.json` 里该动作的 `label`。

**跑**：
```bat
.venv\Scripts\python.exe -B tools\assets\prepare_stage2.py
```

**期望**：无输出（成功即静默）。验证：
```bat
.venv\Scripts\python.exe -c "import json;d=json.load(open('app/assets_data/humanoid_core.json',encoding='utf-8'));print(len(d['actions']),sum(1 for v in d['actions'].values() if v.get('label')))"
```
应输出两数相等（如 `65 65`）。

**不需要**重转、重烘、重打包。

---

### R2. 改播放模式或过渡时长

**改**：manifest 里该动作的 `mode` 或 `blend_seconds`。

**跑**（顺序不可颠倒）：
```bat
.venv\Scripts\python.exe -B tools\assets\prepare_stage2.py
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script tools\assets\bake_stage2.gd
.venv\Scripts\python.exe -c "import shutil;shutil.copy2('build/stage2/godette.tres','app/resources/animations/godette.tres')"
```

**期望**：bake 输出含 `STAGE2_BAKED`。

**不需要**重转 FBX（`mode`/`blend_seconds` 不参与转换）。`root_motion` **会**触发重转，见 R4。

---

### R3. 新增一个动作（最常见）

**前提**：FBX 已放入 `D:\danjigame\模型all\pet动作模型\`。

**步骤 1 — 加 manifest 条目**，字段按 §2.1，最小示例：
```json
"dancing": {"mode":"once","source":"Dancing.fbx","blend_seconds":0.15,"root_motion":"none","semantic_status":"verified","label":"跳舞"}
```

**步骤 2 — 一条命令跑完整链路**
```bat
.venv\Scripts\python.exe -B tools\build_stage2.py --rebuild-assets
```

它会依次执行：prepare → Blender 增量转换 → 原生编译 → Godot 导入 → **清空并全量重烘** → 暂存基线对照 → 发布所有 `.tres` → pytest → 各 Godot 测试 → 预览渲染 → 姿势矩阵 → 非包角色资产线 → 服务打包 → 冻结测试 → 导出 → 包校验 → 视觉验收 → 桌面验收。

**期望**：最后一行 `GODETTE_STAGE2_BUILD_OK <路径>`。

> ⚠️ **这一步很慢**。`--rebuild-assets` 会**删除 `build/stage2/*.tres` 并全量重烘所有角色**，不是只烘新动作。时间随动作总数线性增长。

**只想快速看效果（不做整包验收）时**，用分步版：
```bat
.venv\Scripts\python.exe -B tools\assets\prepare_stage2.py
"D:\steam\steamapps\common\Blender\blender.exe" --background --factory-startup --python-exit-code 1 --python tools\assets\convert_stage2.py
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --editor --import --quit
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script tools\assets\bake_stage2.gd
.venv\Scripts\python.exe -c "import shutil;shutil.copy2('build/stage2/godette.tres','app/resources/animations/godette.tres')"
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --path app -- --review
```
分别期望看到：`PET3D_STAGE2_CONVERTED`、`STAGE2_BAKED`；最后一条会弹出预览窗口（**不加 `--headless`**，原因见 §7.1）。

---

### R4. 换某个动作的源 FBX

**改**：manifest 里该动作的 `source`。**id 保持不变**（文件名由 id 决定）。

**跑**：同 R3（`--rebuild-assets` 或分步版）。

**为什么能自动重转**：`convert_stage2.py::cached_record`（第 238–259 行）只在以下全部成立时复用缓存——
1. `source_file` 与 manifest 一致；
2. `source` 文件 SHA-256 与 `source_manifest.json` 记录一致；
3. GLB `asset.extras.root_motion` 与 manifest 的 `root_motion` 一致；
4. `build/stage2/motions/<id>.glb` 与 `app/resources/animations/source/<id>.glb` 逐字节相同；
5. 记录的 `output_sha256` 与实际产物相同。

任一条不成立即重转。**所以改了 `source` 或 `root_motion` 无需手动删缓存**；手改过 GLB 也会被第 5 条检出。

---

### R5. 删除一个动作

**前提检查**：不能删这 7 个基础动作，运行时逻辑硬依赖：

| 动作 | 依赖点 |
|---|---|
| `idle` | 兜底状态、`PetActionController._start_idle()` |
| `wave` | 点击身体问候、托盘"挥手" |
| `turn` | 转身朝向控制、预览"向左转" |
| `pickup` | 拖拽提起（hold 模式） |
| `fall` / `recover` | 下落/恢复状态机 |
| `walk` | 托盘与行走状态预留 |

**步骤**：
1. 从 manifest `actions` 删除该条；
2. 删除文件：`app/resources/animations/source/<id>.glb` 及其 `.import`，`build/stage2/motions/<id>.glb`（若存在）；
3. 重跑 R3 的整链。

**期望**：`prepare` 会把所有契约的 `animations`/`action_map`/`action_bounds` 收缩到新的动作集合。若忘了删 GLB，`pytest` 的 `test_manifest_actions_have_source_glbs` 不会报错（它校验"清单里的都有文件"，不校验"多余的"），但 `source_manifest.json` 会残留旧记录——以重跑 convert 后的输出为准。

---

### R6. 增加一个角色

**不进交付包（只做资产线验证）**：
1. 模型放 `app/models/<id>.vrm` 或 `.glb`；
2. 新建 `characters/<id>.json`：复制 `characters/alicia.json` 为模板，改 `id`、`model_path`、`skeleton_path`、`bone_map`、`source_bone_map`、`source_to_target_yaw_degrees`、`display`、`author`/`publishable`；
3. 跑 R3 整链。

**期望**：`build_stage2.py` 自动发现该契约并烘焙；`build/stage2/<id>.tres` 生成；非包角色资产线自动运行 `verify_character_assets.gd`，输出 `CHARACTER_ASSETS character=<id> frames=<动作数×10> failures=0`。

**进交付包**（额外 6 处，缺一不可）：
1. `tools/assets/prepare_stage2.py:14` 的 `PACKAGE_CHARACTER_IDS`；
2. `tools/assets/render_recovery.gd:6` 的 `PACKAGE_CHARACTER_IDS`；
3. `tools/build_stage2.py:26` 的 `PACKAGE_CHARACTER_IDS`；
4. `app/export_presets.cfg`：`include_filter` 增加 `assets_data/<id>.json`，`exclude_filter` 移除该角色的排除项；
5. `service/character_catalog.py` 的 `ALLOWED_CHARACTER_IDS`；
6. `app/scripts/main.gd` 的角色索引钉死逻辑（当前恒取第 0 个）与 `app/scripts/ui/settings_window.gd` 的角色选择控件（当前刻意不存在）。

> `app/assets_data/characters.json` 不用手改——它由 `prepare` 按 `PACKAGE_CHARACTER_IDS` 生成。

---

### R7. 全新环境重建

```bat
git -C <ROOT> status --porcelain
<base python 3.13.14> -m venv <ROOT>\.venv
<ROOT>\.venv\Scripts\python.exe -m pip install -r <ROOT>\requirements-dev.txt
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path <ROOT>\app --editor --import --quit
<ROOT>\.venv\Scripts\python.exe -B <ROOT>\tools\build_stage2.py --rebuild-assets
```
首次导入约 1 分钟，属正常。`app\.godot\` 不入库。

---

## 4. 一次改动的标准验证序列

按顺序执行，任一步失败就停下来修，**不要跳过**。

| # | 命令 | 期望 |
|---|---|---|
| 1 | `.venv\Scripts\python.exe -B -m pytest tests/assets tests/service -q` | `95 passed, 1 skipped`（数字随测试增减，关键是 **0 failed**） |
| 2 | `godot --headless --path app --script tools/assets/verify_recovery.gd` | `GODETTE_REGRESSION failures=0`（原版逐帧对照，**最重要的回归锚**） |
| 3 | `godot --path app --script tools/assets/verify_character_assets.gd -- --character=alicia`（**不加 `--headless`**） | `CHARACTER_ASSETS character=alicia frames=650 failures=0` |
| 4 | `godot --path app --script tools/assets/render_recovery.gd`（**不加 `--headless`**） | `GODETTE_RENDER frames=<65×10> framing_failures=0` |
| 5 | `.venv\Scripts\python.exe -B tools\build_stage2.py` | `GODETTE_STAGE2_BUILD_OK` |
| 6 | 人工/AI 查看 `build\stage2\reports\godette-poses\matrix.png` | 每列一个动作，非空、不被裁切 |

第 2 步是唯一能证明"没有把原来能用的动作搞坏"的检查，**任何改动都必须让它保持 `failures=0`**。

---

## 5. 不变量（必须始终成立）

改动后请逐条自查；带 ★ 的由自动化测试强制。

| # | 不变量 | 强制者 |
|---|---|---|
| 1 ★ | 每个 manifest 动作都有 `app/resources/animations/source/<id>.glb` | `test_manifest_actions_have_source_glbs` |
| 2 ★ | 每个契约的 `bone_map` 与 `source_bone_map` 都是同一套 45 键 | `test_humanoid_contracts_match_canonical_bones_and_manifest` |
| 3 ★ | 每个契约的 `animations` == `action_map` == `action_bounds` == manifest 全部动作 | 同上（**等于**，不是子集） |
| 4 ★ | `app/assets_data/characters.json` 恰为 `["godette"]` | `test_runtime_catalog_lists_only_godette` |
| 5 ★ | `export_presets.cfg` 的 `exclude_filter` 含 alicia 相关项 | `test_export_presets_exclude_alicia` |
| 6 ★ | `prepare_stage2.py` / `convert_stage2.py` / `bake_stage2.gd` 文本**不得出现** `alicia`、`soldier`、`work-pet` | `test_active_asset_tools_do_not_read_legacy_project` |
| 7 ★ | `source_bone_map` 的每个值都能在**每个**源 GLB 的节点里找到 | `test_source_bone_maps_resolve_in_source_glbs` |
| 8 | 每个 GLB 都是单动画、`SourceRig` 单位缩放、单位四元数、+Y 上、+Z 前 | `test_stage2_sources.py` |
| 9 | 7 个基础动作（idle/walk/turn/wave/pickup/fall/recover）存在且语义不变 | `test_action_semantics_and_cache_baseline` |
| 10 | 原版逐帧对照 `failures=0` | `verify_recovery.gd`（构建第 2 步） |

第 6 条常被踩：**不要在三个工具脚本里写死任何角色名**。泛化必须靠"扫描 `characters/*.json` + 读 manifest"，而不是加 `if id == "xxx"`。

---

## 6. 成本与规模阈值

| 项目 | 当前值（65 动作 / 2 角色） | 增长方式 |
|---|---|---|
| 单角色动画库 | 32.8 MB（`godette.tres`） | ≈ +0.5 MB / 动作 |
| 导出包 | 随之增大 | 同上 |
| 转换 | 每个新 FBX 数秒 | 线性，且只转新增/变更 |
| **烘焙** | 全量重跑，最重的一步 | **线性**，加 1 个动作也要全量重烘 |
| 渲染验收 | 每角色 动作数 × 10 帧 | 线性 |

**架构阈值建议**：
- 动作数 < 100：维持现状（单库 + 全量重烘）。
- 100–200：应按"常用动作 / 长尾动作"分库，或让 bake 支持按 id 增量。
- > 200：单文件 `.tres` 会逼近 100 MB，需改为按动作拆分的 AnimationLibrary 或运行时动态加载。

---

## 7. 已知故障与排查

### 7.1 渲染脚本挂死一小时（`frame_post_draw` 永不触发）

**现象**：任何含 `await RenderingServer.frame_post_draw` 的脚本在 `--headless` 下永久等待，直到外层超时。
**受影响**：`verify_character_assets.gd`、`render_recovery.gd`、`test_godette_preview.gd`。
**规则**：**凡是要渲染的脚本，一律不加 `--headless`**。`build_stage2.py` 用 `godot(..., render=True)` 表达这一点（`render=True` → 不追加 `--headless`）。
**判断**：若某步超过 15 分钟无新产物，先看目标目录时间戳是否停止增长。

### 7.2 原生 `lifetime_test` 失败：热键不可用

**现象**：`FAIL: Ctrl+Alt+Q available before fixture configures` / `Win32=0`。
**原因**：有残留的 `Godot*.exe`（或 `Pet3D.exe`）正持有全局热键——通常是上一次超时被掐断后没退出的窗口进程。
**处理**：
```bat
tasklist /FO CSV | findstr /I "godot pet3d"
taskkill /F /PID <上一步列出的 PID>
```
确认无残留后重跑。

### 7.3 契约动作数与清单不一致（"全量接入没生效"）

**现象**：manifest 有 65 条，但 `app/assets_data/godette.json` 只有 7 条；预览里也是 7 个。
**原因**：改了 manifest 但**没跑 `prepare_stage2.py`**；或 prepare 的 `sync_actions` 被绕过。
**处理**：跑 prepare，然后核对：
```bat
.venv\Scripts\python.exe -c "import json;m=json.load(open('animations/humanoid_core/manifest.json',encoding='utf-8'));c=json.load(open('app/assets_data/godette.json',encoding='utf-8'));print(len(m['actions']),len(c['animations']),len(c['action_bounds']))"
```
三个数字必须相等。

### 7.4 非包角色库未发布 → 构建因 ERROR 行失败

**现象**：`poses failed`，日志含 `Cannot open file 'res://resources/animations/alicia.tres'`，随后 `ERROR:` 行让 `run()` 判定失败（即使退出码为 0）。
**原因**：`build_stage2.py` 只在 `--rebuild-assets` 分支里把 `build/stage2/*.tres` 复制到 `app/resources/animations/`。不带该参数跑时，非包角色的库可能不存在。
**处理**：手动发布或带 `--rebuild-assets` 重跑：
```bat
.venv\Scripts\python.exe -c "import shutil,pathlib;[shutil.copy2(p,pathlib.Path('app/resources/animations')/p.name) for p in pathlib.Path('build/stage2').glob('*.tres')]"
```

### 7.5 新 GLB 报"资源不存在"

**原因**：没跑 Godot 导入，`.import` 与 `.godot` 缓存未生成。
**处理**：`--headless --path app --editor --import --quit`。

### 7.6 预览下拉框显示英文 id

**原因**：该动作缺 `label`。
**处理**：补 `label` 后跑 R1。

---

## 8. 禁止事项

1. 不要手改任何生成物：`app/assets_data/*.json`、`source_manifest.json`、`build/**`、`*.glb`、`*.tres`。
2. 不要手改契约的 `animations` / `action_map` / `action_bounds`（prepare 会覆盖）。
3. 不要在 `prepare_stage2.py`、`convert_stage2.py`、`bake_stage2.gd` 里出现具体角色名（`alicia`/`soldier`/`work-pet`），测试会红。
4. 不要给渲染脚本加 `--headless`。
5. 不要删除 7 个基础动作。
6. 不要在 `app/project.godot`、`app/main.tscn`、`app/scripts/main.gd`、`app/export_presets.cfg`、`docs/stage-2-plan.md` 上做与任务无关的改动（这 5 个是"热点文件"，冲突成本最高）。
7. 不要把 `build/`、`dist/`、`.godot/`、`experiments/` 提交进库（`.gitignore` 已覆盖）。
8. 不要把 FBX/模型的授权状态当作已核验：动作源授权未核验，仅限本地技术验证。

---

## 9. 快速索引

| 我想…… | 看 |
|---|---|
| 只改中文名 | R1 |
| 让某动作循环播放 | R2 |
| 加一个新动作 | R3 |
| 换某动作的源文件 | R4 |
| 删动作 | R5 |
| 加角色 | R6 |
| 从零重建 | R7 |
| 确认没搞坏原有动作 | §4 第 2 步（`GODETTE_REGRESSION failures=0`） |
| 排查构建失败 | §7 |
| 判断能不能加更多动作 | §6 |
