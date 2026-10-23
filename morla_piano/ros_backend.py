"""Optional ROS 2 transport. Imported only on the robot host after ROS setup."""
from __future__ import annotations

import json
import math
import time
import uuid

from .model import HAND_NAMES, SIDES, check_limits, hand_command_seed, HAND_MEASUREMENT_TOLERANCE_RAD

# NEXUS joint_bridge_node accepts this measurement-only XHand margin.
# It is not a target limit and must never be applied to outgoing commands.


class RosBackend:
    def __init__(self, c, expected_backend="hardware"):
        import rclpy
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import JointState
        from std_msgs.msg import String
        from std_srvs.srv import Trigger

        self.rclpy, self.JointState, self.String, self.Trigger = rclpy, JointState, String, Trigger
        self.c, self.expected_backend = c, expected_backend
        rclpy.init()
        self.node = rclpy.create_node("morla_piano_" + uuid.uuid4().hex[:8])
        self.measured, self.received, self.statuses, self.status_at = {}, {}, {}, {}
        self.publishers, self.clients, self.subscriptions = {}, {}, []
        self.names = {}
        self.fault = None
        self.input_errors = {}
        self.feedback_warnings = {}
        self.sdk_statuses, self.sdk_status_at = {}, {}
        self.claimed, self.acks = False, set()
        self.motion_keys = None
        self.epoch, self.sequence = uuid.uuid4().hex, 0
        self.transition = uuid.uuid4().hex
        self.last_heartbeat = self.last_graph_check = 0.0
        self.baseline_rejections = {}
        state_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.lease_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                                    durability=DurabilityPolicy.TRANSIENT_LOCAL)
        # Inspection creates no command or ownership publishers.
        self.lease_pub = None
        for side in SIDES:
            self.subscriptions.append(self.node.create_subscription(
                String, f"/{side}_hand/driver/sdk_status", lambda msg, s=side: self._sdk_status(s, msg), 1))
            for part in ("arm", "hand"):
                key = side + "_" + part
                names = tuple(f"{side}_joint{i}" for i in range(1, 8)) if part == "arm" else HAND_NAMES
                self.names[key] = names
                self.subscriptions.append(self.node.create_subscription(
                    JointState, f"/{key}/joint_states", lambda msg, k=key: self._feedback(k, msg), state_qos))
                status_topic = f"/{key}/nero_robot_driver/status" if part == "arm" else f"/{key}/adapter/status"
                self.subscriptions.append(self.node.create_subscription(
                    String, status_topic, lambda msg, k=key: self._status(k, msg), 10))
            for verb in ("enable", "estop"):
                self.clients[(side, verb)] = self.node.create_client(Trigger, f"/{side}_arm/nero_robot_driver/{verb}")
        self.subscriptions.append(self.node.create_subscription(String, "/teleop/ownership_ack", self._ack, 10))

    def _feedback(self, key, msg):
        try:
            names = list(msg.name)
            if len(names) != len(self.names[key]) or len(set(names)) != len(names) or set(names) != set(self.names[key]):
                raise ValueError("wrong or duplicate joint names")
            if len(msg.position) != len(names) or not all(math.isfinite(v) for v in msg.position):
                raise ValueError("invalid measured positions")
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            now_ros = self.node.get_clock().now().nanoseconds * 1e-9
            if stamp <= 0 or not -0.05 <= now_ros - stamp <= self.c["timing"]["feedback_timeout_s"]:
                raise ValueError("stale, missing or future feedback timestamp")
            q = [msg.position[names.index(n)] for n in self.names[key]]
            side, part = key.split("_")
            limits = self.c["hands"][side][part + "_limits_rad"]
            tolerance = HAND_MEASUREMENT_TOLERANCE_RAD if part == "hand" else math.radians(0.1)
            outside = [(name, v, low, high) for name, v, (low, high) in zip(self.names[key], q, limits)
                       if v < low or v > high]
            def detail(rows):
                return "; ".join(f"{name}={math.degrees(v):.3f}° [{math.degrees(low):.3f}, {math.degrees(high):.3f}]°"
                                 for name, v, low, high in rows)
            invalid = [row for row in outside if row[1] < row[2] - tolerance or row[1] > row[3] + tolerance]
            if invalid:
                raise ValueError(f"measured joint limit exceeded: {detail(invalid)}; measurement tolerance={tolerance:.6f} rad")
            if not hasattr(self, "feedback_warnings"):
                self.feedback_warnings = {}
            if outside:
                has_reference = "start_reference_rad" in self.c["hands"][side]
                self.feedback_warnings[key] = (f"{key} 实测略超控制目标范围：{detail(outside)}。"
                    + ("已记录软件手姿参考；显式运动接管时，仅匹配参考的小偏差关节使用最近边界目标，最大2.86°。读数不变，保存不运动。"
                       if has_reference and part == "hand" else
                       "读数保留原值；请记录并确认当前悬停手姿参考后再接管，不会自动回位。"))
            else:
                self.feedback_warnings.pop(key, None)
            self.measured[key] = q
            self.received[key] = time.monotonic() - max(0.0, now_ros - stamp)
            self.input_errors.pop(key + "/feedback", None)
        except (ValueError, TypeError) as exc:
            self.input_errors[key + "/feedback"] = f"{key} feedback: {exc}"

    def _status(self, key, msg):
        try:
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise ValueError("expected object")
            self.statuses[key], self.status_at[key] = payload, time.monotonic()
            self.input_errors.pop(key + "/status", None)
        except (ValueError, TypeError) as exc:
            self.input_errors[key + "/status"] = f"{key} status: {exc}"

    def _ack(self, msg):
        try:
            ack = json.loads(msg.data)
            if ack.get("transition_id") == self.transition:
                self.acks.add(ack.get("writer"))
        except (ValueError, TypeError):
            pass

    def _sdk_status(self, side, msg):
        try:
            value = json.loads(msg.data)
            if isinstance(value, dict):
                self.sdk_statuses[side], self.sdk_status_at[side] = value, time.monotonic()
        except (TypeError, ValueError):
            pass

    def _graph_check(self):
        topics = [f"/{s}_{p}/joint_commands" for s in SIDES for p in ("arm", "hand")]
        topics.append("/policy_inference/state")
        for topic in topics:
            competitors = [x.node_name for x in self.node.get_publishers_info_by_topic(topic)
                           if x.node_name != self.node.get_name() or x.node_namespace != self.node.get_namespace()]
            if competitors:
                raise RuntimeError(f"competing writer(s) on {topic}: {competitors}; stop teleop/policy first")
        for side in SIDES:
            for part in ("arm", "hand"):
                topic = f"/{side}_{part}/joint_states"
                if len(self.node.get_publishers_info_by_topic(topic)) != 1:
                    raise RuntimeError(f"{topic}: exactly one feedback publisher required; check duplicate drivers")

    def check_connection(self):
        """Inspect current input once; maintenance must not wait on hand data."""
        missing = [key for key in self.names if key not in self.measured]
        absent = [key for key in self.names if key not in self.statuses]
        if missing or absent:
            errors = list(self.input_errors.values())
            for side, status in getattr(self, "sdk_statuses", {}).items():
                if (time.monotonic() - self.sdk_status_at.get(side, 0) < 1.5
                        and status.get("feedback_valid") is False):
                    errors.append(f"{side}_hand SDK {status.get('error_code')}: {status.get('error')}")
            raise RuntimeError(f"等待有效反馈：缺少关节读数={missing}，缺少驱动状态={absent}"
                               + ("；" + "; ".join(errors) if errors else ""))
        self._graph_check()
        self.health(require_enabled=False)

    def connect(self):
        deadline = time.monotonic() + self.c["ros"]["connect_timeout_s"]
        last_error = None
        while time.monotonic() < deadline:
            self.pump()
            if len(self.measured) == 4 and len(self.statuses) == 4:
                self._graph_check()
                try:
                    self.health(require_enabled=False)
                    return
                except RuntimeError as exc:
                    # Old dictionaries can be populated while current samples
                    # are still arriving after reset. Wait for healthy input
                    # within the original deadline, never erase errors blindly.
                    last_error = str(exc)
            time.sleep(0.01)
        raise RuntimeError(f"ROS connection timed out; feedback={list(self.measured)}, status={list(self.statuses)}; last_error={last_error}")

    def _lease(self, activity):
        self.sequence += 1
        payload = dict(sent_at=time.time(), epoch=self.epoch, sequence=self.sequence,
                       activity=activity, state="PLAYBACK" if activity else "IDLE", paused=False,
                       transition_id=self.transition, takeover_pending=False, policy_start_pending=False)
        self.lease_pub.publish(self.String(data=json.dumps(payload)))
        self.last_heartbeat = time.monotonic()

    def claim(self, keys=None):
        requested = frozenset(self.names if keys is None else keys)
        if not requested or not requested <= self.names.keys():
            raise ValueError("invalid motion scope")
        current = getattr(self, "motion_keys", None)
        if self.claimed and current == requested:
            self.health()
            return
        if self.claimed:
            self._lease(None)
            self.claimed = False
            for publisher in self.publishers.values():
                self.node.destroy_publisher(publisher)
            self.publishers.clear()
            if self.lease_pub:
                self.node.destroy_publisher(self.lease_pub)
                self.lease_pub = None
        self.motion_keys = requested
        if not self.c["calibrated"] and self.expected_backend == "hardware":
            raise RuntimeError("placeholder configuration: calibrate before hardware motion")
        self._graph_check()
        self.health(require_enabled=False)
        # Resolve all holding seeds before ownership/enable. A hand boundary
        # seed requires an explicitly saved reference, never a wider limit.
        self._command_seed(self.measured)
        self.lease_pub = self.node.create_publisher(self.String, "/policy_inference/state", self.lease_qos)
        for key in requested:
            self.publishers[key] = self.node.create_publisher(self.JointState, f"/{key}/joint_commands", 1)
        self.claimed = True
        self.acks.clear()
        self.transition = uuid.uuid4().hex
        self._lease("playback")
        hand_sides = {key.split("_")[0] for key in requested if key.endswith("_hand")}
        arm_sides = {key.split("_")[0] for key in requested if key.endswith("_arm")}
        wanted_acks = {side + "_ee" for side in hand_sides}
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not wanted_acks <= self.acks:
            self.pump()
            time.sleep(0.01)
        if not wanted_acks <= self.acks:
            raise RuntimeError("XHand ownership acknowledgements timed out")
        for side in hand_sides:
            self.baseline_rejections[side] = self.statuses[side + "_hand"]["rejections"]
        # A new scope needs a new enable epoch. Cached enabled=True can belong
        # to the old scope whose command watchdog is about to revoke it.
        enable_requests = {}
        for side in arm_sides:
            enable_requests[side] = time.monotonic()
            self._call(side, "enable")
        deadline = time.monotonic() + 4
        last_error = "waiting for driver status"
        while time.monotonic() < deadline:
            self.pump()
            if all(self.statuses[s + "_arm"].get("enabled")
                   and self.statuses[s + "_arm"].get("enable_status") == "enabled"
                   and self.statuses[s + "_arm"].get("enable_started_monotonic_s", 0) >= enable_requests[s]
                   for s in arm_sides):
                try:
                    self.health()
                    return
                except RuntimeError as exc:
                    last_error = str(exc)
            time.sleep(0.01)
        raise RuntimeError("streaming enable/ownership not confirmed: " + last_error)

    def _call(self, side, verb, wait=True):
        client = self.clients[(side, verb)]
        if not client.wait_for_service(timeout_sec=0.5):
            raise RuntimeError(f"{side} {verb} service unavailable")
        future = client.call_async(self.Trigger.Request())
        if not wait:
            return future
        deadline = time.monotonic() + 4
        while not future.done() and time.monotonic() < deadline:
            self.pump()
            time.sleep(0.005)
        if not future.done() or not future.result().success:
            raise RuntimeError(f"{side} {verb} failed: {future.result().message if future.done() else 'timeout'}")
        return future.result()

    def set_arm_speed(self, percent, targets, check):
        """Change only stream speed, holding existing goals until driver ack.

        SDK acknowledgement is a successful SDK call, not measured motor speed.
        """
        from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType
        from rcl_interfaces.srv import SetParametersAtomically
        wanted, pending = {}, {}
        requested_at = time.monotonic()
        for side in SIDES:
            key = side + "_arm"
            if key not in targets:
                continue
            wanted[side] = int(percent if percent is not None else self.statuses[key]["position_speed_percent"])
            client_key = (side, "stream_speed")
            if client_key not in self.clients:
                self.clients[client_key] = self.node.create_client(
                    SetParametersAtomically, f"/{side}_arm/nero_robot_driver/set_parameters_atomically")
            client = self.clients[client_key]
            discovery_deadline = time.monotonic() + 1
            while not client.service_is_ready():
                check()
                self.send(targets)
                if time.monotonic() >= discovery_deadline:
                    raise RuntimeError(side + " stream speed service unavailable; rebuild/restart drivers")
                time.sleep(.01)
            request = SetParametersAtomically.Request(parameters=[Parameter(name="stream_j_speed_percent",
                value=ParameterValue(type=ParameterType.PARAMETER_INTEGER, integer_value=wanted[side]))])
            pending[side] = client.call_async(request)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            check()
            self.send(targets)  # preserve holding command/watchdog, including while switching both arms
            accepted = True
            for side, future in pending.items():
                if not future.done():
                    accepted = False
                elif future.exception() or not future.result().result.successful:
                    raise RuntimeError(side + " stream speed change rejected")
            if accepted and all(self.statuses[s + "_arm"].get("stream_applied_speed_percent") == v
                                and self.status_at[s + "_arm"] >= requested_at
                                and time.monotonic() - self.status_at[s + "_arm"] < .5
                                for s, v in wanted.items()):
                return
            time.sleep(.01)
        raise RuntimeError("stream speed SDK acknowledgement timed out; no performance started")

    def pump(self):
        # Drain callbacks without blocking musical scheduling.
        for _ in range(12):
            self.rclpy.spin_once(self.node, timeout_sec=0)
        now = time.monotonic()
        if self.claimed and now - self.last_heartbeat >= 0.2:
            self._lease("playback")
        if self.claimed and now - self.last_graph_check > 0.5:
            self._graph_check()
            self.last_graph_check = now

    def health(self, require_enabled=True):
        if self.fault:
            raise RuntimeError(self.fault)
        keys = getattr(self, "motion_keys", None) or self.names
        errors = [v for k,v in self.input_errors.items() if k.split('/')[0] in keys]
        if errors:
            raise RuntimeError("; ".join(errors))
        now = time.monotonic()
        for key in keys:
            if now - self.received.get(key, -float("inf")) > self.c["timing"]["feedback_timeout_s"]:
                raise RuntimeError(f"{key}: feedback missing or stale")
            if now - self.status_at.get(key, -float("inf")) > 1.5:
                raise RuntimeError(f"{key}: driver status missing or stale")
            status = self.statuses[key]
            if status.get("feedback_fresh") is not True:
                raise RuntimeError(f"{key}: driver reports stale feedback")
            if key.endswith("arm"):
                if status.get("backend") != self.expected_backend:
                    raise RuntimeError(f"{key}: expected {self.expected_backend}, got {status.get('backend')}")
                if not status.get("connected"):
                    raise RuntimeError(f"{key}: driver disconnected")
                if require_enabled and (not status.get("enabled") or not status.get("motors_enabled")
                                        or not status.get("motor_status_fresh")):
                    raise RuntimeError(f"{key}: fresh motor enable and streaming permission required; "
                                       f"enabled={status.get('enabled')}, motors_enabled={status.get('motors_enabled')}, "
                                       f"motor_status_fresh={status.get('motor_status_fresh')}; "
                                       "hardware requires the current MORLA driver status contract")
                if status.get("position_active"):
                    raise RuntimeError(f"{key}: another position motion is active")
            elif require_enabled and self.claimed:
                side = key.split("_")[0]
                if not status.get("lease_fresh") or status.get("lease_activity") != "playback":
                    raise RuntimeError(f"{key}: playback ownership not confirmed")
                if status.get("rejections", 0) > self.baseline_rejections.get(side, 0):
                    raise RuntimeError(f"{key}: adapter rejected a command")

    def snapshot(self):
        self.health(require_enabled=self.claimed)
        return {k: list(v) for k, v in self.measured.items()}

    def _command_seed(self, measured):
        result = {}
        for key in getattr(self, "motion_keys", None) or self.names:
            side, part = key.split("_")
            h = self.c["hands"][side]
            try:
                if part == "hand":
                    result[key] = hand_command_seed(measured[key], h["hand_limits_rad"], h.get("start_reference_rad"))
                else:
                    check_limits(measured[key], h["arm_limits_rad"], key)
                    result[key] = list(measured[key])
            except ValueError as exc:
                warning = getattr(self, "feedback_warnings", {}).get(key, str(exc))
                raise RuntimeError(f"运动起点未获参考许可；未发送目标。{warning} ({exc})") from exc
        return result

    def command_snapshot(self):
        return self._command_seed(self.snapshot())

    def send(self, targets):
        self.health()
        if not self.claimed:
            raise RuntimeError("ownership required")
        if not targets.keys() <= (getattr(self, "motion_keys", None) or self.names.keys()):
            raise RuntimeError("command outside claimed motion scope")
        stamp = self.node.get_clock().now().to_msg()
        for key, values in targets.items():
            side, part = key.split("_")
            check_limits(values, self.c["hands"][side][part + "_limits_rad"], key)
            msg = self.JointState()
            msg.header.stamp, msg.name, msg.position = stamp, list(self.names[key]), [float(v) for v in values]
            self.publishers[key].publish(msg)

    def emergency_stop(self):
        if self.expected_backend == "hardware":
            for side in SIDES:
                try:
                    key = (side, "stop_hand_feedback")
                    if key not in self.clients:
                        self.clients[key] = self.node.create_client(self.Trigger, f"/{side}_hand/driver/stop_feedback")
                    self._call(side, key[1], wait=False)
                except Exception as exc:
                    print(f"{side} passive hand stop request failed: {exc}")
        for side in SIDES:
            try:
                self._call(side, "estop", wait=False)
            except Exception as exc:
                print(f"ESTOP request failed for {side}: {exc}", flush=True)
        self.claimed = False
        if self.lease_pub:
            self._lease(None)
        for _ in range(10):
            self.rclpy.spin_once(self.node, timeout_sec=0.01)

    def close(self):
        if self.lease_pub:
            self._lease(None)
        self.claimed = False
        for _ in range(5):
            self.rclpy.spin_once(self.node, timeout_sec=0.01)
        self.node.destroy_node()
        self.rclpy.shutdown()
