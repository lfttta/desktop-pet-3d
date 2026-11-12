# 阶段三：桌面行为与物理

更新日期：2026-11-10。状态：行走与行为状态机已实现并合入 `main`（merge `bb3f1e9`），自动化测试通过、2026-11-10 11:29 的整包构建通过；桌面人工验收见 §5.3（未逐条执行），完成记录见 [阶段三完成记录](reports/stage-3/2026-11-10-stage-3-completion.md)。本文件是阶段三的执行依据，接续 [阶段二计划](stage-2-plan.md) 第 91 行指定的下一步：先建立行为状态机和沿底边行走，再接拖放速度、重力与落地恢复。

## 1. 当前状态与阶段目标

阶段二已完成并通过验收：Godette 单角色七动作、neutral/happy/sad 表情、自动眨眼、本地音频口型、拖拽与贴边、透明区域穿透、无焦点抢占、托盘与热键、多屏与 DPI、单实例与后台服务。交付入口为 `dist/stage-2-godette/Pet3D.exe`，验收记录见 [2026-11-03 单角色验收](reports/stage-2/2026-11-03-godette-completion.md)。

`feat/motion-physics` 分支带来的拖拽弹簧、重力下落、落地恢复与脚底接触（`app/scripts/motion/motion_controller.gd`、`app/scripts/motion/leg_ik.gd`）已随本阶段一并合入 `main`。

本阶段目标：让角色能自主地在桌面底边走动，并在待机时自主挑选环境动作，形成最小可用的行为闭环。

## 2. 范围

### 本阶段做

- 沿工作区底边自主行走，含朝向与步态同步
- 行为掷骰链：待机 / 环境动作 / 移动
- 边缘可达性闸门：空间不足时不产生移动计划
- 行走被拖拽、下落、用户动作、点击打断

### 本阶段不做

- **行走开关**：需要 settings schema v5→v6 加迁移与界面，会改动 v3-core 相关代码，本轮刻意避开
- 沿上边或任意高度的漫游
- 攀爬、跑动换动作（`running`、`old_man_walk` 等留作后续）
- 主动合并 v3-core（该分支已存在，单独排一轮）

## 3. 已核实的技术前提

以下均为读代码核实，尚未运行测试：

| 事实 | 出处 |
|---|---|
| `locomotion: {meters_per_cycle: 1.15, nominal_speed: 0.9}` 已存在但零消费者 | `app/assets_data/godette.json:342-345` |
| `walk` 动画 length 1.0333s、`loop_mode = 1`、32 帧 @30fps | `app/resources/animations/godette.tres:19903` |
| `vertical_only` 语义把 Hips 的 X/Z 冻结到首帧，walk 是纯原地动画 | `tools/assets/convert_stage2.py:80-87` |
| `loco_speed_scale` 写入后无人读取（死变量） | `app/scripts/character_view.gd:198-199` |
| 本分支父提交为 `e945e8b`（= `feat/v3-core-actions` tip），因此**包含 v3-core** | `3D-pet-gtpdo/.git/refs/heads/feat/motion-physics` |

行注释：第 3 条是本阶段最关键的约束——walk 骨架里没有水平位移，若不驱动窗口平移，角色只会原地踏步。

## 4. 设计

### 4.1 位移口径

位移恒等于 `cycles × stride_px`，即位移必须凑成整数个步态周期；位置由动画播放进度推进，而非墙钟或按帧号插值。

```
px_per_unit = window.size.y / camera.size
stride_px   = locomotion.meters_per_cycle × px_per_unit
cycle_seconds = walk 动画 length
intra_progress = animation_position / cycle_length
```

`px_per_unit` 不乘 `dpi_scale`：窗口尺寸本身已包含 DPI 与 0.5–2.0 显示缩放，再乘一次会在 150% 缩放下失准。这与 `feat/motion-physics` 已确立的「所有交互相关计算统一使用物理像素」一致。

