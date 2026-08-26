## 交互要求 无法使用
1. 你在处理所有问题时，**全程思考过程必须使用中文**（包括需求分析、逻辑拆解、方案选择、步骤推导等所有内部推理环节）；


实体机器人是G1 edu u6-zl旗舰版D ，目前配置Dex1‑1 普通夹爪gripper，上半身手臂14个DOF，夹爪2个DOF;下半身腿部12个DOF，腰部3个DOF，整机自由度是31。头部有一个深度相机Intel Realsense D435i 和3D激光雷达LIVOX-MID360，手部有两个腕部普通相机。
G1开发计算单元网络配置:
eth0	192.168.123.164	


G1开发计算单元地址为192.168.123.164，用户名：unitree，密码：123 ，终端开头显示的unitree@ubuntu:说明正在通过命令~/bin/sshpass -p '123' ssh unitree@192.168.123.164 <任何命令>远程 SSH 到G1开发计算单元中.
conda 环境teleimager中teleimager-server --cf可以查询到相机配置，python -m teleimager.image_server可以启动图像服务器。
例如可以~/bin/sshpass -p '123' ssh unitree@192.168.123.164 "/home/unitree/miniconda3/bin/conda env list"查看机器人G1开发计算单元有哪些CONDA环境，
~/bin/sshpass -p '123' ssh -o StrictHostKeyChecking=no unitree@192.168.123.164 "source ~/miniconda3/etc/profile.d/conda.sh && conda activate teleimager && cd ~/teleimager && timeout 30 python -m teleimager.image_server --cf" 可以查看机器人的相机信息。代码在机器人的unitree@ubuntu:~/work目录。

VLA服务器端根据https://github.com/unitreerobotics/unifolm-vla/blob/main/README_cn.md已经部署好，代码位置是本地~/work/robot/unitree/vla/unifolm-vla，地址是：http://192.168.123.99:8778
服务器端模型输出23维的定义如下，和硬件g1_dex1的16维不匹配， 需要自己映射。
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
每次运行完成后，检查更新文档。需要在机器人PC2上执行的命令，你都通过SSH的方式执行。
先在机器人本体（unitree@ubuntu）启动图像服务器,再让机器人进入调试模式，释放高层控制，再运行VLA客户端，并检查是否正常运行。
运行命令：
conda activate unitree_deploy

python /home/css/work/robot/unitree/vla/unifolm-world-model-action-geesun/unifolm-world-model-action/unitree_deploy/scripts/robot_client.py     --robot_type g1_dex1     --action_horizon 16     --exe_steps 16     --observation_horizon 2     --language_instruction "fold the towel"    --num_rollouts_planned 5     --output_dir ./results     --control_freq 30     
每次回答最后回复“瞄瞄”。