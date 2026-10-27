"""CLI for the demo agent: `python -m causeway.demo list` and `... run SCENARIO_DIR -o transcript.json`."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from causeway.client import DEFAULT_BASE_URL, DEFAULT_MODEL, ChatClient
from causeway.demo.agent import dumps, run


def _scenario_tree(path: Path) -> bool:
    """True when path holds at least one scenario folder (a directory with task.txt)."""
    if not path.is_dir():
        return False
    return any(child.is_dir() and (child / "task.txt").is_file() for child in path.iterdir())


def scenarios_root(package_dir: Path | None = None) -> Path:
    """Directory of demo scenarios.

    A checkout and an editable install keep them at <repo>/demo/scenarios, two levels above this
    file. A regular install maps that directory into the package, next to this module. The checkout
    wins when both exist, so a dev tree is not hidden by a stale copy inside the package.
    """
    package_dir = Path(__file__).resolve().parent if package_dir is None else Path(package_dir).resolve()
    installed = package_dir / "scenarios"
    checkout = package_dir.parents[1] / "demo" / "scenarios" if len(package_dir.parents) > 1 else None
    if checkout is not None and _scenario_tree(checkout):
        return checkout
    if _scenario_tree(installed):
        return installed
    return checkout if checkout is not None else installed


SCENARIOS = scenarios_root()


def _scenario_dirs() -> list[Path]:
    if not SCENARIOS.is_dir():
        return []
    return sorted(p for p in SCENARIOS.iterdir() if (p / "task.txt").is_file())


def _resolve(arg: str) -> Path:
    """Accept a scenario folder path, or a bare scenario name under demo/scenarios."""
    path = Path(arg)
    if path.is_dir():
        return path
    return SCENARIOS / arg


def _list(_: argparse.Namespace) -> int:
    for folder in _scenario_dirs():
        task = (folder / "task.txt").read_text(encoding="utf-8").strip()
        print(f"{folder.name}: {task}")
    return 0


def _run(args: argparse.Namespace) -> int:
    scenario = _resolve(args.scenario)
    task_file = scenario / "task.txt"
    source = scenario / "sandbox"
    if not task_file.is_file() or not source.is_dir():
        print(f"error: {scenario} needs task.txt and sandbox/", file=sys.stderr)
        return 2
    task = task_file.read_text(encoding="utf-8").strip()
    try:
        client = ChatClient(base_url=args.base_url, model=args.model)
    except ValueError as err:  # e.g. no MODEL_API_KEY; the message never includes the key
        print(f"error: {err}", file=sys.stderr)
        return 1
    workdir = Path(tempfile.mkdtemp(prefix=f"causeway-{scenario.name}-"))
    try:
        sandbox = workdir / "sandbox"
        shutil.copytree(source, sandbox)
        transcript = run(task, sandbox, client, max_steps=args.max_steps)
    finally:
        client.close()
        try:
            shutil.rmtree(workdir)
        except OSError as err:
            print(f"warning: could not remove temporary sandbox: {err}", file=sys.stderr)
    out = Path(args.output)
    out.write_text(dumps(transcript) + "\n", encoding="utf-8")
    calls = sum(1 for m in transcript["messages"] if m["role"] == "assistant")
    print(f"model calls: {calls}")
    print(f"transcript: {out.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m causeway.demo`."""
    parser = argparse.ArgumentParser(prog="causeway.demo", description="Run the Causeway demo agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the demo scenarios").set_defaults(func=_list)

    run_p = sub.add_parser("run", help="run a scenario and write its transcript")
    run_p.add_argument("scenario", help="scenario folder, or a name under demo/scenarios")
    run_p.add_argument("-o", "--output", default="transcript.json", help="transcript path (default transcript.json)")
    run_p.add_argument("--max-steps", type=int, default=12, help="maximum model calls (default 12)")
    run_p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="chat API base URL, e.g. http://127.0.0.1:1234/v1 for LM Studio")
    run_p.add_argument("--model", default=DEFAULT_MODEL, help="model name sent with each request")
    run_p.set_defaults(func=_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
