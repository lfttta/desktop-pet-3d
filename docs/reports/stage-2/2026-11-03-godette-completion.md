# 阶段二 Godette 单角色完成验收

日期：2026-11-03。范围依据 `docs/stage-2-plan.md`。按用户要求在当前分支 `main` 中实现和集成；未自动提交、合并或推送。原历史报告未修改。

## 交付

- `dist/stage-2-godette/Pet3D.exe`：Godette 桌宠。
- `dist/stage-2-godette/Preview.cmd`：七动作、左右转身、速度、视角、暂停、基础表情与本地音频预览。
- 源工程 `app` 已接入本轮实现，不再依赖实验工程或旧 `work-pet` 的运行资源。
- 角色选择界面和托盘“下个角色”已移除。新包只提供 Godette，其他角色留作历史资料，未删除备份。

## 已完成内容

1. 复用已验证的七动作：待机、行走、转身、挥手、提起、下落、恢复。保留请求代次、优先级、持有/释放、单次结束回待机和转向统一控制。
2. 保留原模型、原骨骼和原待机/挥手基线。新缓存采用 Godot 4 完整局部姿势，位置轨道只补偿一次 motion_scale。
3. 使用 VRM 实际元数据生成 neutral/happy/sad/blink/mouth_a 映射；每帧统一合成权重，替换表情时清掉旧权重，眨眼与说话不被情绪覆盖。
4. 本地三秒合成测试声音配振幅包络，按实际音频播放时间（含混音/输出延迟补偿）驱动嘴部；暂停、停止、结束、隐藏和退出均复位。
5. 动态动作更新可见边界后立即约束窗口，保留拖动、透明穿透、无焦点抢占和贴边效果。
6. 旧设置中的历史角色 ID 迁移为 Godette，称呼、缩放、屏幕和位置保留；迁移写入失败不破坏原文件。
7. 构建只处理 Godette。默认使用验证过的缓存，`--rebuild-assets` 才转换 FBX 并烘焙至暂存路径；基线对照成功后替换缓存，整包验收成功后发布到交付目录。

## 实际执行命令

项目环境 `.venv`，Python 3.13.14。依赖见 `requirements-dev.txt`。

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/service/test_godette_migration.py -q -p no:cacheprovider --basetemp build/test-migration-green
& 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe' --headless --path app --script res://tests/test_godette_performance.gd
.\.venv\Scripts\python.exe -B tools/build_stage2.py --rebuild-assets
.\.venv\Scripts\python.exe -B tools/verify_godette_desktop.py
.\.venv\Scripts\python.exe -B tools/build_stage2.py
git diff --check
```

最后一次整包构建包含：原生编译及三组原生用例、Godot 导入、Python 服务/资产测试、原版姿势对照、七动作运行时检查、协议/设置/窗口/服务客户端测试、动作控制/角色目录/身体与表情生命周期测试、GPU 预览、PyInstaller 服务构建、冻结服务测试、Godot 导出、普通/中文路径与进程生命周期验证、截图和实际桌面输入测试。

## 验收结果

| 检查 | 本轮结果 |
|---|---|
| 源码服务与资产 | 90 passed，1 skipped |
| 冻结服务测试 | 82 passed，1 skipped |
| 原生窗口、生命周期、服务资源 | 90 / 55 / 212 项断言通过 |
| Godot 协议、设置、窗口、服务客户端 | 7 / 8 / 8 / 10 项通过 |
| 七动作资源、控制器、目录、角色视图 | 27 / 17 / 3 / 25 项通过 |
| 表情/音频聚焦测试 | 28 项通过 |
| 完整动作姿态与打断规则 | 68,235 次检查，0 failures |
| 原待机对照 | 251 个采样帧 × 69 骨骼，位置误差 0 |
| 原挥手对照 | 18 个采样点 × 69 骨骼，最大位置误差约 0.0241 mm |
| 旋转对照 | 小于 0.002 rad 阈值 |
| 七动作图像 | 正侧视角 × 五时刻，共 70 帧；非空、无裁切 |
| 表情/音频预览 | 三种表情可切换；嘴部覆盖约 0–1；音频停止/结束闭嘴 |
| 缩放与贴边 | 0.5 / 1 / 1.5 / 2 倍 × 左右边缘，加下落，共 9 组通过 |
| 实际桌面输入 | 透明区域 WindowFromPoint 穿透、身体拖动、无抢焦点、隐藏/恢复、托盘退出通过 |
| 包资源 | 只允许 Godette，禁止角色资源检查通过 |
| 中文路径与生命周期 | 设置恢复、单实例、服务重启、自有服务退出及父进程异常回收通过 |
| 原阶段一包 | 72 个文件 SHA-256 与原清单一致 |

跳过项为 pytest 中可选的独立 PyInstaller 构建用例；本轮整包流程已实际执行 PyInstaller 构建，并对生成的服务 EXE 运行测试，不是未验证打包。

## 性能观测

真实导出程序、D3D12、2560×1600 屏幕（150% DPI），连续 120 秒：60 秒待机，60 秒重复挥手。每 10 秒记录主程序与服务的 CPU 时间、工作集和私有内存。统计使用预热后区间。

| 指标 | 主程序 | 后台服务 |
|---|---:|---:|
| 平均 CPU（单核 100% 口径） | 7.17% | 约 0% |
| 结束时工作集 | 267,128,832 B（约 254.75 MiB） | 24,162,304 B（约 23.04 MiB） |
| 私有内存净变化 | -36,864 B | -98,304 B |

该有界观测未出现持续私有内存增长；不将其解释为数小时稳定性证明或内存上限承诺。原始数据为 `build/stage2/reports/desktop-performance.json`。

## 证据与保留范围

最新日志、截图、包校验和原版对照在 `build/stage2/reports`。关键文件：`python-tests.log`、`frozen-service-tests.log`、`godette-regression.json`、`runtime-validation.json`、`preview.log`、`poses/matrix.png`、`packaged-visuals/verification.json`、`package-verification.json`、`desktop-performance.json`。

本报告为新增文件，旧报告原样保留。新声音由项目代码生成，未引入第三方音频；来源说明已写入 `THIRD_PARTY_NOTICES.md`。构建与运行产物、备份、实验目录不提交进库。

阶段二按当前单角色范围完成。随机行走、拖放速度、重力、抛落和落地物理属于下一阶段；AI、联网 TTS、高级视线和多角色能力未纳入本轮完成声明。
