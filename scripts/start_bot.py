#!/usr/bin/env python3
"""Start the existing local installation without recreating any containers."""
import json
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


class StartupError(Exception):
    pass


def docker(*args):
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StartupError("Docker is unavailable. Check Docker Desktop and its Ubuntu integration.") from None
    if result.returncode:
        raise StartupError("Docker could not complete startup. Check the akubot containers in Docker Desktop.")
    return result.stdout


def inspect(name):
    try:
        return json.loads(docker("inspect", name))[0]
    except (ValueError, IndexError, KeyError):
        raise StartupError("Docker returned an unexpected container status.") from None


def wait_for(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    raise StartupError(message)


def main():
    print("Waiting for Docker Desktop...", flush=True)
    def engine_ready():
        try:
            docker("info", "--format", "{{.ServerVersion}}")
            return True
        except StartupError:
            return False
    wait_for(engine_ready, 180, "Docker did not become ready. Open Docker Desktop and check for an error.")

    names = ("akubot-local-ai-ollama", "akubot-dashboard-and-discord")
    containers = [inspect(name) for name in names]
    for container, service in zip(containers, ("local-ai-ollama", "dashboard-and-discord")):
        labels = container.get("Config", {}).get("Labels", {}) or {}
        if (labels.get("com.docker.compose.project") != "akubot"
                or labels.get("com.docker.compose.service") != service):
            raise StartupError("The existing containers do not match this akubot installation.")
        if container.get("State", {}).get("Paused"):
            raise StartupError("An akubot container is paused. Unpause it in Docker Desktop first.")

    bindings = containers[1].get("HostConfig", {}).get("PortBindings", {}).get("8787/tcp") or []
    if len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
        raise StartupError("The dashboard must be published only on 127.0.0.1.")
    port = str(bindings[0].get("HostPort", ""))
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise StartupError("The dashboard port is not configured correctly.")
    url = f"http://localhost:{port}/"

    for name in names:
        if inspect(name).get("State", {}).get("Status") != "running":
            print(f"Starting {name}...", flush=True)
            docker("start", name)
        if name == names[0]:
            def model_ready():
                state = inspect(name).get("State", {})
                if state.get("Status") in ("exited", "dead"):
                    raise StartupError("The local AI container stopped. Check it in Docker Desktop.")
                return state.get("Health", {}).get("Status") == "healthy"
            wait_for(model_ready, 180, "The local AI container did not become healthy. Check Docker Desktop.")

    print("Waiting for the dashboard...", flush=True)
    opener = build_opener(ProxyHandler({}))
    def dashboard_ready():
        try:
            with opener.open(url, timeout=3) as response:
                return response.status == 200
        except (OSError, URLError):
            return False
    wait_for(dashboard_ready, 120, "The dashboard did not become ready. Check the dashboard-and-Discord container.")
    print("akubot is running. Closing this window will leave it running.", flush=True)
    print(f"AKUBOT_DASHBOARD={url}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except StartupError as error:
        print(f"Could not start akubot: {error}", flush=True)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
