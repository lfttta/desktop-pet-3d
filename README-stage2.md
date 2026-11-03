# Pet3D 阶段二：Godette 单角色

便携包位于 `dist/stage-2-godette`，请保留整个目录。

- 双击 `Pet3D.exe` 启动 Godette 桌宠：透明置顶、身体可拖动、空白穿透；点击身体挥手。
- 双击 `Preview.cmd` 打开动作与表情预览：七动作、左右转身、速度、视角、暂停、自然/开心/难过，以及本地测试音频。
- 音频播放时嘴部按播放时间开合；暂停、停止、播放结束和隐藏/退出会关闭嘴部。
- 设置保留称呼、大小和显示器；已移除角色切换。旧设置选择其他角色时会迁移为 Godette，其余设置保留。
- 托盘提供显示/隐藏、恢复位置、待机/挥手、设置、聊天占位和退出。紧急恢复位置为 `Ctrl+Alt+F10`，退出为 `Ctrl+Alt+Q`。

七类动作是待机、行走、转身、挥手、被提起、下落、恢复。行走和下落目前是动画预览，不会驱动窗口随机行走、重力或抛落；这些属于阶段三。聊天、联网 TTS、高级视线跟随仍未接入。测试声音是本地合成音，不是语音合成服务。

主程序使用同目录内的后台服务，运行不依赖全局 Python。用户设置默认保存在 `%LOCALAPPDATA%/Pet3D`，可通过 `-- --data-dir=<绝对路径>` 指定独立数据目录。

## 开发与构建

Python 版本为 3.13.14；项目使用 `.venv`，构建依赖版本见 `requirements-dev.txt`。

```powershell
# 已有环境：完整单角色构建和验收，默认复用已验证缓存
.\.venv\Scripts\python.exe -B tools/build_stage2.py

# 仅当动作源变化时重建资产，需要本机 Blender 和原 FBX 目录
.\.venv\Scripts\python.exe -B tools/build_stage2.py --rebuild-assets
```

构建从当前 `app` 生成暂存包，验收通过后输出到 `dist/stage-2-godette`。资产重建先写入 `build/stage2/godette.tres`，通过与原版待机/挥手的逐帧对照后才替换运行缓存。原缓存 `app/motions/cache/godette.tres` 仅用于回归对照，不装入新包。

实际桌面验收会短暂移动鼠标测试拖动，随后恢复鼠标位置，并进行两分钟待机/重复动作观测。详细日志、截图和性能数据在 `build/stage2/reports`。

工程内的当前范围见 `docs/stage-2-plan.md`，完成记录见 `docs/reports/stage-2/2026-11-03-godette-completion.md`。原阶段一包、备份和多角色实验资料保留；新包只装配 Godette。第三方来源及署名见 `THIRD_PARTY_NOTICES.md`。
