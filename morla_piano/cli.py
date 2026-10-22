from __future__ import annotations

import argparse
import copy
import json
import queue
import shlex
import sys
import threading
from pathlib import Path

from .engine import Clock, Engine, FakeBackend, VirtualClock
from .model import SIDES, load_config, plan_score, validate_config

ROOT = Path(__file__).resolve().parent.parent


def parser():
    p = argparse.ArgumentParser(description="独立 MORLA 钢琴演奏框架")
    p.add_argument("--config", default=str(ROOT / "config/piano.placeholder.json"))
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="离线检查简谱并生成动作时间线")
    plan.add_argument("score")
    plan.add_argument("--out")
    for name in ("run", "session", "inspect", "capture"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--backend", choices=("fake", "ros"), default="fake")
        cmd.add_argument("--expect-driver", choices=("fake", "hardware"), default="hardware")
        if name in ("run", "session"):
            cmd.add_argument("--log", default=str(ROOT / "logs/performance.jsonl"))
        if name == "run":
            cmd.add_argument("score")
            cmd.add_argument("--fast", action="store_true", help="fake 后端虚拟时间验证")
        if name == "capture":
            cmd.add_argument("--out", required=True, help="另存实测双臂初始位姿和手悬停关节")
    return p


def make_backend(args, c, clock):
    if args.backend == "fake":
        return FakeBackend(c, clock)
    from .ros_backend import RosBackend
    return RosBackend(c, args.expect_driver)


def write_capture(backend, c, path):
    snapshot = backend.snapshot()
    captured = copy.deepcopy(c)
    captured["calibrated"] = False
    for side in SIDES:
        captured["hands"][side]["initial_arm_rad"] = snapshot[side + "_arm"]
        captured["hands"][side]["hover_rad"] = snapshot[side + "_hand"]
        captured["hands"][side].get("arm_positions_rad", {}).pop("0", None)
    validate_config(captured)
    Path(path).write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n")
    print(f"已保存实测初始位置到 {path}；按键与移位参数仍需标定。")


def session(engine, backend, c):
    """Input thread remains responsive to stop while main thread executes music."""
    commands = queue.Queue()

    def read_commands():
        while True:
            try:
                line = input("piano> ")
            except EOFError:
                engine.request_stop()
                commands.put("quit")
                return
            if line.strip() in ("stop", "quit", "exit"):
                engine.request_stop()
            commands.put(line)

    threading.Thread(target=read_commands, daemon=True).start()
    print("命令: prepare | play <谱表> | start <谱表> | position <JSON文件> | capture <输出配置> | status | stop | quit")
    print("start=移动到钢琴初始位+演奏；play要求已prepare。stop可在演奏中输入。")
    while True:
        try:
            engine.idle()
            try:
                line = commands.get(timeout=0.01)
            except queue.Empty:
                continue
            parts = shlex.split(line)
            if not parts:
                continue
            cmd = parts[0]
            if cmd in ("quit", "exit"):
                return
            if cmd == "status":
                print(json.dumps(engine.status(), ensure_ascii=False))
            elif cmd == "prepare":
                engine.prepare()
                print("READY：双臂实测到位，可以 play。")
            elif cmd in ("start", "play"):
                if len(parts) != 2:
                    raise ValueError("用法: start/play <谱表路径>")
                plan = plan_score(parts[1], c)
                if cmd == "start":
                    engine.prepare()
                engine.play(plan)
                print(json.dumps(engine.status(), ensure_ascii=False))
            elif cmd == "position":
                if len(parts) != 2:
                    raise ValueError("position文件格式: {\"left\":[7个rad],\"right\":[7个rad]}")
                pose = json.loads(Path(parts[1]).read_text())
                updated = copy.deepcopy(c)
                for side in SIDES:
                    updated["hands"][side]["initial_arm_rad"] = pose[side]
                    updated["hands"][side].get("arm_positions_rad", {}).pop("0", None)
                validate_config(updated)
                c.clear()
                c.update(updated)
                engine.state = "CONNECTED"
                engine.prepare()
                print("新初始位置已到位（本次会话生效）。")
            elif cmd == "capture" and len(parts) == 2:
                write_capture(backend, c, parts[1])
            elif cmd == "stop":
                if engine.state not in ("FAULT", "STOPPED") and engine.targets is not None:
                    engine._normal_stop()
                print(json.dumps(engine.status(), ensure_ascii=False))
            else:
                raise ValueError("未知命令或参数数量错误")
        except KeyboardInterrupt:
            engine.request_stop()
            if engine.targets is not None and engine.state != "FAULT":
                engine._normal_stop()
            print("已停止。")
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)


def main(argv=None):
    args = parser().parse_args(argv)
    backend = None
    try:
        c = load_config(args.config)
        if args.command == "plan":
            plan = plan_score(args.score, c)
            if args.out:
                Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
            print(f"{plan['title']}: {plan['notes']}个音，{plan['duration_s']:.3f}s，"
                  f"{sum(a['kind'] == 'move' for a in plan['actions'])}次手腕移位")
            if not args.out:
                for a in plan["actions"]:
                    print(f"{a['at_s']:8.3f}s {a['kind']:7} {a['side']:5} {a['finger'] or '-':6} "
                          f"midi={a['midi']} shift={a['shift']:+d}")
            return 0
        if getattr(args, "fast", False) and args.backend != "fake":
            raise ValueError("--fast only works with the fake backend")
        # Validate all score actions before connecting, claiming or enabling anything.
        plan = plan_score(args.score, c) if args.command == "run" else None
        clock = VirtualClock() if getattr(args, "fast", False) else Clock()
        backend = make_backend(args, c, clock)
        backend.connect()
        if args.command == "inspect":
            print(json.dumps(backend.snapshot(), ensure_ascii=False, indent=2))
        elif args.command == "capture":
            write_capture(backend, c, args.out)
        else:
            engine = Engine(c, backend, clock, args.log)
            if args.command == "session":
                session(engine, backend, c)
            else:
                engine.prepare()
                engine.play(plan)
                print(json.dumps(engine.status(), ensure_ascii=False))
                if engine.state != "FINISHED":
                    return 2
        return 0
    except KeyboardInterrupt:
        if backend and backend.claimed:
            backend.emergency_stop()
        print("已中断，已请求停止。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if backend:
            backend.close()


if __name__ == "__main__":
    sys.exit(main())
