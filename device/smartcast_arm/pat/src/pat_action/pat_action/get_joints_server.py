import time

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer

from example_interfaces.action import Fibonacci
from pymycobot.mycobot import MyCobot


class GetJointsServer(Node):
    def __init__(self):
        super().__init__('get_joints_server')

        self.port = "/dev/ttyUSB0"
        self.baud = 1000000

        self.get_logger().info(f"Connecting myCobot: {self.port}, baud={self.baud}")

        self.mc = MyCobot(self.port, self.baud)
        time.sleep(2)

        test_angles = self.mc.get_angles()
        self.get_logger().info(f"Initial angles: {test_angles}")

        self._action_server = ActionServer(
            self,
            Fibonacci,
            '/pat/get_joints',
            self.execute_callback
        )

        self.get_logger().info('GetJoints Action Server started: /pat/get_joints')

    def read_joints(self):
        angles = self.mc.get_angles()

        if not isinstance(angles, list) or len(angles) < 6:
            raise RuntimeError(f"Invalid joint data: {angles}")

        # Fibonacci action은 int32[] sequence라서 임시로 정수 변환
        joints = [int(round(a)) for a in angles[:6]]
        return joints

    def execute_callback(self, goal_handle):
        self.get_logger().info('Goal received: get real myCobot joint values')

        feedback_msg = Fibonacci.Feedback()

        try:
            feedback_msg.sequence = [0]
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().info('Feedback: reading joints...')

            joints = self.read_joints()

            feedback_msg.sequence = [1]
            goal_handle.publish_feedback(feedback_msg)

            result = Fibonacci.Result()
            result.sequence = joints

            goal_handle.succeed()

            self.get_logger().info(f'Result sent: joints = {joints}')
            return result

        except Exception as e:
            self.get_logger().error(f'Failed to read joints: {e}')

            result = Fibonacci.Result()
            result.sequence = []

            goal_handle.abort()
            return result


def main(args=None):
    rclpy.init(args=args)

    node = GetJointsServer()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
