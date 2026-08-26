import argparse
import logging
import os
import time
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
    NOTE: env._get_obs() already converted images BGR->RGB; do NOT convert again.
    """
    rgb_image = obs.observation["images"][CAM_KEY[args.robot_type]]
    observation = {
        "observation.images.top":
        torch.from_numpy(rgb_image).permute(2, 0, 1),
        "observation.state":
        torch.from_numpy(obs.observation["qpos"]),
    }
    if "cam_left_wrist" in obs.observation["images"]:
        left_wrist = obs.observation["images"]["cam_left_wrist"]
        observation["observation.images.left_wrist"] = torch.from_numpy(left_wrist).permute(2, 0, 1)
    if "cam_right_wrist" in obs.observation["images"]:
        right_wrist = obs.observation["images"]["cam_right_wrist"]
        observation["observation.images.right_wrist"] = torch.from_numpy(right_wrist).permute(2, 0, 1)
    return OrderedDict(observation)


def run_policy(
    args: argparse.Namespace,
    env: Any,
    client: LongConnectionClient,
    temporal_ensembler: ACTTemporalEnsembler,
    cond_obs_queues: MutableMapping[str, Deque[torch.Tensor]],
    output_dir: Path,
    arm_ik: Any = None,
    reset_to_init: bool = True,
) -> None:
    """
    Single rollout loop:
        1) warm start the robot (first rollout only),
        2) stream observations,
        3) fetch actions from the policy server,
        4) execute with temporal ensembling for smoother control.
    """

    if reset_to_init:
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

        # Call VLA server for an action chunk.
        pred_actions_23d = client.predict_action(
            args.language_instruction, cond_obs_queues,
            arm_ik=arm_ik,
            gripper_left_q=gripper_left_q,
            gripper_right_q=gripper_right_q,
        )
        
        # Map VLA 23D output chunk -> robot 16D joint-space action chunk.
        # Keep ALL predicted steps: the temporal ensembler requires the input
        # length to equal its chunk_size (the model's full prediction window),
        # so cross-prediction ensembling actually works.
        actions_23d_np = pred_actions_23d.cpu().numpy()
        if actions_23d_np.ndim == 1:
            actions_23d_np = actions_23d_np[None, :]
        if actions_23d_np.shape[0] > args.model_chunk_size:
            actions_23d_np = actions_23d_np[:args.model_chunk_size]
        elif actions_23d_np.shape[0] < args.model_chunk_size:
            logging.warning(
                f"VLA returned {actions_23d_np.shape[0]} steps < model_chunk_size "
                f"{args.model_chunk_size}; temporal ensembling degraded for this cycle."
            )

        robot_actions = []
        current_arm_q = obs["observation.state"][:14].cpu().numpy().astype(np.float64)
        initial_arm_q = current_arm_q.copy()
        current_proprio_23d = None
        if arm_ik is not None:
            current_proprio_23d = arm_ik.joints_to_ee_proprio_23d(
                current_arm_q,
                gripper_left_q=gripper_left_q,
                gripper_right_q=gripper_right_q,
            )
        # Print current EE proprio from IK FK (diagnostic)
        if current_proprio_23d is not None:
            logging.info(
                "  Current EE proprio: "
                f"L_xyz={[f'{x:.3f}' for x in current_proprio_23d[0:3]]}, "
                f"R_xyz={[f'{x:.3f}' for x in current_proprio_23d[9:12]]}"
            )

        # Print 25-step EE trajectory range for this chunk (diagnostic for arm amplitude)
        if len(actions_23d_np) > 0 and actions_23d_np.shape[1] >= 23:
            left_xyz_all = actions_23d_np[:, 0:3]
            right_xyz_all = actions_23d_np[:, 9:12]
            logging.info(
                "  VLA 25-step EE range: "
                f"L_xyz min={[f'{x:.3f}' for x in left_xyz_all.min(0)]}, "
                f"max={[f'{x:.3f}' for x in left_xyz_all.max(0)]}, "
                f"span={[f'{x:.3f}' for x in left_xyz_all.max(0)-left_xyz_all.min(0)]}; "
                f"R_xyz min={[f'{x:.3f}' for x in right_xyz_all.min(0)]}, "
                f"max={[f'{x:.3f}' for x in right_xyz_all.max(0)]}, "
                f"span={[f'{x:.3f}' for x in right_xyz_all.max(0)-right_xyz_all.min(0)]}"
            )

        for idx, action_23d_np in enumerate(actions_23d_np):
            env_action_dim = len(INIT_POSE[args.robot_type])  # 16
            if arm_ik is not None and len(action_23d_np) == 23:
                sol_q, _ = arm_ik.ee_proprio_23d_to_arm_ik(
                    action_23d_np, current_arm_q=current_arm_q)

                base_action = np.zeros(env_action_dim, dtype=np.float32)
                base_action[:14] = sol_q[:14]
                base_action[14] = action_23d_np[19]
                base_action[15] = action_23d_np[18]
                current_arm_q = sol_q[:14].astype(np.float64)

                if idx == 0:
                    q_diff = sol_q[:14] - initial_arm_q
                    logging.info(f"  IK in: {[f'{x:.3f}' for x in initial_arm_q[:4]]}... "
                                 f"out: {[f'{x:.3f}' for x in sol_q[:4]]}...")
                    if current_proprio_23d is not None:
                        left_delta = action_23d_np[0:3] - current_proprio_23d[0:3]
                        right_delta = action_23d_np[9:12] - current_proprio_23d[9:12]
                        logging.info(
                            "  VLA EE target: "
                            f"L_xyz={[f'{x:.3f}' for x in action_23d_np[0:3]]}, "
                            f"R_xyz={[f'{x:.3f}' for x in action_23d_np[9:12]]}, "
                            f"dL={[f'{x:.3f}' for x in left_delta]}, "
                            f"dR={[f'{x:.3f}' for x in right_delta]}, "
                            f"body={[f'{x:.3f}' for x in action_23d_np[20:23]]}"
                        )
                    logging.info(
                        "  IK joint delta: "
                        f"norm={np.linalg.norm(q_diff):.4f}, "
                        f"max_abs={np.max(np.abs(q_diff)):.4f}, "
                        f"first4={[f'{x:.4f}' for x in q_diff[:4]]}"
                    )
            else:
                base_action = np.array(INIT_POSE[args.robot_type])[:env_action_dim]
                if len(action_23d_np) >= 20:
                    base_action[14] = action_23d_np[19]
                    base_action[15] = action_23d_np[18]
            robot_actions.append(base_action)

        pred_actions = torch.from_numpy(np.stack(robot_actions, axis=0)).unsqueeze(0)
        logging.debug(f"VLA returned {pred_actions_23d.shape} -> mapped to robot actions {pred_actions.shape}")

        actions = temporal_ensembler.update(pred_actions)[0]

        for n in range(min(args.exe_steps - t, actions.shape[0])):
            action = actions[n].cpu().numpy()
            logging.info(f"Executing action step {t}: gripper_L={action[14]:.3f}, gripper_R={action[15]:.3f}")
            print(f">>> Exec => step {t} action: gripper_L={action[14]:.3f}, R={action[15]:.3f}", flush=True)

            t1 = time.time()
            obs = env.step(action)
            time.sleep(max(0, 1 / args.control_freq - time.time() + t1))
            t += 1
            actual_q = obs.observation["qpos"][:14]
            logging.info(
                f"Executing step {t}: sent_arm_q[:4]={np.round(action[:4], 3)}, "
                f"actual_q[:4]={np.round(actual_q[:4], 3)}, "
                f"gripper_L={action[14]:.3f}, gripper_R={action[15]:.3f}"
            )
            print(f">>> Exec => step {t} action: gripper_L={action[14]:.3f}, R={action[15]:.3f}", flush=True)

            obs_for_queue = prepare_observation(args, obs)
            cond_obs_queues = populate_queues(cond_obs_queues, obs_for_queue)
            cond_obs_queues = populate_queues(
                cond_obs_queues,
                {"action": torch.from_numpy(action.astype(np.float32))},
            )


def run_eval(args: argparse.Namespace) -> None:
    # Build URL from CLI args (override defaults if provided)
    vla_url = f"http://{args.host}:{args.port}"
    logging.info(f"Connecting to VLA server at {vla_url}")
    client = LongConnectionClient(vla_url, max_retries=3)

    def make_cond_obs_queues():
        cond_obs_queues = {
            "observation.images.top": deque(maxlen=args.observation_horizon),
            "observation.images.left_wrist": deque(maxlen=args.observation_horizon),
            "observation.images.right_wrist": deque(maxlen=args.observation_horizon),
            "observation.state": deque(maxlen=args.observation_horizon),
            "action": deque(maxlen=args.action_horizon),
        }
        return populate_queues(
            cond_obs_queues,
            {"action": ZERO_ACTION[args.robot_type]},
        )

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
            # chunk_size must equal the model's prediction window (e.g. 25 for
            # G1_EE_6D), NOT the number of executed steps, otherwise the
            # ensembler degenerates into pass-through (exe_steps == chunk_size
            # makes actions[:, :-exe_steps] empty).
            if args.exe_steps >= args.model_chunk_size:
                logging.warning(
                    f"exe_steps ({args.exe_steps}) >= model_chunk_size "
                    f"({args.model_chunk_size}): temporal ensemble is inactive."
                )
            temporal_ensembler = ACTTemporalEnsembler(temporal_ensemble_coeff=0.01,
                                                      chunk_size=args.model_chunk_size,
                                                      exe_steps=args.exe_steps)
            cond_obs_queues = make_cond_obs_queues()
            output_dir = Path(args.output_dir) / f"episode_{episode_idx:03d}"
            output_dir.mkdir(parents=True, exist_ok=True)
            run_policy(args, env, client, temporal_ensembler, cond_obs_queues,
                       output_dir, arm_ik=arm_ik,
                       reset_to_init=(episode_idx == 0))
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
        "--model_chunk_size",
        type=int,
        default=25,
        help="Full action-chunk length returned by the VLA model "
             "(G1_EE_6D: 25). Temporal ensemble uses this as its window.",
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
