import json
import logging
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import cv2
import numpy as np
import pandas as pd
import pyarrow as pa
import requests
import torch
import torchvision
from datasets import load_from_disk
from datasets.features.features import register_feature
from safetensors.torch import load_file

import json_numpy

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)


class NumpyEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles numpy arrays."""
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        return super().default(obj)


class LongConnectionClient:
    def __init__(self, base_url, max_retries=3):
        self.session = requests.Session()
        self.base_url = base_url
        self.max_retries = max_retries

    def send_post(self, endpoint, json_data, timeout=30.0):
        """send POST request to VLA server with retry logic"""
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(1, self.max_retries + 1):
            try:
                logging.info(f"Sending request to {url} (attempt {attempt}/{self.max_retries})")
                # Use json_numpy to properly serialize numpy arrays
                payload = json_numpy.dumps(json_data)
                response = self.session.post(url, data=payload, 
                                            headers={"Content-Type": "application/json"},
                                            timeout=timeout)
                
                if response.status_code == 200:
                    data = response.json()
                    # Check if it's an error string
                    if isinstance(data, str) and data == "error":
                        logging.error(f"VLA server returned 'error' - check request format. Payload size: {len(payload)} bytes")
                        continue  # retry on next iteration
                    # Server may return json_numpy-encoded string in double-encode mode
                    if isinstance(data, str):
                        data = json_numpy.loads(data)
                    
                    return data
                    
                # Non-200 status code - log and retry
                logging.warning(f"HTTP {response.status_code}: {response.text[:200]}")
                        
            except requests.exceptions.ConnectionError as e:
                logging.error(f"[Attempt {attempt}/{self.max_retries}] Connection failed to {url}: {e}")
            except requests.exceptions.Timeout as e:
                logging.error(f"[Attempt {attempt}/{self.max_retries}] Request timed out: {e}")
            except Exception as e:
                logging.error(f"[Attempt {attempt}/{self.max_retries}] An error occurred: {e}")
            
            # Wait before retry (don't wait on last attempt)
            if attempt < self.max_retries:
                wait_time = 2 ** (attempt - 1)  # exponential backoff: 1s, 2s...
                logging.info(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
        
        raise ConnectionError(
            f"Failed to connect to VLA server at {url} after {self.max_retries} attempts. "
            f"Please check if the server is running and reachable."
        )

    def close(self):
        """close session"""
        self.session.close()

    def predict_action(self, language_instruction, batch, arm_ik=None,
                       gripper_left_q=0.0, gripper_right_q=0.0) -> torch.Tensor:
        """
        Send observation to VLA server and get predicted action.
        
        VLA server expects format per unifolm-vla API (double-encoded):
        {
            "encoded": "{\"observations\": [{\"full_image\": numpy_array, \"state\": numpy_array, \"instruction\": str}]}"
        }
        
        Server returns action array of shape (25 timesteps, 23 dims) for G1 tasks.
        We return the first timestep's action as the prediction to execute.
        
        Args:
            language_instruction: Task description string.
            batch: Observation dict with queues.
            arm_ik: G1_29_ArmIK instance for computing FK to convert 14D joints -> 23D proprio.
            gripper_left_q: Current left gripper joint value.
            gripper_right_q: Current right gripper joint value.
        """
        # Extract the latest observation from batch queues
        state = list(batch["observation.state"])[-1]  # most recent frame
        image = list(batch["observation.images.top"])[-1]  # most recent image
        
        # Convert tensors to numpy for JSON serialization
        # Image: (C, H, W) -> (H, W, C), values in [0, 255] uint8
        if image.dim() == 3:
            img_np = image.permute(1, 2, 0).cpu().numpy()
            # Ensure uint8 format for VLA server
            if img_np.dtype != np.uint8:
                if img_np.min() < 0 or img_np.max() <= 1.0:
                    img_np = (img_np * 255).astype(np.uint8)
                else:
                    img_np = img_np.astype(np.uint8)
        
        # State: proprioception - convert joint angles to 23D EE proprio via FK
        state_np = state.cpu().numpy().astype(np.float64)
        
        if arm_ik is not None and len(state_np) in (14, 16):
            # Extract arm joints (first 14D) and gripper values (if 16D)
            arm_q = state_np[:14]
            gripper_l = float(state_np[14]) if len(state_np) > 14 else gripper_left_q
            gripper_r = float(state_np[15]) if len(state_np) > 15 else gripper_right_q
            state_23d = arm_ik.joints_to_ee_proprio_23d(
                arm_q=arm_q,
                gripper_left_q=gripper_l,
                gripper_right_q=gripper_r,
            )
        else:
            state_23d = state_np
        
        logging.debug(f"Image: dtype={img_np.dtype}, shape={img_np.shape}, range=[{img_np.min()}, {img_np.max()}]")
        logging.debug(f"State: shape={state_23d.shape}, values={[round(float(x),3) for x in state_23d[:6]]}...")
        
        # Build VLA server request format with raw numpy arrays
        observations = [{
            "full_image": img_np,       # HxWx3 numpy array
            "state": state_23d,         # 23D proprioception numpy array
            "instruction": language_instruction,
        }]
        
        inner_payload = {"observations": observations}
        
        # Use double-encode: serialize inner payload with json_numpy to preserve numpy arrays,
        # then wrap in "encoded" field so server's json.loads can reconstruct them properly
        encoded_str = json_numpy.dumps(inner_payload)
        data = {"encoded": encoded_str}
        
        endpoint = "/act"
        response = self.send_post(endpoint, data)
        
        # VLA server returns action array of shape (25 timesteps, 23 dims) for G1 tasks
        # Response may already be numpy array (from json_numpy) or nested list
        action_np = np.array(response, dtype=np.float32)
        logging.debug(f"VLA returned action shape: {action_np.shape}")
        
        # Return the first timestep's action as prediction to execute now
        if action_np.ndim == 2 and action_np.shape[0] > 1:
            action = torch.tensor(action_np[0]).float()  # First step only
        else:
            action = torch.tensor(action_np).float()
        
        return action


class ACTTemporalEnsembler:
    def __init__(self, temporal_ensemble_coeff: float, chunk_size: int, exe_steps: int) -> None:
        """Temporal ensembling as described in Algorithm 2 of https://arxiv.org/abs/2304.13705.

        The weights are calculated as wᵢ = exp(-temporal_ensemble_coeff * i) where w₀ is the oldest action.
        They are then normalized to sum to 1 by dividing by Σwᵢ. Here's some intuition around how the
        coefficient works:
            - Setting it to 0 uniformly weighs all actions.
            - Setting it positive gives more weight to older actions.
            - Setting it negative gives more weight to newer actions.
        NOTE: The default value for `temporal_ensemble_coeff` used by the original ACT work is 0.01. This
        results in older actions being weighed more highly than newer actions (the experiments documented in
        https://github.com/huggingface/lerobot/pull/319 hint at why highly weighing new actions might be
        detrimental: doing so aggressively may diminish the benefits of action chunking).

        Here we use an online method for computing the average rather than caching a history of actions in
        order to compute the average offline. For a simple 1D sequence it looks something like:

        ```
        import torch

        seq = torch.linspace(8, 8.5, 100)
        print(seq)

        m = 0.01
        exp_weights = torch.exp(-m * torch.arange(len(seq)))
        print(exp_weights)

        # Calculate offline
        avg = (exp_weights * seq).sum() / exp_weights.sum()
        print("offline", avg)

        # Calculate online
        for i, item in enumerate(seq):
            if i == 0:
                avg = item
                continue
            avg *= exp_weights[:i].sum()
            avg += item * exp_weights[i]
            avg /= exp_weights[:i+1].sum()
        print("online", avg)
        ```
        """
        self.chunk_size = chunk_size
        self.ensemble_weights = torch.exp(-temporal_ensemble_coeff * torch.arange(chunk_size))
        self.ensemble_weights_cumsum = torch.cumsum(self.ensemble_weights, dim=0)
        self.exe_steps = exe_steps
        self.reset()

    def reset(self):
        """Resets the online computation variables."""
        self.ensembled_actions = None
        # (chunk_size,) count of how many actions are in the ensemble for each time step in the sequence.
        self.ensembled_actions_count = None

    def update(self, actions):
        """
        Takes a (batch, chunk_size, action_dim) sequence of actions, update the temporal ensemble for all
        time steps, and pop/return the next batch of actions in the sequence.
        """
        self.ensemble_weights = self.ensemble_weights.to(device=actions.device)
        self.ensemble_weights_cumsum = self.ensemble_weights_cumsum.to(device=actions.device)
        if self.ensembled_actions is None:
            # Initializes `self._ensembled_action` to the sequence of actions predicted during the first
            # time step of the episode.
            self.ensembled_actions = actions.clone()
            # Note: The last dimension is unsqueeze to make sure we can broadcast properly for tensor
            # operations later.
            self.ensembled_actions_count = torch.ones(
                (self.chunk_size, 1), dtype=torch.long, device=self.ensembled_actions.device
            )
        else:
            # self.ensembled_actions will have shape (batch_size, chunk_size - 1, action_dim). Compute
            # the online update for those entries.
            self.ensembled_actions *= self.ensemble_weights_cumsum[self.ensembled_actions_count - 1]
            self.ensembled_actions += (
                actions[:, : -self.exe_steps] * self.ensemble_weights[self.ensembled_actions_count]
            )
            self.ensembled_actions /= self.ensemble_weights_cumsum[self.ensembled_actions_count]
            self.ensembled_actions_count = torch.clamp(self.ensembled_actions_count + 1, max=self.chunk_size)
            # The last action, which has no prior online average, needs to get concatenated onto the end.
            self.ensembled_actions = torch.cat([self.ensembled_actions, actions[:, -self.exe_steps :]], dim=1)
            self.ensembled_actions_count = torch.cat(
                # [self.ensembled_actions_count, torch.ones_like(self.ensembled_actions_count[-self.exe_steps:])]
                [
                    self.ensembled_actions_count,
                    torch.ones((self.exe_steps, 1), dtype=torch.long, device=self.ensembled_actions_count.device),
                ]
            )
        # "Consume" the first action.

        actions, self.ensembled_actions, self.ensembled_actions_count = (
            self.ensembled_actions[:, : self.exe_steps],
            self.ensembled_actions[:, self.exe_steps :],
            self.ensembled_actions_count[self.exe_steps :],
        )
        return actions


@dataclass
class VideoFrame:
    """
    Provides a type for a dataset containing video frames.

    Example:

    ```python
    data_dict = [{"image": {"path": "videos/episode_0.mp4", "timestamp": 0.3}}]
    features = {"image": VideoFrame()}
    Dataset.from_dict(data_dict, features=Features(features))
    ```
    """

    pa_type: ClassVar[Any] = pa.struct({"path": pa.string(), "timestamp": pa.float32()})
    _type: str = field(default="VideoFrame", init=False, repr=False)

    def __call__(self):
        return self.pa_type


with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        "'register_feature' is experimental and might be subject to breaking changes in the future.",
        category=UserWarning,
    )
    # to make VideoFrame available in HuggingFace `datasets`
    register_feature(VideoFrame, "VideoFrame")


def get_image(cam_list, target_shape=None, save_image=False):
    curr_images = []
    for cam in cam_list:
        color, _ = cam.get_frame()
        if save_image:
            cv2.imwrite("/home/world-model-x/output.png", color)
        color = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
        if target_shape:
            color = cv2.resize(color, target_shape)
        curr_images.append(color)
    curr_images = np.stack(curr_images, axis=0)
    return curr_images


def load_action_from_dataset(dataset_dir, episode_id):
    data = load_from_disk(dataset_dir + "/train")
    episode_data = load_file(dataset_dir + "/meta_data/episode_data_index.safetensors")
    start_id = episode_data["from"][episode_id]
    end_id = episode_data["to"][episode_id]
    actions = torch.FloatTensor(data["action"][start_id:end_id])
    return actions


def load_stats_from_prompt_dir(dataset_dir, prompt_dir, subdir=""):
    dataset_dir += subdir + "/meta_data"
    stats = load_file(dataset_dir + "/stats.safetensors")
    return stats


def populate_queues(queues, batch):
    for key in batch:
        # Ignore keys not in the queues already (leaving the responsibility to the caller to make sure the
        # queues have the keys they want).
        if key not in queues:
            continue
        if len(queues[key]) != queues[key].maxlen:
            # initialize by copying the first observation several times until the queue is full
            while len(queues[key]) != queues[key].maxlen:
                queues[key].append(batch[key])
        else:
            # add latest observation to the queue
            queues[key].append(batch[key])
    return queues


def action_safe_checking(action, action_max, action_min, threshold=0.01):
    over_max = any(action - threshold > action_max.cpu().numpy())
    over_min = any(action + threshold < action_min.cpu().numpy())
    return not (over_max or over_min)


def get_init_pose(dataset_dir, start_id=0):
    # load all par
    dataset_dir_path = Path(dataset_dir) / "data" / "chunk-000"
    parquet_files = list(dataset_dir_path.glob("*.parquet"))
    parquet_files = sorted([str(f) for f in parquet_files])
    first_rows = [pd.read_parquet(f, engine="pyarrow").iloc[[0]] for f in parquet_files]
    df = pd.concat(first_rows, ignore_index=True)
    action_array = np.stack(df["action"].values)
    init_pose = action_array[192:193, ...]
    return init_pose


def save_image(obs, num_step=None, output_dir=None):
    rgb_image = cv2.cvtColor(obs.observation["images"]["cam_left_high"], cv2.COLOR_BGR2RGB)
    cv2.imwrite(f"{output_dir}/top_{num_step:06d}.png", rgb_image)


def log_to_tensorboard(writer, data, tag, fps=10):
    if isinstance(data, torch.Tensor) and data.dim() == 5:
        video = data
        n = video.shape[0]
        video = video.permute(2, 0, 1, 3, 4)  # t,n,c,h,w
        frame_grids = [
            torchvision.utils.make_grid(framesheet, nrow=int(n), padding=0) for framesheet in video
        ]  # [3, n*h, 1*w]
        grid = torch.stack(frame_grids, dim=0)  # stack in temporal dim [t, 3, n*h, w]
        grid = (grid + 1.0) / 2.0
        grid = grid.unsqueeze(dim=0)
        writer.add_video(tag, grid, fps=fps)
