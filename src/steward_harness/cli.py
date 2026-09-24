"""CLI entry point for the steward kernel."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import uuid
from pathlib import Path

import yaml

from steward_harness.config.loader import load_config
from steward_harness.daemon import StewardDaemon, exit_on_thread_fault
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.providers import build_runtimes
from steward_harness.state import ConversationId, TaskSpec
from steward_harness.task_store import GitTaskStore


def _cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    execution_broker = UntrustedExecutionBroker.for_steward(config, args.config)
    boundary = execution_broker.status()
    boundary_state = "ok" if boundary.enforced else f"unavailable ({boundary.reason})"
    print(f"untrusted execution: {boundary_state}")
    if config.requires_execution_boundary and not boundary.enforced:
        print(
            "error: this instance requires an enforceable untrusted execution identity",
            file=sys.stderr,
        )
        return 1

    runtimes = build_runtimes(config.provider, execution_broker)
    selected: str | None = None
    for family in config.provider.family_order:
        runtime = runtimes.get(family)
        availability = runtime.available() if runtime is not None else None
        available = availability is not None and availability.available
        if available and selected is None:
            selected = family
        reason = (
            availability.reason
            if availability is not None
            else "no adapter registered"
        )
        state = "ok" if available else f"unavailable ({reason})"
        print(f"provider {family} local prerequisites: {state}")

    if selected is None:
        print(
            "error: no provider in the configured order is locally available",
            file=sys.stderr,
        )
        return 1
    print(
        f"provider order: {' > '.join(config.provider.family_order)} "
        f"(first locally available: {selected})"
    )

    print(
        f"steward {config.identity.slug!r}: "
        f"{len(config.repositories)} repositories, {len(config.pipelines)} pipelines"
    )
    for name, pipeline in config.pipelines.items():
        probe = pipeline.probe.type
        print(f"  pipeline {name}: probe={probe} repair={pipeline.repair_mode.value}")
    if config.world is not None:
        # Rhythms are files on the world's default branch, not configuration;
        # `check` validates the config it was handed, and does not read a world.
        print(f"  rhythms: {len(config.rhythms)} configured procedures")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    broker = UntrustedExecutionBroker.for_steward(config, args.config)
    boundary = broker.status()
    if config.requires_execution_boundary and not boundary.enforced:
        print(
            f"error: untrusted execution boundary unavailable: {boundary.reason}",
            file=sys.stderr,
        )
        return 1
    # This process is the unit of failure: a thread that dies takes it with
    # it, and the service manager restarts it.
    threading.excepthook = exit_on_thread_fault
    daemon = StewardDaemon(config, args.config, broker=broker)
    # A service manager stops this unit with SIGTERM, and Python's default
    # action for SIGTERM ends the process where it stands. Every drain under
    # `stop()` -- the executor join that lets an in-flight turn reach durable
    # acceptance -- therefore only ever ran under Ctrl-C, and a deployment
    # restart killed a live operator turn mid-flight. That turn cannot be
    # replayed afterwards, because replaying it might repeat external work,
    # so the only place it can be saved is here, before the process dies.
    # `TimeoutStopSec` on the unit is what bounds the wait.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: daemon.request_stop())
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        daemon.stop()
        print("steward stopped", file=sys.stderr)
    return 0


def _cmd_task_add(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        repository = config.repositories.get(args.repository)
        if repository is None:
            raise ValueError(f"unknown repository: {args.repository}")
        owner = ConversationId(args.owner)
        if owner.owner_kind != "conversation" or not owner.reference.strip():
            raise ValueError("task reply owner must be a telegram or desk conversation")
        spec = TaskSpec(
            args.repository, args.title.strip(),
            Path(args.brief_file).read_text(encoding="utf-8").strip(), args.priority,
        )
        state_path = Path(config.provider.state_db).resolve()
        store = GitTaskStore(state_path.with_name(state_path.name + ".tasks.git"))
        task_id, _ = store.create(
            spec, owner=str(owner), source=f"operator-cli:{uuid.uuid4().hex}",
        )
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(task_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="steward", description="Autonomous project steward kernel"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_p = subparsers.add_parser("check", help="Validate a steward configuration")
    check_p.add_argument("config")

    run_p = subparsers.add_parser("run", help="Run the steward kernel")
    run_p.add_argument("--config", required=True)

    task_p = subparsers.add_parser("task", help="Manage accepted tasks")
    task_commands = task_p.add_subparsers(dest="task_command", required=True)
    add_p = task_commands.add_parser("add", help="File an operator-requested task")
    add_p.add_argument("--config", required=True)
    add_p.add_argument("--repository", required=True)
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--owner", required=True, help="Reply conversation, e.g. telegram:123")
    add_p.add_argument("--priority", type=int, default=0)
    add_p.add_argument("--brief-file", required=True, help="UTF-8 file containing the request")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "task":
        return _cmd_task_add(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
