"""
This file contains utilities for recording frames from cameras. For more info look at `OpenCVCamera` docstring.
"""

import struct
import threading
import time
from collections import deque
from multiprocessing import shared_memory

import cv2
import numpy as np
import zmq

from unitree_deploy.robot_devices.cameras.configs import ImageClientCameraConfig
from unitree_deploy.robot_devices.robots_devices_utils import (
    RobotDeviceAlreadyConnectedError,
    RobotDeviceNotConnectedError,
)
from unitree_deploy.utils.rich_logger import log_error, log_info, log_success, log_warning


class ImageClient:
    def __init__(
        self,
        tv_img_shape=None,
        tv_img_shm_name=None,
        left_wrist_img_shape=None,
        left_wrist_img_shm_name=None,
        right_wrist_img_shape=None,
        right_wrist_img_shm_name=None,
        image_show=False,
        server_address="192.168.0.109",  # G1机器人IP地址
        head_port=55555,
        left_wrist_port=55556,
        right_wrist_port=55557,
        unit_test=False,
    ):
        """
        tv_img_shape: User's expected head camera resolution shape (H, W, C). It should match the output of the image service terminal.
        tv_img_shm_name: Shared memory is used to easily transfer images across processes to the Vuer.
        left_wrist_img_shape: Left wrist camera resolution shape (H, W, C).
        left_wrist_img_shm_name: Shared memory for left wrist camera.
        right_wrist_img_shape: Right wrist camera resolution shape (H, W, C).
        right_wrist_img_shm_name: Shared memory for right wrist camera.
        image_show: Whether to display received images in real time.
        server_address: The ip address to execute the image server script.
        head_port: ZMQ port for head camera.
        left_wrist_port: ZMQ port for left wrist camera.
        right_wrist_port: ZMQ port for right wrist camera.
        Unit_Test: When both server and client are True, it can be used to test the image transfer latency, \
                   network jitter, frame loss rate and other information.
        """
        self.running = True
        self._stream_threads = []
        self._image_show = image_show
        self._server_address = server_address
        self._head_port = head_port
        self._left_wrist_port = left_wrist_port
        self._right_wrist_port = right_wrist_port

        self.tv_img_shape = tv_img_shape
        self.left_wrist_img_shape = left_wrist_img_shape
        self.right_wrist_img_shape = right_wrist_img_shape

        self.tv_enable_shm = False
        if self.tv_img_shape is not None and tv_img_shm_name is not None:
            self.tv_image_shm = shared_memory.SharedMemory(name=tv_img_shm_name)
            self.tv_img_array = np.ndarray(tv_img_shape, dtype=np.uint8, buffer=self.tv_image_shm.buf)
            self.tv_enable_shm = True

        self.left_wrist_enable_shm = False
        if self.left_wrist_img_shape is not None and left_wrist_img_shm_name is not None:
            self.left_wrist_image_shm = shared_memory.SharedMemory(name=left_wrist_img_shm_name)
            self.left_wrist_img_array = np.ndarray(left_wrist_img_shape, dtype=np.uint8, buffer=self.left_wrist_image_shm.buf)
            self.left_wrist_enable_shm = True

        self.right_wrist_enable_shm = False
        if self.right_wrist_img_shape is not None and right_wrist_img_shm_name is not None:
            self.right_wrist_image_shm = shared_memory.SharedMemory(name=right_wrist_img_shm_name)
            self.right_wrist_img_array = np.ndarray(right_wrist_img_shape, dtype=np.uint8, buffer=self.right_wrist_image_shm.buf)
            self.right_wrist_enable_shm = True

        # Performance evaluation parameters
        self._enable_performance_eval = unit_test
        if self._enable_performance_eval:
            self._init_performance_metrics()

    def _init_performance_metrics(self):
        self._frame_count = 0  # Total frames received
        self._last_frame_id = -1  # Last received frame ID

        # Real-time FPS calculation using a time window
        self._time_window = 1.0  # Time window size (in seconds)
        self._frame_times = deque()  # Timestamps of frames received within the time window

        # Data transmission quality metrics
        self._latencies = deque()  # Latencies of frames within the time window
        self._lost_frames = 0  # Total lost frames
        self._total_frames = 0  # Expected total frames based on frame IDs

    def _update_performance_metrics(self, timestamp, frame_id, receive_time):
        # Update latency
        latency = receive_time - timestamp
        self._latencies.append(latency)

        # Remove latencies outside the time window
        while self._latencies and self._frame_times and self._latencies[0] < receive_time - self._time_window:
            self._latencies.popleft()

        # Update frame times
        self._frame_times.append(receive_time)
        # Remove timestamps outside the time window
        while self._frame_times and self._frame_times[0] < receive_time - self._time_window:
            self._frame_times.popleft()

        # Update frame counts for lost frame calculation
        expected_frame_id = self._last_frame_id + 1 if self._last_frame_id != -1 else frame_id
        if frame_id != expected_frame_id:
            lost = frame_id - expected_frame_id
            if lost < 0:
                log_info(f"[Image Client] Received out-of-order frame ID: {frame_id}")
            else:
                self._lost_frames += lost
                log_info(
                    f"[Image Client] Detected lost frames: {lost}, Expected frame ID: {expected_frame_id}, Received frame ID: {frame_id}"
                )
        self._last_frame_id = frame_id
        self._total_frames = frame_id + 1

        self._frame_count += 1

    def _print_performance_metrics(self, receive_time):
        if self._frame_count % 30 == 0:
            # Calculate real-time FPS
            real_time_fps = len(self._frame_times) / self._time_window if self._time_window > 0 else 0

            # Calculate latency metrics
            if self._latencies:
                avg_latency = sum(self._latencies) / len(self._latencies)
                max_latency = max(self._latencies)
                min_latency = min(self._latencies)
                jitter = max_latency - min_latency
            else:
                avg_latency = max_latency = min_latency = jitter = 0

            # Calculate lost frame rate
            lost_frame_rate = (self._lost_frames / self._total_frames) * 100 if self._total_frames > 0 else 0

            log_info(
                f"[Image Client] Real-time FPS: {real_time_fps:.2f}, Avg Latency: {avg_latency * 1000:.2f} ms, Max Latency: {max_latency * 1000:.2f} ms, \
                  Min Latency: {min_latency * 1000:.2f} ms, Jitter: {jitter * 1000:.2f} ms, Lost Frame Rate: {lost_frame_rate:.2f}%"
            )

    def _close(self):
        for sock in getattr(self, '_sockets', []):
            sock.close()
        if hasattr(self, '_context'):
            self._context.term()
        if self._image_show:
            cv2.destroyAllWindows()
        log_success("Image client has been closed.")

    def _receive_stream(self, port, img_shape, shm_array_func, label):
        """Single camera ZMQ receive loop running in its own thread."""
        sock = self._context.socket(zmq.SUB)
        # Keep only the newest JPEG frame for low-latency control.
        sock.setsockopt(zmq.CONFLATE, 1)
        sock.setsockopt(zmq.RCVTIMEO, 200)
        sock.connect(f"tcp://{self._server_address}:{port}")
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        self._sockets.append(sock)

        log_warning(f"[{label}] Connected to port {port}, waiting for data...")
        try:
            while self.running:
                try:
                    message = sock.recv()
                except zmq.Again:
                    continue
                receive_time = time.time()

                if self._enable_performance_eval:
                    header_size = struct.calcsize("dI")
                    try:
                        header = message[:header_size]
                        jpg_bytes = message[header_size:]
                        timestamp, frame_id = struct.unpack("dI", header)
                    except struct.error as e:
                        log_error(f"[{label}] Error unpacking header: {e}, discarding message.")
                        continue
                else:
                    jpg_bytes = message

                np_img = np.frombuffer(jpg_bytes, dtype=np.uint8)
                current_image = cv2.imdecode(np_img, cv2.IMREAD_COLOR)
                if current_image is None:
                    log_error(f"[{label}] Failed to decode image.")
                    continue

                shm_array = shm_array_func()
                if shm_array is not None:
                    if current_image.shape[:2] == shm_array.shape[:2]:
                        np.copyto(shm_array, current_image)
                    else:
                        resized = cv2.resize(current_image, (shm_array.shape[1], shm_array.shape[0]))
                        np.copyto(shm_array, resized)

                if self._enable_performance_eval:
                    self._update_performance_metrics(timestamp, frame_id, receive_time)
                    self._print_performance_metrics(receive_time)

        except KeyboardInterrupt:
            pass
        except Exception as e:
            if self.running:
                log_error(f"[{label}] An error occurred: {e}")

    def receive_process(self):
        self._context = zmq.Context()
        self._sockets = []

        log_warning("\nImage client has started, waiting to receive data...")

        threads = []

        if self.tv_enable_shm:
            t = threading.Thread(
                target=self._receive_stream,
                args=(self._head_port, self.tv_img_shape,
                      lambda: self.tv_img_array if self.tv_enable_shm else None,
                      "HeadCamera"),
                daemon=True)
            threads.append(t)

        if self.left_wrist_enable_shm:
            t = threading.Thread(
                target=self._receive_stream,
                args=(self._left_wrist_port, self.left_wrist_img_shape,
                      lambda: self.left_wrist_img_array if self.left_wrist_enable_shm else None,
                      "LeftWristCamera"),
                daemon=True)
            threads.append(t)

        if self.right_wrist_enable_shm:
            t = threading.Thread(
                target=self._receive_stream,
                args=(self._right_wrist_port, self.right_wrist_img_shape,
                      lambda: self.right_wrist_img_array if self.right_wrist_enable_shm else None,
                      "RightWristCamera"),
                daemon=True)
            threads.append(t)

        self._stream_threads = threads

        for t in threads:
            t.start()

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.running = False
        finally:
            self._close()


