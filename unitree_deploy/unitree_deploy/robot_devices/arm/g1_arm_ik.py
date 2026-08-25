import casadi
import meshcat.geometry as mg
import numpy as np
import pinocchio as pin
from pinocchio import casadi as cpin
from pinocchio.visualize import MeshcatVisualizer

from unitree_deploy.utils.weighted_moving_filter import WeightedMovingFilter

# Get absolute path to the package directory for URDF loading
import os
_ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "g1")


class G1_29_ArmIK:
    def __init__(self, unit_test=False, visualization=False):
        np.set_printoptions(precision=5, suppress=True, linewidth=200)

        self.unit_test = unit_test
        self.visualization = visualization

        urdf_path = os.path.join(_ASSET_DIR, "g1_body29_hand14.urdf")
        if not os.path.exists(urdf_path):
            raise FileNotFoundError(f"URDF file not found: {urdf_path}")

        if not self.unit_test:
            self.robot = pin.RobotWrapper.BuildFromURDF(
                urdf_path,
                _ASSET_DIR,
            )
        else:
            self.robot = pin.RobotWrapper.BuildFromURDF(
                urdf_path,
                _ASSET_DIR,
            )  # for test

        self.mixed_jointsToLockIDs = [
            "left_hip_pitch_joint",
            "left_hip_roll_joint",
            "left_hip_yaw_joint",
            "left_knee_joint",
            "left_ankle_pitch_joint",
            "left_ankle_roll_joint",
            "right_hip_pitch_joint",
            "right_hip_roll_joint",
            "right_hip_yaw_joint",
            "right_knee_joint",
            "right_ankle_pitch_joint",
            "right_ankle_roll_joint",
            "waist_yaw_joint",
            "waist_roll_joint",
            "waist_pitch_joint",
            "left_hand_thumb_0_joint",
            "left_hand_thumb_1_joint",
            "left_hand_thumb_2_joint",
            "left_hand_middle_0_joint",
            "left_hand_middle_1_joint",
            "left_hand_index_0_joint",
            "left_hand_index_1_joint",
            "right_hand_thumb_0_joint",
            "right_hand_thumb_1_joint",
            "right_hand_thumb_2_joint",
            "right_hand_index_0_joint",
            "right_hand_index_1_joint",
            "right_hand_middle_0_joint",
            "right_hand_middle_1_joint",
        ]

        self.reduced_robot = self.robot.buildReducedRobot(
            list_of_joints_to_lock=self.mixed_jointsToLockIDs,
            reference_configuration=np.array([0.0] * self.robot.model.nq),
        )

        self.reduced_robot.model.addFrame(
            pin.Frame(
                "L_ee",
                self.reduced_robot.model.getJointId("left_wrist_yaw_joint"),
                pin.SE3(np.eye(3), np.array([0.05, 0, 0]).T),
                pin.FrameType.OP_FRAME,
            )
        )

        self.reduced_robot.model.addFrame(
            pin.Frame(
                "R_ee",
                self.reduced_robot.model.getJointId("right_wrist_yaw_joint"),
                pin.SE3(np.eye(3), np.array([0.05, 0, 0]).T),
                pin.FrameType.OP_FRAME,
            )
        )

        # Recreate data after adding custom frames so oMf has the right size
        self.reduced_robot.data = self.reduced_robot.model.createData()

        # for i in range(self.reduced_robot.model.nframes):
        #     frame = self.reduced_robot.model.frames[i]
        #     frame_id = self.reduced_robot.model.getFrameId(frame.name)

        # Creating Casadi models and data for symbolic computing
        self.cmodel = cpin.Model(self.reduced_robot.model)
        self.cdata = self.cmodel.createData()

        # Creating symbolic variables
        self.cq = casadi.SX.sym("q", self.reduced_robot.model.nq, 1)
        self.cTf_l = casadi.SX.sym("tf_l", 4, 4)
        self.cTf_r = casadi.SX.sym("tf_r", 4, 4)
        cpin.framesForwardKinematics(self.cmodel, self.cdata, self.cq)

        # Get the hand joint ID and define the error function
        self.L_hand_id = self.reduced_robot.model.getFrameId("L_ee")
        self.R_hand_id = self.reduced_robot.model.getFrameId("R_ee")

        self.translational_error = casadi.Function(
            "translational_error",
            [self.cq, self.cTf_l, self.cTf_r],
            [
                casadi.vertcat(
                    self.cdata.oMf[self.L_hand_id].translation - self.cTf_l[:3, 3],
                    self.cdata.oMf[self.R_hand_id].translation - self.cTf_r[:3, 3],
                )
            ],
        )
        self.rotational_error = casadi.Function(
            "rotational_error",
            [self.cq, self.cTf_l, self.cTf_r],
            [
                casadi.vertcat(
                    cpin.log3(self.cdata.oMf[self.L_hand_id].rotation @ self.cTf_l[:3, :3].T),
                    cpin.log3(self.cdata.oMf[self.R_hand_id].rotation @ self.cTf_r[:3, :3].T),
                )
            ],
        )

        # Defining the optimization problem
        self.opti = casadi.Opti()
        self.var_q = self.opti.variable(self.reduced_robot.model.nq)
        self.var_q_last = self.opti.parameter(self.reduced_robot.model.nq)  # for smooth
        self.param_tf_l = self.opti.parameter(4, 4)
        self.param_tf_r = self.opti.parameter(4, 4)
        self.translational_cost = casadi.sumsqr(self.translational_error(self.var_q, self.param_tf_l, self.param_tf_r))
        self.rotation_cost = casadi.sumsqr(self.rotational_error(self.var_q, self.param_tf_l, self.param_tf_r))
        self.regularization_cost = casadi.sumsqr(self.var_q)
        self.smooth_cost = casadi.sumsqr(self.var_q - self.var_q_last)

        # Setting optimization constraints and goals
        self.opti.subject_to(
            self.opti.bounded(
                self.reduced_robot.model.lowerPositionLimit,
                self.var_q,
                self.reduced_robot.model.upperPositionLimit,
            )
        )
        self.opti.minimize(
            50 * self.translational_cost + self.rotation_cost + 0.02 * self.regularization_cost + 0.1 * self.smooth_cost
        )

        opts = {
            "ipopt": {"print_level": 0, "max_iter": 50, "tol": 1e-6},
            "print_time": False,  # print or not
            "calc_lam_p": False,  # https://github.com/casadi/casadi/wiki/FAQ:-Why-am-I-getting-%22NaN-detected%22in-my-optimization%3F
        }
        self.opti.solver("ipopt", opts)

        self.init_data = np.zeros(self.reduced_robot.model.nq)
        self.smooth_filter = WeightedMovingFilter(np.array([0.4, 0.3, 0.2, 0.1]), 14)
        self.vis = None

        if self.visualization:
            # Initialize the Meshcat visualizer for visualization
            self.vis = MeshcatVisualizer(
                self.reduced_robot.model, self.reduced_robot.collision_model, self.reduced_robot.visual_model
            )
            self.vis.initViewer(open=True)
            self.vis.loadViewerModel("pinocchio")
            self.vis.displayFrames(True, frame_ids=[101, 102], axis_length=0.15, axis_width=5)
            self.vis.display(pin.neutral(self.reduced_robot.model))

            # Enable the display of end effector target frames with short axis lengths and greater width.
            frame_viz_names = ["L_ee_target", "R_ee_target"]
            frame_axis_positions = (
                np.array([[0, 0, 0], [1, 0, 0], [0, 0, 0], [0, 1, 0], [0, 0, 0], [0, 0, 1]]).astype(np.float32).T
            )
            frame_axis_colors = (
                np.array([[1, 0, 0], [1, 0.6, 0], [0, 1, 0], [0.6, 1, 0], [0, 0, 1], [0, 0.6, 1]]).astype(np.float32).T
            )
            axis_length = 0.1
            axis_width = 10
            for frame_viz_name in frame_viz_names:
                self.vis.viewer[frame_viz_name].set_object(
                    mg.LineSegments(
                        mg.PointsGeometry(
                            position=axis_length * frame_axis_positions,
                            color=frame_axis_colors,
                        ),
                        mg.LineBasicMaterial(
                            linewidth=axis_width,
                            vertexColors=True,
                        ),
                    )
                )

    # If the robot arm is not the same size as your arm :)
    def scale_arms(self, human_left_pose, human_right_pose, human_arm_length=0.60, robot_arm_length=0.75):
        scale_factor = robot_arm_length / human_arm_length
        robot_left_pose = human_left_pose.copy()
        robot_right_pose = human_right_pose.copy()
        robot_left_pose[:3, 3] *= scale_factor
        robot_right_pose[:3, 3] *= scale_factor
        return robot_left_pose, robot_right_pose

    def solve_ik(self, left_wrist, right_wrist, current_lr_arm_motor_q=None, current_lr_arm_motor_dq=None):
        if current_lr_arm_motor_q is not None:
            self.init_data = current_lr_arm_motor_q
        self.opti.set_initial(self.var_q, self.init_data)

        # left_wrist, right_wrist = self.scale_arms(left_wrist, right_wrist)
        if self.visualization:
            self.vis.viewer["L_ee_target"].set_transform(left_wrist)  # for visualization
            self.vis.viewer["R_ee_target"].set_transform(right_wrist)  # for visualization

        self.opti.set_value(self.param_tf_l, left_wrist)
        self.opti.set_value(self.param_tf_r, right_wrist)
        self.opti.set_value(self.var_q_last, self.init_data)  # for smooth

        try:
            self.opti.solve()
            # sol = self.opti.solve_limited()

            sol_q = self.opti.value(self.var_q)
            self.smooth_filter.add_data(sol_q)
            sol_q = self.smooth_filter.filtered_data

            v = current_lr_arm_motor_dq * 0.0 if current_lr_arm_motor_dq is not None else (sol_q - self.init_data) * 0.0

            self.init_data = sol_q

            sol_tauff = pin.rnea(
                self.reduced_robot.model,
                self.reduced_robot.data,
                sol_q,
                v,
                np.zeros(self.reduced_robot.model.nv),
            )

            if self.visualization:
                self.vis.display(sol_q)  # for visualization

            return sol_q, sol_tauff

        except Exception as e:
            print(f"ERROR in convergence, plotting debug info.{e}")

            sol_q = self.opti.debug.value(self.var_q)
            self.smooth_filter.add_data(sol_q)
            sol_q = self.smooth_filter.filtered_data

            v = current_lr_arm_motor_dq * 0.0 if current_lr_arm_motor_dq is not None else (sol_q - self.init_data) * 0.0

            self.init_data = sol_q

            sol_tauff = pin.rnea(
                self.reduced_robot.model,
                self.reduced_robot.data,
                sol_q,
                v,
                np.zeros(self.reduced_robot.model.nv),
            )

            print(
                f"sol_q:{sol_q} \nmotorstate: \n{current_lr_arm_motor_q} \nleft_pose: \n{left_wrist} \nright_pose: \n{right_wrist}"
            )
            if self.visualization:
                self.vis.display(sol_q)  # for visualization

            # return sol_q, sol_tauff
            return current_lr_arm_motor_q, np.zeros(self.reduced_robot.model.nv)
        
    def solve_tau(self, current_lr_arm_motor_q=None, current_lr_arm_motor_dq=None):
        try:
            sol_tauff = pin.rnea(
                self.reduced_robot.model,
                self.reduced_robot.data,
                current_lr_arm_motor_q,
                np.zeros(14),
                np.zeros(self.reduced_robot.model.nv),
            )
            return sol_tauff

        except Exception as e:
            print(f"ERROR in convergence, plotting debug info.{e}")
            return np.zeros(self.reduced_robot.model.nv)

    def solve_fk(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Compute forward kinematics for left and right end-effectors.

        Args:
            q: Joint angles for the reduced robot (14D: 7 per arm).

        Returns:
            (left_ee, right_ee): Each is a dict with keys:
                'position': np.ndarray of shape (3,) - XYZ position in base frame
                'rotation': np.ndarray of shape (3,3) - rotation matrix
        """
        pin.framesForwardKinematics(self.reduced_robot.model, self.reduced_robot.data, q)

        left_se3 = self.reduced_robot.data.oMf[self.L_hand_id]
        right_se3 = self.reduced_robot.data.oMf[self.R_hand_id]

        left_ee = {
            'position': left_se3.translation.copy(),
            'rotation': left_se3.rotation.copy(),
        }
        right_ee = {
            'position': right_se3.translation.copy(),
            'rotation': right_se3.rotation.copy(),
        }
        return left_ee, right_ee

    def rotation_to_r6(self, rot_matrix: np.ndarray) -> np.ndarray:
        """
        Convert a 3x3 rotation matrix to 6D rotation representation.
        R6 = first two columns of the rotation matrix, flattened.

        Args:
            rot_matrix: 3x3 rotation matrix

        Returns:
            np.ndarray of shape (6,)
        """
        return rot_matrix[:, :2].T.reshape(-1)

    def joints_to_ee_proprio_23d(
        self,
        arm_q: np.ndarray,
        gripper_left_q: float = 0.0,
        gripper_right_q: float = 0.0,
        waist_rpy: np.ndarray | None = None,
    ) -> np.ndarray:
        """
        Convert robot joint states to the 23D end-effector proprio format
        expected by the VLA model.

        23D layout:
            [0-2]   Left EEF XYZ position
            [3-8]   Left EEF R6 rotation (first two rows of rotation matrix, flattened)
            [9-11]  Right EEF XYZ position
            [12-17] Right EEF R6 rotation
            [18]    Right gripper open/close
            [19]    Left gripper open/close
            [20-22] Waist/body RPY (defaults to zeros)

        Args:
            arm_q: 14D joint angles for both arms (7 per arm).
            gripper_left_q: Left gripper joint value (scalar).
            gripper_right_q: Right gripper joint value (scalar).
            waist_rpy: Optional 3D waist roll-pitch-yaw. Defaults to zeros.

        Returns:
            np.ndarray of shape (23,) in the VLA proprio format.
        """
        left_ee, right_ee = self.solve_fk(arm_q)
        left_r6 = self.rotation_to_r6(left_ee['rotation'])
        right_r6 = self.rotation_to_r6(right_ee['rotation'])

        if waist_rpy is None:
            waist_rpy = np.zeros(3)

        proprio = np.concatenate([
            left_ee['position'],       # 0-2:  Left XYZ
            left_r6,                    # 3-8:  Left R6
            right_ee['position'],      # 9-11: Right XYZ
            right_r6,                   # 12-17: Right R6
            np.array([gripper_right_q]),  # 18: Right gripper
            np.array([gripper_left_q]),   # 19: Left gripper
            waist_rpy,                  # 20-22: Waist RPY
        ])

        return proprio

    def ee_proprio_23d_to_arm_ik(
        self,
        proprio_23d: np.ndarray,
        current_arm_q: np.ndarray | None = None,
        current_arm_dq: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Convert a 23D end-effector proprio/action target back to joint angles via IK.

        Args:
            proprio_23d: 23D target in VLA format.
            current_arm_q: Current 14D arm joint angles for IK warm start.
            current_arm_dq: Current 14D arm joint velocities.

        Returns:
            (sol_q, sol_tauff): Joint angles and torques.
        """
        left_pos = proprio_23d[0:3]
        left_r6 = proprio_23d[3:9]
        right_pos = proprio_23d[9:12]
        right_r6 = proprio_23d[12:18]

        left_rot = self._r6_to_rotation(left_r6)
        right_rot = self._r6_to_rotation(right_r6)

        left_tf = np.eye(4)
        left_tf[:3, :3] = left_rot
        left_tf[:3, 3] = left_pos

        right_tf = np.eye(4)
        right_tf[:3, :3] = right_rot
        right_tf[:3, 3] = right_pos

        return self.solve_ik(left_tf, right_tf, current_arm_q, current_arm_dq)

    def _r6_to_rotation(self, r6: np.ndarray) -> np.ndarray:
        """
        Convert 6D rotation representation back to a 3x3 rotation matrix.
        Uses Gram-Schmidt orthogonalization on the first two columns.

        Args:
            r6: np.ndarray of shape (6,) - flattened first two rows of rotation matrix.

        Returns:
            np.ndarray of shape (3,3) - orthogonal rotation matrix.
        """
        r6_flat = np.asarray(r6).flatten()
        r1 = r6_flat[:3]
        r2 = r6_flat[3:6]

        # Gram-Schmidt: make r1 unit, then r2 orthogonal to r1 and unit
        r1 = r1 / (np.linalg.norm(r1) + 1e-8)
        r2 = r2 - np.dot(r2, r1) * r1
        r2 = r2 / (np.linalg.norm(r2) + 1e-8)

        # r3 = r1 x r2
        r3 = np.cross(r1, r2)

        return np.stack([r1, r2, r3], axis=1)
