# Godette 阶段二合并前复核

日期：2026-11-03。用户授权：检查无问题后提交分支并整合。

## 分支与范围

- 基线：`main`，`9f7b6af`。
- 功能分支：`feat/godette-stage2`。
- 提交拆分：旧设置迁移、单角色表现、构建与验收、生成资源及窗口桥 DLL、交付文档；另增审查修复和测试资源标识提交。
- `feat/godette-framework` 及其现有 worktree 属于另一条工作线，本次未修改或清理。
- 未提交 build、dist、experiments、backups、prototypes、Godot 导入缓存和既有未跟踪测试数据。原有历史报告没有修改。

## 审查与修复

独立只读审查发现三个问题，均在合并前用新测试实际复现：

1. 重复请求转身沿用旧播放时间，同时重设朝向起点，导致方向跳变。现重复动作请求重置片段时间轴。
2. 预览的“向左转”误作用于其他动作，使挥手、提起和恢复倒放。现仅对转身传入左右方向。
3. 开心表情包含 O 元音；停止或结束音频后会恢复该张嘴权重。现音频接管嘴部后，停止保持全部元音归零，非嘴部情绪绑定保留。

新增 `app/tests/test_godette_review_regressions.gd`，修复前为 7 个失败断言，修复后为 0。测试由整包构建自动执行。

## 干净 worktree 验证

由已提交版本创建独立 worktree，在其中重新创建 `.venv`、导入 Godot 并实际编译原生桥、打包服务和导出程序。未复制原工程的 `.godot` 或构建缓存。

执行命令：

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/service tests/assets -q -p no:cacheprovider --basetemp build/premerge-python
.\.venv\Scripts\python.exe -B tools/build_stage2.py
```

- Python 服务与资产：90 passed，1 skipped；跳过的是另一个可选 PyInstaller 构建测试，实际整包流程已执行 PyInstaller。
- 冻结服务：82 passed，1 skipped。
- 原生窗口/生命周期/服务资源：90 / 55 / 212 项断言通过。
- Godot 协议、设置、窗口、服务客户端及角色动作、表情音频测试通过。
- 新审查回归：`GODETTE_REVIEW_REGRESSIONS failures=0`。
- 原待机/挥手姿势对照通过；完整动作检查 68,235 次，0 failures。
- GPU 预览、70 帧正侧姿态图、9 组缩放与贴边截图通过。
- 导出资源排除、中文空格路径、设置恢复、单实例、服务重启、父进程异常退出回收通过。

首次手工 Python 测试因新 worktree 尚无临时目录的父目录而未运行；创建构建目录后原命令通过。修复提交的整包流程在最后一个桌面输入步骤曾未检测到透明穿透状态；未改变程序或断言，对同一导出包单独重跑 `verify_godette_desktop.verify` 后通过，包含穿透、拖动、不抢焦点、隐藏/恢复及退出回收。未证明首次输入失败的具体原因，不将其隐去。

该复测还完成 120 秒运行观测：平均主程序 CPU 约为单核的 10.26%，结束工作集约 254.68 MiB，预热后私有内存净增长 200,704 B；服务 CPU 约 0%。这是短时观测，不代表长期上限。

干净 worktree 的报告及最终导出包将保存在主工程 `build/integration-stage2`；通过验收的包同步至 `dist/stage-2-godette`。构建产物不纳入 Git 提交。

## 整合方式

使用 `git merge --no-ff feat/godette-stage2` 合回 `main`，之后在主工程重跑 Python 与 Godot 回归；通过后正常推送 `main` 到本地备份远端 `origin`。不强制推送，不改写 main 历史；仅清理本轮创建的验收 worktree 和已合并功能分支。

此文件补充此前完成报告中的“尚未提交”状态，保留旧报告作为当时的验收记录。
