# V3 验收记录

分支：`feat/v3-core`，基线提交 `aea2f33`。

## 已实现

### 聊天

- 流式回复、停止生成、连续对话。
- `chat.send` 的 `accepted` 帧同时返回 `request_id` 和 `conversation_id`，停止后继续发消息仍留在同一会话。
- 历史记录自动滚动到底部。

### 人格与记忆

- 人格目录、系统提示词、边界规则从 `data/persona/godette.json` 读取。
- 记忆候选、人工新增、纠正（旧版本 superseded）、版本链、归档/恢复。
- 列表同时展示 active、archived、superseded 等状态。
- 后台压缩任务、冲突检测与处理、可选向量索引。
- 记忆正文以 Markdown 文件保存，SQLite 保存元数据。

### 表达、状态、主动行为

- 台词库开关、冷却与每日上限、仅删除式润色。
- 表情/语气风格分档写入提示词。
- 情绪状态建议的应用与边界校验。
- 主动消息的预算、静默时段、频率限制（默认关闭）。

### 桌面感知与主动行为

- 桌面状态分类：`desktop.status` 返回固定形状的采样快照与说话策略；状态按 `unknown` → `do_not_disturb`（锁屏记为 `reason=locked`）→ `fullscreen` → `sleep_hours` → `idle` → `active_interaction` → `active_work` 的顺序判定，采样失败一律降级为 `unknown`，不抛异常。
- 静默策略：全屏、勿扰、休眠时段默认静默；工作专注默认最小化（仅关怀类台词，且频率收紧）；空闲默认温和（开放关怀/空闲/夜间类台词）；未识别的策略值按静默处理。关闭桌面感知后不再采样，并释放已缓存的采样器。
- 云端主动消息与本地回退：开启云端时由服务线程调度、后台线程生成一条主动消息，同一时刻最多一个请求在途；生成失败、台词重复、预算或账单不允许、或生成期间云端开关被关闭时，回退到本地台词库。连续失败按指数退避，避免反复计费。
- 默认值：桌面感知开启（`desktop.enabled=true`，采样间隔 3 秒）；主动行为关闭（`autonomy.enabled=false`）；云端主动消息开启（`autonomy.cloud_enabled=true`），随主动行为开启后生效。
- 隐私边界：只读全屏、勿扰、锁屏、空闲这几类元数据；不读窗口标题、进程名、屏幕内容，也不读键盘与鼠标内容。日志只记录事件代码，不记录消息正文、密钥或窗口标题。
- 已知限制：主动消息通过 `autonomy.bubble` 事件展示，不写入对话历史；桌面感知仅在 Windows 生效，其他平台返回 `known=false` / `unknown`；各状态的说话策略为固定枚举（静默/最小化/正常等），不支持自定义规则。

### 情绪表情

- 表情呈现：服务端随聊天完成和 `state.get` 轮询下发 `presentation`（`emotion` + `weight`），前端 `emotion_presenter.gd` 映射为 `neutral` / `happy` / `sad`，权重钳制到 0..1，无法识别的表情或权重回退为 `neutral` / 1.0。
- 前端迟滞：与演员当前实际表情的差异小于 0.12 时不重复应用；演员无法呈现某表情时回退到 `neutral`；只有被接受的表情才更新缓存，避免轮询造成表情抖动。
- 默认值：表情开关 `expression.enabled=true`，台词库 `expression.phrases_enabled=true`。

### 成长、用量、诊断

- 从 preference/relationship/feedback 记忆和纠正记录收集证据，达到阈值后生成候选。
- 候选审批、回滚、核心特质锁定。
- 用量面板：今日/本月/按模型/按天，预算与价格配置状态。
- 诊断导出 zip：manifest、脱敏设置、诊断信息、日志；不包含 API Key。

### 设置

- 设置 schema v4，新增 memory/expression/autonomy/growth/privacy 分组。
- v1/v2/v3 设置迁移；设置文件损坏时先备份再恢复默认。
- 数据库结构升级前备份，失败时回滚。
- API Key 使用 DPAPI 保存在用户数据目录，不进入仓库、设置文件、日志或诊断包。

## 测试结果

- Python：`315 passed, 2 skipped`。
- Godot headless：
  - `test_app_settings` 8 项
  - `test_chat_framework` 18 项
  - `test_desktop_settings` 60 项
  - `test_emotion_presenter` 47 项
  - `test_protocol` 7 项
  - `test_service_client` 12 项
  - `test_v3_core` 108 项
  - `test_v3_panels` 87 项
  - 全部 0 失败。
- 真实服务链路：
  - DeepSeek 流式回复正常。
  - 生成中取消后无迟到增量；取消后下一条消息继续同一会话；历史中只有一条完整助手回复。
  - 正常退出时 Godot 与后台 Python 服务均退出，数据目录锁可被下一次启动重新获取。
  - 记忆新增 → 纠正 → 列表 → 详情/版本 → 归档 → 恢复链路通过。
  - 后台压缩任务非阻塞；静默时段内任务可完成。
  - 成长链：会话数与偏好记忆达标后生成候选，审批、历史、回滚、核心特质锁定通过。
- 桌面感知与情绪表情：
  - pytest 最终通过。
  - Godot 界面测试通过。

## 尚未完成 / 占位

- 角色动作与骨骼：未改动，沿用现有动画系统。
- 语音与 TTS：`tts.synthesize` 仍为保留命令，未实现。
- 多用户：未实现。
- 本地模型：`Router` 保留 `local_model` 开关，未接入本地模型实现。
- 向量检索：代码与开关已接入，但当前 DeepSeek 端点不提供 `/v1/embeddings`，需要支持 embedding 的兼容服务；未配置时 `memory.index.*` 返回 `unavailable`。
- 成长、压缩默认关闭，需在设置中开启；主动行为默认关闭，上文“桌面感知与主动行为”已说明。
- 打包与分发未做。
- 停止按钮的物理点击未做自动化；`ChatSession.cancel()` 的流 ID 绑定、迟到增量丢弃、后续会话延续由 headless 测试覆盖，按钮直接调用该方法。

## 本地运行

```bat
run-dev.cmd
```

使用 `%LOCALAPPDATA%\Pet3D-v3-dev` 作为开发数据目录；`PET3D_PYTHON` 指向本地 Python 环境。
