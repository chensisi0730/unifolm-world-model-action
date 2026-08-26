# VLA 客户端运行记录

## 2026-08-26 15:20 "动作幅度太小"根因修复 ✅ 手臂动作恢复正常幅度

### 运行配置

| 项目 | 值 |
|------|-----|
| VLA 服务器 | http://0.0.0.0:8778（unnorm_key=`g1_fold_towel`，用户手动重启） |
| 图像服务器 | G1 本体 `teleimager.image_server --rs`（三相机 ready；注意必须带 `--rs`） |
| 网络 | **eno2 = 192.168.123.99 恢复正常**（曾短暂改用 USB 网卡 enxbe3af2b6059f，已撤销恢复原状） |
| robot_type | g1_dex1 |
| 指令 | "fold the towel" |
| 参数 | action_horizon=16, exe_steps=16, obs_horizon=2, rollouts=5, freq=30Hz |

### 根因分析（诊断过程）

1. **最小实验排除硬件**：单次 write_arm 阶跃(+0.2rad) 与 30Hz 流式 write_arm 均精确跟踪
   （稳态误差 0.02 rad），证明 DDS lowcmd 通道、kp/kd、mode_pr 全部正常。
2. **客户端加 sent vs actual 对比日志定位执行侧死区**：rollout 开头发送的 IK 目标从
   step 1 就正确，但机器人前 ~300ms 纹丝不动，随后 33ms 内以 ~10 rad/s（超限幅）
   猛跳到位 —— 与 `_ctrl_motor_state` 中 `drive_to_waypoint` 的 0.8s 阻塞窗特征吻合。
3. **两个软件根因**：
   - **g1_arm.py 控制线程模式粘连（bug）**：`drive_to_waypoint` 分支执行完后若无人再调
     `write_arm`，会无限重复阻塞式 0.8s 驱动；新命令必须等当前阻塞窗结束才被读取，
     造成每次 rollout 开头平均 ~400ms 死区 + 结束时超速跳变。
   - **robot_client.py 每个 rollout 强制复位 INIT_POSE**：模型目标每 chunk 仅距当前
     位姿 3~8cm，复位把上一 rollout 的累积运动清零 → 机器人永远做"回位→挪一点→又
     回位"的往复运动。80 步总位移仅 ~2cm，即"动作幅度太小无实际意义"的直接原因。

### 修复内容

- `g1_arm.py _ctrl_motor_state`：`_drive_to_waypoint` 执行一次后自动切回
  `schedule_waypoint` 保持位姿，新指令最多延迟一个控制周期即生效（消除死区与跳变）。
- `robot_client.py run_policy`：新增 `reset_to_init` 参数，仅在第一个 rollout 前复位
  INIT_POSE，后续 rollout 从当前状态闭环连续执行。
- `g1_arm.py connect()`：DDS 接口改为自动检测 192.168.123.x 网段网卡（兼容 eno2/USB 网卡）。
- `robot_client.py`：执行循环增加 sent_arm_q vs actual_q 逐步对比日志（保留用于后续诊断）。

### 修复后运行结果（5 rollouts × 16 步，exit=0）

- 左手 EE：[0.236, 0.149] → [0.286, 0.316]，**累计位移约 17cm**
- 右手 EE：[0.230, -0.173] → [0.321, -0.270]，**累计位移约 13cm**
- 双臂 y 方向持续相向合拢，动作连贯符合 fold towel 语义
- 启动后 1~2 步即进入精密跟踪（误差 <0.01 rad），无死区、无跳变
- 夹 gripper 保持 [4.46, 4.49] 小幅调整（fold_towel 数据集特性；若需大幅开合需确认桌面有毛巾实物）

### 结论

系统恢复正常。"幅度太小"由「每 rollout 强制复位」+「DDS 控制线程 drive 模式粘连」
叠加导致，非模型或硬件问题。模型本身首 chunk 预测贴近当前位姿属正常平滑行为，
闭环连续执行后累积位移即可达有效幅度。

## 2026-08-26 11:43 最新运行 ✅ 正常运行

### 运行配置

| 项目 | 值 |
|------|-----|
| VLA 服务器 | http://192.168.0.102:8778（unnorm_key=`g1_clean_table`） |
| 图像服务器 | G1 本体 teleimager.image_server --cf |
| robot_type | g1_dex1 |
| 指令 | "Clean the table" |
| 参数 | action_horizon=16, exe_steps=16, obs_horizon=2, rollouts=5, freq=30Hz |

### 运行结果

- ✅ 5 个 rollout × 16 步共 80 步全部执行完毕，无报错
- ✅ 三路摄像头（头部、左腕、右腕）全部连接成功
- ✅ 左夹爪 gripper_L 保持 4.50（稳定）
- ✅ 右夹爪 gripper_R 在 4.45-4.50 之间微调
- EE 目标 dL≈3-4cm，dR≈2-9cm，IK 求解正常
- IK joint delta norm≈0.96-1.46 rad/chunk
- 结论：系统正常运行，eno2 网络修复后 DDS 通信正常

## 2026-08-26 eno2网络修复后运行 ✅ DDS接口修复

