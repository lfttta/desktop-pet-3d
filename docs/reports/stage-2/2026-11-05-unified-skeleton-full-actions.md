# 统一人形骨骼与全量动作接入验收

日期：2026-11-05。范围依据：用户在 2026-11-05 的决定——把 `D:\danjigame\模型all\pet动作模型` 下的**全部动作**接入统一动作库，并把 Alicia 作为第二个角色接入统一骨骼管线。交付包范围不变，仍只装 Godette。

执行位置：独立 worktree `D:\danjigame\3D-pet\wt\unified-skeleton`，分支 `feat/unified-skeleton-contract`，基线 `b5cba7a`。主仓库 `D:\danjigame\3D-pet-gtpdo` 的 `main` 及其工作区未修改，`experiments/` 只读引用。本分支未合并、未推送。

## 1. 范围与结论

- 动作库由 7 个扩充到 **65 个**（原 7 条逐字保留，新增 58 条）。
- 资产管线由"只认 godette 七动作"改为**清单 + 契约驱动**：新增角色或动作无需改脚本中的角色名或动作名。
- 交付包仍为 **Godette 单角色**；Alicia 仅作本地资产线验证，不进入导出包。
- 本机整包构建通过：`GODETTE_STAGE2_BUILD_OK`。

## 2. 动作清单摘要

`animations/humanoid_core/manifest.json` 共 **65** 条动作。

| 维度 | 分布 |
|---|---|
| 来源 | 62 条由 62 个 FBX 逐一转换（一一对应，无重复源）+ 3 条派生动作（turn/pickup/recover，程序化生成） |
| `mode` | `loop` 13、`once` 50、`hold` 2 |
| `root_motion` | `none` 56、`vertical_only` 7、`preserve` 2 |
| 显示名 | 65 条全部带 `label`（中文） |

新增条目的 `mode` 与 `root_motion` 由文件名关键词推断（含 `idle/walk/run/dance/breath` → loop；含 `walk/run/step` → vertical_only），**未逐条人工核对语义**，见第 6 节。

## 3. 管线改动

| 文件 | 改动 |
|---|---|
| `tools/assets/prepare_stage2.py` | 动作集合改读 manifest；`sync_actions()` 把每个契约的 `animations`/`action_map`/`action_bounds` 同步为 manifest 全量；`discover_contracts()` 自动发现 `characters/*.json`；表情解析仅对声明 `capabilities.expressions` 且 `source_format=vrm0` 的契约执行；catalog 由 `PACKAGE_CHARACTER_IDS` 生成 |
| `tools/assets/bake_stage2.gd` | 删除 `IDS`/`ACTIONS` 硬编码，改为契约发现 × manifest 动作 |
| `tools/assets/convert_stage2.py` | 增量转换：仅当源文件名、源 SHA-256、`derived`、GLB 内记录的 `root_motion`、运行时副本与输出摘要全部一致时复用缓存 |
| `tools/assets/verify_character_assets.gd` | 新增。单角色资产线门禁：全量动作齐全、映射骨骼变换有限、髋部高度在 rest 推导区间内、渲染正/侧视 ×5 时刻矩阵 |
| `tools/assets/render_recovery.gd` | 改为契约发现；帧数按包内角色动态计算；**新增缺失库守卫**：非包角色的库不存在时打印 `RENDER_SKIPPED` 并跳过，不再触发加载错误 |
| `tools/build_stage2.py` | 帧数动态化；新增非包角色资产线步骤；整包其余流程未改 |
| `tools/verify_recovery_visuals.py` | 姿势图数量断言动态化 |
| `app/scripts/ui/action_preview.gd` | 动作与显示名改读运行时清单 |
| `tests/verify_stage2_export.gd` | 包内播放动作改读包内清单全量 |

新增文档：`docs/humanoid-skeleton-spec.md`（规范）、`docs/humanoid-action-runbook.md`（操作手册）。

## 4. 验证结果（本机实测）

| 检查 | 结果 |
|---|---|
| 整包构建 `python -B tools/build_stage2.py` | 29 个步骤全部 PASS，末行 `GODETTE_STAGE2_BUILD_OK` |
| Python 资产与服务测试 | 95 passed, 1 skipped（跳过项为可选独立 PyInstaller 用例；整包流程已实际执行 PyInstaller） |
| 原版逐帧基线 `verify_recovery.gd` | `GODETTE_REGRESSION failures=0` |
| 运行时七动作检查 `verify_recovery_runtime.gd` | `failures=0` |
| Godot 单元/集成测试 | protocol / app_settings / windows / service_client / stage2_libraries / stage2_controllers / stage2_character_catalog / stage2_character_view / godette_performance / godette_lifecycle / godette_preview / review-regressions 全部 PASS |
| 姿势矩阵 `render_recovery.gd` | `GODETTE_RENDER frames=650 framing_failures=0`（65 动作 × 2 视角 × 5 时刻） |
| Alicia 资产线 `verify_character_assets.gd` | `CHARACTER_ASSETS character=alicia frames=650 failures=0` |
| 包内视觉 | `PACKAGE_VISUALS_OK cases=9 pose_frames=1300` |
| 实际桌面输入 | 透明穿透、身体拖动、不抢焦点、隐藏/恢复、托盘退出、服务回收均通过（`GODETTE_DESKTOP_OK`） |
| 烘焙库内容 | `godette.tres` 与 `alicia.tres` 各 65 个动画，各约 31.3 MiB |