只有整数圈量化才能保证窗口线速度恒等于动画步态速度，从而脚底不打滑。

### 4.2 新增文件

**`app/scripts/behavior/walk_plan.gd`**（纯逻辑，可 headless 测）

- `plan(start_x, direction, cycles) -> Dictionary`：返回 `{start_x, target_x, cycles, cycle_seconds}`
- `position_at(plan, cycles_done, intra_progress) -> float`：全程单调，端点精确落在 `start_x` 与 `target_x`

设计原则参照 `dsh-pet-indesktop/pet/movement.py:103-159`（MIT, Copyright (c) 2026 Merzlin）的「整圈量化 + 帧驱动位置」思路，本实现自行编写，不移植其代码。

**`app/scripts/behavior/behavior_controller.gd`**（纯逻辑）

- 掷骰链：30% 待机 / 40% 环境动作 / 30% 移动。随机源可注入，测试可钉死分支。
- **环境动作白名单**：只收单次（`mode == "once"`）、非物理、`length ≤ 10s` 的动作。候选：`looking_around`、`thinking`、`yawn`、`neck_stretching`、`happy`、`bored`、`bashful`、`surprised`、`agreeing`、`shrugging`、`clapping`、`cheering`。
- **明确排除**：
  - `pickup` / `fall` / `recover` —— 由物理状态机接管
  - `walk` / `running` / `old_man_walk` / `holding_walk` —— 由位移接管
  - `male_sitting_pose` —— length 0.033s，一帧闪过
  - `dancing`(21.4s) / `salsa_dancing`(21.6s) / `angry`(19.2s) —— 超过 10s 上限
- **边缘可达性闸门**：先算左右剩余空间，只有 ≥ 1 圈步长才允许往该方向走；两侧都不够则回待机，不掷骰。
- **行走圈数**：2–8 圈，再按可用空间向下夹到整数圈；夹到 0 则取消本次移动。
- 动作与移动之间插入待机间隔，避免无缝连播显得抽搐。

### 4.3 修改文件

**`app/scripts/motion/motion_controller.gd`**

在 `GROUNDED` 下增加水平行走子模式：

- `begin_walk(cycle_seconds, stride_px, direction, cycles)` / `walk_progress()` / `end_walk()`
- 行走时 `anchor.x` 由 walk plan 赋值，`anchor.y` 恒为 `ground_y()`（本阶段只沿底边）
- 保持既有不变量：`HELD` / `FALLING` 期间行走立即作废；落地后不自动续走；仍走 `_clamp_anchor()` 兜底

**`app/scripts/character_view.gd`**

- `walk_cycle_position() -> float`：读 `player.current_animation_position`，作为位移权威
- **圈数绕回检测**：每帧比较新旧位置，`new < old` 判定绕回并累加。护栏：仅当 `old > 0.5 × cycle && new < 0.2 × cycle` 才计数，防止重播或动画混合把位置归零时误判
- `face_direction(dir: int)`：`rotation.y` 平滑趋近 `dir > 0 ? +PI/2 : -PI/2`（约 0.2s）。仅在 `not _turn_active and action != "turn"` 时生效
  - 理由：`turn` 动作是 180° 翻面（`humanoid_core.json` 中 `heading_degrees: 180`），用于背对观众，不能当侧身行走的朝向使用
  - 行走结束回到「面向观众」的 yaw 时，须记住上一次 `turn` 的结果（±PI），不能粗暴归零，否则手动翻面后再走一次会丢失朝向

**`app/scripts/pet_window.gd`**

