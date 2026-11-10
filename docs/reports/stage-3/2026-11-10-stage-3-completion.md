# 阶段三完成记录：沿底边行走与行为状态机

日期：2026-11-10。范围依据 `docs/stage-3-plan.md`。本轮只做收尾核对、构建门禁补齐与文档更正，**没有改动任何运行时代码**；原历史报告未修改，新增的 [脚掌翻折修复记录](foot-pose-repair.md) 保持原样。

## 交付现状

行走与行为状态机已合入 `main`：

| 提交 | 内容 |
|---|---|
| `011cbbe` | 沿底边自主行走与行为状态机 |
| `b105b6a` | 停止行走后朝向与脚底贴地恢复 |
| `6a5f4cd` | 行走时窗口上下抖动 |
| `76c0ee6` | 步长改用动画实测值，修正脚打滑 |
| `cc5331c` | 修正脚底 IK 的坐标系、残留覆盖与脚掌朝向 |
| `bb3f1e9` | `--no-ff` 合并 `feat/behavior-walk` 到 `main` |
| `b9ecf7b` | 悬空放置的角色不再一碰就掉到地面 |
| `1c265a2` | 运行包说明改按阶段三的实际行为 |

新增源码：`app/scripts/behavior/{walk_plan,behavior_controller,walk_gait}.gd`，以及 `app/scripts/motion/{motion_controller,leg_ik}.gd` 与 `app/scripts/pet_window.gd`、`app/scripts/character_view.gd` 中的行走接线。交付入口仍是 `dist/stage-2-godette/Pet3D.exe`（目录名沿用阶段二，内容已是阶段三）。

## 实现要点

**位移口径**：位移恒为整数个步态周期，窗口 x 由动画播放进度推进，而不是墙钟。`walk_plan.gd` 的 `position_at()` 在两端精确落点，不累积浮点漂移。

**步长来源**：`walk_gait.gd` 在加载时用动画旋转轨道和骨骼层级做正向运动学，取低位支撑阶段（高度阈值 25%）的踝关节后移速度，按 60 个采样取中位数，Godette 实测 **0.93396** 世界单位/圈。契约里的 `meters_per_cycle: 1.15` 零消费者、未经证实，只作兜底。

**行为掷骰链**：`behavior_controller.gd` 按 30% 待机 / 40% 环境动作 / 30% 移动决策；随机源可注入，测试能钉死分支。环境动作白名单只在 10 个单次动作里选，且仍按契约二次过滤（`mode == "once"`、`length ≤ 10s`），动作被删、变长或改成循环会自动退出候选。

**边缘闸门**：单侧空间不足一个完整周期就不往那侧走；两侧都不够时改判动作或待机，不会规划走不完的移动。

**打断**：拖拽、下落、非接地、用户动作、点击都会立即作废行走，落地后不自动续走。

**悬空闸门**：`_begin_walk()` 在脚底与地面线差值超过 0.5px 时拒绝起步（`pet_window.gd:522`），避免悬空角色因行走把鞋底钉回底边而瞬移。

## 本轮新增验证

`tools/build_stage2.py` 的门禁列表此前**不含** `test_motion_physics.gd` 与 `test_motion_wiring.gd`——两套测试在本地跑过，但整包构建从未执行，回归防线存在缺口。现已补入，并确认 23 个门禁项都有对应测试文件、无遗漏。

用构建脚本自身的 `godot()` 路径执行这两项，日志写入 `build/stage2/reports/`，判定条件（含 marker 且 `failures=0`）与构建时一致，均 `PASS`。

实际执行：

```powershell
.\.venv\Scripts\python.exe -B tools\build_stage2.py
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_walk_plan.gd
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_behavior_controller.gd
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_walk_wiring.gd
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_leg_contact.gd
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_motion_physics.gd
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_motion_wiring.gd
```

| 套件 | 结果（2026-11-10 实测） |
|---|---|
| `test_walk_plan.gd` | 202 项，0 失败 |
| `test_behavior_controller.gd` | 200 项，0 失败 |
| `test_walk_wiring.gd` | 44 项，0 失败 |
| `test_leg_contact.gd` | 47 项，0 失败 |
| `test_motion_physics.gd` | 58 项，0 失败 |
| `test_motion_wiring.gd` | 41 项，0 失败 |

六套日志均无 `SCRIPT ERROR`、无行首 `ERROR:`。

## 未完成

**整包构建本轮未跑通**。`tools/build_stage2.py` 在 `native/build/lifetime_test.exe` 处失败（`build_stage2.py:88`），两个用例报 `Ctrl+Alt+Q available before fixture configures` 与 `shutdown releases Ctrl+Alt+Q registration`。探针实测 `RegisterHotKey` 返回 `lastError=1409`（`ERROR_HOTKEY_ALREADY_REGISTERED`），占用者是当时正在运行的 `dist/stage-2-godette/Pet3D.exe`（PID 28752，12:31 启动）。这是**运行实例占着全局热键**导致的环境冲突，与门禁改动无关：失败发生在 `build_stage2.py:88`，而门禁列表在第 120 行；`run()` 按顺序执行，第 88 行抛出即终止，第 120 行根本没有被执行到。此前 2026-11-10 11:29 的整包构建结果是 `GODETTE_STAGE2_BUILD_OK`（CPU 8.45%，pytest 531 passed 2 skipped）。

**桌面人工验收 7 条未逐条执行**，见 `docs/stage-3-plan.md` §5.3。已有的是相关现象的观测（真实 D3D12 下姿态对比、拖放落地与一次完整自主行走的日志），不构成逐条验收结论。

**脚底是否打滑、窗口是否发抖**只能肉眼判断；headless 测试只能保证位移是整数圈、窗口偏移在整段行走期间冻结。

## 事实与推断的边界

第 1 节提交列表、第 1 节的源码文件名、验证表中的全部数字均来自本轮实际执行。步长 0.93396 与 `walk_gait.gd` 的算法描述取自该文件源码与既有报告，本轮未重新测量。整包构建失败的归因依据是失败位置、探针的错误码与占用进程三者吻合；未在关掉实例后重跑验证，因此「关掉即可通过」属于推断。
