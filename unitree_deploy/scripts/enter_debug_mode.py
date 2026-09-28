#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G1 进入调试模式(低层控制): 释放当前占用的高层控制模式 (MotionSwitcherClient.ReleaseMode)

在机器人 PC2 的 g1ctrl conda 环境中执行:
    python ~/work/enter_debug_mode.py

释放后机器人进入低层控制, 可接收 rt/lowcmd (如 VLA 客户端 g1_arm.py)。
"""
import sys
import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient


def main():
    # DDS 域初始化 (domain 0; 机器人本体 eth0=192.168.123.164 网段)
    ChannelFactoryInitialize(0)

    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()

    status, result = msc.CheckMode()
    print(f"[Before] status={status}, mode={result}")
    if not result['name']:
        print("No high-level mode occupied - already in low-level (debug) mode")
        return 0
    while result['name']:
        print(f"Releasing mode: {result['name']} ...")
        msc.ReleaseMode()
        time.sleep(1)
        status, result = msc.CheckMode()
    print(f"[After] status={status}, mode={result}")
    print("Debug mode entered OK (high-level released, rt/lowcmd now available)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
