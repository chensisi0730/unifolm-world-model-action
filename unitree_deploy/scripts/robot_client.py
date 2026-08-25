import argparse
import logging
import os
import time
import cv2
import numpy as np
import torch
import tqdm

from typing import Any, Deque, MutableMapping, OrderedDict
from collections import deque
from pathlib import Path

from unitree_deploy.real_unitree_env import make_real_env
from unitree_deploy.utils.eval_utils import (
    ACTTemporalEnsembler,
    LongConnectionClient,
    populate_queues,
)
from unitree_deploy.robot_devices.arm.g1_arm_ik import G1_29_ArmIK

# -----------------------------------------------------------------------------
# Network & environment defaults
# -----------------------------------------------------------------------------
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
HOST = "192.168.0.102"
PORT = 8778
BASE_URL = f"http://{HOST}:{PORT}"

# fmt: off
INIT_POSE = {
    'g1_dex1': np.array([0.10559805, 0.02726714, -0.01210221, -0.33341318, -0.22513399, -0.02627627, -0.15437093,  0.1273793 , -0.1674708 , -0.11544029, -0.40095493,  0.44332668,  0.11566751,  0.3936641, 5.4, 5.4], dtype=np.float32),
    'g1_realsense': np.array([0.10559805, 0.02726714, -0.01210221, -0.33341318, -0.22513399, -0.02627627, -0.15437093,  0.1273793 , -0.1674708 , -0.11544029, -0.40095493,  0.44332668,  0.11566751,  0.3936641, 5.4, 5.4], dtype=np.float32),
    'z1_dual_dex1_realsense': np.array([-1.0262332,  1.4281361, -1.2149128,  0.6473399, -0.12425245, 0.44945636,  0.89584476,  1.2593982, -1.0737865,  0.6672816, 0.39730102, -0.47400007, 0.9894176, 0.9817477 ], dtype=np.float32),
    'z1_realsense': np.array([-0.06940782, 1.4751548, -0.7554075, 1.0501366, 0.02931615, -0.02810347, -0.99238837], dtype=np.float32),
}
ZERO_ACTION = {
    'g1_dex1': torch.zeros(16, dtype=torch.float32),
    'g1_realsense': torch.zeros(16, dtype=torch.float32),
    'z1_dual_dex1_realsense': torch.zeros(14, dtype=torch.float32),
    'z1_realsense': torch.zeros(7, dtype=torch.float32),
}
CAM_KEY = {
    'g1_dex1': 'cam_high',
    'g1_realsense': 'cam_high',
    'z1_dual_dex1_realsense': 'cam_high',
    'z1_realsense': 'cam_high',
}
# fmt: on


def prepare_observation(args: argparse.Namespace, obs: Any) -> OrderedDict:
    """
    Convert a raw env observation into the model's expected input dict.
    """
    rgb_image = cv2.cvtColor(
        obs.observation["images"][CAM_KEY[args.robot_type]], cv2.COLOR_BGR2RGB)
    observation = {
        "observation.images.top":
        torch.from_numpy(rgb_image).permute(2, 0, 1),
        "observation.state":
        torch.from_numpy(obs.observation["qpos"]),
        "action": ZERO_ACTION[args.robot_type],
    }
    return OrderedDict(observation)


def run_policy(
    args: argparse.Namespace,
    env: Any,
    client: LongConnectionClient,
    temporal_ensembler: ACTTemporalEnsembler,
    cond_obs_queues: MutableMapping[str, Deque[torch.Tensor]],
    output_dir: Path,
    arm_ik: Any = None,
) -> None:
    """
    Single rollout loop:
        1) warm start the robot,
        2) stream observations,
        3) fetch actions from the policy server,
        4) execute with temporal ensembling for smoother control.
    """

    _ = env.step(INIT_POSE[args.robot_type])
    time.sleep(2.0)
    t = 0

    while t < args.exe_steps:
        # Capture observation
        obs = env.get_observation(t)
        # Format observation
        obs = prepare_observation(args, obs)
        cond_obs_queues = populate_queues(cond_obs_queues, obs)

        # Get current gripper values from the robot state (last 2 dims of 16D state)
        current_state = obs["observation.state"]
        gripper_left_q = float(current_state[14]) if len(current_state) > 14 else 0.0
        gripper_right_q = float(current_state[15]) if len(current_state) > 15 else 0.0

        # Call VLA server for action prediction (returns single 23D action)
        pred_action_23d = client.predict_action(
            args.language_instruction, cond_obs_queues,
            arm_ik=arm_ik,
            gripper_left_q=gripper_left_q,
            gripper_right_q=gripper_right_q,
        )
        
        # Map VLA 23D output -> robot 16D joint space action
        action_23d_np = pred_action_23d.cpu().numpy()
        
        if arm_ik is not None and len(action_23d_np) == 23:
            # Use IK to convert 23D end-effector target to 14D arm joint angles
            # Warm-start IK from actual current robot state for better convergence
            current_arm_q = obs["observation.state"][:14].cpu().numpy().astype(np.float64)
            sol_q, _ = arm_ik.ee_proprio_23d_to_arm_ik(
                action_23d_np, current_arm_q=current_arm_q)
            
            # Build 16D action: 14D arm joints + 2D gripper
            env_action_dim = len(INIT_POSE[args.robot_type])  # 16
            base_action = np.zeros(env_action_dim, dtype=np.float32)
            base_action[:14] = sol_q[:14]           # arm joints from IK
            base_action[14] = action_23d_np[19]     # Left gripper (dim 19 in 23D)
            base_action[15] = action_23d_np[18]     # Right gripper (dim 18 in 23D)
            
            if t % 10 == 0:
                logging.info(f"  IK in: {[f'{x:.3f}' for x in current_arm_q[:4]]}... "
                           f"out: {[f'{x:.3f}' for x in sol_q[:4]]}... "
                           f"diff: {[f'{x:.4f}' for x in (sol_q - current_arm_q)[:4]]}")
        else:
            # Fallback: use INIT_POSE for arms, only update grippers
            env_action_dim = len(INIT_POSE[args.robot_type])  # 16
            base_action = np.array(INIT_POSE[args.robot_type])[:env_action_dim]
            if len(action_23d_np) >= 20:
                base_action[14] = action_23d_np[19]  # Left gripper
                base_action[15] = action_23d_np[18]  # Right gripper
        
        pred_actions = torch.from_numpy(base_action).unsqueeze(0)  # shape: (1, 16)
        
        logging.debug(f"VLA returned {pred_action_23d.shape}D -> mapped to robot action {pred_actions.shape}")

        # VLA returns single-step actions, so execute directly without temporal ensembling
        action = pred_actions[0].cpu().numpy()  # shape: (16,)
        
        logging.info(f"Executing action step {t}: gripper_L={action[14]:.3f}, gripper_R={action[15]:.3f}")

        # Execute the single action step immediately
        print(f">>> Exec => step {t} action: gripper_L={action[14]:.3f}, R={action[15]:.3f}", flush=True)
        
        # Maintain real-time loop at control_freq Hz
        t1 = time.time()
        obs = env.step(action)
        time.sleep(max(0, 1 / args.control_freq - time.time() + t1))
        t += 1

        # Update queues for next prediction cycle
        obs = prepare_observation(args, obs)
        cond_obs_queues = populate_queues(cond_obs_queues, obs)


