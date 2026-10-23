from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from .model import FINGER_JOINTS, FINGERS, HAND_NAMES, SIDES, arm_target, check_limits, performance_timing


class Stopped(RuntimeError):
    pass


class MotionConfirmationError(RuntimeError):
    """Healthy feedback could not confirm a commanded position in time."""


class Clock:
    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(max(0.0, seconds))


class VirtualClock(Clock):
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


def smooth(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


class FakeBackend:
    """Instantaneous software plant; not a physics or contact simulator."""
    def __init__(self, c, clock=None):
        self.c, self.clock = c, clock or Clock()
        self.measured = {}
        for side in SIDES:
            self.measured[side + "_arm"] = list(c["hands"][side]["initial_arm_rad"])
            self.measured[side + "_hand"] = list(c["hands"][side]["hover_rad"])
        self.claimed = False
        self.history = []
        self.stopped = False

    def connect(self):
        pass

    def claim(self, keys=None):
        self.motion_keys = frozenset(self.measured if keys is None else keys)
        self.claimed = True

    def health(self):
        if self.stopped:
            raise RuntimeError("backend emergency stopped")

    def snapshot(self):
        self.health()
        return {k: list(v) for k, v in self.measured.items()}

    def send(self, targets):
        self.health()
        if not self.claimed:
            raise RuntimeError("ownership not claimed")
        self.measured.update({k: list(v) for k, v in targets.items()})
        self.history.append((self.clock.now(), self.snapshot()))

    def pump(self):
        pass

    def emergency_stop(self):
        self.stopped = True

    def close(self):
        self.claimed = False


class Engine:
    def __init__(self, c, backend, clock=None, log_path=None):
        self.c, self.backend = c, backend
        self.clock = clock or Clock()
        self.stop_event = threading.Event()
        self.state = "CONNECTED"
        self.error = None
        self.targets = None
        self.shifts = dict.fromkeys(SIDES, 0)
        self.pressed = None
        self.events = []
        self.playback = None
        self._playback_started = None
        self.log_path = Path(log_path) if log_path else None

    def log(self, event, **details):
        row = dict(monotonic_s=self.clock.now(), event=event, **details)
        if event != "speed_sample":
            self.events.append(row)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def request_stop(self):
        self.stop_event.set()

    def _check(self):
        if self.stop_event.is_set():
            raise Stopped("operator stopped playback")
        self.backend.pump()
        self.backend.health()

    def _send(self):
        scope = getattr(self.backend, "motion_keys", None)
        if scope is not None and not self.targets.keys() <= scope:
            raise RuntimeError("command outside claimed motion scope")
        for key, q in self.targets.items():
            side, part = key.split("_")
            check_limits(q, self.c["hands"][side][part + "_limits_rad"], side)
        self.backend.send(self.targets)

    def _command_snapshot(self):
        # Keep the feedback/telemetry API raw; command seeds have a separate
        # path for explicitly recorded near-boundary hand references.
        targets = (self.backend.command_snapshot() if hasattr(self.backend, "command_snapshot")
                   else self.backend.snapshot())
        scope = getattr(self.backend, "motion_keys", None)
        if scope is not None:
            targets = {k:v for k,v in targets.items() if k in scope}
        for side in SIDES:
            key = side + "_hand"
            raw = self.backend.measured.get(key, [])
            if key in targets and raw and any(a != b for a, b in zip(raw, targets[key])):
                self.log("hand_boundary_seed", side=side, measured_rad=list(raw), target_rad=list(targets[key]))
        return targets

    def _wait(self, duration, check_stop=True):
        end = self.clock.now() + duration
        period = 1 / self.c["timing"]["control_hz"]
        while self.clock.now() < end - 1e-9:
            if check_stop:
                self._check()
            else:
                self.backend.pump()
                self.backend.health()
            self._send()
            self.clock.sleep(min(period, end - self.clock.now()))

    def _arrived(self, targets, tolerance):
        measured = self.backend.snapshot()
        return all(max(abs(a - b) for a, b in zip(q, measured[k])) <= tolerance
                   for k, q in self._arrival_targets(targets).items())

    def _arrival_targets(self, targets):
        return {key: q for key, q in targets.items()
                if not (key.endswith("_hand") and
                        self.c["hands"][key.split("_")[0]].get("arrival_check_enabled", True) is False)}

    def _await_arrival(self, targets, tolerance_rad=None, dwell_s=None):
        t = self.c["timing"]
        tolerance = t["arrival_tolerance_rad"] if tolerance_rad is None else tolerance_rad
        dwell = t["arrival_dwell_s"] if dwell_s is None else dwell_s
        if not math.isfinite(tolerance) or tolerance <= 0 or not math.isfinite(dwell) or dwell < 0:
            raise ValueError("invalid arrival tolerance or dwell")
        checked = self._arrival_targets(targets)
        skipped = sorted(set(targets) - set(checked))
        if skipped:
            self.log("arrival_verification_skipped", keys=skipped, measured_confirmed=False)
        targets = checked
        if not targets:
            # Keep health checks, command delivery and the settling interval.
            self._check()
            self._send()
            self._wait(dwell)
            return
        deadline = self.clock.now() + t["arrival_timeout_s"]
        arrived_at = None
        while self.clock.now() < deadline:
            self._check()
            self._send()
            if self._arrived(targets, tolerance):
                if arrived_at is None:
                    arrived_at = self.clock.now()
                if self.clock.now() - arrived_at >= dwell - 1e-9:
                    return
            else:
                arrived_at = None
            self.clock.sleep(1 / t["control_hz"])
        # Capture before _fault() sends emergency stop and the arm can relax.
        measured = self.backend.snapshot()
        details = {}
        worst = None
        for key, target in targets.items():
            actual = measured[key]
            errors = [abs(a-b) for a, b in zip(target, actual)]
            details[key] = dict(target_rad=list(target), measured_rad=list(actual), error_rad=errors)
            for i, error in enumerate(errors):
                if worst is None or error > worst[0]:
                    worst = (error, key, i, target[i], actual[i])
        self.log("arrival_timeout", tolerance_rad=tolerance, dwell_s=dwell,
                 timeout_s=t["arrival_timeout_s"], joints=details)
        error, key, i, target, actual = worst
        name = f"J{i+1}" if key.endswith("_arm") else HAND_NAMES[i]
        raise MotionConfirmationError(
            f"measured arrival timed out: {key} {name} "
            f"目标={math.degrees(target):.3f}°，实测={math.degrees(actual):.3f}°，"
            f"误差={math.degrees(error):.3f}°，容差={math.degrees(tolerance):.3f}°；"
            "已停止，请检查该关节反馈和目标")

    def _speed_restore_check(self):
        self.backend.pump()
        self.backend.health()

    def performance_speed(self, enabled):
        if "performance" not in self.c:
            return
        percent = self.c["performance"]["sdk_speed_percent"] if enabled else None
        if hasattr(self.backend, "set_arm_speed"):
            self.backend.set_arm_speed(percent, self.targets, self._check if enabled else self._speed_restore_check)
        self.log("performance_speed", enabled=enabled, sdk_speed_percent=percent,
                 timing=performance_timing(self.c) if enabled else self.c["timing"])

    def prepare(self, approach=None):
        """Move from measured operator position to configured piano reference."""
        self.stop_event.clear()
        self.playback = None
        self._playback_started = None
        self.state = "PREPARING"
        try:
            self.backend.claim()
            self.backend.health()
            self.targets = self._command_snapshot()
            self.performance_speed(False)
            goal = {side + "_arm": arm_target(self.c, side, 0) for side in SIDES}
            # First hover fingers, preserving all three measured thumb joints.
            for side in SIDES:
                for finger in FINGERS:
                    self._finger(side, finger, False)
            self._send()
            self._await_arrival({side + "_hand": self.targets[side + "_hand"] for side in SIDES})
            if approach is not None:
                self.log("prepare_approach_begin", sides=list(SIDES))
                approach()
                self._check()
                self.targets = self._command_snapshot()
                self.state = "PREPARING"
                self._manual_calibration = False
                self.log("prepare_approach_complete", sides=list(SIDES))
            # The final interpolation must start at the completed approach,
            # never at the pose captured before the four-stage work trajectory.
            original = {k: list(v) for k, v in self.targets.items()}
            timing = self.c["timing"]
            distance = max(abs(a - b) for k in goal for a, b in zip(original[k], goal[k]))
            duration = max(timing["prepare_duration_s"], 1.5 * distance / timing["max_arm_velocity_rad_s"])
            begin = self.clock.now()
            self.log("prepare_begin", duration_s=duration)
            while self.clock.now() - begin < duration - 1e-9:
                self._check()
                ratio = smooth((self.clock.now() - begin) / duration)
                for key in goal:
                    self.targets[key] = [a + ratio * (b - a) for a, b in zip(original[key], goal[key])]
                self._send()
                self.clock.sleep(1 / timing["control_hz"])
            self.targets.update(goal)
            self._await_arrival(goal)
            self.shifts = dict.fromkeys(SIDES, 0)
            self.state = "READY"
            self.error = None
            self.log("ready")
        except Stopped:
            try:
                self._normal_stop()
            except BaseException as exc:
                self._fault(exc)
                raise
            raise
        except BaseException as exc:
            self._fault(exc)
            raise

    def _finger(self, side, finger, press):
        h = self.c["hands"][side]
        q = self.targets[side + "_hand"]
        if press:
            for name, value in h["press_rad"][finger].items():
                q[HAND_NAMES.index(name)] = value
        else:
            for index in FINGER_JOINTS[finger]:
                q[index] = h["hover_rad"][index]

    def _normal_stop(self):
        """Healthy cancellation: freeze at measured arm pose and release fingers."""
        self._freeze_playback()
        self.backend.health()
        self.targets = self._command_snapshot()
        for side in SIDES:
            if side + "_hand" in self.targets:
                for finger in FINGERS:
                    self._finger(side, finger, False)
        self.pressed = None
        self._send()
        self._wait(self.c["timing"]["release_settle_s"], check_stop=False)
        self.state = "STOPPED"
        self.log("stopped")

    def _fault(self, exc):
        self._freeze_playback()
        if isinstance(exc, MotionConfirmationError):
            try:
                # Keep arm power/ownership: stop the task and release fingers.
                # Health checks still escalate a failed recovery to emergency stop.
                self._normal_stop()
                self.error = str(exc)
                self.log("motion_unconfirmed", error=str(exc))
                return
            except BaseException as recovery_error:
                exc = recovery_error
        self.state = "FAULT"
        self.error = str(exc)
        self.log("fault", error=str(exc))
        if self.backend.claimed:
            self.backend.emergency_stop()

    def play(self, plan):
        if self.state != "READY" or any(self.shifts.values()):
            raise RuntimeError("run prepare before every performance")
        self.stop_event.clear()
        self.state = "PLAYING"
        moves = {}
        sampled_at = -float("inf")
        # Switch before starting the score clock; service time is not musical time.
        try:
            self.performance_speed(True)
        except Stopped:
            self._normal_stop()
            self.performance_speed(False)
            return
        except BaseException as exc:
            self._fault(exc)
            raise
        index, begin = 0, self.clock.now()
        timing, actions = performance_timing(self.c), plan["actions"]
        self._playback_started = begin
        self.playback = dict(title=plan["title"], duration_s=plan["duration_s"], elapsed_s=0.,
                             total_actions=len(actions), completed_actions=0,
                             total_notes=sum(a["kind"] == "press" for a in actions),
                             started_notes=0, completed_notes=0, current_action=None, completed=False)
        self.log("play_begin", title=plan["title"])
        try:
            while True:
                self._check()
                elapsed = self.clock.now() - begin
                for side, move in list(moves.items()):
                    q0, q1, start, duration, arrived_at = move
                    ratio = smooth((elapsed - start) / duration)
                    self.targets[side + "_arm"] = [a + ratio * (b - a) for a, b in zip(q0, q1)]
                    if elapsed >= start + duration:
                        if self._arrived({side + "_arm": q1}, timing["arrival_tolerance_rad"]):
                            if arrived_at is None:
                                move[4] = elapsed
                            elif elapsed - arrived_at >= timing["arrival_dwell_s"] - 1e-9:
                                del moves[side]
                        else:
                            move[4] = None
                while index < len(actions) and actions[index]["at_s"] <= elapsed + 1e-9:
                    a = actions[index]
                    late = elapsed - a["at_s"]
                    if late > timing["max_lateness_s"]:
                        raise RuntimeError(f"row {a['row']}: scheduler late by {late:.3f}s; stopped rather than bursting notes")
                    side, kind = a["side"], a["kind"]
                    if kind == "move":
                        if side in moves or (self.pressed and self.pressed[0] == side):
                            raise RuntimeError("attempted wrist shift while finger down or wrist moving")
                        q0 = list(self.targets[side + "_arm"])
                        q1 = arm_target(self.c, side, a["shift"])
                        moves[side] = [q0, q1, a["at_s"], a["duration_s"], None]
                        self.shifts[side] = a["shift"]
                    elif kind == "press":
                        if self.pressed or side in moves:
                            raise RuntimeError(f"row {a['row']}: finger already down or wrist not arrived")
                        if not self._arrived({side + "_arm": arm_target(self.c, side, a["shift"])}, timing["arrival_tolerance_rad"]):
                            raise RuntimeError("wrist left its planned position before key press")
                        self._finger(side, a["finger"], True)
                        self.pressed = (side, a["finger"])
                    elif kind == "release":
                        if self.pressed != (side, a["finger"]):
                            raise RuntimeError("unmatched finger release")
                        self._finger(side, a["finger"], False)
                        self.pressed = None
                    else:
                        raise ValueError(f"unknown action {kind}")
                    self.log(kind, scheduled_s=a["at_s"], actual_s=elapsed, lateness_s=max(0, late),
                             side=side, finger=a["finger"], midi=a["midi"], shift=a["shift"], row=a["row"])
                    index += 1
                    self.playback["completed_actions"] = index
                    self.playback["current_action"] = dict(a)
                    if kind == "press":
                        self.playback["started_notes"] += 1
                    elif kind == "release":
                        self.playback["completed_notes"] += 1
                measured = self.backend.snapshot()
                if "performance" in self.c and self.log_path and elapsed - sampled_at >= .1:
                    self.log("speed_sample", elapsed_s=elapsed, measured_rad=measured,
                             target_rad={k:list(v) for k,v in self.targets.items()},
                             pressed=self.pressed, wrist_shifts=dict(self.shifts),
                             feedback_received_monotonic_s=dict(getattr(self.backend, "received", {})))
                    sampled_at = elapsed
                for side in SIDES:
                    key = side + "_arm"
                    if max(abs(a - b) for a, b in zip(measured[key], self.targets[key])) > timing["max_tracking_error_rad"]:
                        raise RuntimeError(f"{side} arm tracking error exceeds configured bound")
                self._send()
                if index == len(actions) and elapsed >= plan["duration_s"] and not moves:
                    break
                self.clock.sleep(1 / timing["control_hz"])
            self._await_arrival({s + "_hand": self.targets[s + "_hand"] for s in SIDES})
            self._freeze_playback()
            self.playback.update(elapsed_s=plan["duration_s"], completed=True)
            self.state = "FINISHED"
            self.log("finished")
        except Stopped:
            try:
                self._normal_stop()
            except BaseException as exc:
                self._fault(exc)
                raise
        except BaseException as exc:
            self._fault(exc)
            raise

        finally:
            if self.backend.claimed and self.state != "FAULT":
                try:
                    self.performance_speed(False)
                except BaseException as exc:
                    self._fault(exc)
                    raise

    def idle(self):
        try:
            self.backend.pump()
            if self.targets is not None and self.state in ("READY", "FINISHED", "STOPPED"):
                self.backend.health()
                self._send()
        except Exception as exc:
            self._fault(exc)

    def _freeze_playback(self):
        if self.playback is not None and self._playback_started is not None:
            self.playback["elapsed_s"] = min(self.playback["duration_s"],
                                               max(0., self.clock.now() - self._playback_started))
            self._playback_started = None

    def status(self):
        playback = dict(self.playback) if self.playback is not None else None
        if playback is not None:
            if self._playback_started is not None:
                playback["elapsed_s"] = min(playback["duration_s"], max(0., self.clock.now() - self._playback_started))
            playback["percent"] = (100. if playback["completed"] else
                                   min(99.9, 100 * playback["elapsed_s"] / max(.001, playback["duration_s"])))
        return dict(state=self.state, error=self.error, wrist_shifts=self.shifts, pressed=self.pressed,
                    playback=playback)
