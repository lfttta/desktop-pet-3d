# humanoid_core 人形骨骼与动作规范

更新日期：2026-11-05。适用范围：`animations/humanoid_core/` 动作清单、`characters/` 下挂接该动作库的角色契约，以及相应的离线烘焙流程。本文是规范性文档；文中引用的实现与数据文件是当前依据，规范与其冲突时以本文为目标状态并同步修订实现。

## 1. canonical 45 骨 profile 与必需子集

canonical 骨架使用 SkeletonProfileHumanoid 标准命名（键名即标准名，值为具体模型中的骨骼名）。基准集合取自 `characters/godette.json` 的 `bone_map`（45 键）；`characters/alicia.json` 的 `bone_map` 与 `source_bone_map` 经核对为同一键集合（键顺序允许不同）。

45 根标准骨：

- 脊柱链：`Hips`、`Spine`、`Chest`、`Neck`、`Head`
- 左臂：`LeftShoulder`、`LeftUpperArm`、`LeftLowerArm`、`LeftHand`
- 右臂：`RightShoulder`、`RightUpperArm`、`RightLowerArm`、`RightHand`
- 左腿：`LeftUpperLeg`、`LeftLowerLeg`、`LeftFoot`、`LeftToes`
- 右腿：`RightUpperLeg`、`RightLowerLeg`、`RightFoot`、`RightToes`
- 左手 12 节：`LeftThumb{Metacarpal,Proximal,Distal}`、`LeftIndex{Proximal,Intermediate,Distal}`、`LeftMiddle{Proximal,Intermediate,Distal}`、`LeftLittle{Proximal,Intermediate,Distal}`
- 右手 12 节：`RightThumb{Metacarpal,Proximal,Distal}`、`RightIndex{Proximal,Intermediate,Distal}`、`RightMiddle{Proximal,Intermediate,Distal}`、`RightLittle{Proximal,Intermediate,Distal}`

计数：5 + 4×2 + 4×2 + 12×2 = 45（每只手 4 组手指 × 3 节 = 12，双手共 24；无无名指组）。

必需子集（一个动作源只有覆盖以下 9 根骨才算可用的全身动作，烘焙与重定向按此判定）：

`Hips`、`Spine`、`Head`、`LeftUpperArm`、`RightUpperArm`、`LeftUpperLeg`、`RightUpperLeg`、`LeftFoot`、`RightFoot`

手指、`Toes`、`Shoulder`、`Chest`、`Neck` 等为 profile 的一部分但不在必需子集内：源动作缺这些轨道时，对应骨骼保持目标 rest，不得报错。

## 2. 角色契约：必需段与可选段

`characters/*.json` 中 `schema_version >= 2` 且 `motion_library` 以 `humanoid_core` 开头的文件即本规范的角色契约（当前为 `godette.json` 与 `alicia.json`）。

必需段（两份现有契约共有；其中 `bone_map`/`source_bone_map` 的 45 键集合、`source_to_target_yaw_degrees` 与动作集合一致性由 `tests/assets/test_unified_contracts.py` 强制，其余为契约语义要求）：

| 段 | 说明 |
|---|---|
| `schema_version` | 整数，契约结构版本，当前 ≥ 2 |
| `id` | 角色 ID，同时是运行目录、模型与库命名的键 |
| `model_path` / `skeleton_path` | 模型与骨架资源路径 |
| `skeleton_profile` | 固定 `humanoid`（对齐第 1 节 profile） |
| `bone_map` | 标准 45 骨 → 模型骨名映射，键集合必须与 godette.json 基准一致 |
| `source_bone_map` | 源（烘焙）侧 45 骨映射，键集合与 `bone_map` 相同 |
| `source_to_target_yaw_degrees` | 源骨架到目标模型的朝向偏角（见第 4 节） |
| `motion_library` | 固定 `humanoid_core@<version>`，指向 `animations/humanoid_core/` |
| `animation_library` / `animation_library_name` | 该角色组装后的 Godot 动画库资源 |
| `action_map` | 动作 ID → 库内动画名；键集合等于 manifest `actions`（由 `prepare_stage2.py` 同步），且与 `animations` 集合相同 |
| `animations` | 库内动画名列表；集合与 `action_map`、`action_bounds` 键集相同，三者均等于 manifest `actions` |
| `foot_anchor` / `default_height` / `action_bounds` / `locomotion` | 落点锚、身高与每动作包围盒等运行参数 |
| `capabilities` | 能力开关（expressions/blink/mouth/look/secondary_motion），缺的能力必须显式为 false，不得伪造 |
| `display` / `source` / `publishable` / `enabled_by_default` / `notes` | 展示、来源与发布元数据 |

