import os
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from cast_interfaces.action import (
    CastingPick,
    CruciblePick,
    CrucibleReturn,
    DieStamp,
    MetalPour,
    PatternPick,
    PatternReturn,
    ReturnToBase,
    TatLoad,
)


class MyCobotController:
    def __init__(self, node, port="/dev/ttyJETCOBOT", baud=1000000):
        self._node = node
        self._robot = None
        self._last_coords = [0.0, 0.0, 0.0]
        if os.environ.get("CAST_MOVE_DRY_RUN", "").lower() in ("1", "true", "yes"):
            node.get_logger().info("MyCobot dry-run mode enabled")
            return
        try:
            from pymycobot.mycobot import MyCobot

            self._robot = MyCobot(port, baud)
            node.get_logger().info(f"MyCobot connected on {port} @ {baud}")
        except Exception as exc:
            node.get_logger().warn(f"MyCobot unavailable, using dry-run mode: {exc}")

    def send_angles(self, angles, speed):
        if self._robot is not None:
            self._robot.send_angles(list(angles), speed)
            return
        self._node.get_logger().info(f"send_angles speed={speed} angles={angles}")

    def is_moving(self):
        if self._robot is not None:
            return bool(self._robot.is_moving())
        return False

    def get_coords(self):
        if self._robot is not None:
            coords = self._robot.get_coords()
            if coords and len(coords) >= 3:
                self._last_coords = [float(coords[0]), float(coords[1]), float(coords[2])]
        return list(self._last_coords)

    def send_coords(self, coords, speed, mode):
        if self._robot is not None:
            current = self._robot.get_coords()
            if current and len(current) >= 6:
                current[0], current[1], current[2] = coords[0], coords[1], coords[2]
                self._robot.send_coords(current, speed, mode)
                return
        self._last_coords = list(coords)
        self._node.get_logger().info(f"send_coords speed={speed} coords={coords}")

    def set_gripper_value(self, value, speed):
        if self._robot is not None:
            self._robot.set_gripper_value(value, speed)
            return
        self._node.get_logger().info(f"set_gripper_value value={value} speed={speed}")


