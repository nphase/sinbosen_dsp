"""The `sinbosen-la548-ui` entry point; the studio starts without importing gradio or plotly."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from .studio_app import create_studio_app

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local LA548 DSP Studio (requires the ui extra)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--classic", action="store_true", help="start the older Gradio UI instead")
    parser.add_argument("--demo", action="store_true", help="start offline (also the default)")
    args = parser.parse_args(argv)
    if args.classic:
        from . import ui

        ui.launch_classic(args.host, args.port)
        return
    loopback = args.host in LOOPBACK_HOSTS
    if not loopback:
        print("Warning: the UI has no authentication and can change the device's output levels; "
              "anyone who can reach this address can use it.")
    shown = f"[{args.host}]" if ":" in args.host else args.host
    print(f"DSP Studio: http://{shown}:{args.port}  (Ctrl-C to stop)")
    app = create_studio_app(allowed_hosts=() if loopback else (args.host,))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