可选段（能力数据，允许整段缺失；`alicia.json` 即无 `rest`、无 `inspected_meshes`，`expressions` 为空对象且 `capabilities.expressions=false`）：

- `rest`：rest 姿态快照（bones/motion_scale/path/transform），供姿态回归对照；
- `expressions`：表情 blendshape 绑定，仅当模型真实携带该元数据时填写；
- `inspected_meshes`：导入检查得到的网格 AABB 与 blendshape 清单，仅记录实测数据。

角色特有的附加数据（如 `hit_shapes`、`retarget_method`、`import_options`）按需保留，不影响契约判定。

运行时副本口径：`app/assets_data/<id>.json` 由 `tools/assets/prepare_stage2.py` 从仓库契约生成，按 `DEV_FIELDS`（`rest`、`inspected_meshes`、`source`、`import_options`）剥离开发字段并以 LF 写出，`alicia` 与 `godette` 同一口径。因此运行副本与 `characters/<id>.json` 不要求逐字节一致（`characters/alicia.json` 保留 `source`，运行副本没有）；验收以"运行副本 == prepare 对该契约的输出"为准。

动作集合的一致性按**相等**判定：`animations/humanoid_core/manifest.json` 是动作集合的唯一来源，`tools/assets/prepare_stage2.py::sync_actions` 在每次生成时把每个契约的 `animations`、`action_map`、`action_bounds` 重写为 manifest 的全量动作（契约中已调过的包围盒保留，新动作继承该契约的首个包围盒）。因此契约枚举的动作数恒等于 manifest 的动作数，`tests/assets/test_unified_contracts.py` 与 `tests/assets/test_stage2_contract.py` 均按相等断言。

> 历史说明：本规范早期版本允许契约只选取 manifest 的子集（当时 godette 与 alicia 各含 7 个基线动作）。全量动作接入后该口径作废——契约少声明一个动作，运行时就少暴露一个动作，属于缺陷而非裁剪。

动作的日常增删改流程见 [`docs/humanoid-action-runbook.md`](humanoid-action-runbook.md)。

## 3. 动作条目字段规范

`animations/humanoid_core/manifest.json`（`schema_version` 1，`id` `humanoid_core`）的 `actions` 为动作 ID → 条目。ID 规则：取 FBX 文件名主干，转 ASCII 小写，非 `[a-z0-9]` 字符转 `_`，连续 `_` 合一，首尾 `_` 去除；与既有 ID 冲突且源文件不同时加 `_2` 后缀并在 `note` 说明；主干完全非 ASCII 时用 `motion_<序号>` 并在 `note` 记录原名。

| 字段 | 类型 | 取值 | 说明 |
|---|---|---|---|
| `mode` | 枚举 | `loop` / `once` / `hold` | 循环、单次回待机、保持至释放。新条目按文件名主干判定：主干（大小写不敏感）含 `idle`/`walk`/`run`/`dance`/`breath` 为 `loop`，否则 `once`。注意按字面子串匹配：`Dancing`、`Dancing_Twerk`、`Hip_Hop_Dancing`、`Salsa_Dancing` 不含字面 `dance`，故为 `once`；`Macarena_Dance`、`Breakdance_Swipes` 含 `dance`，为 `loop` |
| `root_motion` | 枚举 | `none` / `vertical_only` / `preserve` | 烘焙时对 Hips 位移轨道的剥离策略（见第 4 节）。新条目：主干含 `walk`/`run`/`step` 为 `vertical_only`，否则 `none` |
| `source` | 字符串 | FBX 文件名 | 相对 `D:\danjigame\模型all\pet动作模型`；同一 FBX 可被多个条目引用 |
| `blend_seconds` | 数值 | 秒 | 动作切入混合时长；新增条目固定 0.15 |
| `semantic_status` | 枚举 | `verified` / `candidate` / `authored` | `verified`=直接来自 FBX 源并经语义核对；`candidate`=直接来自 FBX 源但尚未完成语义核对；`authored`=派生动作 |
| `derived` | 字符串 | 仅派生条目 | 派生规则名：`turn`→`turn_step`、`pickup`→`suspended`、`recover`→`landing_cushion`；非派生条目不得携带 |
| `note` | 字符串 | 可选 | 仅 ID 冲突或非 ASCII 主干时使用 |
| `label` | 字符串 | 中文显示名 | 预览界面下拉框的显示名，由 `app/scripts/ui/action_preview.gd` 读取；缺失不报错，但会回退显示英文 id |