class ImageClientCamera:
    def __init__(self, config: ImageClientCameraConfig):
        self.config = config
        self.fps = config.fps
        self.head_camera_type = config.head_camera_type
        self.head_camera_image_shape = config.head_camera_image_shape
        self.head_camera_id_numbers = config.head_camera_id_numbers
        self.wrist_camera_type = config.wrist_camera_type
        self.wrist_camera_image_shape = config.wrist_camera_image_shape
        self.wrist_camera_id_numbers = config.wrist_camera_id_numbers
        self.aspect_ratio_threshold = config.aspect_ratio_threshold
        self.mock = config.mock
        self.head_zmq_port = config.head_zmq_port
        self.left_wrist_zmq_port = config.left_wrist_zmq_port
        self.right_wrist_zmq_port = config.right_wrist_zmq_port
        self.server_address = config.server_address

        self.is_binocular = (
            len(self.head_camera_id_numbers) > 1
            or self.head_camera_image_shape[1] / self.head_camera_image_shape[0] > self.aspect_ratio_threshold
        )

        self.has_wrist_camera = self.wrist_camera_type is not None

        self.tv_img_shape = (
            (self.head_camera_image_shape[0], self.head_camera_image_shape[1] * 2, 3)
            if self.is_binocular
            and not (self.head_camera_image_shape[1] / self.head_camera_image_shape[0] > self.aspect_ratio_threshold)
            else (self.head_camera_image_shape[0], self.head_camera_image_shape[1], 3)
        )

        self.tv_img_shm = shared_memory.SharedMemory(create=True, size=np.prod(self.tv_img_shape) * np.uint8().itemsize)
        self.tv_img_array = np.ndarray(self.tv_img_shape, dtype=np.uint8, buffer=self.tv_img_shm.buf)

        self.left_wrist_img_shape = None
        self.left_wrist_img_shm = None
        self.left_wrist_img_array = None
        self.right_wrist_img_shape = None
        self.right_wrist_img_shm = None
        self.right_wrist_img_array = None

        if self.has_wrist_camera:
            self.left_wrist_img_shape = (self.wrist_camera_image_shape[0], self.wrist_camera_image_shape[1], 3)
            self.left_wrist_img_shm = shared_memory.SharedMemory(
                create=True, size=np.prod(self.left_wrist_img_shape) * np.uint8().itemsize
            )
            self.left_wrist_img_array = np.ndarray(self.left_wrist_img_shape, dtype=np.uint8, buffer=self.left_wrist_img_shm.buf)

            self.right_wrist_img_shape = (self.wrist_camera_image_shape[0], self.wrist_camera_image_shape[1], 3)
            self.right_wrist_img_shm = shared_memory.SharedMemory(
                create=True, size=np.prod(self.right_wrist_img_shape) * np.uint8().itemsize
            )
            self.right_wrist_img_array = np.ndarray(self.right_wrist_img_shape, dtype=np.uint8, buffer=self.right_wrist_img_shm.buf)

        self.img_shm_name = self.tv_img_shm.name
        self.is_connected = False

    def connect(self):
        try:
            if self.is_connected:
                raise RobotDeviceAlreadyConnectedError(f"ImageClient is already connected.")

            self.img_client = ImageClient(
                tv_img_shape=self.tv_img_shape,
                tv_img_shm_name=self.tv_img_shm.name,
                left_wrist_img_shape=self.left_wrist_img_shape,
                left_wrist_img_shm_name=self.left_wrist_img_shm.name if self.left_wrist_img_shm else None,
                right_wrist_img_shape=self.right_wrist_img_shape,
                right_wrist_img_shm_name=self.right_wrist_img_shm.name if self.right_wrist_img_shm else None,
                server_address=self.server_address,
                head_port=self.head_zmq_port,
                left_wrist_port=self.left_wrist_zmq_port,
                right_wrist_port=self.right_wrist_zmq_port,
            )

            image_receive_thread = threading.Thread(target=self.img_client.receive_process, daemon=True)
            self._image_receive_thread = image_receive_thread
            image_receive_thread.start()

            self.is_connected = True

        except Exception as e:
            self.disconnect()
            log_error(f"❌ Error in ImageClientCamera.connect: {e}")

    def read(self) -> np.ndarray:
        pass

    def async_read(self):
        try:
            if not self.is_connected:
                raise RobotDeviceNotConnectedError(
                    "ImageClient is not connected. Try running `camera.connect()` first."
                )
            current_tv_image = self.tv_img_array.copy()
            current_left_wrist_image = self.left_wrist_img_array.copy() if self.left_wrist_img_array is not None else None
            current_right_wrist_image = self.right_wrist_img_array.copy() if self.right_wrist_img_array is not None else None

            colors = {}
            if self.is_binocular:
                colors["cam_left_high"] = current_tv_image[:, : self.tv_img_shape[1] // 2]
                colors["cam_right_high"] = current_tv_image[:, self.tv_img_shape[1] // 2 :]
            else:
                colors["cam_high"] = current_tv_image

            if current_left_wrist_image is not None:
                colors["cam_left_wrist"] = current_left_wrist_image
            if current_right_wrist_image is not None:
                colors["cam_right_wrist"] = current_right_wrist_image

            return colors

        except RobotDeviceNotConnectedError:
            raise
        except Exception as e:
            # Transient read errors must NOT tear down the connection;
            # keep the receiver threads alive and retry on the next call.
            log_error(f"❌ Error in ImageClientCamera.async_read (connection kept): {e}")
            return None

    def disconnect(self):
        if not self.is_connected:
            return

        try:
            # Stop receiver loops and wait for them to leave shared-memory writes
            # before unlinking the backing segments.
            if self.img_client is not None:
                self.img_client.running = False
            receive_thread = getattr(self, "_image_receive_thread", None)
            if receive_thread is not None and receive_thread is not threading.current_thread():
                receive_thread.join(timeout=2.0)

            self.tv_img_shm.unlink()
        except FileNotFoundError:
            pass
        try:
            self.tv_img_shm.close()
        except FileNotFoundError:
            pass

        for shm in [self.left_wrist_img_shm, self.right_wrist_img_shm]:
            if shm is not None:
                try:
                    shm.unlink()
                except FileNotFoundError:
                    pass
                try:
                    shm.close()
                except FileNotFoundError:
                    pass

        self.is_connected = False

    def __del__(self):
        if getattr(self, "is_connected", False):
            self.disconnect()