class CastActionServer(Node):
    def __init__(self, node_name, action_type, action_name, runner):
        super().__init__(node_name)
        self._action_type = action_type
        self._runner = runner
        self._mc = MyCobotController(self)
        self._lock = threading.Lock()
        self._is_executing = False
        self._server = ActionServer(
            self,
            action_type,
            action_name,
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )
        self.get_logger().info(f"{node_name} started")

    def goal_callback(self, goal_request):
        del goal_request
        with self._lock:
            if self._is_executing:
                return GoalResponse.REJECT
            return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        del goal_handle
        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        with self._lock:
            self._is_executing = True
        result = self._action_type.Result()
        try:
            ok = self._runner(self, goal_handle)
            result.success = ok
            if ok:
                result.message = f"{self._action_type.__name__} completed"
                goal_handle.succeed()
            else:
                result.message = f"{self._action_type.__name__} canceled"
                goal_handle.canceled()
        except Exception as exc:
            self.get_logger().error(f"Exception: {exc}")
            result.success = False
            result.message = "Exception"
            goal_handle.abort()
        finally:
            with self._lock:
                self._is_executing = False
        return result

    def publish_feedback(self, goal_handle, stage, step, total):
        feedback = self._action_type.Feedback()
        feedback.cur_stage = stage
        feedback.progress_pct = int(step * 100 / total)
        goal_handle.publish_feedback(feedback)
        self.get_logger().info(f"[{feedback.progress_pct}%] {stage}")

    def check_canceled(self, goal_handle):
        if goal_handle.is_cancel_requested:
            self.get_logger().warn("Goal canceled")
            return True
        return False

    def _sleep(self, goal_handle, seconds):
        end_time = time.monotonic() + seconds
        while time.monotonic() < end_time:
            if self.check_canceled(goal_handle):
                return False
            time.sleep(min(0.1, end_time - time.monotonic()))
        return True

    def _wait_move(self, goal_handle, timeout=15.0):
        """로봇이 실제로 멈출 때까지 폴링. 완료 후 True, 취소/타임아웃 시 False."""
        time.sleep(0.3)  # 로봇이 움직이기 시작할 때까지 여유
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.check_canceled(goal_handle):
                return False
            if not self._mc.is_moving():
                return True
            time.sleep(0.1)
        self.get_logger().warn("move timeout — proceeding anyway")
        return not goal_handle.is_cancel_requested

    def move_angles(self, goal_handle, stage, step, total, angles, speed=50, delay_sec=3):
        if self.check_canceled(goal_handle):
            return False
        self._mc.send_angles(angles, speed)
        ok = self._wait_move(goal_handle, timeout=delay_sec + 10)
        if ok:
            self.publish_feedback(goal_handle, stage, step, total)
        return ok

    def grip_open(self, goal_handle, stage, step, total):
        if self.check_canceled(goal_handle):
            return False
        self._mc.set_gripper_value(100, 100)
        ok = self._sleep(goal_handle, 1)  # 그리퍼는 is_moving 미지원, 고정 대기
        if ok:
            self.publish_feedback(goal_handle, stage, step, total)
        return ok

    def grip_close(self, goal_handle, stage, step, total):
        if self.check_canceled(goal_handle):
            return False
        self._mc.set_gripper_value(0, 100)
        ok = self._sleep(goal_handle, 1)
        if ok:
            self.publish_feedback(goal_handle, stage, step, total)
        return ok

    def move_z(self, goal_handle, stage, step, total, delta_z, speed=30, delay_sec=2):
        if self.check_canceled(goal_handle):
            return False
        coords = self._mc.get_coords()
        coords[2] += delta_z
        self._mc.send_coords(coords, speed, 0)
        ok = self._wait_move(goal_handle, timeout=delay_sec + 10)
        if ok:
            self.publish_feedback(goal_handle, stage, step, total)
        return ok


def _pattern_id(goal_handle):
    return ((goal_handle.request.pattern_id - 1) % 3) + 1


def return_to_base(server, goal_handle):
    return server.move_angles(goal_handle, "Return to base", 1, 1, [0, 0, 0, 0, 0, 45], 50, 1)


def pattern_pick(server, goal_handle):
    pid = _pattern_id(goal_handle)
    if pid == 1:
        steps = [
            ("angles", "PP1: home", [0, 0, 0, 0, 0, 45], 50, 1),
            ("open", "PP1: open"),
            ("angles", "PP1: to pickup ready", [90, 0, 0, 0, 0, 45]),
            ("angles", "PP1: to pickup", [90, 17.5, -144.8, 38, 0, 45]),
            ("close", "PP1: grip"),
            ("z", "PP1: lift", 50),
            ("angles", "PP1: retract", [90, 0, 0, 0, 0, 45]),
            ("angles", "PP1: home", [0, 0, 0, 0, 0, 45]),
        ]
    elif pid == 2:
        steps = [
            ("angles", "PP2: home", [0, 0, 0, 0, 0, 45], 50, 1),
            ("open", "PP2: open"),
            ("angles", "PP2: to pickup ready", [90, 0, 0, 0, 0, 45]),
            ("angles", "PP2: to pickup", [90, -16, -114, 42, 0, 45]),
            ("close", "PP2: grip"),
            ("z", "PP2: lift", 50),
            ("angles", "PP2: retract", [90, 0, 0, 0, 0, 45]),
            ("angles", "PP2: home", [0, 0, 0, 0, 0, 45]),
        ]
    else:
        steps = [
            ("angles", "PP3: home", [0, 0, 0, 0, 0, 45], 50, 1),
            ("open", "PP3: open"),
            ("angles", "PP3: to pickup ready", [90, 0, 0, -90, 0, 45]),
            ("angles", "PP3: to pickup", [90, -43, -69, 23, 0, 45]),
            ("close", "PP3: grip"),
            ("z", "PP3: lift", 50),
            ("angles", "PP3: home", [0, 0, 0, 0, 0, 45]),
        ]
    return run_steps(server, goal_handle, steps)