现有 7 个条目（`idle`/`walk`/`turn`/`wave`/`pickup`/`fall`/`recover`）是历史基线，逐字保留不改写；新增条目排在其后，按源文件名排序。派生动作（turn/pickup/recover）不因新增 FBX 而新增条目。

## 4. 烘焙规则

离线烘焙把 FBX 源动作统一到 canonical 骨架，再重定向到各角色缓存；日常运行不依赖烘焙环境。

1. **source_to_target_yaw**：源骨架与目标模型朝向差固定写进契约的 `source_to_target_yaw_degrees`；`godette.json` 与 `alicia.json` 均为 180。离线重定向时在 `tools/assets/bake_stage2.gd:134` 以该角度构造偏角四元数，并施加到源世界旋转差（第 144、185 行）与髋部位移（第 199 行）；运行时不再逐帧旋转。
2. **rest 方向自动对齐**：按 `retarget_method`（godette.json）执行——"prebaked world rest rotation deltas with anatomical direction rest alignment; recomposed target local rotations; only hips translation; all other local offsets and scales retain target rest"。实现上按解剖方向对（`bake_stage2.gd:135-151`）计算源/目标 rest 方向差作为修正四元数，再与源世界旋转差、目标 world rest 合成为目标局部旋转（第 185 行）；目标仅 Hips 有位移，其余骨骼的局部偏移与缩放保持目标 rest（`docs/reports/stage-2/recovery.md` 第 18、29 行）。
3. **腿长比补偿**：每个角色保留原骨长（recovery.md 第 29 行），重定向时按源骨架与目标角色大腿/小腿的长度比计算缩放比（`bake_stage2.gd:152-160`），并把髋部位移按该比例补偿（第 199 行），使脚部落点与目标腿长匹配，避免悬脚与滑步；补偿在离线重定向中一次性完成，不进入运行时。
4. **30fps**：所有烘焙产物统一 30fps。转换条目记录源 fps（`tools/assets/convert_stage2.py:130`，`source_manifest.json` 中为 30.0）；派生条目按 `round(时长×30)` 定帧并显式 `scene.render.fps = 30`（`convert_stage2.py:174-177`）。
5. **仅旋转轨道 + Hips 位置轨道**：产物中每根骨只有旋转（Quaternion）轨道，位置轨道仅 Hips 一条并只做一次 motion_scale 归一化（recovery.md 第 18 行；`convert_stage2.py:64` 丢弃位移/旋转之外的通道）。Hips 位置轨道按条目 `root_motion` 剥离：`none` 冻结 XYZ、`vertical_only` 冻结 XZ 保留 Y、`preserve` 全保留（`convert_stage2.py:80-87`）。

## 5. 验收门槛分级

| 级别 | 对象 | 门槛 |
|---|---|---|
| 包内角色 | 随包交付、有原版回归基线的角色（当前为 godette） | 与原版逐帧对照：重建资产先写暂存输出，待机/挥手与原版逐帧骨骼对照不退化后才允许替换运行缓存（`README-stage2.md` 第 27 行；`docs/stage-2-plan.md` §4 集成门槛）；回归基线由 `tests/assets/test_asset_contract.py` 的 `app/motions/cache/godette.tres` 与 `app/models/godette.vrm` SHA256 固定 |
| 非包角色 | 契约就绪但未进包的角色（当前为 alicia） | ① 结构：本规范第 1、2 节的契约断言全部通过（`tests/assets/test_unified_contracts.py`）；② 姿态健全：烘焙后动作无髋部下沉、四肢折叠等 rest 破坏（recovery.md 第 18 行的故障形态为回归红线），逐帧采样校验骨骼变换有限且髋部高度落在该骨架 rest 高度推导出的区间内；③ 渲染矩阵：`tools/assets/verify_character_assets.gd -- --character=<id>` 渲染正/侧视 ×5 时刻 ×全量动作，要求每帧非空且 `failures=0` |

两级门槛都不引用旧多角色统计；历史三角色报告只作资料，不作门槛（`docs/stage-2-plan.md` §1）。
