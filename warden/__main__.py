import signal
import subprocess
import sys
import threading

from warden import __version__
from warden.agent import Agent
from warden.config import ConfigError, load
from warden.modules.power import Power
from warden.modules.sensors import Sensors
from warden.modules.system import System


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stderr or result.stdout


def schedule(agent):
    def start(delay, fn):
        timer = threading.Timer(delay, agent.locked(fn))
        timer.daemon = True
        timer.start()
        return timer

    return start


def build(config, bus):
    agent = Agent(bus, config.topic, __version__, state_every_s=config.state_every_s)
    factories = {
        "power": lambda: Power(agent, config.grace_s, schedule(agent), run, dry_run=config.dry_run),
        "system": lambda: System(agent),
        "sensors": lambda: Sensors(agent),
    }
    for name in config.modules:
        agent.add(factories[name]())
    return agent


def main():
    try:
        config = load()
    except ConfigError as e:
        print(f"warden: {e}", file=sys.stderr)
        return 2

    from warden.bus import Bus

    bus = Bus(config, client_id=f"warden-{config.user}", will_topic=f"{config.topic}/status")
    agent = build(config, bus)
    bus.on_connect = agent.on_connect
    bus.on_message = agent.on_message

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    bus.start()
    while not stop.wait(1):
        agent.tick()
    agent.stop()
    bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