def die_stamp(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "DS: over mold", [0, 0, 0, -17.31, 0, -45]),
        ("angles", "DS: press mold", [0, -76.6, 0, -17.31, 0, -45], 100),
        ("angles", "DS: retract", [0, 0, 0, 0, 0, -45], 50, 1),
    ])


def pattern_return(server, goal_handle):
    pid = _pattern_id(goal_handle)
    if pid == 1:
        steps = [
            ("angles", "PR1: to storage ready", [90, 0, 0, 0, 0, 45]),
            ("angles", "PR1: above storage", [90, 25.2, -111.5, -7, 0, 45]),
            ("z", "PR1: place pattern", -60),
            ("open", "PR1: release"),
            ("z", "PR1: lift", 60),
            ("angles", "PR1: retract", [90, 0, 0, 0, 0, 45]),
        ]
    elif pid == 2:
        steps = [
            ("angles", "PR2: to storage ready", [90, 0, 0, 0, 0, 45]),
            ("angles", "PR2: above storage", [90, -10, -63, -17, 0, 45]),
            ("z", "PR2: place pattern", -70),
            ("open", "PR2: release"),
            ("z", "PR2: lift", 70),
            ("angles", "PR2: retract", [90, 0, 0, 0, 0, 45]),
        ]
    else:
        steps = [
            ("angles", "PR3: to storage ready", [90, 0, 0, 0, 0, 45]),
            ("angles", "PR3: to storage alt", [90, 0, 0, -90, 0, 45]),
            ("angles", "PR3: lower to storage", [90, -36.12, -63.28, 9.05, 0, 45]),
            ("z", "PR3: place pattern", -10),
            ("open", "PR3: release"),
            ("z", "PR3: lift out", 60),
        ]
    return run_steps(server, goal_handle, steps)


def crucible_pick(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "CP: home", [0, 0, 0, 0, 0, 45], 50, 1),
        ("open", "CP: open"),
        ("angles", "CP: to crucible ready", [-90, 0, 0, 0, 0, 45]),
        ("angles", "CP: to crucible", [-90, -86, 0, 90, 0, 45]),
        ("close", "CP: grip crucible"),
        ("angles", "CP: retract", [-90, 0, 0, 0, 0, 45]),
        ("angles", "CP: home", [0, 0, 0, 0, 0, 45], 50, 1),
    ])


def metal_pour(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "MP: above mold", [0, 0, -142, 143, 1, 45]),
        ("angles", "MP: tilt pour", [0, 0, -142, 143, 1, 125], 10, 10),
        ("angles", "MP: restore tilt", [0, 0, -142, 143, 1, 45], 50, 1),
        ("angles", "MP: home", [0, 0, 0, 0, 0, 45], 50, 1),
    ])


def crucible_return(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "CR: to crucible ready", [-90, 0, 0, 0, 0, 45]),
        ("angles", "CR: to crucible storage", [-90, -87, 0, 90, 0, 45]),
        ("open", "CR: release crucible"),
        ("angles", "CR: retract", [-90, 0, 0, 0, 0, 45]),
    ])


def casting_pick(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "CsP: home", [0, 0, 0, 0, 0, 45]),
        ("open", "CsP: open"),
        ("angles", "CsP: above mold", [0, 0, 0, -17.31, 0, 45]),
        ("angles", "CsP: into mold", [0, -76.6, 0, -17.31, 0, 45], 100),
        ("z", "CsP: lower", -10),
        ("close", "CsP: grip casting"),
        ("angles", "CsP: home", [0, 0, 0, 0, 0, 45], 50, 1),
    ])


