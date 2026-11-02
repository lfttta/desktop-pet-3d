# AGENTS.md — Pet3D 工程协作约定

本文件对本仓库内所有 agent（含主 agent、实现 agent、验收 agent）生效。开始任何改动前先读完第 1 节和最后一节。

## 1. 项目事实（不要猜，以此为准）

| 项目 | 值 |
|---|---|
| git 仓库根 | `D:\danjigame\3D-pet-gtpdo` |
| Godot | `D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe`（4.7.2.stable） |
| Python | 3.13.14，使用项目专用环境 |
| 当前阶段 | 阶段 2，**只做 Godette 单角色** |
| 有效范围文档 | `docs/stage-2-plan.md` |
| 已作废文档 | `docs/superpowers/plans/2026-11-02-stage-2.md`（多角色方案，仅作历史参考） |
| 备份远端 | `D:\danjigame\3D-pet\git-backup.git`（本地 bare 仓库，`origin`） |

注意：`D:\danjigame\3D-pet\work-pet` 是早期阶段 0 的旧资料，**不是本工程的一部分**，不要往那里写代码，也不要从那里读运行资源。

## 2. 分支与 worktree

- `main` 只由集成者更新。实现 agent 不得直接提交到 `main`。
- 一个验收项 = 一个分支 = 一个 worktree。分支生命周期控制在小时级。
- 命名：`feat/<范围>-<内容>`、`fix/<范围>-<内容>`、`test/<范围>-<内容>`、`docs/<内容>`。
  例：`feat/godette-actions`、`fix/window-passthrough`。

```bat
:: 开任务
git -C D:\danjigame\3D-pet-gtpdo worktree add -b feat/godette-actions "D:\danjigame\3D-pet\wt\godette-actions" main

:: 在 worktree 内工作
cd /d "D:\danjigame\3D-pet\wt\godette-actions"
```

worktree 首次打开时 `app\.godot` 会重新生成（约 1 分钟），属正常现象；它不入库。

## 3. 文件所有权（避免冲突的根本）

| 区域 | 归属 |
|---|---|
| `app/scripts/animation/*`、`app/scripts/character_view.gd` | 动作 agent |
| `app/scripts/pet_window.gd`、`screen_math.gd`、`skin_bounds.gd`、`drag_state.gd` | 窗口/行为 agent |
| `app/scripts/ui/*`、`app/scripts/app_settings.gd` | 界面 agent |
| `service/*` | 服务 agent |
| `native/src/*` | 原生 agent |
| `tools/build_*.py`、`tools/assets/*` | 构建 agent |
| `tests/**` | 各 agent 自测；不要两人同时改同一个测试文件 |

**热点文件，只有集成者可以改**：

`app/project.godot`、`app/main.tscn`、`app/scripts/main.gd`、`app/export_presets.cfg`、`docs/stage-2-plan.md`

两个分支都必须改热点文件时：A 先合并，B `git rebase main` 后再改。不要各改一份最后硬合。

## 4. 提交规范

```
stage2: 接入 godette 七动作控制器

- 新增 action_controller.gd 的请求代次与取消
- 移除多角色分支，只保留 godette

验证: godot --headless --path app --script res://tests/test_protocol.gd
Refs: docs/stage-2-plan.md §4
Agent: implementer-a
```

- 一个 commit 只做一件事；改代码和改构建产物分开。
- `验证:` 行必须写实际执行过的命令，不要写"已测试"。
- 提交前先 `git status` 看清楚，不要盲用 `git add -A`。

## 5. 验收命令

Godot 脚本测试（`--path` 指向 `app`）：

```bat
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_protocol.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_app_settings.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_windows.gd
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe" --headless --path app --script res://tests/test_service_client.gd
```

Python 测试：

```bat
python -m pytest tests/service tests/assets -q
```

整包构建（慢，含原生编译、PyInstaller、Godot 导出、整包验收）：

```bat
python -B tools/build_stage1.py
python -B tools/build_stage2.py
```

**局部改动只跑最相关的聚焦用例；不要用"整包构建通过"代替聚焦测试，也不要用聚焦测试冒充整包验收。**

## 6. 合并流程（集成者执行，实现 agent 无权做）

1. `git log --oneline main..<分支>` 检查提交粒度
2. `git diff main...<分支>` 亲自读完整 diff，不看 agent 的总结
3. 在一个干净 worktree 里重跑第 5 节的验收命令，不采信"我已跑过"
4. `git merge --no-ff <分支>`
5. 跑 main 上的回归；失败则 `git revert` 或 `git reset --hard <合并前>`
6. `git worktree remove <路径>` + `git branch -d <分支>`

## 7. 硬性禁止

- 禁止 `git push --force`，禁止改写 `main` 历史。
- 禁止把 `build/`、`dist/`、`experiments/`、`backups/`、`prototypes/`、`app/.godot/`、`.tmp-*` 提交进库。
- 禁止修改 `docs/reports/` 下的历史记录（只允许新增文件，不允许改旧结论）。
- 禁止删除 `backups/` 里的任何内容。
- 禁止在 `D:\danjigame\3D-pet\work-pet` 里写代码。
- 禁止把模型、DLL 之外的新二进制资产直接提交，先说明来源与授权并更新 `THIRD_PARTY_NOTICES.md`。
