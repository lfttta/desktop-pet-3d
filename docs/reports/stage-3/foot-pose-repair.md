# 脚掌翻折与行走异常修复记录

状态：源码修复完成，聚焦回归通过；已打开修复版供桌面复测。没有修改模型、动作缓存或骨骼绑定，没有推送远端。

## 复现与原因

实际桌面截图可见双腿折到身体前方、鞋底朝外。用 Godette 原模型固定动画时间，并在 `Skeleton3D.skeleton_updated` 后读取最终姿态，排除了随机行为、动画相位和帧率对比较的干扰。

新增 `app/tests/test_leg_contact.gd` 的初次结果为 **41 项检查、39 项失败**。已证实：

1. `leg_ik.gd` 在世界空间计算旋转差，却直接乘到骨架局部空间的骨骼姿态上。角色有旋转/缩放时，校正方向错误。
2. `clear_feet()` 和零权重只关闭求解，不清除 `persistent=true` 的骨骼覆盖。切回 walk 后校正仍残留。一次固定帧探针中右脚残差约 0.32 骨架单位、旋转差约 67°。
3. 改动小腿旋转后，脚掌随父级转动，未保留动画原本的脚掌朝向。
4. 固定膝盖极向量在不需要校正时也可能改变原姿态。

## 修复

- 所有 IK 运算统一在骨架空间完成，世界高度目标先逆变换。
- 用动画当前膝盖方向确定弯曲平面；目标与当前高度相同时直接保留动画。
- 修正踝关节位置后保留脚掌原有旋转。
- 关闭单脚、全部脚或将权重置零时，释放大腿、小腿、脚部的持久覆盖。
- 左右行走根据模型前向与屏幕移动方向确定朝向，不受之前手动转身的影响。
- 移除未经验证的 `0.4461` 固定步长。新增 `walk_gait.gd`：加载时用动画旋转轨道和骨骼层级计算脚部轨迹，按低位支撑阶段的后移速度估计整圈距离，结果缓存；不 seek 正在播放的动画。
- 屏幕位移使用相机投影换算；开始自主行走前同步实际窗口位置，避免尚未初始化的锚点造成跳位。

## 对之前判断的更正

- 原地行走动画配合整体平移是可用方式，本身不导致骨骼扭曲。
- 单脚前后摆幅不等于完整周期的行进距离。此前“1.15 错了 2.5 倍”“0.4461 就是整圈步长”并未得到有效验证，已从实现移除。
- 足部局部 +Z 不能直接当作脚趾方向；朝向测试使用 Foot → Toes 的真实骨骼连线。
- 透明窗口左上角为负数并不代表角色出屏；应看可见角色边界。
- 原 headless 断言检查锚点 y 稳定，不能据此声称真实窗口的 y 抖动已经测量通过。

## 验证

使用本机已验证的 Godot 4.7.2，对该 worktree 的 app 执行：

```bat
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_leg_contact.gd
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_motion_physics.gd
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_motion_wiring.gd
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_walk_wiring.gd
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_godette_lifecycle.gd
Godot_v4.7.2-stable_win64_console.exe --headless --path app --script res://tests/test_stage2_character_view.gd
```

运行器逐个校验退出码，并检查完整输出中的 ERROR / SCRIPT ERROR，没有用管道过滤掩盖失败。

| 套件 | 结果 |
|---|---|
| leg_contact | 47 检查，0 失败 |
| motion_physics | 53 检查，0 失败 |
| motion_wiring | 41 检查，0 失败 |
| walk_wiring | 39 检查，0 失败 |
| godette_lifecycle | 0 失败 |
| stage2_character_view | 25 检查，0 失败 |

47 项新检查包括四种 yaw 的同帧姿态对照、脚底目标、脚掌旋转保持、关闭后的恢复、反复 idle/walk 切换、支撑脚滑动和转身后的左右方向。当前 Godette 的整圈行进估计为 0.93396 世界单位，数值是计算结果，不作为测试常量。

已在真实 D3D12 渲染下比较原动画与多次 IK 切换后的 idle/walk 画面，双腿和鞋掌姿态一致；实际桌面截图也显示正常站姿，不再双腿折在身前。测试实例日志记录拖放落地与完整自主行走结束，后台服务为 ready。

本轮未执行整包导出、全量 Python 测试、多显示器矩阵或长时间稳定性测试。渲染比较图、诊断脚本和临时日志位于系统临时目录，没有写入仓库。
