# Pet3D

Windows 桌面 3D 陪伴桌宠：Godot 4.7 负责窗口、角色与动作，Python 标准库后台负责对话、记忆、人格与状态。

当前为 **0.1.0 实验版本**，私有开发仓库。

## 功能

### 对话
- OpenAI 兼容 Provider（默认 DeepSeek），流式回复、停止生成、连续会话、历史记录。
- 人格卡、边界规则、本地快捷指令（问候、`记住……`、挥手、过来、安静）。
- 结构化输出校验、一次格式修复、降级回复、熔断、超时与预算控制。

### 记忆
- SQLite 元数据 + Markdown 正文。
- 新增、纠正（旧版 superseded）、版本链、归档/恢复、删除 Outbox。
- 关键词/FTS 检索、去重合并、冲突检测与人工处理。
- 后台压缩/归档任务（默认关闭，可选模型压缩）。
- 可选向量索引；需要支持 `/v1/embeddings` 的 Provider。

### 人格、情绪与成长
- PAD 情绪状态（pleasure / arousal / dominance）与 energy / affinity / stress，带平滑、衰减。
- 模型只能提交状态增量；核心人格值不会被模型直接改写。
- 表情映射：`happy` / `sad` / `neutral` + 权重，前端带迟滞与回退。
- 人格成长：证据收集、候选生成、审批/拒绝/历史/回滚、核心特质锁定（默认关闭）。

### 桌面感知与主动行为
- Windows 下只读全屏、系统勿扰/演示/锁屏、键鼠空闲秒数，不读窗口标题、进程名、屏幕内容或键鼠内容。
- 状态：`do_not_disturb / fullscreen / sleep_hours / idle / active_interaction / active_work / unknown`。
- 主动行为受安静时段、手动静音、最小间隔、每小时上限、每日 Token/月度费用预算共同约束。
- 云端主动消息 + 本地台词回退；失败指数退避；相似内容去重；不写记忆、不改人格。
- 默认值：桌面感知开启，主动行为关闭，云端主动消息开启（随主动行为生效）。

### 骨骼与动作库
- 统一骨骼 + 65 个动作（核心、舞蹈、情绪、互动、日常、移动、道具、姿态）。
- 右键角色或托盘菜单触发；`动作预览.cmd` 可查看全部动作、速度、方向、表情。
- 聊天模型的 `action` 字段仍只允许原有 8 个动作 id（`none/idle/walk/turn/wave/pickup/fall/recover`）；65 个动作通过菜单手动使用。

### 设置、用量与诊断
- 设置 schema v5，支持 v1–v4 迁移。
- API Key 使用 Windows DPAPI 保存，不进入仓库、设置文件、日志或诊断包。
- 用量账本：今日/本月、按模型、按天。
- 诊断导出 zip：manifest、脱敏设置、诊断信息、日志。

## 架构

```text
Godot 4.7 应用 (app/)
  ├─ 桌宠窗口 / VRM 角色 / 动作控制 / 聊天与设置面板
  └─ service_client.gd
        │  JSON-lines over stdin/stdout
        ▼
Python 3.13 后台服务 (service/)
  ├─ runtime: reader thread + inbound queue + 100ms tick
  ├─ chat / memory / state / autonomy / expression / growth / usage / diag
  ├─ SQLite (pet.db) + Markdown 记忆正文
  └─ OpenAI 兼容 Provider（可替换）
```

服务只使用 Python 标准库；网络调用在后台线程，SQLite 写入保持在服务主线程。

## 环境要求

- Windows 10/11 x64
- Godot 4.7.2（项目 `app/project.godot`）
- Python 3.13.14（服务本体无第三方依赖）
- 可选：Visual Studio Build Tools（重编原生窗口桥）、PyInstaller（打包服务）

## 快速开始

```bat
git clone <repo-url>
cd desktop-pet-3d
```

设置 Python 与 Godot：

```bat
set PET3D_PYTHON=C:\Python313\python.exe
set GODOT=C:\Godot\Godot_v4.7.2-stable_win64.exe
```

也可以直接双击 `run-dev.cmd`：脚本会依次查找仓库内 `.venv`、相邻的 `3D-pet-gtpdo\.venv`、PATH 上的 `python`，Godot 同理。数据目录默认 `%LOCALAPPDATA%\Pet3D-v3-dev`，可用 `PET3D_DATA` 覆盖。

首次在编辑器打开 `app/project.godot` 会生成 `.godot` 导入缓存（约 1 分钟，不入库）。

启动后在设置 → AI 中填入 API Key（DPAPI 加密保存在本机），即可聊天。

