"""Own and clean up the entire software-only launch process tree."""
import os
from pathlib import Path
import signal
import subprocess
import sys


def descendants(parent):
    result = set()
    for line in subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True).splitlines():
        pid, ppid = map(int, line.split())
        result.add((pid, ppid))
    family = {parent}
    while True:
        new = {pid for pid, ppid in result if ppid in family}
        if new <= family:
            return family - {parent}
        family |= new


def reset_sigint():
    # Background shell jobs inherit ignored SIGINT; ROS launch needs its default restored.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def main():
    if os.environ.get("ROS_DOMAIN_ID") != "73":
        raise RuntimeError("software tests require ROS_DOMAIN_ID=73")
    Path("logs").mkdir(exist_ok=True)
    with open("logs/ros-fake-drivers.log", "w") as log:
        launch = subprocess.Popen(["ros2", "launch", "./launch/piano_drivers.launch.py", "mode:=fake"],
                                  stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                  preexec_fn=reset_sigint)
        try:
            with open("logs/ros-inspect.json", "w") as inspection:
                subprocess.run([sys.executable, "-m", "morla_piano", "inspect", "--backend", "ros",
                                "--expect-driver", "fake"], stdout=inspection, check=True)
            for score, name in (("scores/demo.json", "ros-performance"), ("scores/template.csv", "ros-csv-performance")):
                path = Path(f"logs/{name}.jsonl")
                path.unlink(missing_ok=True)
                subprocess.run([sys.executable, "-m", "morla_piano", "run", score, "--backend", "ros",
                                "--expect-driver", "fake", "--log", str(path)], check=True)
        finally:
            children = descendants(launch.pid)
            if launch.poll() is None:
                launch.send_signal(signal.SIGINT)
            try:
                launch.wait(timeout=12)
            except subprocess.TimeoutExpired:
                launch.terminate()
                launch.wait(timeout=3)
            for pid in children:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    main()