## 5. 性能与体积

真实导出程序、D3D12、2560×1600（150% DPI），连续 120 秒（60 秒待机 + 60 秒重复动作）：

| 指标 | 主程序 | 后台服务 |
|---|---:|---:|
| 平均 CPU（单核 100% 口径） | 7.43% | 0.03% |
| 结束时工作集 | 347,086,848 B（约 331 MiB） | 23,257,088 B（约 22 MiB） |
| 私有内存首末差 | +89,968,640 B | −102,400 B |

内存变化的形态是**一次性阶跃而非持续增长**：待机 60 秒稳定在工作集 270 MiB / 私有 414 MiB，开始重复播放动作时升至 334 MiB / 504 MiB，随后 50 秒保持平坦，末次采样还略降。这符合"动作开始播放时一次性加载资源"的预期，但本观测只有 120 秒、只覆盖 idle 与 wave 两类动作，**不构成长期稳定性或内存上限的证明**。

体积：

| 对象 | 大小 |
|---|---|
| 单角色烘焙库 | 31.3 MiB（原基线库为数百 KB 量级） |
| 交付包 `dist/stage-2-godette` | 139.7 MiB / 74 个文件 |
| 其中 `Pet3D.exe` | 104.1 MiB |
| 其中 `Pet3D.pck` | 13.5 MiB（动画库压入 pck 后显著小于文本形态） |

## 6. 边界与未验证项

1. **新动作没有原版基线**。逐帧对照只覆盖 Godette 的待机与挥手（原版缓存 `app/motions/cache/godette.tres`）。另外 58 个动作的验收标准是"结构齐全 + 姿态有限 + 髋部高度合理 + 渲染非空"，**不校验动作语义是否好看**。
2. **`mode` 与 `root_motion` 未逐条人工核对**。按文件名推断的结果可能与实际动作不符，例如 `Dancing`/`Dancing_Twerk`/`Hip_Hop_Dancing` 被判为 `once`，`climbing_*`、`laying_*`、`sitting`、`male_sitting_pose` 使用 `root_motion: none`，可能出现原地滑步或脚不入地。需要人工过目 `build/stage2/reports/godette-poses/matrix.png` 后按需调整清单再烘。
3. **动作源授权未核验**。FBX 由用户提供，仓库既有结论为"授权未核验，仅本地技术验证"，本轮不改变该边界。
4. **`alicia.tres` 不入库**。该文件 31.3 MiB，且 Alicia 模型 `publishable: false`（不可再分发），按 `AGENTS.md` 不作为新增二进制资产提交。由此带来的干净检出场景由 `render_recovery.gd` 的缺失库守卫处理：库不存在时打印 `RENDER_SKIPPED <id>` 并跳过，构建仍通过。该守卫生效已实测（临时移走 `alicia.tres` 后仍得到 `GODETTE_RENDER frames=650 framing_failures=0` 且无 ERROR 行）。
5. **`characters/alicia.json` 与 `experiments/stage2-recovery/characters/alicia.json` 不再逐字节相同**。`prepare_stage2.py` 会把契约的 `animations`/`action_map`/`action_bounds` 同步为 65 个动作，这是本轮设计的预期行为，不是篡改。其余字段未改。
6. **契约 45 键是硬性要求**。`tests/assets/test_unified_contracts.py` 断言每个 humanoid_core 契约的 `bone_map` 与 `source_bone_map` 均为 45 键。将来接入缺手指骨的模型需要把该断言放宽为"必需子集强制、可选骨允许缺失"。
7. **未做长时间稳定性观测**，未验证 65 动作库长时间循环播放的内存与显存表现。

## 7. 复现命令

```powershell
cd D:\danjigame\3D-pet\wt\unified-skeleton
.\.venv\Scripts\python.exe -B tools\build_stage2.py --rebuild-assets   # 全量重建（含 Blender 转换与全量烘焙）
.\.venv\Scripts\python.exe -B tools\build_stage2.py                    # 使用既有缓存构建与验收
```

日常增删改动作的流程见 `docs/humanoid-action-runbook.md`。
