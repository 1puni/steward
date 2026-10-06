"""Executable construction and lifetime for the state-native steward kernel."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import cast
from itertools import chain
from pathlib import Path

from steward_harness.retention import prune_tasks, prune_world_sessions, retire_native_homes
from steward_harness.repository_reconciler import RepositoryReconciler
from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.config.schema import (
    StewardConfig,
    TelegramAdapterCommandConfig,
)
from steward_harness.conversations import ConversationService
from steward_harness.telegram.api import TelegramAPIError
from steward_harness.inbox import EventLog, InboundMessage, Inbox, InboxDrain, Source
from steward_harness.git import redact_command_output
from steward_harness.git_transport import (
    ControllerGitTransport,
    GitTransportError,
    controller_transport,
    world_transport,
)
from steward_harness.incidents.kernel import IncidentProbeLoop
from steward_harness.kernel import Dispatch, Owner, StewardKernel, repository_lease
from steward_harness.git_reconcile import ResolveTurn, ResolverTurn
from steward_harness.prompts import build_conflict_prompt
from steward_harness.provider_types import ProviderFamily, ProviderProfile
from steward_harness.runtime.contracts import CognitionAdapter
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.providers import build_runtimes
from steward_harness.procedures import Procedures, interval
from steward_harness.targets import Targets
from steward_harness.state import (
    ConversationBusy,
    ConversationId,
    StateDatabase,
    TaskId,
)
from steward_harness.task_runner import TaskRunner
from steward_harness.task_lock import locked_tasks
from steward_harness.telegram.service import (
    TelegramContentRejected,
    TelegramReceiptsUnadopted,
    TelegramService,
)
from steward_harness.telegram.tasks import app_link, task_card
from steward_harness.web.health import HealthServer
from steward_harness.web.tasks import TaskBoard, TaskWeb
from steward_harness.world.git_world import GitWorld
from steward_harness.lease import Busy, Lease
from steward_harness.world.turn_checkpoint import WorldTurnCheckpoint, WorldContentConflict, WorldUpdatePending

log = logging.getLogger(__name__)

#: How often the world exchanges with its remote, when it has one.
WORLD_EXCHANGE_SECONDS = 30


def exit_on_thread_fault(args: threading.ExceptHookArgs) -> None:
    """A thread that dies unexpectedly takes the process with it.

    There is no chain carrying a fault from one lane to the others any more,
    because there is one pass and it raises. This covers the threads that are
    genuinely concurrent with it — Telegram's long poll and the inbox drain —
    and it covers threads nobody registered, which the chain could not. The
    service manager restarts what exits non-zero.

    Installed by `steward run`, not by `run_forever`, because deciding to end
    the process belongs to whatever owns it. A library that calls `os._exit`
    takes the choice away from every other caller, starting with the suite.
    """
    if args.exc_type is SystemExit:
        return
    log.critical(
        "thread %s died; exiting",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    os._exit(1)


def deferral_cause(error: BaseException) -> str:
    """Render a deferral reason that can actually name a subprocess failure.

    `str(CalledProcessError)` is only "Command '[...]' returned non-zero exit
    status N". The stderr saying *why* hangs off the exception and was being
    dropped, so 227 world-checkpoint deferrals logged a sentence that
    structurally could not contain their own cause.

    Redacted rather than raw: Git talks to authenticated HTTPS remotes, so its
    stderr can carry an installation token inside a URL, and this text goes to
    the journal.
    """
    detail = str(error)
    captured = getattr(error, "stderr", None) or getattr(error, "output", None)
    if not captured:
        return detail
    if isinstance(captured, bytes | bytearray):
        captured = bytes(captured).decode("utf-8", "replace")
    captured = redact_command_output(str(captured), tail=True).strip()
    return f"{detail}: {captured}" if captured else detail


def _controller_executable_refusal(executable: Path) -> str | None:
    """Why the controller must not run this operator command, if it must not.

    The startup boundary audit already proves the agent cannot write it. This
    re-checks at the moment of use, because running it is the whole point.
    """
    for path in (executable, executable.parent):
        try:
            stat = path.lstat()
        except OSError:
            return f"{path} is missing"
        if path.is_symlink():
            return f"{path} is a symlink"
        if stat.st_uid != os.geteuid():
            return f"{path} is not owned by the controller"
        if stat.st_mode & 0o022:
            return f"{path} is writable by others"
    return None


class KernelCommands:
    """Small operator surface over current state and the two repository paths."""

    def __init__(
        self,
        config: StewardConfig,
        state: StateDatabase,
        conversations: ConversationService,
        reconciler: RepositoryReconciler,
        transports: Mapping[str, ControllerGitTransport],
        broker: UntrustedExecutionBroker,
        *,
        procedures: Procedures | None = None,
        targets: Targets | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.conversations = conversations
        self.reconciler = reconciler
        self.transports = dict(transports)
        self.broker = broker
        self.procedures = procedures or Procedures(config, state, transports)
        self.targets = targets or Targets(config, state, transports, self.procedures)

    def __call__(
        self,
        name: str,
        arg: str | None,
        chat_id: int,
        topic_id: int,
        user_id: int,
    ) -> str:
        if name == "status":
            owners = tuple(sorted(locked_tasks(
                self.state.tasks.locks_root)))
            blocked = [r for r in self.state.pending_result_receipts() if r.get("delivery_error")]
            delivery = "" if not blocked else "\nUndeliverable results: " + "; ".join(
                f"{r.get('owner') or r.get('target', 'unowned')}: {r['delivery_error']}"
                for r in blocked[:10]
            )
            # Rhythms are silent unless they ask, so their silence is counted.
            refused = self.state.refused_results(time.time() - 86_400)
            if refused:
                delivery += "\nRefused by Telegram (24h): " + "; ".join(
                    f"{r['owner']}: {r['rejected']}" for r in refused[:10])
            silent = self.state.recorded_not_sent(time.time() - 86_400)
            recorded = "" if not silent else (
                f"\nRecorded, not sent (24h): {len(silent)}: "
                + ", ".join(sorted({key.rsplit(":", 1)[0] for key in silent})))
            return (
                f"Steward {self.config.identity.slug}"
                f"{' [PAUSED]' if self.state.paused() else ''}: "
                f"{len(self.config.repositories)} repositories; "
                f"{len(owners)} active owners: {', '.join(map(str, owners[:10])) or 'idle'}"
                f"{' …' if len(owners) > 10 else ''}.{delivery}{recorded}"
            )
        if name == "tasks":
            tasks = self.state.tasks.all()[:20]
            url = self.config.telegram.task_app_url if self.config.telegram else None
            launch = app_link(url)
            heading = f"[Open task board]({launch})" if launch else "**Tasks**"
            chat = self.config.telegram.chat_id if self.config.telegram else None
            cards = "\n\n".join(task_card(task, url, chat) for task in tasks)
            return heading + "\n\n" + (cards or "No tasks yet.")
        if name == "pause":
            self.state.set_paused(True)
            return "⏸️ Scheduler paused; accepted publication and target convergence remain active."
        if name == "resume":
            self.state.set_paused(False)
            return "▶️ Scheduler resumed."
        if name in {"model", "model_family", "clear", "cancel"}:
            return self._conversation(name, arg, topic_id)
        if name == "task":
            return self._task(arg)
        if name == "rhythm":
            return self._rhythm(arg)
        if name == "git":
            return self._git(arg)
        adapter = (
            self.config.telegram.adapter_commands.get(name)
            if self.config.telegram is not None
            else None
        )
        if adapter is not None:
            return self._adapter(adapter, name, arg, chat_id, topic_id, user_id)
        raise ValueError(f"command {name!r} is not admitted")

    def _conversation(self, name: str, arg: str | None, topic_id: int) -> str:
        """Commands scoped to this topic's ordinary conversation."""
        existing = self.state.find_conversation("telegram", str(topic_id))
        if name == "cancel":
            # Cancellation acts on a live turn. Creating a conversation in order
            # to find nothing to cancel would be a mutation wearing a read's face.
            if existing is None:
                return "No active conversation is bound to this topic."
            turn = self.state.active_turn(existing.conversation_id)
            if turn is None or not self.conversations.cancel(turn.turn_id):
                return "No active conversation is bound to this topic."
            return f"🛑 Cancellation requested for {turn.turn_id}."

        conversation = existing or self.conversations.conversation_for(
            "telegram", str(topic_id)
        )
        if name == "clear":
            self.state.clear_conversation(conversation.conversation_id)
            return "🧹 This topic's ordinary provider conversation was cleared."
        if name == "model":
            if arg is None:
                return f"This topic runs {conversation.provider}/{conversation.profile}."
            if arg not in {"fast", "balanced", "deep"}:
                return "Usage: /model [fast|balanced|deep]."
            selected = self.conversations.set_profile(conversation.conversation_id, arg)
            return f"Quality for this topic set to {selected.profile}."
        if arg is None:
            return (
                f"This topic uses {conversation.provider}. Configured providers: "
                + ", ".join(self.config.provider.family_order)
                + "."
            )
        if arg not in self.config.provider.family_order:
            return f"Provider {arg!r} is not configured."
        selected = self.conversations.switch_provider(conversation.conversation_id, arg)
        return (
            f"Provider for this topic set to {selected.provider}; generation "
            f"{selected.generation}. The new provider receives no foreign session."
        )

    def _task(self, arg: str | None) -> str:
        parts = (arg or "").split(maxsplit=2)
        usage = (
            "Usage: /task show|confirm|reject|answer|retry|note|cancel|priority|model|model_family "
            "<task_id> [value]"
        )
        if len(parts) < 2:
            return usage
        verb, raw_id = parts[:2]
        if verb == "cancel" and "::" in (arg or ""):
            return self._cancel(arg or "")
        detail = parts[2].removeprefix("::").strip() if len(parts) == 3 else ""
        try:
            task_id = self.state.tasks.resolve(raw_id)
            if verb in {"model", "model_family"}:
                session = self.state.tasks.get(task_id).session_id
                lineage = self.state.get_conversation(session)
                if verb == "model" and detail:
                    self.state.set_conversation_profile(session, detail)
                elif verb == "model_family" and detail:
                    if detail not in self.config.provider.family_order:
                        raise ValueError(f"provider {detail!r} is not configured")
                    if detail != lineage.provider:
                        self.state.bind_conversation_provider(session, detail, None)
                lineage = self.state.get_conversation(session)
                return (
                    f"Task {task_id} uses {lineage.provider}/{lineage.profile}; "
                    f"generation {lineage.generation}."
                )
            if verb == "show":
                task = self.state.tasks.get(task_id)
                reason = f"\nReason: {task.reason}" if task.reason else ""
                history = self.state.tasks.checkpoints(task, 5)
                progress = (
                    "\nRecent checkpoints:\n"
                    + "\n".join(
                        f"  {item.disposition.value}"
                        + (f" — {item.question}" if item.question else "")
                        for item in history
                    )
                    if history
                    else "\nNo checkpoint recorded yet."
                )
                progress += f"\nPending inputs: {len(task.pending)}"
                return (
                    task_card(
                        task, self.config.telegram.task_app_url if self.config.telegram else None,
                        self.config.telegram.chat_id if self.config.telegram else None,
                    )
                    + (
                        " (cancellation requested)"
                        if task.definition.hold == "cancelled" else ""
                    )
                    + f"\nRepository: {task.repository}\n"
                    f"Priority: {task.priority}\nBrief: {task.brief}{reason}{progress}\n\n"
                    f"`/task note {task.task_id.short} <text>`"
                )
            if verb == "confirm" and not detail:
                self.state.tasks.confirm(task_id)
                return f"✅ Confirmed {task_id}; it is queued."
            if verb == "reject":
                self.state.tasks.reject(task_id, detail or "operator rejected proposal")
                return f"🚫 Rejected {task_id}."
            if verb == "answer" and detail:
                self.state.tasks.answer(task_id, detail)
                return f"✅ Answer recorded; {task_id} is queued."
            if verb == "note" and detail:
                self.state.tasks.note(task_id, detail)
                return (
                    f"Note recorded for {task_id}; pending input for its next execution. "
                    "It does not interrupt active work. /task show reports consumption."
                )
            if verb == "retry":
                self.state.tasks.retry(task_id, detail or None)
                return f"🔁 {task_id} is queued on its retained branch."
            if verb == "cancel":
                cancelled = self.state.tasks.cancel(task_id, detail or None)
                # The marker says it was abandoned; the signal stops the
                # slice that is running right now.
                self.conversations.cancel(cancelled.session_id)
                return f"🛑 Cancellation recorded for {task_id}."
            if verb == "priority" and detail:
                self.state.tasks.set_priority(task_id, int(detail))
                return f"Priority for {task_id} is now {int(detail)}."
        except (LookupError, PermissionError, RuntimeError, ValueError) as error:
            return f"⚠️ {error}"
        return usage

    def _cancel(self, arg: str) -> str:
        """Withdraw one task or several, with one reason for all of them.

        Bulk-cleaning superseded work is the second thing the operator asked a
        task board for, and the board reads only, so it belongs to the command
        that already cancels rather than to a second way of cancelling. `::`
        already separated a reason from what precedes it here; everything
        before it is now ids. Without it the argument still means one id and a
        free-text reason, exactly as before.

        Each id is attempted on its own: one unknown slug or one task the
        remote already took must not withdraw nothing else.
        """
        ids, _, reason = arg.partition("::")
        normalized = reason.strip() or None
        lines = []
        for raw_id in ids.split()[1:]:
            try:
                cancelled = self.state.tasks.cancel(self.state.tasks.resolve(raw_id), normalized)
                # The marker says it was abandoned; the signal stops the
                # slice that is running right now.
                self.conversations.cancel(cancelled.session_id)
                lines.append(f"🛑 Cancellation recorded for {raw_id}.")
            except (LookupError, PermissionError, RuntimeError, ValueError) as error:
                lines.append(f"⚠️ {raw_id}: {error}")
        return "\n".join(lines) or "Usage: /task cancel <task_id> [more ids] :: <reason>"

    def _rhythm(self, arg: str | None) -> str:
        parts = (arg or "list").split()
        if parts == ["list"]:
            lines = []
            runs = [task.procedure for task in self.state.tasks.all()]
            observations = self.procedures.world_rhythm_observations()
            for name, rhythm in self.config.rhythms.items():
                schedule = (f"after {rhythm.after}" if rhythm.after is not None
                            else f"every {rhythm.schedule}s" if isinstance(rhythm.schedule, int)
                            else f"after {rhythm.schedule.quiet}s of quiet Git activity")
                count = sum(bool(run and run.event.startswith(f"rhythm:{name}:")) for run in runs)
                history = f"{count} accepted runs" if count else "no accepted run recorded"
                if rhythm.input == "world":
                    key = f"rhythm:{name}:{interval(self.config.rhythms, name, time.time())}"
                    turn = self.state.turn_for_source(ConversationId(f"rhythm:{name}"), key)
                    history = f"this interval: {turn.state if turn else 'not run yet'}"
                    observed = observations[name]
                    history += (f"; obligation={observed['event']}, {observed['progress']}, observed_at={observed['observed_at']:.0f}, "
                                f"due_at={observed['due_at']:.0f}, "
                                f"overdue_seconds={observed['overdue_seconds']:.0f}")
                pause = "; automatic admission paused" if self.state.paused() else ""
                lines.append(f"{name}: {schedule}, {rhythm.procedure}, {rhythm.input}, "
                             f"owner={rhythm.owner or 'retained only'}; {history}{pause}")
            return "\n".join(lines) or "No rhythms configured."
        if len(parts) == 2 and parts[0] == "run" and parts[1] in self.config.rhythms:
            rhythm = self.config.rhythms[parts[1]]
            if rhythm.input == "world":
                return self.procedures.continue_world_rhythm(parts[1])
            repository, candidate, base, activity = self.procedures.observe(rhythm)
            task = self.procedures.request(rhythm.procedure, repository, candidate, base,
                                          event=f"rhythm:{parts[1]}:manual:{uuid.uuid4().hex}",
                                          owner=rhythm.owner, activity=activity,
                                          workdir=rhythm.workdir)
            return f"Queued {task}"
        return "Usage: /rhythm list | run <name>. Edit schedules in controller configuration."

    def _git(self, arg: str | None) -> str:
        parts = (arg or "").split()
        usage = "Usage: /git reconcile <repo> | target <name> | retarget <task_id> <repo>"
        if len(parts) < 2:
            return usage
        verb, name, *rest = parts
        if verb == "target" and not rest:
            # An operator asking for a target owes them a fresh observation, not
            # whatever the loop last saw.
            return self.targets.advance(name, force=True) if name in self.config.targets else f"Unknown target {name!r}."
        if verb == "retarget" and len(rest) == 1:
            if rest[0] not in self.config.repositories:
                return f"Unknown repository {rest[0]!r}."
            try:
                task = self.state.tasks.retarget(self.state.tasks.resolve(name), rest[0])
            except (LookupError, RuntimeError, ValueError) as error:
                return f"⚠️ {error}"
            return f"🔀 Retargeted {task.task_id} to {task.repository}."
        repository = self.config.repositories.get(name)
        if repository is None:
            return f"Unknown repository {name!r}."
        if verb == "reconcile" and not rest:
            # The operator is a driver like any other, and this runs on the
            # Telegram thread, beside the pass. It takes the same lock.
            try:
                with repository_lease(self.state, name):
                    landed = self.reconciler.publish_repository(name)
            except Busy:
                return f"⏳ {name} is already being published; it will finish on its own."
            if landed is None:
                return f"{name}: {self.reconciler.last_outcome.get(name, 'publication deferred')}"
            return f"🧾 Landed {landed} on {repository.default_branch}."
        return usage

    def _adapter(
        self,
        adapter: TelegramAdapterCommandConfig,
        name: str,
        arg: str | None,
        chat_id: int,
        topic_id: int,
        user_id: int,
    ) -> str:
        telegram = self.config.telegram
        assert telegram is not None
        topic_name = next(
            (key for key, value in telegram.topics.items() if value == topic_id),
            "steward" if topic_id <= 1 else f"topic_{topic_id}",
        )
        if adapter.allowed_topics and topic_name not in adapter.allowed_topics:
            return f"/{name} is not allowed in this topic."
        argv = [*adapter.command.argv, *((arg,) if arg is not None else ())]
        context = {
            "STEWARD_TELEGRAM_COMMAND": name,
            "STEWARD_TELEGRAM_CHAT_ID": str(chat_id),
            "STEWARD_TELEGRAM_TOPIC_ID": str(topic_id),
            "STEWARD_TELEGRAM_USER_ID": str(user_id),
        }
        try:
            if adapter.authority == "controller":
                refusal = _controller_executable_refusal(Path(argv[0]))
                if refusal:
                    return f"⚠️ /{name} refused: {refusal}"
                # Only the operator's authenticated command reaches here, so
                # this is the one path that acts with the controller's own
                # identity. It inherits nothing the controller holds.
                result = subprocess.run(
                    argv,
                    cwd=adapter.command.cwd,
                    timeout=adapter.command.timeout_seconds,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", **context},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                result = self.broker.run(
                    argv,
                    cwd=adapter.command.cwd,
                    timeout=adapter.command.timeout_seconds,
                    extra_env=context,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"⚠️ /{name} did not start cleanly: {error}"
        output = result.stdout.strip()
        if result.returncode:
            return f"⚠️ /{name}: " + (result.stderr.strip() or output or "adapter failed")[-1000:]
        return output[-4000:] if output else f"✅ /{name} accepted."


class StewardDaemon:
    """Own one kernel lease and all runtime services beneath it."""

    def __init__(
        self,
        config: StewardConfig,
        config_path: str | Path,
        *,
        broker: UntrustedExecutionBroker | None = None,
        adapters: Mapping[ProviderFamily, CognitionAdapter] | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path).resolve()
        self.broker = broker or UntrustedExecutionBroker.for_steward(
            config, self.config_path
        )
        self.adapters = adapters
        self._alerted: dict[str, float] = {}
        self._stop = threading.Event()
        self._inbox: InboxDrain | None = None
        self._telegram: TelegramService | None = None
        self._result_dispatch = Dispatch(1)
        self._kernel: StewardKernel | None = None
        self._health: HealthServer | None = None
        self._rhythm_health: dict = {}

    def request_stop(self) -> None:
        """Ask the owned loop to finish its pass and drain, without waiting.

        Safe from a signal handler, which is the caller that matters: the
        drain itself happens in `_run_owned`'s `finally`, on the thread that
        owns the lease, rather than underneath whatever the handler
        interrupted.
        """
        self._stop.set()

    def run_forever(self, poll_seconds: float | None = None) -> None:
        """Validate authority, initialize state under the lease, then serve."""
        self._require_boundary()
        with self._daemon_lease():
            self._run_owned(poll_seconds or self.config.controller.poll_seconds)

    def _daemon_lease(self) -> Lease:
        """Create the process lock only after startup authority was validated."""
        state_path = Path(self.config.provider.state_db).resolve()
        return Lease(
            state_path.parent,
            timeout_seconds=0,
            lock_name=f".{state_path.name}.kernel.lock",
        )

    def _run_owned(self, poll: float) -> None:
        """§6's `main()`: one loop, one pass, and no supervisor beneath it."""
        try:
            step = self._start_owned()
            while not self._stop.is_set():
                step()
                self._stop.wait(poll)
        finally:
            self.stop()

    def _start_owned(self) -> Callable[[], None]:
        state = StateDatabase(Path(self.config.provider.state_db).resolve())
        state.tasks.remote = self.config.tasks.remote_url
        state.tasks.repositories = set(self.config.repositories)
        state.tasks.default_provider = self.config.provider.default_family
        state.tasks.default_profile = self.config.provider.default_profile
        state.tasks.sync()
        transports = {
            name: controller_transport(self.config, name)
            for name in self.config.repositories
        }
        cognition = Cognition(
            self.adapters if self.adapters is not None else build_runtimes(self.config.provider, self.broker),
            self.config.provider.models,
            writable_roots=self.broker.writable_roots,
            on_storage_deferred=lambda reason: self._alert(
                "storage", "⚠️ Disk below the native storage reserve: new turns are waiting, "
                f"none are lost. {reason}"),
        )
        def resolve(turn: ResolverTurn) -> None:
            name = self.config.world.reconcile if self.config.world else None
            procedure = self.config.procedures.get(name) if name else None
            prompt = build_conflict_prompt(turn.conflicted_files, turn.base_ref,
                turn.branch, turn.stop_index, max_stops=20)
            if procedure:
                prompt += "\n\n" + Path(procedure.instructions).read_text()
            cognition.run(CognitionRequest(
                execution_id=f"world-reconcile:{turn.branch}:{uuid.uuid4().hex}",
                profile=self.config.provider.default_profile,
                prompt=prompt, cwd=turn.worktree,
                **(procedure.limits(self.config.provider.timeout_seconds) if procedure
                   else dict(timeout_seconds=self.config.provider.timeout_seconds)),
                **(procedure.routing(self.config.provider.family_order) if procedure
                   else dict(provider_order=self.config.provider.family_order)),
                sandbox_mode="workspace-write"))

        checkpoint = self._world_checkpoint(state, resolve)
        # Every origin admits through the accepted task Git store.
        worktrees_root = Path(self.config.provider.workdir) / "worktrees"
        state.tasks.transports = transports

        def telegram_admin(scope, request):
            if self._telegram is None:
                raise ValueError("Telegram is not available")
            return self._telegram.admin(scope, request)

        conversations = ConversationService(
            state,
            cognition,
            provider_order=self.config.provider.family_order,
            profile=self.config.provider.default_profile,
            workspace=checkpoint or Path(self.config.provider.workdir).resolve(),
            telegram_admin=telegram_admin,
            timeout_seconds=self.config.provider.timeout_seconds,

            desk_provider=self.config.desk.provider if self.config.desk else None,
            desk_profile=self.config.desk.profile if self.config.desk else None,
            telegram_actions=(
                self.config.telegram.agent_actions
                if self.config.telegram is not None
                else ()
            ),
            delivery_roots=(
                self.config.telegram.delivery_roots
                if self.config.telegram is not None
                else ()
            ),
        )

        procedures = Procedures(self.config, state, transports,
                                world=checkpoint.world if checkpoint else None,
                                alert=self._alert)
        targets = Targets(self.config, state, transports, procedures)
        self._procedures, self._targets = procedures, targets
        reconciler = RepositoryReconciler(
            state=state,
            repositories=self.config.repositories,
            transports=transports,
            worktrees_root=worktrees_root,
            broker=self.broker,
            procedures=procedures,
        )
        tasks = TaskRunner(
            state=state,
            repositories=self.config.repositories,
            transports=transports,
            worktrees_root=worktrees_root,
            broker=self.broker,
            cognition=cognition,
            # A mid-task switch makes the selected provider primary but keeps
            # every other configured adapter eligible in declared order.
            provider_fallbacks=self.config.provider.family_order,
            timeout_seconds=self.config.provider.timeout_seconds,
            poll_seconds=self.config.controller.poll_seconds,
            actor_name=self.config.identity.name,
            telegram_admin=telegram_admin,
        )
        incidents = IncidentProbeLoop(
            state,
            self.config,
            self.broker,

            notify=self._notify,
        )
        self._recover_turns(state, conversations, checkpoint)

        commands = KernelCommands(
            self.config,
            state,
            conversations,
            reconciler,
            transports,
            self.broker,
            procedures=procedures, targets=targets,
        )
        desk = self._desk(conversations)

        kernel = StewardKernel(
            state,
            reconciler,
            tasks,
            dispatch=Dispatch(self.config.controller.workers),
        )
        self._kernel = kernel

        # The boundary was validated in `run_forever`, before the lease and
        # before any of this was constructed. Nothing between there and here
        # touches OS identity, and `broker.status()` reads only that, so asking
        # twice asks the same question of the same object.
        state.interrupt_abandoned_turns()
        tasks.reconcile_worktrees()
        desk_events = EventLog(self.config.desk.events_file) if self.config.desk else None
        if desk is not None and desk_events is not None:
            recovered = desk.inbox.recover()
            if recovered:
                desk_events.append(
                    "status", f"desk online; {recovered} claim(s) requeued"
                )
        if self.config.controller.health_bind:
            self._health = HealthServer(
                self.config.controller.health_bind, self._board(state),
                rhythms=lambda: self._rhythm_health,
                observation_max_age=max(60, self.config.controller.poll_seconds * 3),
            )
            self._health.start()
        self._start_telegram(state, conversations, commands)
        sources = [source for source in (
            self._telegram.source if self._telegram is not None else None, desk,
        ) if source is not None]
        if sources:
            self._inbox = InboxDrain(sources)
            self._inbox.start()
        return self._pass(state, conversations, kernel, incidents, desk_events, checkpoint)

    def _board(self, state: StateDatabase) -> TaskWeb | None:
        """The read-only task board, when there is a bot to sign its readers in.

        No configuration of its own. The listener is the health bind, and the
        token and allowlist are the Telegram ones — a board nobody can be
        authenticated against would be a surface with no operator.
        """
        telegram = self.config.telegram
        if telegram is None:
            return None
        return TaskWeb(
            TaskBoard(state, telegram.chat_id),
            token_path=telegram.token_path,
            allowed_users=telegram.allowed_users,
        )

    def _start_telegram(
        self,
        state: StateDatabase,
        conversations: ConversationService,
        commands: KernelCommands,
    ) -> None:
        """Attach the Telegram ingress, if one is configured, to the shared kernel."""
        telegram = self.config.telegram
        if telegram is None:
            return

        def telegram_turn(
            event: str,
            _chat: int,
            topic: int,
            user: int,
            text: str,
            images: tuple[Path, ...],
            *,
            ongoing_only: bool = False,
        ) -> str:
            return conversations.run_turn(
                transport="telegram",
                transport_key=str(topic),
                source_event_key=event,
                operator_id=str(user),
                text=text,
                images=images,
                ongoing_only=ongoing_only,
            ).transport_reply

        service = TelegramService(
            telegram,
            telegram_turn,
            state_db=state,
            execution_broker=self.broker,
            command_handler=commands,
            ongoing_topics=conversations.native_telegram_topics,
            native_turn_handler=lambda *args: telegram_turn(*args, ongoing_only=True),
        )
        self._telegram = service
        try:
            service.start()
        except TelegramReceiptsUnadopted as error:
            # Everything else keeps running; results for Telegram stay pending.
            log.critical("Telegram ingress NOT started: %s", error, exc_info=True)
            self._telegram = None
            service.api.close()

    def _pass(
        self,
        state: StateDatabase,
        conversations: ConversationService,
        kernel: StewardKernel,
        incidents: IncidentProbeLoop,
        desk_events: EventLog | None,
        checkpoint: WorldTurnCheckpoint | None = None,
    ) -> Callable[[], None]:
        """Build the one pass over every owner this daemon is responsible for.

        The pass enumerates and hands each owner to the shared `Dispatch`; it
        never waits for one. `controller.workers` bounds background cognition;
        a separate single transport worker delivers retained results. The shared
        executor's queue is who goes next for cognition, and a
        live conversation is not here at all — the inbox drain answers it, so
        a full budget never keeps the operator waiting.

        Each lane below is a derivation of who is owed work right now, and an
        absent subsystem derives nothing rather than being asked about. The
        pass never tests for one: it concatenates the lanes and submits what
        comes out. Pause is likewise a filter inside the lanes it applies to —
        `/pause` stops the steward taking on work, and says so, so delivering
        an answer the operator is already waiting for is not filtered.

        Publication and target convergence have independent owners. Work is derived when asked,
        so a pass that skips something picks it up on the next one.
        """
        dispatch = kernel.dispatch
        paused = state.paused

        def deliver_result(owner: ConversationId) -> None:
            """Report one finished task back to the transport that admitted it."""
            sending: list[str] = []

            def send(text: str, source_key: str) -> None:
                sending.append(source_key)
                if owner.kind == "desk" and desk_events is not None:
                    desk_events.append("reply", text, source_key)
                elif owner.kind == "telegram" and self._telegram is not None:
                    self._telegram.send_result(
                        self._telegram.config.chat_id, int(owner.reference), text, source_key,
                    )
                else:
                    raise OSError(f"result transport unavailable for {owner}")
            try:
                conversations.deliver_task_result(owner, send=send)
            except TelegramContentRejected as error:
                # Telegram answers this reply the same way every time (a deleted
                # topic, say). Settle the receipt that was refused: left pending,
                # it would keep every later result for this owner behind it.
                receipt = state.result_receipt(sending[-1]) if sending else {}
                if receipt:
                    receipt.pop("delivery_error", None)
                    receipt.update(done=True, rejected=str(error), rejected_at=time.time())
                    state.save_result_receipt(receipt)
                log.error("task result for %s refused by Telegram: %s", owner, error)
            except (Busy, ConversationBusy, GitTransportError, TelegramAPIError,
                    OSError, subprocess.TimeoutExpired, WorldContentConflict,
                    WorldUpdatePending, subprocess.CalledProcessError) as error:
                log.info("task result deferred: %s", deferral_cause(error))

        def assess_result(owner: ConversationId) -> None:
            try:
                conversations.assess_task_result(owner)
            except (Busy, ConversationBusy, GitTransportError, OSError,
                    subprocess.TimeoutExpired, WorldContentConflict,
                    WorldUpdatePending, subprocess.CalledProcessError) as error:
                log.info("task assessment deferred: %s", deferral_cause(error))

        def assessments() -> Iterator[Owner]:
            for owner in dict.fromkeys(ConversationId(r["owner"])
                                      for r in state.pending_result_assessments()):
                yield ("assessment", owner), lambda owner=owner: assess_result(owner)

        def probes() -> Iterator[Owner]:
            if paused():
                return
            for name in self.config.pipelines:
                yield ("probe", name), lambda name=name: incidents.observe(name)

        def rhythm() -> Iterator[Owner]:
            now = time.time()
            observations = (self._procedures.world_rhythm_observations(now=now)
                            if self.config.rhythms else {})
            self._rhythm_health = dict(
                observed_at=now, paused=paused(), rhythms=observations,
                workers=self.config.controller.workers,
                dispatch=dispatch.observation() if hasattr(dispatch, "observation") else None)
            if paused() or not self.config.rhythms:
                return
            yield ("rhythms",), self._procedures.advance_rhythms
            # One world writer; choose fresh due work when its queued slot runs.
            if any(value['eligible'] for value in observations.values()):
                yield ("rhythm", "world"), lambda: self._procedures.advance_world_rhythm(conversations)

        def targets() -> Iterator[Owner]:
            for name in self.config.targets:
                if self._targets.due(name):
                    yield ("target", name), lambda name=name: log.info(self._targets.advance(name))

        def route_error(owner: ConversationId) -> str | None:
            if owner.kind == "telegram":
                telegram = self.config.telegram
                if telegram is None:
                    return "Telegram transport unavailable"
                # Named topics select controller destinations; they are not an
                # ingress allowlist. Retain the route of an admitted conversation.
                if owner.reference not in {str(topic) for topic in telegram.topics.values()}:
                    try:
                        state.get_conversation(owner)
                    except LookupError:
                        return "route is not a configured Telegram topic"
                if self._telegram is None:
                    return "Telegram transport unavailable"
            elif owner.kind != "desk" or desk_events is None:
                return f"result transport unavailable for {owner}"
            return None

        def record_error(receipt: dict, error: str) -> None:
            if receipt.get("delivery_error") != error:
                receipt["delivery_error"] = error
                state.save_result_receipt(receipt)
                log.warning("result %s deferred: %s", receipt["source_key"], error)

        reminded = [None]

        def result_owners() -> Iterator[Owner]:
            # A stuck task is named again at most daily; reading every task
            # to decide that is worth doing once an hour, not every pass.
            hour = int(time.time() // 3600)
            if reminded[0] != hour:
                reminded[0] = hour
                try:
                    state.remind_open_tasks()
                except (GitTransportError, OSError, ValueError) as error:
                    log.warning("open task reminder deferred: %s", error)
            # External target transitions have no task owner. Assign a real
            # configured operator route; never invent a topic or a task.
            for receipt in state.pending_result_receipts():
                if receipt.get("owner"):
                    continue
                telegram = self.config.telegram
                topic = (telegram.topics.get("incidents", telegram.topics.get("operator"))
                         if telegram else None)
                route = (("telegram", str(topic)) if topic is not None else
                         ("desk", "operator") if desk_events is not None else None)
                if route is None:
                    record_error(receipt, "no configured operator result route")
                    continue
                owner = state.get_or_create_conversation(
                    *route, provider=state.tasks.default_provider,
                    profile=state.tasks.default_profile,
                ).conversation_id
                receipt["owner"] = str(owner)
                receipt.pop("delivery_error", None)
                state.save_result_receipt(receipt)
            for owner in state.pending_task_result_conversations():
                error = route_error(owner)
                if error:
                    receipt = state.retain_pending_result(owner)
                    if receipt is not None:
                        record_error(receipt, error)
                    continue
                yield ("result", owner), lambda owner=owner: deliver_result(owner)

        def results() -> Iterator[Owner]:
            try:
                yield from result_owners()
            except (OSError, GitTransportError, Busy, subprocess.CalledProcessError) as error:
                log.warning("result discovery deferred: %s", error)

        def converge_world() -> None:
            try:
                published = checkpoint.converge()
            except (Busy, GitTransportError, OSError, RuntimeError,
                    subprocess.TimeoutExpired, WorldContentConflict, WorldUpdatePending) as error:
                log.warning("world remote deferred: %s", deferral_cause(error))
            else:
                if published:
                    log.info("world published %s", published)

        next_world_exchange = [0.0]

        def world_remote() -> Iterator[Owner]:
            # Publication, like repository publication, continues while paused.
            if checkpoint is None or checkpoint.transport is None:
                return
            if time.monotonic() >= next_world_exchange[0]:
                next_world_exchange[0] = time.monotonic() + WORLD_EXCHANGE_SECONDS
                yield ("world", "remote"), converge_world

        lanes = (kernel.owners, probes, rhythm, targets, assessments, world_remote)

        def sync_tasks():
            try:
                state.tasks.sync()
            except (RuntimeError, ValueError, OSError) as error:
                log.warning("task intake deferred: %s", deferral_cause(error))

        next_retention = 0.0

        def retain_workspaces() -> None:
            prune_tasks(kernel.tasks)
            if checkpoint is not None:
                prune_world_sessions(checkpoint, self.config.controller.world_session_idle_seconds)
            retire_native_homes(
                state, self.broker, self.config.provider.native_homes,
                self.config.world.root if self.config.world else None,
                {name: repository.path for name, repository in self.config.repositories.items()},
            )

        def step() -> None:
            nonlocal next_retention
            if time.monotonic() >= next_retention:
                dispatch.submit(("workspace-retention",), retain_workspaces)
                next_retention = time.monotonic() + 3600
            kernel.dispatch.submit(("task-intake", "git"), sync_tasks)
            dispatch.reap()
            self._result_dispatch.reap()
            for key, work in results():
                self._result_dispatch.submit(key, work)
            kernel.tasks.flush_inputs()
            for key, work in chain.from_iterable(lane() for lane in lanes):
                dispatch.submit(key, work)

        return step

    def _world_checkpoint(
        self, state: StateDatabase, resolve: ResolveTurn,
    ) -> WorldTurnCheckpoint | None:
        """One object owns the configured world, lease, and acceptance boundary."""
        config = self.config.world
        if config is None:
            return None
        return WorldTurnCheckpoint(
            GitWorld(config.root, execution_broker=self.broker),
            Lease(
                (lock := config.lock_path(state.path)).parent,
                lock_name=lock.name,
                require_existing=config.lock_dir is not None,
            ),
            Path(self.config.provider.workdir) / "world-turns",
            execution_broker=self.broker,
            state=state,
            resolve_turn=resolve,
            transport=world_transport(self.config),
        )

    @staticmethod
    def _recover_turns(
        state: StateDatabase,
        conversations: ConversationService,
        checkpoint: WorldTurnCheckpoint | None,
    ) -> None:
        """Apply retained world effects before any new writer is started.

        Recovery goes through the same boundary as ordinary acceptance.
        """
        for prepared in state.pending_turns():
            try:
                conversations.accept_prepared(prepared["turn_id"])
            except (WorldContentConflict, WorldUpdatePending) as error:
                log.warning(
                    "world turn %s retained for recovery: %s",
                    prepared["turn_id"],
                    error,
                )
        conversations.recover_completions()
        if checkpoint is not None:
            checkpoint.startup_cleanup()

    def _desk(self, conversations: ConversationService) -> Source | None:
        """The filesystem desk: clients queue messages in its inbox and read replies from its log."""
        desk = self.config.desk
        if desk is None:
            return None
        events = EventLog(desk.events_file)

        def answer(message: InboundMessage) -> None:
            if events.has_reply(message.msg_id):
                return
            if message.profile is not None and desk.profile is None:
                conversation = conversations.conversation_for("desk", str(message.topic_id))
                if conversation.profile != message.profile:
                    conversations.set_profile(
                        conversation.conversation_id, cast(ProviderProfile, message.profile)
                    )
            reply = conversations.run_turn(
                transport="desk",
                transport_key=str(message.topic_id),
                source_event_key=message.msg_id,
                operator_id="desk",
                text=(f"{message.context}\n\n{message.text}" if message.context
                      else message.text),
                episode_input=message.text,
            ).transport_reply
            if reply:
                events.append("reply", reply, message.msg_id)

        return Source(Inbox(desk.inbox_dir), answer)

    def _require_boundary(self) -> None:
        if not self.config.requires_execution_boundary:
            return
        boundary = self.broker.status()
        if not boundary.enforced:
            raise RuntimeError(
                f"untrusted execution boundary unavailable: {boundary.reason}"
            )

    def _notify(self, text: str) -> None:
        """Deliver incident state changes without making transport part of policy."""
        self._notify_topic("incidents", text)

    def _alert(self, key: str, text: str, *, quiet_seconds: float = 3600) -> None:
        """Tell the operator, without a model, that the harness itself stalled.

        A watcher that hands its findings to cognition fails exactly when
        cognition does; on 2026-10-04 every such handoff was refused by the
        storage reserve it was reporting. This path is the transport alone.
        Each key speaks at most once per quiet period.
        """
        now = time.monotonic()
        if now - self._alerted.get(key, -quiet_seconds) < quiet_seconds:
            return
        self._alerted[key] = now
        log.warning("operator alert %s: %s", key, text)
        self._notify_topic("incidents", text[:3500])

    def _notify_topic(self, topic: str, text: str) -> None:
        service = self._telegram
        if service is None:
            log.info("%s: %s", topic, text)
            return
        topics = service.config.topics
        topic_id = topics.get(topic, topics.get("operator", 0))
        try:
            service.send_reply(service.config.chat_id, topic_id, text)
        except Exception:
            log.exception("%s notification failed", topic)

    def stop(self) -> None:
        """Release the lease last, after every other writer has finished.

        With one pass there is nothing to co-ordinate: the pass *is* the
        writer, and it has already returned by the time this runs. Only the
        threads genuinely concurrent with it have to be waited for.
        """
        self._stop.set()
        if self._telegram is not None:
            self._telegram.request_stop()
        if self._inbox is not None:
            self._inbox.request_stop()
        if self._kernel is not None:
            # Task turns have no routine deadline; draining writers would wait
            # on them indefinitely unless they are asked to end first.
            self._kernel.tasks.interrupt_running()
            self._kernel.stop()
        self._result_dispatch.stop()
        # Inbound answers reply through Telegram, so they finish before it closes.
        if self._inbox is not None:
            self._inbox.stop()
        if self._telegram is not None:
            self._telegram.stop()
        if self._health is not None:
            self._health.stop()


__all__ = ["KernelCommands", "StewardDaemon"]