def run_eval(args: argparse.Namespace) -> None:
    # Build URL from CLI args (override defaults if provided)
    vla_url = f"http://{args.host}:{args.port}"
    logging.info(f"Connecting to VLA server at {vla_url}")
    client = LongConnectionClient(vla_url, max_retries=3)

    # Initialize ACT temporal moving-averge smoother
    temporal_ensembler = ACTTemporalEnsembler(temporal_ensemble_coeff=0.01,
                                              chunk_size=args.action_horizon,
                                              exe_steps=args.exe_steps)
    temporal_ensembler.reset()

    # Initialize observation and action horizon queue
    cond_obs_queues = {
        "observation.images.top": deque(maxlen=args.observation_horizon),
        "observation.state": deque(maxlen=args.observation_horizon),
        "action": deque(
            maxlen=16),  # NOTE: HAND CODE AS THE MODEL PREDCIT FUTURE 16 STEPS
    }

    # Initialize FK/IK solver for G1 arm (converts between joint angles and EE poses)
    arm_ik = None
    if args.robot_type in ("g1_dex1", "g1_realsense"):
        logging.info("Initializing G1 Arm IK/FK solver...")
        arm_ik = G1_29_ArmIK(unit_test=False, visualization=False)

    env = make_real_env(
        robot_type=args.robot_type,
        dt=1 / args.control_freq,
    )
    env.connect()

    try:
        for episode_idx in tqdm.tqdm(range(0, args.num_rollouts_planned)):
            output_dir = Path(args.output_dir) / f"episode_{episode_idx:03d}"
            output_dir.mkdir(parents=True, exist_ok=True)
            run_policy(args, env, client, temporal_ensembler, cond_obs_queues,
                       output_dir, arm_ik=arm_ik)
    finally:
        env.close()


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host",
                        type=str,
                        default=HOST,
                        help=f"VLA server host (default: {HOST})")
    parser.add_argument("--port",
                        type=int,
                        default=PORT,
                        help=f"VLA server port (default: {PORT})")
    parser.add_argument("--robot_type",
                        type=str,
                        default="g1_dex1",
                        help="The type of the robot embodiment.")
    parser.add_argument(
        "--action_horizon",
        type=int,
        default=16,
        help="Number of future actions, predicted by the policy, to keep",
    )
    parser.add_argument(
        "--exe_steps",
        type=int,
        default=16,
        help=
        "Number of future actions to execute, which must be less than the above action horizon.",
    )
    parser.add_argument(
        "--observation_horizon",
        type=int,
        default=2,
        help="Number of most recent frames/states to consider.",
    )
    parser.add_argument(
        "--language_instruction",
        type=str,
        default="Pack black camera into box",
        help="The language instruction provided to the policy server.",
    )
    parser.add_argument("--num_rollouts_planned",
                        type=int,
                        default=10,
                        help="The number of rollouts to run.")
    parser.add_argument("--output_dir",
                        type=str,
                        default="./results",
                        help="The directory for saving results.")
    parser.add_argument("--control_freq",
                        type=float,
                        default=30,
                        help="The Low-level control frequency in Hz.")
    return parser


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    
    try:
        run_eval(args)
    except KeyboardInterrupt:
        logging.info("\n[Client] Ctrl+C received - shutting down gracefully...")
