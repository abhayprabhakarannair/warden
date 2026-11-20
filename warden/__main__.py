import signal
import subprocess
import sys
import threading
import traceback

from warden import __version__
from warden.agent import Agent
from warden.config import ConfigError, load
from warden.modules.clients import Clients
from warden.modules.power import Power
from warden.modules.sensors import Sensors
from warden.modules.system import System
from warden.modules.wake import Wake
from warden.modules.wan import Wan
from warden.modules.wifi import UbusWifi


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


def poll_forever(module, stop):
    """Runs module.poll() until stop is set. A failing poll is logged and skipped: if this thread died, the
    module's state would freeze while the agent still reported online. The same error repeated is logged
    once until a poll succeeds."""
    last = None
    while not stop.wait(module.poll_every_s()):
        try:
            module.poll()
            last = None
        except Exception as e:  # noqa: BLE001
            if repr(e) != last:
                print(f"warden: {module.name} poll failed", file=sys.stderr)
                traceback.print_exc()
            last = repr(e)


def build(config, bus):
    agent = Agent(bus, config.topic, __version__, state_every_s=config.state_every_s)
    wan = Wan(agent, config.wans, dict(config.wan_preferences)) if "wan" in config.modules else None
    wifi = UbusWifi(config.wifi_api, config.wifi_user, config.wifi_password) if config.wifi_api else None
    factories = {
        "power": lambda: Power(agent, config.grace_s, schedule(agent), run, dry_run=config.dry_run),
        "system": lambda: System(agent),
        "sensors": lambda: Sensors(agent),
        "wake": lambda: Wake(agent, config.wake_targets, config.wake_broadcast),
        "wan": lambda: wan,
        "clients": lambda: Clients(agent, config.lan, wifi=wifi, route=wan.route if wan else None),
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
    for module in agent.modules:
        if hasattr(module, "poll"):
            threading.Thread(target=poll_forever, args=(module, stop), daemon=True).start()
    while not stop.wait(1):
        agent.tick()
    agent.stop()
    bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