- `_process()` 在 `_update_motion(delta)` 之后加 `_update_walk()` 与 `_update_behavior(delta)`：读动画进度 → 更新 `anchor.x` → 圈数走满则 `_finish_walk(true)` + 请求 `idle` + 重算 `_attached_edges` + 存档
- **窗口 x 用起步时冻结的 `_walk_center_offset`**，不每帧从 `_subject_rect()` 反推。走路时腿部摆动会让骨骼包围盒中心抖动几像素，逐帧反推会导致窗口左右发抖
- 中断优先级沿用 `app/scripts/animation/action_controller.gd:6-9` 的既有表，不改数字：

  | 事件 | 源优先级 | 对行走的影响 |
  |---|---|---|
  | 拖拽按下 / 拖动 | 80 | 立即作废行走 |
  | 下落 | 75 | 行走作废 |
  | 恢复 | 70 | 行走作废 |
  | 用户点动作 | 50 | 行走作废，播放该动作 |
  | 单击（未拖动） | — | 取消行走并挥手 |
  | 自主行走 | 10 | 可被以上任意一项打断 |

- 行走开始时若当前动作非 `idle`，直接拒绝起走（不在中途切断环境动作）
- `Engine.max_fps` 行走期间先保持 30（与阶段二实测口径一致）；若目视卡顿再提 60，并补一次 CPU 实测

### 4.4 三个已定位的碰撞点

均为读代码得出，实现时逐条处理：

1. **贴边钳制与行走抢位置**
   `_constrain_window()` 原有 `if _motion_active: return` 的早退，专为防贴边逻辑跟重力打架而设。行走若不加同样闸门，每帧的钳制会把 `anchor.x` 拽回去。**处理**：早退条件改为 `_motion_active or _walk_active`。

2. **`visible_bounds_changed` 高频触发钳制**
   `main.gd:114-117` 将该信号直连 `_constrain_window`。走路时腿部摆动使骨骼包围盒每帧变化，会高频触发钳制。**处理**：同上闸门覆盖；`_walk_center_offset` 在整段行走期间不刷新。

3. **`face_direction` 与 `turn` 抢 `rotation.y`**
   `_turn_active` 时 `sync_turn_heading()` 每帧写 `rotation.y`，动作结束时定格到 `_turn_start + _turn_delta`（±180°）。**处理**：`_apply_walk_yaw()` 在 `_turn_active` 时直接返回；`_animation_finished()` 把 turn 的最终朝向记入 `_rest_yaw`，侧身行走结束后平滑回到该朝向，不会丢失手动翻面状态。

### 4.5 实现中额外发现并修正的问题

以下不在原设计里，是在写测试时暴露出来的：

1. **`_ensure_motion_bounds()` 会重置 anchor** —— 它原本每次都调 `motion.settle_at(窗口位置)`。`_begin_walk` 若调用它，行走起点会被拉回窗口中心，实测中把 anchor 从 960 拽到 31。**处理**：拆出 `_sync_motion_anchor()`，在 `_motion_active or _walk_active` 或 `is_held()/is_falling()` 时直接返回；`_begin_walk` 只更新 bounds、不碰 anchor。
2. **`_ensure_motion_bounds()` 绕过 `_work_area()`** —— 直接问 `DisplayServer.screen_get_usable_rect()`，让 headless 的 `work_area_override` 失效。**处理**：改走 `_work_area()`。
3. **圈数会超过计划总数** —— 慢帧时动画可能多绕回，`_walk_cycles_done` 越过 `cycles` 后位置被 clamp，角色停在目标之前。**处理**：累加后夹到 `motion.walk_cycles_total()`。
4. **`advance_walk` 的完成判定不能依赖 `intra_progress >= 1`** —— 绕回检测按整圈上报，最后一圈结束时小数进度恰好归零，那个条件永远不会成立。**处理**：改为仅由圈数判定。

### 4.6 已知小瑕疵

行走中被强制结束时，存档位置仍是起步前的位置（只在走完时存一次）。不会造成错位，仅表现为「少走一段」，本阶段接受。

## 5. 验收门槛

### 5.1 自动化测试

```bat
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_walk_plan.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_behavior_controller.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_walk_wiring.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_motion_physics.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_motion_wiring.gd
.venv\Scripts\python.exe -B -m pytest tests/service tests/assets -q
```

