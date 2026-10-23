"""Only MORLA drivers/adapters; no Quest, retargeter, policy or coordinator."""
from pathlib import Path
import json
import sys

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, RegisterEventHandler, EmitEvent
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ROOT = Path(__file__).resolve().parent.parent


def setup(context):
    mode = LaunchConfiguration("mode").perform(context)
    if mode not in ("fake", "hardware"):
        raise RuntimeError("mode must be fake or hardware")
    config = json.loads(Path(LaunchConfiguration("config").perform(context)).read_text())
    nodes = []
    for side in ("left", "right"):
        h = config["hands"][side]
        arm_config = str(Path(get_package_share_directory("nero_robot_control")) / "config" / f"nero_driver_{side}.yaml")
        parameters = {"arm_side": side, "sdk_backend": mode, "stop_on_shutdown": True,
                      "workpos_arrival_tolerance_deg": 3.0, "native_j7_first": True, "stream_position_mode": "j", "stream_j_speed_percent": 5}
        if mode == "fake":
            parameters["initial_position"] = [float(v) for v in h["initial_arm_rad"]]
        else:
            parameters["can_channel"] = LaunchConfiguration(f"{side}_can").perform(context)
        nodes.append(Node(package="nero_robot_control", executable="nero_robot_driver", name="nero_robot_driver",
                          namespace=f"/{side}_arm", parameters=[arm_config, parameters], output="screen"))
        adapter_params = {
            "side": side, "hand_id": 0, "require_armed": True,
            "joint_lower_limits": [float(p[0]) for p in h["hand_limits_rad"]],
            "joint_upper_limits": [float(p[1]) for p in h["hand_limits_rad"]],
            "state_topic": f"/{side}_hand/joint_states", "command_topic": f"/{side}_hand/joint_commands",
            "native_state_topic": f"/{side}_hand/driver/xhand_state",
            "native_command_topic": f"/{side}_hand/driver/xhand_command",
            "status_topic": f"/{side}_hand/adapter/status",
        }
        nodes.append(Node(package="xhand_adapter", executable="xhand_adapter", name=f"piano_xhand_adapter_{side}",
                          parameters=[adapter_params], output="screen"))
        if mode == "hardware":
            port = LaunchConfiguration(f"{side}_serial_port").perform(context)
            if not port:
                raise RuntimeError(f"hardware requires {side}_serial_port:=/dev/serial/by-id/...")
            vendor = str(Path(get_package_share_directory("xhand_control_ros2")) / "config/xhand_config.yaml")
            nodes.append(Node(package="xhand_control_ros2", executable="xhand_control_ros2_node",
                              name=f"piano_xhand_{side}", namespace=f"{side}_hand/driver",
                              parameters=[vendor, {"port_name": port, "update_rate": 50.0, "piano_feedback_mode": True}], output="screen"))
    if mode == "fake":
        nodes.append(ExecuteProcess(cmd=[sys.executable, str(ROOT / "scripts/fake_xhands.py"),
                                         LaunchConfiguration("config").perform(context)], output="screen"))
    # A native driver may fail before publishing any state while launch itself
    # remains alive. Shut down the whole exclusive lane on any child exit.
    handlers = [RegisterEventHandler(OnProcessExit(target_action=node,
                on_exit=[EmitEvent(event=Shutdown(reason="piano driver exited"))])) for node in nodes]
    return handlers + nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("mode", default_value="fake"),
        DeclareLaunchArgument("config", default_value=str(ROOT / "config/piano.placeholder.json")),
        DeclareLaunchArgument("left_can", default_value="can_nero_left"),
        DeclareLaunchArgument("right_can", default_value="can_nero_right"),
        DeclareLaunchArgument("left_serial_port", default_value=""),
        DeclareLaunchArgument("right_serial_port", default_value=""),
        OpaqueFunction(function=setup),
    ])
