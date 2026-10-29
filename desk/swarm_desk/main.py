"""`python -m swarm_desk`: open the desktop window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parents[1]
REPO = DESK_DIR.parent


def _paths() -> None:
    """The desk reuses the dashboard's run reader and job launcher: make `swarm_ui` importable from a checkout."""
    ui = str(REPO / "ui")
    if ui not in sys.path:
        sys.path.insert(0, ui)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swarm-desk", description="Desktop neuron view of the swarm.")
    p.add_argument("--workspace", type=Path, default=REPO / "workspace", help="where the swarm's projects live")
    p.add_argument("--demo", action="store_true", help="start the free scripted demo as soon as the window opens")
    p.add_argument("--open", metavar="PROJECT/RUN", help="open this run (replay, or live if it is still going)")
    p.add_argument("--quit-after", type=float, metavar="S", help=argparse.SUPPRESS)  # smoke tests and screenshots
    args = p.parse_args(argv)
    _paths()

    import tkinter as tk

    from swarm_desk.app import Desk
    from swarm_desk.feed import RunRef

    root = tk.Tk()
    desk = Desk(root, REPO, args.workspace.resolve(), DESK_DIR / ".data")
    if args.open and "/" in args.open:
        project, run = args.open.split("/", 1)
        desk.open(RunRef(project, run))
    if args.demo:
        root.after(600, lambda: desk.launch("demo"))
    if args.quit_after:
        root.after(int(args.quit_after * 1000), desk.close)
    root.lift()
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0
