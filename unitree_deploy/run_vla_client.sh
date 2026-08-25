#!/bin/bash
# VLA Client启动脚本 - G1 RealSense D435i配置
# 服务器地址: http://192.168.0.102:8778

cd /home/css/work/robot/unitree/vla/unifolm-world-model-action-geesun/unifolm-world-model-action/unitree_deploy

source ~/miniconda3/etc/profile.d/conda.sh
conda activate unitree_deploy

python scripts/robot_client.py \
    --host 192.168.0.102 \
    --port 8778 \
    --robot_type g1_dex1 \
    --language_instruction "Pick up the object" \
    --control_freq 30 \
    --action_horizon 10 \
    --exe_steps 10 \
    --output_dir /home/css/work/vla_results