### 修复内容
- **DDS网络接口修复**：`g1_arm.py` 中 `ChannelFactoryInitialize(0, 'wlx90de8098b1a6')` → `ChannelFactoryInitialize(0, 'eno2')`
  - 原因：机器人DDS通过以太网(eth0: 192.168.123.164)发布，客户端需用 eno2(192.168.123.99) 接收
  - WiFi接口(wlx90de8098b1a6→wlan0)仅用于SSH和图像流，不用于DDS通信
- **机器人调试模式**：通过 MotionSwitcherClient.ReleaseMode() 释放高层控制模式

### 运行配置

| 项目 | 值 |
|------|-----|
| VLA 服务器 | http://192.168.0.102:8778（unnorm_key=`g1_clean_table`） |
| 图像服务器 | G1 本体 teleimager.image_server --rs |
| robot_type | g1_dex1 |
| 指令 | "Clean the table" |
| 参数 | action_horizon=16, exe_steps=16, obs_horizon=2, rollouts=5, freq=30Hz |

### 运行结果

- ✅ 5 个 rollout × 16 步共 80 步全部执行完毕，无报错
- ✅ 左夹爪出现显著开合：gripper_L ∈ [1.10, 4.44]（变化量 3.3）
- 右夹爪保持 [4.49, 4.50]，符合 g1_clean_table 单侧操作特性
- EE 目标 dL/dR 每 chunk 3~8cm，IK joint delta norm≈0.33~0.52 rad/chunk
- 结论：系统正常运行，动作与之前一致。手臂幅度小是 clean-table 模型特性

## 2026-08-25 第二次运行：改用匹配指令 "Clean the table" ✅ 改善确认

按标准命令运行（指令改为 "Clean the table"，与 unnorm_key=`g1_clean_table` 匹配）：

- ✅ 80 步全部执行完毕，exit=0，无错误
- ✅ **左夹爪出现大幅开合**：gripper_L ∈ [1.76, 3.98]（变化量 2.2，上轮仅 ±0.02）
- 右夹爪保持 [4.48, 4.51]，符合 g1_clean_table 单侧操作特性
- EE 目标 dL/dR 每 chunk 仍为 2~5 cm —— 首步目标贴近当前位姿属模型平滑预测的正常表现，
  连续 chunk 累积后即形成有效运动；IK joint delta norm≈0.26~0.47 rad/chunk
- 结论：**指令匹配后动作不再是退化均值输出**。若手臂整体位移仍显保守，
  需检查桌面实物场景（空桌面时 clean-table 模型本就不应有大幅动作）

## 2026-08-25 第一次运行诊断：动作幅度太小问题（指令不匹配）

### 运行配置

| 项目 | 值 |
|------|-----|
| VLA 服务器 | http://192.168.0.102:8778（unnorm_key=`g1_clean_table`） |
| 图像服务器 | G1 本体 teleimager-server --rs（pid 见机器人端，端口 60000） |
| robot_type | g1_dex1 |
| 指令 | "Pick up the cup and place it on the table" |
| 参数 | action_horizon=16, exe_steps=16, obs_horizon=2, rollouts=5, freq=30Hz |

### 运行结果

- ✅ 链路正常：VLA 服务器、图像服务器、DDS 机械臂、双夹爪全部连接成功
- ✅ 5 个 rollout × 16 步共 80 步全部执行完毕，无报错
- ❌ 动作幅度小，无实际意义（见下）

### 根因分析

日志证据（每个 rollout 的首个 chunk 诊断）：

```
VLA EE target: L_xyz≈[0.23, 0.19, 0.16], R_xyz≈[0.22, -0.20, 0.17]
dL/dR = 2~5 cm（EE 目标与当前位置差）
夹爪 gripper_L∈[4.456,4.478], gripper_R∈[4.490,4.510]，变化仅 ±0.02
```

1. **指令与模型任务不匹配（主因）**：模型 checkpoint 仅支持以下 13 个 G1 数据集任务
   （`UnifoLM-VLA-Base/dataset_statistics.json`），**没有"抓杯子"任务**：
   g1_stack_block / g1_pack_pencilbox / g1_wipe_table / g1_erase_board /
   g1_bag_insert / g1_pour_medicine / g1_pack_pingpong / g1_organize_tools /
   **g1_clean_table（当前加载）** / g1_prepare_fruit /
   g1_dual_clean_table_left / g1_dual_clean_table_right / g1_fold_towel
   模型收到分布外指令后输出退化为接近训练均值保守动作 → 5 个 rollout 的 EE 目标几乎相同。
2. 夹爪几乎不开合、手臂小幅移动即该退化行为的直接表现。

### 结论与建议

- 客户端程序本身运行正常；幅度小是**任务语义不匹配**导致，不是代码 bug。
- 要获得有效动作：客户端 `--language_instruction` 必须与服务器 `--unnorm_key`
  及桌面实物场景三者一致，例如 unnorm_key=`g1_clean_table` 配指令 "Clean the table"，
  且桌面上摆放待清理杂物。
- 若需"抓杯子"任务，须用包含该任务的数据微调并扩充 dataset_statistics.json。

### 诊断方法备注

robot_client.py 已内置逐 chunk 日志：`VLA EE target`（EE 目标及 dL/dR 偏差）、
`IK joint delta`（关节增量范数）。若 dL/dR 长期 < 5cm 且夹爪不动，优先排查指令/unnorm_key 匹配。