新增断言：

- `test_walk_plan.gd`：整圈量化、端点精确、单调不越界、夹到 0 圈时取消、`cycles` 边界；以及 `MotionController` 的行走子模式（起点贴地、整圈推进、精确落点、被拖拽/下落取消、空中不能起走）
- `test_behavior_controller.gd`：每个掷骰区间可钉死复现；两侧无空间时不产生移动；白名单严格过滤 `male_sitting_pose` / `pickup` / `fall` / 循环动作 / 超时动作；行走圈数永不超出可用空间
- `test_walk_wiring.gd`：真实窗口上的端到端行走 —— 步长来自契约、整段走完后距离恰为整数圈、拖拽与用户动作都能打断、行为链在忙碌时不决策

**回归硬闸门**：`test_motion_physics.gd` 与 `test_motion_wiring.gd` 必须全绿，拖拽、下落、落地行为不得变化。

> 2026-11-10 补充：这两套测试此前**不在** `tools/build_stage2.py` 的门禁列表里，整包构建从未执行过它们。现已补入该列表（连同阶段三的 `walk_plan`、`behavior_controller`、`walk_wiring`、`leg_contact`），并逐项确认测试文件存在、日志含 marker 且无 `SCRIPT ERROR:` / 行首 `ERROR:`。

### 5.2 已执行的验证结果

2026-11-09 首轮（行走与行为状态机实现时）：

| 测试 | 结果 |
|---|---|
| `test_walk_plan.gd` | 202 项，0 失败 |
| `test_behavior_controller.gd` | 200 项，0 失败 |
| `test_walk_wiring.gd` | 34 项，0 失败 |
| `test_motion_physics.gd` | 53 项，0 失败（回归） |
| `test_motion_wiring.gd` | 41 项，0 失败（回归） |
| `pytest tests/service tests/assets` | 531 passed, 2 skipped |
| 其余 Godot 套件（controllers / character_view / libraries / action_menu / action_wiring / lifecycle / catalog） | 全部 0 失败 |

2026-11-10 复验（脚底姿态与悬空放置修复之后，见 [脚掌翻折修复记录](reports/stage-3/foot-pose-repair.md)）：

| 测试 | 结果 |
|---|---|
| `test_leg_contact.gd` | 47 项，0 失败（新增，IK 修复的直接回归） |
| `test_motion_physics.gd` | 58 项，0 失败 |
| `test_motion_wiring.gd` | 41 项，0 失败 |
| `test_walk_wiring.gd` | 44 项，0 失败 |
| `test_stage2_character_view.gd` | 25 项，0 失败 |
| `test_godette_lifecycle.gd` | 0 失败 |
| 整包构建 `tools/build_stage2.py` | `GODETTE_STAGE2_BUILD_OK`，CPU 8.45%，pytest 531 passed 2 skipped |

### 5.3 桌面人工验收

状态（2026-11-12 更新）：第 1、3、4、5、6 条已用窗口运动探针在打包版 `dist/stage-2-godette` 上实测；第 2 条部分覆盖；第 7 条仍未执行。

