#!/usr/bin/env python3
"""MAV_WEB — Web dashboard entrypoint for MAVERIC GSS."""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time


def _parse_cli_into_env() -> None:
    """Translate CLI flags into env vars consumed deeper in the stack.

    Runs before ``create_app()`` so any flag that affects server-side
    construction (e.g. ``--ephemeral``, which redirects every disk-write
    path to a tempdir) takes effect on the first runtime build.
    """
    parser = argparse.ArgumentParser(prog="MAV_WEB", description=__doc__)
    parser.add_argument(
        "--ephemeral", action="store_true",
        help="Redirect all disk writes to a tempdir (cleaned on exit) and "
             "block gss.yml / mission.yml save-back. Use for fake_flight "
             "test sessions when you want zero-trace operations state.",
    )
    parser.add_argument(
        "--mission", metavar="ID",
        help="Mission package to run (loads/persists its own gss.<ID>.yml "
             "operator config; plain launches keep maveric's gss.yml).",
    )
    args, _ = parser.parse_known_args()
    if args.ephemeral:
        os.environ["GSS_EPHEMERAL"] = "1"
    if args.mission:
        os.environ["GSS_MISSION"] = args.mission.strip()


_parse_cli_into_env()

# Bootstrap runtime dependencies BEFORE any non-stdlib import.
# If any critical dep is missing, this call self-installs and os.execv's.
from mav_gss_lib.updater import bootstrap_dependencies
bootstrap_dependencies()

# Safe — all critical deps guaranteed importable past this line.
import uvicorn

from mav_gss_lib.server.app import create_app
from mav_gss_lib.server.state import HOST, PORT

app = create_app()


def _wait_for_server_and_open(url: str, host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                import webbrowser
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    runtime = app.state.runtime
    mission_name = runtime.mission_name
    mission = runtime.mission_id
    print(f"{mission_name} GSS Web -> {url}")
    # Skip the auto-open when a self-restart brought us up: the updater sets
    # MAV_UPDATE_APPLIED and the mission switcher sets MAV_MISSION_SWITCHED
    # in the new process env for exactly this kind of signal — the operator's
    # existing browser tab is already polling /api/status and will reload
    # itself once uvicorn is ready, so spawning a second tab just leaves
    # them with duplicates. MAV_UPDATE_APPLIED is read non-destructively so
    # `check_for_updates` can still pop it later to render the UI's
    # "Updated to <sha>" label. Read happens before the thread starts, which
    # is before uvicorn's lifespan schedules the check — race-free.
    if not os.environ.get("MAV_UPDATE_APPLIED") and not os.environ.get("MAV_MISSION_SWITCHED"):
        threading.Thread(
            target=_wait_for_server_and_open,
            args=(url, HOST, PORT),
            daemon=True,
            name=f"{mission}-web-open",
        ).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", ws_max_size=65536)
