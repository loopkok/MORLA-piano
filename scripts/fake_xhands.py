"""Native XHand software plant for ROS integration tests. Opens no serial device."""
import json
import sys
from pathlib import Path

import rclpy
from rclpy.node import Node
from xhand_control_interfaces.msg import XHandCommand, XHandState, XHandStateArray

# Direct execution from launch does not require pip installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from morla_piano.model import HAND_NAMES


class FakeHands(Node):
    def __init__(self, config):
        super().__init__("piano_fake_xhands")
        self.positions = {s: [float(v) for v in config["hands"][s]["hover_rad"]] for s in ("left", "right")}
        self.pubs, self.subs = {}, []
        for side in self.positions:
            self.pubs[side] = self.create_publisher(XHandStateArray, f"/{side}_hand/driver/xhand_state", 1)
            self.subs.append(self.create_subscription(XHandCommand, f"/{side}_hand/driver/xhand_command",
                                                      lambda msg, s=side: self.command(s, msg), 10))
        self.create_timer(0.02, self.tick)

    def command(self, side, msg):
        self.positions[side] = [msg.position[list(msg.name).index(n)] for n in HAND_NAMES]

    def tick(self):
        for side, values in self.positions.items():
            state = XHandState()
            state.name, state.position = list(HAND_NAMES), values
            array = XHandStateArray()
            array.header.stamp = self.get_clock().now().to_msg()
            array.hand_id, array.hand_states = [0], [state]
            self.pubs[side].publish(array)


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    rclpy.init(args=[])
    node = FakeHands(config)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
