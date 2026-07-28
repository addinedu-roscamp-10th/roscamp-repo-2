import threading
from functools import partial

import rclpy
from rclpy.action import ActionClient
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


class CastActionClient(Node):
    def __init__(self):
        super().__init__("cast_action_client")
        self.pattern_pick_client = ActionClient(self, PatternPick, "pattern_pick_action")
        self.die_stamp_client = ActionClient(self, DieStamp, "die_stamp_action")
        self.pattern_return_client = ActionClient(self, PatternReturn, "pattern_return_action")
        self.crucible_pick_client = ActionClient(self, CruciblePick, "crucible_pick_action")
        self.metal_pour_client = ActionClient(self, MetalPour, "metal_pour_action")
        self.crucible_return_client = ActionClient(self, CrucibleReturn, "crucible_return_action")
        self.casting_pick_client = ActionClient(self, CastingPick, "casting_pick_action")
        self.tat_load_client = ActionClient(self, TatLoad, "tat_load_action")
        self.return_to_base_client = ActionClient(self, ReturnToBase, "return_to_base_action")

    def mold_sequence(self, pattern_id):
        print(f"\n[Mold Making] pattern_id={pattern_id}")
        pp = PatternPick.Goal()
        pp.pattern_id = pattern_id
        pr = PatternReturn.Goal()
        pr.pattern_id = pattern_id
        return (
            self.send(self.pattern_pick_client, pp)
            and self.send(self.die_stamp_client, DieStamp.Goal())
            and self.send(self.pattern_return_client, pr)
            and self.send(self.return_to_base_client, ReturnToBase.Goal())
        )

    def pour_sequence(self):
        print("\n[Pour]")
        return (
            self.send(self.crucible_pick_client, CruciblePick.Goal())
            and self.send(self.metal_pour_client, MetalPour.Goal())
            and self.send(self.crucible_return_client, CrucibleReturn.Goal())
            and self.send(self.return_to_base_client, ReturnToBase.Goal())
        )

    def demold_sequence(self):
        print("\n[Demold]")
        return (
            self.send(self.casting_pick_client, CastingPick.Goal())
            and self.send(self.tat_load_client, TatLoad.Goal())
            and self.send(self.return_to_base_client, ReturnToBase.Goal())
        )

    def full_cycle(self, pattern_id):
        print("\n[Full Cycle]")
        return self.mold_sequence(pattern_id) and self.pour_sequence() and self.demold_sequence()

    def send(self, client, goal):
        if not client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Server not available (5s timeout)")
            return False

        done = threading.Event()
        state = {"ok": False}

        def feedback_callback(feedback_msg):
            feedback = feedback_msg.feedback
            print(f"  [{feedback.progress_pct:3d}%] {feedback.cur_stage}")

        def result_callback(future):
            wrapped = future.result()
            result = wrapped.result
            state["ok"] = bool(result.success)
            self.get_logger().info(
                f"  {'SUCCESS' if state['ok'] else 'FAILED'}: {result.message}"
            )
            done.set()

        def goal_response_callback(future):
            goal_handle = future.result()
            if not goal_handle.accepted:
                self.get_logger().error("Goal rejected by server")
                done.set()
                return
            goal_handle.get_result_async().add_done_callback(result_callback)

        future = client.send_goal_async(goal, feedback_callback=feedback_callback)
        future.add_done_callback(goal_response_callback)
        done.wait()
        return state["ok"]

    def test_pattern_pick(self, pattern_id):
        goal = PatternPick.Goal()
        goal.pattern_id = pattern_id
        return self.send(self.pattern_pick_client, goal)

    def test_pattern_return(self, pattern_id):
        goal = PatternReturn.Goal()
        goal.pattern_id = pattern_id
        return self.send(self.pattern_return_client, goal)


def print_main_menu():
    print(
        "\n==============================\n"
        "  Cast Action Client\n"
        "==============================\n"
        " 1. Mold Making  (pattern_pick -> die_stamp -> pattern_return -> return_to_base)\n"
        " 2. Pour         (crucible_pick -> metal_pour -> crucible_return -> return_to_base)\n"
        " 3. Demold       (casting_pick -> tat_load -> return_to_base)\n"
        " 4. Full Cycle   (Mold -> Pour -> Demold)\n"
        " 5. Return to Base\n"
        " 6. Individual step test\n"
        " 0. Exit"
    )


def print_step_menu():
    print(
        "\n-- Individual Step --\n"
        " 1. pattern_pick   (pattern_id input)\n"
        " 2. die_stamp\n"
        " 3. pattern_return (pattern_id input)\n"
        " 4. crucible_pick\n"
        " 5. metal_pour\n"
        " 6. crucible_return\n"
        " 7. casting_pick\n"
        " 8. tat_load\n"
        " 9. return_to_base\n"
        " 0. Back"
    )


def read_int(prompt):
    try:
        return int(input(prompt))
    except ValueError:
        return None


def main(args=None):
    rclpy.init(args=args)
    client = CastActionClient()
    executor = MultiThreadedExecutor()
    executor.add_node(client)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        while rclpy.ok():
            print_main_menu()
            choice = read_int("Select: ")
            if choice == 1:
                pattern_id = read_int("Pattern ID (1/2/3): ")
                if pattern_id is not None:
                    client.mold_sequence(pattern_id)
            elif choice == 2:
                client.pour_sequence()
            elif choice == 3:
                client.demold_sequence()
            elif choice == 4:
                pattern_id = read_int("Pattern ID (1/2/3): ")
                if pattern_id is not None:
                    client.full_cycle(pattern_id)
            elif choice == 5:
                client.send(client.return_to_base_client, ReturnToBase.Goal())
            elif choice == 6:
                handle_step_menu(client)
            elif choice == 0:
                break
            else:
                print("Invalid input")
    finally:
        executor.shutdown()
        client.destroy_node()
        rclpy.shutdown()
        spin_thread.join(timeout=1.0)


def handle_step_menu(client):
    print_step_menu()
    sub = read_int("Select: ")
    actions = {
        2: partial(client.send, client.die_stamp_client, DieStamp.Goal()),
        4: partial(client.send, client.crucible_pick_client, CruciblePick.Goal()),
        5: partial(client.send, client.metal_pour_client, MetalPour.Goal()),
        6: partial(client.send, client.crucible_return_client, CrucibleReturn.Goal()),
        7: partial(client.send, client.casting_pick_client, CastingPick.Goal()),
        8: partial(client.send, client.tat_load_client, TatLoad.Goal()),
        9: partial(client.send, client.return_to_base_client, ReturnToBase.Goal()),
    }
    if sub == 1:
        pattern_id = read_int("Pattern ID: ")
        if pattern_id is not None:
            client.test_pattern_pick(pattern_id)
    elif sub == 3:
        pattern_id = read_int("Pattern ID: ")
        if pattern_id is not None:
            client.test_pattern_return(pattern_id)
    elif sub in actions:
        actions[sub]()