| # | 条目 | 结果 | 证据 |
|---|---|---|---|
| 1 | 待机 30 秒内至少自主行走一次，脚底不打滑 | ✅ | 70 秒内 5 段行走，首段在启动后 0.1–2.8 秒出现；每段 `y_spread = 0`（窗口全程不上下漂）。脚底相对屏幕底边的像素级滑动属视觉判断，见下 |
| 2 | 走到边缘 1 圈步长内停住，不越界、不抖动 | ◐ | 所有运行中窗口未越出工作区（含 267 px 透明内边距）；实测步长为 261.5 px 整圈倍数（1038≈4 圈、2092≈8 圈、1553≈6 圈）。但「停在边缘」由多次行走自然覆盖，未单独构造贴边用例 |
| 3 | 行走中拖拽 → 立即被抓取，松手正常下落 | ✅ | 行走中在躯干按下并上移，窗口跟随（held y 850→781）；松手后落回 846（地面线 850，差 4 px） |
| 4 | 行走中单击 → 停下并挥手 | ✅ | 行走中单击后 0.35 秒采样窗口位移为 **0**（未点击时同段行程约 85 px/0.35 秒）。挥手动作为动画，日志单页 4 KiB 已写满看不到，属视觉判断 |
| 5 | 走完后重启回到走完的位置 | ✅ | 走到 x=2053，运行中 `placement.x = 0.9364`；重启（`--no-behavior`）落到 2042，Δ=(-11, +2) px |
| 6 | 0.5 / 1 / 1.5 / 2 倍缩放下各走一段 | ✅ | 每档改 `settings.json` 的 `scale` 后重启实测：步长随窗口高度线性缩放（0.5→130.8 px、1.0→261.5、1.5→392.3、2.0→523.0）；每段位移都是整圈倍数（倍率 2.97–6.0，1.5 档有一段 3.48 是到边提前停），四档 `y_spread` 全为 0 |
| 7 | 多屏切换与 DPI 变化时不崩溃 | ⬜ 未执行 | 需要第二显示器或改系统缩放，未在本机模拟 |

两条探针脚本放在会话临时目录（`accept_motion.py`、`accept_input.py`、`accept_persist2.py`），未入库。

探针踩到的两个坑，记下来避免下次误判：**①** 应用日志每次运行只写满一个 4 KiB 页（`READY` 的 JSON 一行就占掉大半），后续 `ACTION` 行不再落盘——不能用日志判断点击/拖拽是否生效，要用窗口位移。**②** 位置保存是行走结束后延迟 300 ms 写入，探针在结束后立刻杀进程会读到旧值，看起来像「位置没保存」，实际是测量太早。

## 6. 后续阶段

以下已评估并排序，本阶段不实施：

| 阶段 | 内容 | 参考项目对应 | 判断 |
|---|---|---|---|
| 3-B | 聊天气泡 + 头顶快捷输入 | `speech_bubble.py` 1810 行 + `speech_bubble_text.py` 纯函数 | 真缺口，投入产出比最高 |
| 3-C | Agent 状态联动 | `agent_link` 6303 行，本地 JSONL 总线 | 真缺口，纯本地不联网 |
| 3-D | TTS | `tts/{edge,sapi,mimo,doubao}` 声明式注册表 | 真缺口，`tts.synthesize` 当前返回 `not_available` |
| 3-E | 截屏视觉 + 主动识屏 | `vision.py`、`proactive/` | 真缺口 |
| 3-F | 分档降载 | `tick_governor.py` 四档 + 800ms 滞回 + AC 感知 | 打磨项 |

**已优于参考项目、不再引入**：LLM 对话编排（`service/ai/orchestrator.py` 657 行，含熔断、预算账本、schema 修复重试）、记忆系统（`service/memory/*` 2863 行）、人格与提示词、拖拽抛掷物理、逐像素穿透。

**参考项目的渲染层不可移植**：其为 Python + PySide6 的 2D 视频精灵（WebM/ffmpeg），本项目是 Godot 4.7.2 + VRM，UI 与渲染均不通用。可借鉴的只有纯算法、Win32 ctypes 段与实机实测常量。

## 7. 事实与推断的边界

第 3 节的技术前提来自读取源码；第 4.4 节的碰撞点在实现时逐条处理，第 4.5 节记录的是**写测试时才暴露出来的四个问题**，均已修正并有测试覆盖。第 5.2 节是实际执行结果，不是预期。

仍待实测的部分：桌面人工验收 7 条。其中「脚底是否打滑」和「窗口是否发抖」只能由肉眼判断，headless 测试只能保证位移是整数圈、窗口偏移在整段行走期间冻结。
