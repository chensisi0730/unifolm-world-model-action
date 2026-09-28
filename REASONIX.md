## 交互要求 无法使用
1. 你在处理所有问题时，**全程思考过程必须使用中文**（包括需求分析、逻辑拆解、方案选择、步骤推导等所有内部推理环节）；

参考https://github.com/unitreerobotics/teleimager/blob/main/README_zh-CN.md
https://github.com/unitreerobotics/unifolm-world-model-action/blob/main/unitree_deploy/docs/README_cn.md
https://github.com/unitreerobotics/unifolm-vla
https://github.com/unitreerobotics/unifolm-wla


实体机器人是G1 edu u6-zl旗舰版D ，目前配置Dex1‑1 普通夹爪gripper，上半身手臂14个DOF，夹爪2个DOF;下半身腿部12个DOF，腰部3个DOF，整机自由度是31。头部有一个深度相机Intel Realsense D435i 和3D激光雷达LIVOX-MID360，手部有两个腕部普通相机。
G1开发计算单元网络配置:
eth0	192.168.123.164	此刻网络不可达时，使用wlan0
wlan0   192.168.0.117


G1开发计算单元地址为192.168.123.164，用户名：unitree，密码：123 ，终端开头显示的unitree@ubuntu:说明正在通过命令~/bin/sshpass -p '123' ssh unitree@192.168.123.164 <任何命令>远程 SSH 到G1开发计算单元中.
需要在机器人PC2上执行的命令，都通过SSH的方式执行。
conda 环境teleimager中teleimager-server --cf --uvc --v4l2 --rs可以查询到相机配置，python -m teleimager.image_server可以启动图像服务器。

例如：
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "source ~/miniconda3/etc/profile.d/conda.sh && conda activate teleimager && cd ~/work/teleimager && timeout 30 pip install -e ".[all]" && timeout 30 python -m teleimager.image_server --cf" 可以查看机器人的相机信息。

conda activate teleimager && pip show teleimager查看teleimager包源码真实位置。
服务端配置cam_config_server.yaml要根据teleimager环境中的teleimager包的安装位置来决定,实际加载/home/unitree/work/teleimager/cam_config_server.yaml.
查询机器人图像信息的命令如下（都在机器人 PC2 上执行，本地通过 SSH 调用）：

## 1️⃣ 相机发现（teleimager 自带，最常用）

```bash
# 本地执行，SSH 到机器人 PC2
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 \
  "source ~/miniconda3/etc/profile.d/conda.sh && conda activate teleimager \
   && cd ~/work/teleimager && timeout 30 python -m teleimager.image_server --cf"
```
> `--cf` = camera find，列出所有检测到的相机（视频路径、序列号、物理路径），用于填写 `cam_config_server.yaml`。

## 2️⃣ USB 设备列表

```bash
# 列出所有 USB 设备（相机为 1572:xxxx RealSense）
## ~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "lsusb"

# 完整 USB 树（看拓扑、端口、驱动绑定）
## ~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "lsusb -t"
```

## 3️⃣ video 设备属性

```bash
# 列出所有视频设备
## ~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "ls -la /dev/video*"

## 5️⃣ RealSense 专用
```bash
# RealSense 库枚举（D405/D435i 走 librs 驱动时可见）
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 \
  "source ~/miniconda3/etc/profile.d/conda.sh && conda activate teleimager && rs-enumerate-devices"
```

## 7️⃣ 查看当前配置文件
```bash
# 服务端配置（图像服务器使用）
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "cat ~/work/teleimager/cam_config_server.yaml"

# 客户端配置
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "cat ~/work/teleimager/cam_config_client.yaml"
```

## 拷贝：
~/bin/sshpass -p '123' scp -o StrictHostKeyChecking=no \
  /home/css/work/robot/unitree/vla/unifolm-world-model-action-geesun/unifolm-world-model-action/unitree_deploy/scripts/enter_debug_mode.py \
  unitree@192.168.0.117:~/work/enter_debug_mode.py

## 让机器人进入调试模式，释放高层控制,即调用MotionSwitcherClient.ReleaseMode() 把 `ai` 模式释放，进入低层，可收 `rt/lowcmd` ：
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.0.117 \
  "source ~/miniconda3/etc/profile.d/conda.sh && conda activate g1ctrl  && python ~/work/enter_debug_mode.py"


**典型排查流程**：命令 1（发现相机）→ 命令 3（确认设备号）→ 命令 4（验证出图）→ 命令 6（查占用）→ 命令 7（核对配置）




VLA服务器端根据https://github.com/unitreerobotics/unifolm-vla/blob/main/README_cn.md已经部署好，代码位置是本地~/work/robot/unitree/vla/unifolm-vla，地址是：http://192.168.123.99:8778
服务器端模型输出23维的定义如下，和硬件g1_dex1的16维不匹配， 需要自己映射
| 索引   | 含义                     | 维度数 |
|--------|--------------------------|--------|
| 0–2    | 左臂末端XYZ位置          | 3      |
| 3–8    | 左臂末端6D旋转（由RPY转换） | 6      |
| 9–11   | 右臂末端XYZ位置          | 3      |
| 12–17  | 右臂末端6D旋转（由RPY转换） | 6      |
| 18     | 右夹爪开合               | 1      |
| 19     | 左夹爪开合               | 1      |
| 20–22  | 身体/腰部姿态（body [3:6]） | 3      |


VLA服务端和客户端在同一台电脑，不需要配置SSH隧道连接VLA服务器。
VLA客户端程序位置是~/work/robot/unitree/vla/unifolm-world-model-action-geesun/unifolm-world-model-action，部署在有GPU的本地电脑端CONDA环境unitree_deploy中，地址是：192.168.123.99。
每次运行完成后，酌情更新文档README_cn.md和unitree_deploy/RUNLOG.md

# 先执行如下例子，10秒后退出进程：
# (g1ctrl) unitree@ubuntu:~/wangc/unitree_sdk2_python/example/g1/low_level$ python g1_low_level_example.py
在机器人本体（unitree@ubuntu）启动图像服务器,再让机器人进入调试模式，释放高层控制,
## VLA 客户端（`g1_arm.py`）如何用 lowcmd 控制？
运行VLA客户端：
conda activate unitree_deploy &&
python /home/css/work/robot/unitree/vla/unifolm-world-model-action-geesun/unifolm-world-model-action/unitree_deploy/scripts/robot_client.py     --robot_type g1_dex1     --action_horizon 16     --exe_steps 16     --observation_horizon 2     --language_instruction "Prepare the fruit"    --num_rollouts_planned 30     --output_dir ./results     --control_freq 30

**常用任务对照表：**

| task_name | language_instruction |
|-----------|---------------------|
| g1_stack_block | Stack the block |
| g1_pack_pencilbox | Pack the pencil box |
| g1_wipe_table | Wipe the table |
| g1_erase_board | Erase the board |
| g1_bag_insert | Insert into the bag |
| g1_pour_medicine | Pour the medicine |
| g1_pack_pingpong | Pack the ping pong |
| g1_organize_tools | Organize the tools |
| g1_clean_table | Clean the table |
| g1_prepare_fruit | Prepare the fruit |
| g1_fold_towel | Fold the towel |

头部摄像头换成了双目摄像头，腕部相机换成了D405,请读取摄像头信息。
本地客户端电脑也接入了摄像头，用来观察机器人的动作。
## 

每次回答最后回复“人人”。image input is not supported  模型没有多模态能力，不要申请访问图片。



