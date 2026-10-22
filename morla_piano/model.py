from __future__ import annotations

import copy
import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

SIDES = ("left", "right")
FINGERS = ("index", "middle", "ring", "pinky")
HAND_NAMES = ("thumb_bend_joint", "thumb_rota_joint1", "thumb_rota_joint2",
              "index_bend_joint", "index_joint1", "index_joint2", "mid_joint1",
              "mid_joint2", "ring_joint1", "ring_joint2", "pinky_joint1", "pinky_joint2")
FINGER_JOINTS = {"index": (3, 4, 5), "middle": (6, 7), "ring": (8, 9), "pinky": (10, 11)}
WHITE = (0, 2, 4, 5, 7, 9, 11)


def finite(value, label="number", minimum=None):
    if isinstance(value, bool):
        raise ValueError(f"{label}: boolean is not a number")
    n = float(value)
    if not math.isfinite(n) or (minimum is not None and n < minimum):
        raise ValueError(f"{label}: invalid value {value!r}")
    return n


def integer(value, label="integer"):
    n = finite(value, label)
    if int(n) != n:
        raise ValueError(f"{label}: expected integer")
    return int(n)


def pitch(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", str(value))
    if not match:
        raise ValueError(f"invalid pitch {value!r}; use C4, F#3, Bb3 or MIDI integer")
    name, accidental, octave = match.groups()
    return (int(octave) + 1) * 12 + dict(C=0, D=2, E=4, F=5, G=7, A=9, B=11)[name.upper()] + {"": 0, "#": 1, "b": -1}[accidental]


def pitch_name(midi):
    return ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")[midi % 12] + str(midi // 12 - 1)


def white_rank(midi):
    if midi % 12 not in WHITE:
        raise ValueError(f"{pitch_name(midi)} is a black key; add a calibrated key_override")
    return midi // 12 * 7 + WHITE.index(midi % 12)


def white_midi(rank):
    return rank // 7 * 12 + WHITE[rank % 7]


def resolve_note(value, tonic, first_midi):
    if str(value) in ("0", "-", "rest"):
        return None
    s = str(value).strip()
    if s.startswith("key:"):
        return first_midi + integer(s[4:], "key number") - 1
    if s.startswith("midi:"):
        return integer(s[5:], "MIDI")
    match = re.fullmatch(r"([#b]?)([1-7])([+-]*)", s)
    if match:
        acc, degree, octaves = match.groups()
        return pitch(tonic) + WHITE[int(degree) - 1] + 12 * (octaves.count("+") - octaves.count("-")) + {"": 0, "#": 1, "b": -1}[acc]
    return pitch(value)


def vector(value, n, label):
    if not isinstance(value, list) or len(value) != n:
        raise ValueError(f"{label} must contain {n} values")
    return [finite(v, label) for v in value]


def check_limits(values, limits, label):
    for i, (value, (low, high)) in enumerate(zip(values, limits)):
        if not low <= value <= high:
            raise ValueError(f"{label} joint {i}: {value} outside [{low}, {high}]")


HAND_MEASUREMENT_TOLERANCE_RAD = 0.05


def hand_command_seed(values, limits, reference=None):
    """Build a legal holding target from an explicitly recorded hand reference.

    Measurements remain untouched. Only small excursions at a boundary near
    that recorded reference may use the nearest legal command boundary.
    """
    q = vector(values, 12, "measured hand")
    ref = vector(reference, 12, "start_reference_rad") if reference is not None else None
    if len(limits) != 12:
        raise ValueError("hand limits must contain 12 pairs")
    margin = HAND_MEASUREMENT_TOLERANCE_RAD
    if ref is not None:
        for i, (v, (lo, hi)) in enumerate(zip(ref, limits)):
            if v < lo - margin or v > hi + margin:
                raise ValueError(f"start_reference_rad joint {i} exceeds measurement margin")
    result = []
    for i, (v, (lo, hi)) in enumerate(zip(q, limits)):
        if lo <= v <= hi:
            result.append(v)
            continue
        edge = lo if v < lo else hi
        if (ref is None or abs(v - edge) > margin + 1e-12
                or abs(ref[i] - edge) > margin + 1e-12
                or abs(v - ref[i]) > margin + 1e-12):
            raise ValueError(f"hand joint {i}: no matching recorded boundary reference")
        result.append(edge)
    check_limits(result, limits, "hand command seed")
    return result


def load_config(path):
    c = json.loads(Path(path).read_text())
    validate_config(c)
    return c


def validate_config(c):
    if c.get("version") != 1:
        raise ValueError("config version must be 1")
    if not isinstance(c.get("calibrated"), bool):
        raise ValueError("calibrated must be a boolean")
    for key in ("control_hz", "feedback_timeout_s", "arrival_tolerance_rad", "arrival_timeout_s",
                "arrival_dwell_s", "max_arm_velocity_rad_s", "prepare_duration_s", "move_duration_s",
                "settle_s", "release_settle_s", "max_lateness_s", "max_tracking_error_rad"):
        finite(c["timing"][key], key, 0.000001)
    finite(c["timing"]["lead_in_s"], "lead_in_s", 0)
    if c["timing"]["control_hz"] < 20:
        raise ValueError("control_hz must be >= 20")
    if c["timing"]["settle_s"] < c["timing"]["arrival_dwell_s"] + 2 / c["timing"]["control_hz"]:
        raise ValueError("settle_s must allow arrival_dwell_s plus two control ticks")
    keyboard = c["keyboard"]
    if integer(keyboard["key_count"]) != 61:
        raise ValueError("this demo expects a 61-key keyboard")
    first = pitch(keyboard["first_note"])
    if first < 0 or first + 60 > 127:
        raise ValueError("keyboard outside MIDI range")
    for side in SIDES:
        h = c["hands"][side]
        if not isinstance(h.get("arrival_check_enabled", True), bool):
            raise ValueError(f"{side} arrival_check_enabled must be a boolean")
        vector(h["initial_arm_rad"], 7, side + " initial_arm_rad")
        vector(h["arm_step_rad_per_white"], 7, side + " arm_step")
        vector(h["hover_rad"], 12, side + " hover_rad")
        for key, n in (("arm_limits_rad", 7), ("hand_limits_rad", 12)):
            if len(h[key]) != n:
                raise ValueError(f"{side} {key}: wrong length")
            for pair in h[key]:
                low, high = vector(pair, 2, key)
                if low >= high:
                    raise ValueError(f"invalid limits {pair}")
        check_limits(h["initial_arm_rad"], h["arm_limits_rad"], side)
        check_limits(h["hover_rad"], h["hand_limits_rad"], side)
        if "start_reference_rad" in h:
            hand_command_seed(h["start_reference_rad"], h["hand_limits_rad"], h["start_reference_rad"])
        anchor = pitch(h["anchor_note"])
        offsets = h["finger_white_offsets"]
        if set(offsets) != set(FINGERS):
            raise ValueError("exactly index/middle/ring/pinky required; no thumb")
        if len(set(integer(v) for v in offsets.values())) != 4:
            raise ValueError("finger offsets must be distinct")
        if not first <= anchor < first + 61:
            raise ValueError("anchor outside keyboard")
        white_rank(anchor)
        lo, hi = h["shift_range_white"]
        if integer(lo) > 0 or integer(hi) < 0:
            raise ValueError("shift range must include zero")
        for finger in FINGERS:
            entries = h["press_rad"][finger]
            if not entries:
                raise ValueError("press must drive at least one finger joint")
            target = list(h["hover_rad"])
            for name, value in entries.items():
                if name not in HAND_NAMES or HAND_NAMES.index(name) not in FINGER_JOINTS[finger]:
                    raise ValueError(f"{finger} cannot command joint {name}")
                target[HAND_NAMES.index(name)] = finite(value)
            check_limits(target, h["hand_limits_rad"], side + " press")
        for shift, q in h.get("arm_positions_rad", {}).items():
            integer(shift, "arm position shift")
            check_limits(vector(q, 7, "arm_positions_rad"), h["arm_limits_rad"], side)
            if integer(shift) == 0 and q != h["initial_arm_rad"]:
                raise ValueError("arm_positions_rad[0] must equal initial_arm_rad")
        for note, override in h.get("key_overrides", {}).items():
            midi = pitch(note)
            if not first <= midi < first + 61 or override["finger"] not in FINGERS:
                raise ValueError("invalid key_override")
            integer(override["shift_white"])
            # Pose calibration is necessary to reach a black key in depth as well as laterally.
            if str(override["shift_white"]) not in h.get("arm_positions_rad", {}):
                raise ValueError("key_override requires explicit arm_positions_rad at its shift")


def arm_target(c, side, shift):
    h = c["hands"][side]
    lo, hi = h["shift_range_white"]
    if not lo <= shift <= hi:
        raise ValueError(f"{side}: wrist shift {shift} outside calibrated range")
    target = h.get("arm_positions_rad", {}).get(str(shift))
    if target is None:
        target = [q + shift * d for q, d in zip(h["initial_arm_rad"], h["arm_step_rad_per_white"])]
    check_limits(target, h["arm_limits_rad"], side + " arm target")
    return list(target)


@dataclass(frozen=True)
class Note:
    row: int
    start_s: float
    end_s: float
    side: str
    midi: int
    finger: str | None = None
    shift: int | None = None


@dataclass(frozen=True)
class Action:
    at_s: float
    kind: str
    side: str
    finger: str | None = None
    midi: int | None = None
    shift: int = 0
    duration_s: float = 0.0
    row: int = 0

    def as_dict(self):
        return asdict(self)


def load_score(path, c):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        meta = {"tempo_bpm": c["score_defaults"]["tempo_bpm"], "tonic": c["score_defaults"]["tonic"], "events": rows}
    else:
        meta = json.loads(path.read_text())
    tempo = finite(meta["tempo_bpm"], "tempo_bpm", 0.001)
    tonic = meta.get("tonic", c["score_defaults"]["tonic"])
    unit = meta.get("time_unit", "beats")
    if unit not in ("beats", "seconds"):
        raise ValueError("time_unit must be beats or seconds")
    scale = 60 / tempo if unit == "beats" else 1.0
    first = pitch(c["keyboard"]["first_note"])
    notes, total = [], 0.0
    for row, e in enumerate(meta["events"], 1):
        start = finite(e["start"], f"row {row} start", 0) * scale
        duration = finite(e["duration"], f"row {row} duration", 0.000001) * scale
        total = max(total, start + duration)
        midi = resolve_note(e["note"], tonic, first)
        if midi is None:
            continue
        if not first <= midi < first + c["keyboard"]["key_count"]:
            raise ValueError(f"row {row}: note outside 61 keys")
        side = e["hand"]
        if side not in SIDES:
            raise ValueError(f"row {row}: hand must be left/right")
        finger = e.get("finger") or None
        if finger is not None and finger not in FINGERS:
            raise ValueError(f"row {row}: invalid finger (thumb is not used)")
        raw_gate = e.get("gate")
        gate = finite(meta.get("gate", 0.8) if raw_gate in (None, "") else raw_gate, "gate", 0.000001)
        if gate > 1:
            raise ValueError("gate must be <= 1")
        shift = e.get("shift_white")
        notes.append(Note(row, start, start + duration * gate, side, midi, finger,
                          None if shift in (None, "") else integer(shift)))
    if not notes:
        raise ValueError("score contains no notes")
    notes.sort(key=lambda n: (n.start_s, n.row))
    for a, b in zip(notes, notes[1:]):
        if b.start_s < a.end_s - 1e-9 or abs(b.start_s - a.start_s) < 1e-9:
            raise ValueError(f"rows {a.row}/{b.row}: overlapping notes; this demo presses one key globally at a time")
        if b.start_s < a.end_s + c["timing"]["release_settle_s"] - 1e-9:
            raise ValueError(f"rows {a.row}/{b.row}: insufficient global finger release time; lower gate or add rest")
    return meta, notes, total


def plan_score(path, c):
    validate_config(c)
    meta, notes, total = load_score(path, c)
    timing = c["timing"]
    lead = timing["lead_in_s"]
    shifts = dict.fromkeys(SIDES, 0)
    free = dict.fromkeys(SIDES, 0.0)
    actions = []
    for n in notes:
        h, side = c["hands"][n.side], n.side
        overrides = h.get("key_overrides", {})
        override = next((value for key, value in overrides.items() if pitch(key) == n.midi), None)
        # A calibrated black-key pose is not a generic white-key translation.
        # Reserve its shift index unless a note has its own explicit override.
        reserved = {value["shift_white"] for key, value in overrides.items() if pitch(key) % 12 not in WHITE}
        candidates = []
        for finger in FINGERS:
            if n.finger is not None and n.finger != finger:
                continue
            if override:
                if finger != override["finger"]:
                    continue
                shift = override["shift_white"]
            else:
                shift = white_rank(n.midi) - white_rank(pitch(h["anchor_note"])) - h["finger_white_offsets"][finger]
                if shift in reserved:
                    continue
            if n.shift is not None and shift != n.shift:
                continue
            try:
                arm_target(c, side, shift)
            except ValueError:
                continue
            candidates.append((abs(shift - shifts[side]), FINGERS.index(finger), finger, shift))
        if not candidates:
            raise ValueError(f"row {n.row}: no calibrated fingering for {pitch_name(n.midi)}")
        _, _, finger, shift = min(candidates)
        start, end = lead + n.start_s, lead + n.end_s
        if shift != shifts[side]:
            q0, q1 = arm_target(c, side, shifts[side]), arm_target(c, side, shift)
            # Cubic smoothstep has maximum derivative 1.5.
            duration = max(timing["move_duration_s"], 1.5 * max(abs(a - b) for a, b in zip(q0, q1)) / timing["max_arm_velocity_rad_s"])
            move_at = start - duration - timing["settle_s"]
            if move_at < free[side] - 1e-9:
                missing = free[side] - move_at
                raise ValueError(f"row {n.row}: {side} wrist move lacks {missing:.3f}s; add rest, lower tempo, increase lead-in or choose another fingering")
            actions.append(Action(move_at, "move", side, shift=shift, duration_s=duration, row=n.row))
        elif start < free[side] - 1e-9:
            raise ValueError(f"row {n.row}: insufficient finger release time; lower gate or tempo")
        actions.append(Action(start, "press", side, finger, n.midi, shift, row=n.row))
        actions.append(Action(end, "release", side, finger, n.midi, shift, row=n.row))
        free[side] = end + timing["release_settle_s"]
        shifts[side] = shift
    order = {"release": 0, "move": 1, "press": 2}
    actions.sort(key=lambda a: (a.at_s, order[a.kind], a.side))
    return {"title": meta.get("title", Path(path).stem), "duration_s": lead + total,
            "notes": len(notes), "actions": [a.as_dict() for a in actions]}


def copy_config(c):
    return copy.deepcopy(c)
