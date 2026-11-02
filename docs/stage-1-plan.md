# 阶段 1：应用骨架

根目录 `D:\danjigame\3D-pet-gtpdo`；用户已授权持续实施，暂不使用 Git。阶段 0 原型保留在 prototypes/stage0 与 dist/stage-0，正式源码从 app 开始。V3 的“本阶段未通过，不进入聊天和完整行为开发”继续遵守：本轮不实现 AI 回复或完整行为。阶段 0 未覆盖的设备/实机项继续保留。

## 本轮目标

- 同一用户数据目录只运行一个实例，再次启动唤回已有角色。
- 设置窗口可选择角色、显示大小和屏幕；自动保存位置，重启恢复。
- 显示器缺失临时回主屏，保留原屏幕偏好；托盘可找回角色。
- Python 子服务无控制台，用 UTF-8 JSON 行协议握手；有版本检查、错误、取消与退出流程。
- 服务单独写版本化配置 JSON 和 SQLite。配置原子替换；坏配置备份；保存失败可见，不能误报成功。
- Windows Job Object 回收自有服务；服务崩溃不带走角色，最多自动重启一次，不重放业务请求。
- 设置/聊天窗口可以获得焦点，角色窗口保持不抢焦点。聊天窗口本轮显示服务状态与功能准备状态，不假装已连接 AI。
- 发布目录含 Godot exe/pck/原生 DLL 和 PyInstaller onedir 服务，双击启动；不依赖用户全局 Python。

## 接口

协议每行 UTF-8 JSON，version=1，id 为字符串，type 为命令/事件，payload 为对象；上限 1 MiB。stdout 仅协议，stderr 日志。命令 hello/settings.get/settings.update/chat.cancel/tts.cancel/shutdown；暂未实现的聊天/记忆/合成请求返回明确的 not_available 错误。事件 ready/result/cancelled/error，保持请求 id。

hello payload 含 protocol_version=1；ready payload 含 service_version、protocol_version、settings、warnings。settings.update payload 为受控顶层 patch，result payload 含已持久化的 settings。退出回复 shutdown 的 result 后关闭；配置保存先完成原子替换才返回 result。

设置 schema_version=1，character_id 默认 godette，scale 默认1.0（0.5—2.0），screen_id 默认空字符串（主屏），placement 默认为相对工作区的角色脚底位置与边缘附着：{x:1.0,y:1.0,edge_x:1,edge_y:1}。配置允许 user_name（默认“你”）；不在本轮接收 API Key。数据路径默认 %LOCALAPPDATA%/Pet3D，可用 --data-dir 明确指定测试目录。

原生新增 acquire_instance(key:String)->int（1主实例、0已通知现有实例、-1错误），adopt_service_process(pid:int)->bool，get_displays_json()->String；shutdown 收尾互斥/唤回事件/Job。托盘新增6设置、7聊天；保留原1—5含5退出。

## 工作分工与验证

- native：原生单实例/Job/显示器信息与回归，产物只更新 app/addons/pet_window。
- service：服务、配置/数据库与协议测试、PyInstaller 构建。
- app：管道客户端、服务生命周期、设置/聊天窗口、位置和缩放集成、最终构建与可见验证。

每部分先运行关键失败用例再修正。最后验证普通/中文空格路径、重复启动、重启恢复、服务崩溃恢复、杀主程序回收服务、配置损坏/写入失败。不能由纯逻辑测试推断实机结果。
