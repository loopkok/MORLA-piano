import copy
import json
import tempfile
import unittest
from pathlib import Path

from morla_piano.engine import Engine, FakeBackend, VirtualClock, MotionConfirmationError
from morla_piano.model import FINGER_JOINTS, SIDES, load_config, plan_score, resolve_note, validate_config

ROOT = Path(__file__).resolve().parent.parent


class PianoTests(unittest.TestCase):
    def setUp(self):
        self.c = load_config(ROOT / "config/piano.placeholder.json")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def plan(self, events, **meta):
        path = Path(self.temp.name) / "score.json"
        path.write_text(json.dumps(dict(tempo_bpm=60, events=events, **meta)))
        return plan_score(path, self.c)

    def event(self, start=0, hand="left", note="C3", **extra):
        return dict(start=start, duration=1, hand=hand, note=note, **extra)

    def engine(self, backend_type=FakeBackend):
        clock = VirtualClock()
        backend = backend_type(self.c, clock)
        backend.connect()
        engine = Engine(self.c, backend, clock)
        return engine, backend, clock

    def test_confirmed_finger_layout(self):
        expected = (("left", ("C3", "D3", "E3", "F3"), ("pinky", "ring", "middle", "index")),
                    ("right", ("C4", "D4", "E4", "F4"), ("index", "middle", "ring", "pinky")))
        for side, notes, fingers in expected:
            plan = self.plan([self.event(i, side, note) for i, note in enumerate(notes)])
            presses = [a for a in plan["actions"] if a["kind"] == "press"]
            self.assertEqual([a["finger"] for a in presses], list(fingers))
            self.assertTrue(all(a["shift"] == 0 for a in presses))

    def test_end_to_end_timing_and_only_active_finger_changes(self):
        engine, backend, _ = self.engine()
        for side in SIDES:
            backend.measured[side + "_hand"][:3] = [.2, .1, .3]
        plan = plan_score(ROOT / "scores/demo.json", self.c)
        engine.prepare()
        engine.play(plan)
        self.assertEqual(engine.state, "FINISHED")
        self.assertIsNone(engine.pressed)
        self.assertEqual(len([e for e in engine.events if e["event"] == "press"]), 12)
        for row in engine.events:
            if row["event"] in ("press", "release", "move"):
                self.assertLessEqual(row["lateness_s"], .011)
        for _, snapshot in backend.history:
            active = []
            for side in SIDES:
                q = snapshot[side + "_hand"]
                self.assertEqual(q[:3], [.2, .1, .3])
                for finger, indices in FINGER_JOINTS.items():
                    if any(q[i] != self.c["hands"][side]["hover_rad"][i] for i in indices):
                        active.append((side, finger))
            self.assertLessEqual(len(active), 1)

    def test_json_and_csv_both_execute(self):
        for path in (ROOT / "scores/demo.json", ROOT / "scores/template.csv"):
            engine, _, _ = self.engine()
            engine.prepare()
            engine.play(plan_score(path, self.c))
            self.assertEqual(engine.state, "FINISHED")

    def test_rejects_overlapping_keys_across_hands(self):
        with self.assertRaisesRegex(ValueError, "overlapping"):
            self.plan([self.event(), self.event(.2, "right", "C4")])

    def test_alternating_hands_also_need_physical_release_time(self):
        with self.assertRaisesRegex(ValueError, "global finger release time"):
            self.plan([self.event(gate=1), self.event(1, "right", "C4")])

    def test_rejects_insufficient_wrist_gap_before_hardware(self):
        with self.assertRaisesRegex(ValueError, "wrist move lacks"):
            self.plan([self.event(), self.event(1, note="G3")])

    def test_cannot_play_without_prepare_or_replay_without_prepare(self):
        engine, _, _ = self.engine()
        plan = self.plan([self.event()])
        with self.assertRaises(RuntimeError):
            engine.play(plan)
        engine.prepare()
        engine.play(plan)
        with self.assertRaises(RuntimeError):
            engine.play(plan)

    def test_explicit_fingering_and_wrist_position(self):
        self.c["hands"]["right"]["arm_positions_rad"]["1"] = [0, .46, 0, 1.3, 0, 0, -.15]
        plan = self.plan([self.event(hand="right", note="G4", finger="pinky", shift_white=1)])
        engine, _, _ = self.engine()
        engine.prepare()
        engine.play(plan)
        self.assertEqual(engine.targets["right_arm"], self.c["hands"]["right"]["arm_positions_rad"]["1"])

    def test_rests_octaves_numbered_and_physical_key_notes(self):
        self.assertEqual(resolve_note("1", "C4", 36), 60)
        self.assertEqual(resolve_note("1-", "C4", 36), 48)
        self.assertEqual(resolve_note("1+", "C4", 36), 72)
        self.assertEqual(resolve_note("key:13", "C4", 36), 48)
        self.assertIsNone(resolve_note("0", "C4", 36))
        plan = self.plan([self.event(), self.event(10, note="0")])
        self.assertEqual(plan["notes"], 1)
        self.assertEqual(plan["duration_s"], 13)

    def test_black_keys_require_calibration(self):
        with self.assertRaisesRegex(ValueError, "black key"):
            self.plan([self.event(note="Bb3")])
        h = self.c["hands"]["left"]
        h["arm_positions_rad"]["4"] = list(h["initial_arm_rad"])
        h["key_overrides"]["Bb3"] = dict(finger="index", shift_white=4)
        plan = self.plan([self.event(note="A#3")])
        self.assertEqual(next(a for a in plan["actions"] if a["kind"] == "press")["finger"], "index")

    def test_black_key_pose_is_not_reused_for_automatic_white_mapping(self):
        h = self.c["hands"]["right"]
        h["arm_positions_rad"]["1"] = [0, .46, 0, 1.3, 0, 0, -.15]
        h["key_overrides"]["F#4"] = dict(finger="index", shift_white=1)
        plan = self.plan([self.event(hand="right", note="G4")])
        press = next(a for a in plan["actions"] if a["kind"] == "press")
        self.assertNotEqual(press["shift"], 1)

    def test_bad_config_and_score_values(self):
        with self.assertRaisesRegex(ValueError, "thumb"):
            self.plan([self.event(finger="thumb")])
        with self.assertRaisesRegex(ValueError, "outside 61"):
            self.plan([self.event(note="C8")])
        with self.assertRaises(ValueError):
            self.plan([self.event(start=float("nan"))])
        with self.assertRaises(ValueError):
            self.plan([self.event(gate=0)])
        self.c["hands"]["left"]["press_rad"]["index"]["thumb_bend_joint"] = .2
        with self.assertRaisesRegex(ValueError, "cannot command"):
            validate_config(self.c)

    def test_normal_stop_releases_finger_without_estop(self):
        engine, backend, clock = self.engine()
        engine.prepare()
        original_sleep = clock.sleep
        begin = clock.now()
        def stop_during_note(seconds):
            original_sleep(seconds)
            if clock.now() > begin + 2.2:
                engine.request_stop()
        clock.sleep = stop_during_note
        engine.play(self.plan([self.event()]))
        self.assertEqual(engine.state, "STOPPED")
        self.assertFalse(backend.stopped)
        self.assertEqual(backend.measured["left_hand"], self.c["hands"]["left"]["hover_rad"])

    def test_feedback_failure_stops_remaining_notes(self):
        engine, backend, clock = self.engine()
        engine.prepare()
        original_health = backend.health
        deadline = clock.now() + 2.3
        def fail():
            original_health()
            if clock.now() > deadline:
                raise RuntimeError("feedback stale")
        backend.health = fail
        with self.assertRaisesRegex(RuntimeError, "feedback stale"):
            engine.play(self.plan([self.event(), self.event(1)]))
        self.assertEqual(engine.state, "FAULT")
        self.assertTrue(backend.stopped)
        self.assertEqual(len([e for e in engine.events if e["event"] == "press"]), 1)

    def test_late_scheduler_does_not_burst_notes(self):
        engine, backend, clock = self.engine()
        engine.prepare()
        original_sleep = clock.sleep
        def stalled_sleep(seconds):
            original_sleep(seconds + .2)
        clock.sleep = stalled_sleep
        with self.assertRaisesRegex(RuntimeError, "scheduler late"):
            engine.play(self.plan([self.event()]))
        self.assertTrue(backend.stopped)

    def test_measured_arrival_not_command_sent(self):
        class StuckBackend(FakeBackend):
            def send(self, targets):
                # Hand responds, but mechanical arms never move.
                for side in SIDES:
                    self.measured[side + "_hand"] = list(targets[side + "_hand"])
        engine, backend, _ = self.engine(StuckBackend)
        backend.measured["left_arm"][0] += .1
        with self.assertRaisesRegex(RuntimeError, "arrival timed out"):
            engine.prepare()
        self.assertEqual(engine.state, "STOPPED")
        self.assertFalse(backend.stopped)
        self.assertTrue(backend.claimed)

    def test_writer_conflict_while_idle_requests_stop(self):
        engine, backend, _ = self.engine()
        engine.prepare()
        def conflict():
            raise RuntimeError("competing writer")
        backend.pump = conflict
        engine.idle()
        self.assertEqual(engine.state, "FAULT")
        self.assertTrue(backend.stopped)

    def test_unconfirmed_press_releases_without_disabling_arms(self):
        engine, backend, _ = self.engine()
        engine.prepare()
        arm = list(backend.measured["left_arm"])
        engine._finger("left", "pinky", True)
        engine._send()
        engine._fault(MotionConfirmationError("press not confirmed"))
        self.assertEqual(engine.state, "STOPPED")
        self.assertFalse(backend.stopped)
        self.assertTrue(backend.claimed)
        self.assertEqual(backend.measured["left_arm"], arm)
        self.assertEqual(backend.measured["left_hand"], self.c["hands"]["left"]["hover_rad"])
        self.assertEqual(engine.error, "press not confirmed")

    def test_unconfirmed_motion_with_lost_feedback_still_estops(self):
        engine, backend, _ = self.engine()
        engine.prepare()
        def stale():
            raise RuntimeError("feedback stale")
        backend.health = stale
        engine._fault(MotionConfirmationError("arrival timeout"))
        self.assertEqual(engine.state, "FAULT")
        self.assertTrue(backend.stopped)
        self.assertEqual(engine.error, "feedback stale")

    def test_prepare_uses_completed_approach_as_final_motion_start_on_every_run(self):
        engine, backend, _ = self.engine()
        calls = []
        def approach():
            for side in SIDES:
                self.assertEqual(backend.measured[side + "_hand"], self.c["hands"][side]["hover_rad"])
                backend.measured[side + "_arm"][0] += .1
            calls.append((len(backend.history), backend.snapshot()))
        for _ in range(2):
            engine.prepare(approach=approach)
            index, pose = calls[-1]
            for side in SIDES:
                self.assertEqual(backend.history[index][1][side + "_arm"], pose[side + "_arm"])
            self.assertEqual(engine.state, "READY")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len([r for r in engine.events if r['event'] == 'prepare_approach_complete']), 2)

    def test_failed_approach_never_starts_final_zero_motion(self):
        engine, backend, _ = self.engine()
        backend.measured['right_arm'][0] += .1
        original = backend.snapshot()
        def fail():
            raise RuntimeError('native trajectory failed')
        with self.assertRaisesRegex(RuntimeError, 'native trajectory failed'):
            engine.prepare(approach=fail)
        self.assertEqual(engine.state, 'FAULT')
        self.assertFalse(any(r['event'] == 'prepare_begin' for r in engine.events))
        self.assertEqual(backend.measured['right_arm'], original['right_arm'])

    def test_right_hand_arrival_bypass_keeps_other_arrival_checks(self):
        engine, backend, _ = self.engine()
        self.c['hands']['right']['arrival_check_enabled'] = False
        backend.claim()
        engine.targets = backend.snapshot()
        wrong = list(engine.targets['right_hand'])
        wrong[5] += .12
        engine._await_arrival({'right_hand': wrong})
        assert engine.events[-1]['event'] == 'arrival_verification_skipped'
        assert engine._arrived({'right_hand': wrong}, .025)
        for key in ['left_hand', 'left_arm', 'right_arm']:
            wrong = list(backend.measured[key]);wrong[0] += .12
            assert not engine._arrived({key: wrong}, .025)

    def test_right_hand_arrival_bypass_still_requires_feedback_and_legal_targets(self):
        engine, backend, _ = self.engine()
        self.c['hands']['right']['arrival_check_enabled'] = False
        backend.claim()
        engine.targets = backend.snapshot()
        engine.targets['right_hand'][0] = -1
        with self.assertRaises(ValueError):
            engine._await_arrival({'right_hand': engine.targets['right_hand']})
        engine.targets = backend.snapshot()
        def stale():
            raise RuntimeError('feedback stale')
        backend.health = stale
        with self.assertRaisesRegex(RuntimeError, 'feedback stale'):
            engine._await_arrival({'right_hand': engine.targets['right_hand']})

    def test_playback_progress_is_live_and_finishes_with_exact_counts(self):
        engine, backend, _ = self.engine()
        plan = self.plan([self.event(0), self.event(2, note='D3')])
        engine.prepare()
        samples = []
        backend.pump = lambda: samples.append(engine.status().get('playback'))
        engine.play(plan)
        live = [x for x in samples if x is not None]
        assert live and any(0 < x['percent'] < 100 for x in live)
        assert all(x['percent'] < 100 and not x['completed'] for x in live)
        assert [x['elapsed_s'] for x in live] == sorted(x['elapsed_s'] for x in live)
        final = engine.status()['playback']
        assert final['percent'] == 100 and final['completed']
        assert final['completed_notes'] == final['total_notes'] == 2
        assert final['completed_actions'] == final['total_actions']
        engine.prepare()
        assert engine.status()['playback'] is None

    def test_cancelled_playback_progress_freezes_below_completion(self):
        engine, backend, clock = self.engine()
        plan = self.plan([self.event(0), self.event(3, note='D3')])
        engine.prepare()
        def cancel():
            if engine.playback and engine.playback['started_notes']:
                engine.request_stop()
        backend.pump = cancel
        engine.play(plan)
        stopped = engine.status()['playback']
        assert engine.state == 'STOPPED'
        assert stopped['percent'] < 100 and not stopped['completed']
        clock.sleep(10)
        assert engine.status()['playback']['elapsed_s'] == stopped['elapsed_s']

    def test_failed_claim_does_not_stop_unowned_robot(self):
        engine, backend, _ = self.engine()
        def reject():
            raise RuntimeError("competing writer")
        backend.claim = reject
        with self.assertRaises(RuntimeError):
            engine.prepare()
        self.assertEqual(engine.state, "FAULT")
        self.assertFalse(backend.stopped)


if __name__ == "__main__":
    unittest.main()
