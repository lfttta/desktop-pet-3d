# Pet3D 阶段二：Godette 单角色

当前开发范围已调整为只做 Godette。其他角色适配和多角色切换留到后期。当前执行顺序及验收标准见 [Godette 单角色方案](docs/stage-2-plan.md)。

下一步先将已经验证的 Godette 七动作和控制逻辑接入主工程，精简角色选择与构建流程；之后完成 Godette 基础表情、眨眼和简单音频口型，最后验证单角色便携包。

本次是文档范围调整，代码与现有程序包尚未精简：

- `app` 当前保持正常 Godette 基线，仍使用原待机、挥手缓存。
- `experiments/stage2-recovery/package/Preview.cmd` 是已有的三角色七动作验证入口；它仍是历史验证包，不是单角色新成品。
- `python -B tools/build_stage2.py` 当前仍执行隔离多角色流程，后续需要改造后才能用于单角色交付。
- `dist/stage-2-godette` 是下一版计划输出路径，目前尚未生成。
- 原版快照、阶段一包及其他角色资料保留。纸片剑士继续排除；随机行走、拖拽抛落、重力和落地物理属于阶段三。

已有验证结果见 [骨骼修复报告](docs/reports/stage-2/recovery.md)。其中的角色数量、测试结果与资源限制描述的是当时的实验包，不代表本轮单角色工作已完成。

Godette 的模型和动作来源记录继续保留于 `THIRD_PARTY_NOTICES.md`，精简角色数量不改变已有来源与署名要求。