def tat_load(server, goal_handle):
    return run_steps(server, goal_handle, [
        ("angles", "TL: to TAT position", [-30.9, -60.3, 0, 24, 0, 45]),
        ("open", "TL: release casting"),
    ])


def run_steps(server, goal_handle, steps):
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        kind, stage, *args = step
        if kind == "angles":
            angles = args[0]
            speed = args[1] if len(args) > 1 else 50
            delay = args[2] if len(args) > 2 else 3
            ok = server.move_angles(goal_handle, stage, index, total, angles, speed, delay)
        elif kind == "open":
            ok = server.grip_open(goal_handle, stage, index, total)
        elif kind == "close":
            ok = server.grip_close(goal_handle, stage, index, total)
        elif kind == "z":
            delta_z = args[0]
            speed = args[1] if len(args) > 1 else 30
            delay = args[2] if len(args) > 2 else 2
            ok = server.move_z(goal_handle, stage, index, total, delta_z, speed, delay)
        else:
            raise ValueError(f"Unknown step kind: {kind}")
        if not ok:
            return False
    return True


def spin_server(node_name, action_type, action_name, runner, args=None):
    rclpy.init(args=args)
    node = CastActionServer(node_name, action_type, action_name, runner)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


_ALL_SERVERS = [
    ("return_to_base_server",  ReturnToBase,   "return_to_base_action",  return_to_base),
    ("pattern_pick_server",    PatternPick,    "pattern_pick_action",    pattern_pick),
    ("die_stamp_server",       DieStamp,       "die_stamp_action",       die_stamp),
    ("pattern_return_server",  PatternReturn,  "pattern_return_action",  pattern_return),
    ("crucible_pick_server",   CruciblePick,   "crucible_pick_action",   crucible_pick),
    ("metal_pour_server",      MetalPour,      "metal_pour_action",      metal_pour),
    ("crucible_return_server", CrucibleReturn, "crucible_return_action", crucible_return),
    ("casting_pick_server",    CastingPick,    "casting_pick_action",    casting_pick),
    ("tat_load_server",        TatLoad,        "tat_load_action",        tat_load),
]


def all_servers_main(args=None):
    rclpy.init(args=args)
    executor = MultiThreadedExecutor()
    nodes = [
        CastActionServer(name, atype, aname, runner)
        for name, atype, aname, runner in _ALL_SERVERS
    ]
    for node in nodes:
        executor.add_node(node)
    try:
        executor.spin()
    finally:
        executor.shutdown()
        for node in nodes:
            node.destroy_node()
        rclpy.shutdown()


def return_to_base_main(args=None):
    spin_server("return_to_base_server", ReturnToBase, "return_to_base_action", return_to_base, args)


def pattern_pick_main(args=None):
    spin_server("pattern_pick_server", PatternPick, "pattern_pick_action", pattern_pick, args)


def die_stamp_main(args=None):
    spin_server("die_stamp_server", DieStamp, "die_stamp_action", die_stamp, args)


def pattern_return_main(args=None):
    spin_server("pattern_return_server", PatternReturn, "pattern_return_action", pattern_return, args)


def crucible_pick_main(args=None):
    spin_server("crucible_pick_server", CruciblePick, "crucible_pick_action", crucible_pick, args)


def metal_pour_main(args=None):
    spin_server("metal_pour_server", MetalPour, "metal_pour_action", metal_pour, args)


def crucible_return_main(args=None):
    spin_server("crucible_return_server", CrucibleReturn, "crucible_return_action", crucible_return, args)


def casting_pick_main(args=None):
    spin_server("casting_pick_server", CastingPick, "casting_pick_action", casting_pick, args)


def tat_load_main(args=None):
    spin_server("tat_load_server", TatLoad, "tat_load_action", tat_load, args)
