#!/usr/bin/env python3
"""Offline score/coordination checks only. Never opens ROS, CAN, serial or SDK."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from morla_piano.model import load_config, plan_score
from morla_piano.engine import Engine, FakeBackend, VirtualClock


def run(config_path, scores, output):
    raw = Path(config_path).read_bytes()
    config = load_config(config_path)
    profiles = {
        'original': None,
        'candidate_10pct': dict(sdk_speed_percent=10, max_arm_velocity_rad_s=.225,
            move_duration_s=.45, settle_s=.2, release_settle_s=.12, min_press_s=.15),
        'comparison_15pct': dict(sdk_speed_percent=15, max_arm_velocity_rad_s=.3,
            move_duration_s=.35, settle_s=.15, release_settle_s=.12, min_press_s=.15),
    }
    report = dict(config_sha256=hashlib.sha256(raw).hexdigest(), hardware_motion=False,
        caveat='Instantaneous software plant. SDK % is not modeled physical velocity; no contact/audio or real finger response proof.',
        profiles={})
    for name, profile in profiles.items():
        c = copy.deepcopy(config)
        c.pop('performance', None)
        if profile:
            c['performance'] = profile
        results = []
        for path in sorted(Path(scores).glob('*C3_C5_40bpm.csv')):
            passed = []
            failures = {}
            for bpm in range(40, 121):
                c['score_defaults']['tempo_bpm'] = bpm
                try:
                    plan_score(path, c)
                    passed.append(bpm)
                except ValueError as exc:
                    if bpm in (50,60,70,80,90,100,120):
                        failures[bpm] = str(exc)
            c['score_defaults']['tempo_bpm'] = 60 if profile else 40
            plan = plan_score(path, c)
            clock = VirtualClock()
            backend = FakeBackend(c, clock)
            # Keep no unbounded command history during full virtual playback.
            from collections import deque
            backend.history = deque(maxlen=2)
            engine = Engine(c, backend, clock)
            engine.prepare()
            engine.play(plan)
            assert engine.state == 'FINISHED' and engine.pressed is None
            presses = [a for a in plan['actions'] if a['kind']=='press']
            releases = [a for a in plan['actions'] if a['kind']=='release']
            holds = [b['at_s']-a['at_s'] for a,b in zip(presses,releases)]
            gaps = [b['at_s']-a['at_s'] for a,b in zip(releases,presses[1:])]
            results.append(dict(score=path.name, max_offline_bpm=max(passed,default=None),
                simulated_bpm=c['score_defaults']['tempo_bpm'], duration_s=plan['duration_s'],
                min_press_s=min(holds), min_release_gap_s=min(gaps), finished=True, failures=failures))
        report['profiles'][name] = dict(parameters=profile, scores=results)
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    for name, value in report['profiles'].items():
        print(name, [(s['score'],s['max_offline_bpm']) for s in value['scores']])


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True)
    p.add_argument('--scores',default=str(Path(__file__).resolve().parents[1]/'scores'))
    p.add_argument('--output',required=True)
    a=p.parse_args()
    run(a.config,a.scores,a.output)