服务也可以单独冒烟，不启动 Godot：

```bat
python -I service\main.py --data-dir %TEMP%\pet3d-data
```

向 stdin 写入：

```json
{"version":1,"id":"1","type":"hello","payload":{"protocol_version":1}}
```

## 目录结构

| 路径 | 内容 |
|---|---|
| `app/` | Godot 工程：角色、动作、聊天/设置/记忆/成长/用量面板、服务客户端 |
| `service/` | Python 后台：协议、路由、AI、记忆、状态、表达、成长、诊断 |
| `characters/` | 角色契约源（统一骨骼 v2） |
| `data/` | 人格卡与台词库等种子数据 |
| `animations/` | 动作资产管线中间产物 |
| `native/` | 窗口桥 GDExtension 源码 |
| `tests/service/` | pytest 服务测试 |
| `tests/assets/` | 资产/动作契约测试 |
| `docs/` | 设计、验收、动作规范与历史报告 |
| `app/resources/animations/` | `godette.tres` 与 65 个 GLB 动作源 |

## 数据与隐私

运行时数据在用户目录，不在仓库内：

| 启动方式 | 数据目录 |
|---|---|
| `run-dev.cmd` | `%LOCALAPPDATA%\Pet3D-v3-dev` |
| Godot 不带参数 | `%LOCALAPPDATA%\Pet3D` |
| 服务单独启动 | `--data-dir` 指定，默认 `%LOCALAPPDATA%\Pet3D` |

目录内包含 `settings.json`、`pet.db`、`service.lock`、DPAPI 加密的 `secrets.bin`。

- 桌面感知只采集全屏/勿扰/锁屏/空闲元数据。
- 默认不记录 prompt 正文；诊断包导出前脱敏。
- API Key 不写入源码、设置文件、日志或诊断包。
- 记忆写入默认需要显式操作；自动记录可在设置中开启。

## 测试

Python（3.13.14）：

```bat
python -m pytest tests/service tests/assets -q
```

当前基线：`530 passed, 2 skipped`。

Godot headless（在仓库根执行）：

```bat
"%GODOT%" --headless --path app --script res://tests/test_v3_core.gd
"%GODOT%" --headless --path app --script res://tests/test_action_wiring.gd
```

完整套件清单以 `tools/build_stage2.py` 为准；当前 17 个套件全部 `failures=0`，另加 `main.gd --check-only`。

完整构建（原生编译、PyInstaller、Godot 导出、整包验收）需要本机工具链：

```bat
python -B tools/build_stage2.py
```

## 已知限制

- 桌面感知仅 Windows；其他平台返回 `unknown`。
- 主动行为、人格成长、记忆压缩默认关闭，需要在设置中开启。
- 向量检索需要支持 embeddings 的 Provider；未配置时 `memory.index.*` 返回 `unavailable`。
- Alicia 的契约与模型已并入，但未启用：缺少 `alicia.tres`，角色目录只放行 Godette。
- 聊天模型的动作白名单仍是 8 个；65 个动作通过右键/托盘菜单和动作预览使用。
- 主动消息只显示在聊天窗口，不写入对话历史。
- 完整分发打包/导出未在本次验收中重跑。
- 兼容性与性能只在当前开发机验证。

## 第三方素材与许可证

- Godette：SirRichard94，VRM 适配 Lyuma，CC BY 3.0，保留署名。
- Alicia Solid VRM：© DWANGO Co., Ltd.；公开再分发前须单独确认规则。
- 士兵 GLB、部分动作 GLB：来源与再分发授权未完全确认。
- Godot Engine、V-Sekai/godot-vrm、Godot-MToon-Shader、MerZlin/dsh-pet-indesktop 等许可见 `THIRD_PARTY_NOTICES.md` 与 `app/addons/*/LICENSE`。

**本仓库为私有开发仓库，第三方素材的再分发授权未全部确认；在确认之前不要公开转发或公开发布。** 代码本身暂未授予开源许可证。

## 开发约定

- 主开发分支：`feat/v3-core-actions`
- 备份标签：`pre-actions-merge`
- 协作约定见 `AGENTS.md`
- 验收记录见 `docs/v3-acceptance.md` 与 `docs/合并记录.md`
- 动作规范见 `docs/humanoid-skeleton-spec.md`、`docs/humanoid-action-runbook.md`

## 致谢

Godette 模型作者与 VRM 适配者、Godot 与 V-Sekai 社区、以及所有第三方素材作者。详见 `THIRD_PARTY_NOTICES.md`。
