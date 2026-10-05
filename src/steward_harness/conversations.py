"""The provider-neutral world-session execution and acceptance boundary."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Literal, cast

from steward_harness.task_calls import TaskCalls, TaskCallServer, REPLY_DIRECTIVE
from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.config.schema import ProcedureConfig
from steward_harness.prompts import build_turn_prompt, build_result_assessment_request
from steward_harness.provider_types import ProviderFamily, ProviderProfile
from steward_harness.runtime.contracts import (
    NativeStorageDeferred, RuntimeInput, RuntimeExecutionError, RuntimeUnavailable,
)
from steward_harness.world.orientation import repository_orientation, world_orientation
from steward_harness.world.turn_checkpoint import WorldTurnCheckpoint, WorldTurnWorktree, WorldUpdatePending, WorldContentConflict
from steward_harness.lease import Busy, Lease
from steward_harness.state import (
    Conversation,
    ConversationBusy,
    ConversationId,
    StateDatabase,
    TaskAdmission,
    TaskId,
    Turn,
    TurnId,
)


Transport = Literal["telegram", "desk", "rhythm"]


_TASK_MARKER = "TASK_PROPOSAL:"
_ACTION_MARKER = "TASK_ACTION:"


@dataclass(frozen=True, slots=True)
class ConversationTurnResult:
    """The stable visible result of one accepted inbound event."""

    conversation_id: ConversationId
    turn_id: TurnId
    reply_text: str
    task_admission: TaskAdmission | None = None
    task_rejection: str | None = None
    execution_turn_id: TurnId | None = None

    @property
    def transport_reply(self) -> str:
        """The execution owns final narration; attached sources share its receipt."""
        return self.reply_text if self.execution_turn_id is None else ""


def _retired_task_markers(reply: str) -> tuple[str, str | None]:
    """Hide retired control lines and refuse their authority, regardless of syntax.

    Historical completed turns replay their accepted receipts. Unaccepted output
    is only narration; parsing an old proposal cannot make it an operation.
    """
    lines = reply.rstrip().splitlines()
    visible = [line for line in lines if not line.startswith((_TASK_MARKER, _ACTION_MARKER))]
    rejection = None if len(visible) == len(lines) else (
        "Final task markers are retired; use a live task call and its receipt. No operation was performed."
    )
    return "\n".join(visible).strip(), rejection


class ConversationService:
    """Run ordinary turns and admit only authorized, typed repository tasks."""

    def __init__(
        self,
        state: StateDatabase,
        cognition: Cognition,
        *,
        provider_order: tuple[ProviderFamily, ...],
        profile: ProviderProfile,
        workspace: Path | WorldTurnCheckpoint,
        timeout_seconds: int,
        desk_provider: ProviderFamily | None = None,
        desk_profile: ProviderProfile | None = None,
        telegram_actions: tuple[str, ...] = (),
        delivery_roots: tuple[str, ...] = (),
        telegram_admin=None,
    ) -> None:
        if not provider_order or len(set(provider_order)) != len(provider_order):
            raise ValueError("conversation provider order must be nonempty and unique")
        if isinstance(workspace, WorldTurnCheckpoint):
            if workspace.state is not state:
                raise ValueError("conversation and world checkpoint must share state")
        elif not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError("conversation workspace must be an existing absolute directory")
        if timeout_seconds <= 0:
            raise ValueError("conversation timeout must be positive")
        self._input_lock = RLock()
        self._native_inputs: dict[ConversationId, tuple[TurnId, Callable[[RuntimeInput], None]]] = {}
        self._state = state
        self._cognition = cognition
        self._provider_order = provider_order
        self._profile = profile
        if desk_provider is not None and desk_provider not in provider_order:
            raise ValueError("desk provider must be configured")
        self._desk_provider = desk_provider
        self._desk_profile = desk_profile
        self._workspace = workspace
        self._timeout_seconds = timeout_seconds

        self._telegram_admin = telegram_admin
        self._telegram_actions = telegram_actions
        self._delivery_roots = delivery_roots

    def native_telegram_topics(self) -> tuple[int, ...]:
        """Topics with a real provider input handle, never an additional writer."""
        with self._input_lock:
            return tuple(
                int(identity.transport_key)
                for identity in self._native_inputs
                if identity.transport == "telegram"
                and identity.transport_key.lstrip("-").isdigit()
            )

    def cancel(self, execution_id: object) -> bool:
        """Stop the run under this execution ID, wherever it is.

        The whole of live cancellation. There is nothing to persist,
        because a run that is not running cannot be stopped and one that
        is has a process to signal. A task's abandonment is separately
        durable -- that is the withdrawal marker, and it is a different
        fact from "stop what you are doing".
        """
        return self._cognition.cancel(str(execution_id))

    def run_turn(
        self,
        *,
        transport: Transport,
        transport_key: str,
        source_event_key: str,
        operator_id: str,
        text: str,
        episode_input: str | None = None,
        images: tuple[Path, ...] = (),
        ongoing_only: bool = False,
        allow_empty_output: bool = True,
        procedure: ProcedureConfig | None = None,
        reserved_rhythm: bool = False,
        notify_owner: str | None = None,
        drive_tasks: bool = False,
        automatic: bool = False,
    ) -> ConversationTurnResult:
        """Produce, retain, and accept one source event; replay never admits work.

        `episode_input` is what the world records as the turn's input, and it
        defaults to `text` because for an operator message the two are the same
        thing. They part company when the harness itself is the speaker: a
        task-result turn's `text` is a page of controller instruction wrapped
        around a brief that already lives on the task's own branch, and copying
        that into the world commit stores it twice while telling the next pass
        nothing it could not derive. Pass the short form there. The provider
        still receives `text` whole — this changes what is remembered, never
        what is asked.
        """
        conversation = self.conversation_for(transport, transport_key)
        prior = self._state.turn_for_source(conversation.conversation_id, source_event_key)
        if (prior is not None
                and (prior.execution_turn_id is None or prior.input_disposition == "accepted")
                and self._state.prepared_turn(str(prior.execution_turn_id or prior.turn_id)) is not None):
            # Validate replay identity, then use its receipt even while a newer
            # source owns an unfinished checkout. Replaying is not a new writer.
            self._state.start_turn(conversation.conversation_id, source_event_key, operator_id, text)
            accepted = self.accept_prepared(str(prior.execution_turn_id or prior.turn_id))
            return replace(accepted, turn_id=prior.turn_id, execution_turn_id=prior.execution_turn_id)
        self.recover_completion(conversation.conversation_id)
        conversation = self._state.get_conversation(conversation.conversation_id)
        with self._input_lock:
            active = self._native_inputs.get(conversation.conversation_id) if not images else None
            execution_turn_id = active[0] if active else None
            if ongoing_only and active is None:
                raise ConversationBusy("native delivery lane cannot start an execution")
            turn, started = self._state.start_turn(
                conversation.conversation_id, source_event_key, operator_id, text,
                execution_turn_id,
            )
            if turn.execution_turn_id is not None:
                if started:
                    assert active is not None
                    # Persist ambiguous delivery before offering bytes. Neither a send nor
                    # an acknowledgement is a completed world/source receipt.
                    active[1](RuntimeInput(
                        source_id=str(turn.turn_id), text=text,
                        origin="controller" if operator_id.startswith("harness:") else "operator",
                        author=operator_id,
                    ))
                if turn.input_disposition == "accepted":
                    receipt = self._state.prepared_turn(str(turn.execution_turn_id))
                    if receipt is not None:
                        accepted = self.accept_prepared(str(turn.execution_turn_id))
                        assert isinstance(accepted, ConversationTurnResult)
                        return ConversationTurnResult(
                            accepted.conversation_id, turn.turn_id, accepted.reply_text,
                            accepted.task_admission, accepted.task_rejection,
                            turn.execution_turn_id,
                        )
                if self._state.active_turn(conversation.conversation_id) is not None:
                    raise ConversationBusy("native input retained; awaiting execution acceptance")
                raise RuntimeExecutionError(
                    f"native input {turn.turn_id} has {turn.input_disposition} delivery without "
                    "an accepted execution; inspect retained evidence before retrying"
                )
        event_id = str(turn.turn_id)
        if reserved_rhythm:
            if not turn.rhythm_continuation:
                raise ValueError("reserved execution requires a rhythm continuation source")
            # The rhythm lease excludes concurrent consumers. Custody is taken
            # before tools; a claimed crash can never enter this branch.
            started = (turn.state == "running" and self._claimed(event_id) is None)
        if not started:
            if self._state.prepared_turn(event_id) is not None:
                accepted = self.accept_prepared(event_id)
                assert isinstance(accepted, ConversationTurnResult)
                return accepted
            if self._state.active_turn(conversation.conversation_id) is not None:
                raise ConversationBusy("execution has not reached durable acceptance")
            raise RuntimeExecutionError(
                f"turn {turn.turn_id} has no durable acceptance receipt; "
                "inspect its recorded history before retrying"
            )
        def build_prompt() -> str:
            """Prepare discovery inside the guarded turn, so failures release it."""
            checkpoint = self._workspace if isinstance(self._workspace, WorldTurnCheckpoint) else None
            # A worldless turn reads the checkout it runs in, read-only.
            orientation = world_orientation() if checkpoint else repository_orientation()
            return build_turn_prompt(
                text,
                transport=transport,
                orientation=orientation,
                event_id=event_id,
                telegram_actions=self._telegram_actions,
                delivery_roots=self._delivery_roots,
            )

        accepted = self._execute_turn(
            conversation,
            turn,
            build_prompt=build_prompt,
            episode_input=episode_input if episode_input is not None else text,
            images=images,
            live_input=True,
            allow_empty_output=allow_empty_output,
            procedure=procedure,
            keep_unclaimed=reserved_rhythm,
            notify_owner=notify_owner,
            drive_tasks=drive_tasks,
            automatic=automatic,
        )
        assert isinstance(accepted, ConversationTurnResult)
        return accepted

    def _execute_turn(
        self,
        conversation: Conversation,
        turn: Turn,
        *,
        build_prompt: Callable[[], str],
        episode_input: str,
        images: tuple[Path, ...],
        live_input: bool,
        allow_empty_output: bool = True,
        procedure: ProcedureConfig | None = None,
        keep_unclaimed: bool = False,
        notify_owner: str | None = None,
        drive_tasks: bool = False,
        automatic: bool = False,
    ) -> ConversationTurnResult | Turn:
        """Run, retain and accept one declared world-session turn.

        The prompt arrives unbuilt because the guard below is what releases a
        `running` turn, and everything that can raise between `start_turn` and
        acceptance has to be inside it. A caller that built its prompt first
        would own a failure this method cannot see.
        """
        event_id = str(turn.turn_id)
        lineage_generation = conversation.generation
        lineage_provider = conversation.provider

        def session_started(provider: str, session: str) -> None:
            nonlocal lineage_generation, lineage_provider
            lineage = self._state.bind_conversation_provider(
                conversation.conversation_id,
                provider,
                session,
                expected_generation=lineage_generation,
            )
            lineage_generation = lineage.generation
            lineage_provider = lineage.provider

        def session_invalidated(provider: str) -> None:
            # A provider that has lost its saved session leaves this lineage
            # with nothing to resume, which is the same bind with no session.
            nonlocal lineage_generation, lineage_provider
            lineage = self._state.bind_conversation_provider(
                conversation.conversation_id,
                provider,
                None,
                expected_generation=lineage_generation,
            )
            lineage_generation = lineage.generation
            lineage_provider = lineage.provider

        def input_ready(send: Callable[[RuntimeInput], None]) -> None:
            with self._input_lock:
                self._state.bind_native_execution(turn.turn_id, lineage_generation)
                self._native_inputs[conversation.conversation_id] = (turn.turn_id, send)

        def release_input() -> None:
            with self._input_lock:
                current = self._native_inputs.get(conversation.conversation_id)
                if current is not None and current[0] == turn.turn_id:
                    del self._native_inputs[conversation.conversation_id]

        checkpoint = self._workspace if isinstance(self._workspace, WorldTurnCheckpoint) else None
        worktree = None
        result = None
        withdrawn = False
        task_calls = None
        def prepare_request() -> CognitionRequest:
            nonlocal worktree, withdrawn, task_calls
            prompt = build_prompt()
            if checkpoint is not None:
                try:
                    worktree = checkpoint.checkout(workspace_id=conversation.conversation_id.workspace)
                except Busy:
                    # No provider has received the source, so the turn never
                    # happened: its replay after contention starts it afresh.
                    if not keep_unclaimed:
                        self._state.withdraw_turn(turn.turn_id)
                    withdrawn = True
                    raise
            # Establish custody before a provider can change the checkout. If
            # retaining its eventual output fails, this claim still fences it.
            self._state.claim_turn(
                turn.turn_id, episode_input=episode_input,
                world_root=str(checkpoint.world.root) if checkpoint else None,
                base_sha=worktree.base_sha if worktree else None,
            )
            task_calls = TaskCallServer(TaskCalls(self._state, turn.turn_id, cancel=self.cancel,
                                                 notify_owner=notify_owner, telegram_admin=self._telegram_admin,
                                                 drive_tasks=drive_tasks))
            if conversation.conversation_id.kind != "rhythm" or drive_tasks:
                prompt += "\n\n" + task_calls.prompt
                if drive_tasks:
                    prompt += "\nThis rhythm may submit, answer, retry or note tasks for its configured owner. It cannot cancel tasks or resume cancelled work. Results return to the owner for normal assessment."
                if not automatic and conversation.conversation_id.kind != "rhythm":
                    prompt += "\n" + REPLY_DIRECTIVE
            return CognitionRequest(
                execution_id=event_id,
                task_call_socket=task_calls.path,
                on_process_started=task_calls.bind,
                native_owner=str(conversation.conversation_id),
                native_generation=lambda provider: lineage_generation + (provider != lineage_provider),
                profile=cast(ProviderProfile, conversation.profile),
                prompt=prompt,
                cwd=(worktree.path if worktree else self._workspace.world.root
                     if isinstance(self._workspace, WorldTurnCheckpoint) else self._workspace),
                **(procedure.limits(self._timeout_seconds) if procedure
                   else dict(timeout_seconds=self._timeout_seconds)),
                # A procedure's model pins only its own provider; fallbacks run their own.
                **(procedure.routing(self._provider_order) if procedure
                   else dict(provider_order=self._order_from(conversation.provider))),
                provider_session_id=conversation.provider_session_id,
                session_provider=cast(ProviderFamily, conversation.provider)
                if conversation.provider_session_id
                else None,
                images=images,
                sandbox_mode="workspace-write" if checkpoint else "read-only",
                allow_empty_output=allow_empty_output,
                on_session_started=session_started,
                on_session_invalidated=session_invalidated,
                on_input_ready=input_ready if live_input else None,
                on_input_result=(
                    lambda evidence: self._state.record_native_input_result(
                        turn.turn_id, evidence.source_id, evidence.disposition
                    )
                ) if live_input else (lambda _evidence: None),
            )
        try:
            try:
                result = self._cognition.run(prepare_request, execution_id=event_id)
            finally:
                if task_calls is not None:
                    task_calls.close()
            release_input()
            if not result.output.strip() and not allow_empty_output:
                # A returned provider failure like any other: nothing to
                # retain, and its partial work passes to the next source.
                self._state.release_claim(turn.turn_id)
                raise RuntimeError("provider returned an empty world-session reply")
            # The provider is finished. Preserve its output before Git can fail;
            # the retained checkout must not pass to another source meanwhile.
            with self._capture_lease(conversation.conversation_id):
                self._state.retain_output(
                    turn.turn_id, output=result.output, provider=result.resolved.provider,
                    model=result.effective_model, provider_session_id=result.provider_session_id,
                    profile=result.resolved.profile, generation=lineage_generation,
                )
                self._prepare_completion(event_id)
            # Nothing can arrive between these two lines to withdraw the turn:
            # `cognition.run` has returned and its `finally` has already popped
            # `Cognition._active`, so `/cancel` finds no bound conversation.
            return self.accept_prepared(event_id)
        except BaseException as error:
            if result is None and isinstance(error, Exception):
                # A returned provider failure retains ordinary partial native
                # work. A process crash or unretained completed reply remains
                # uncertain and requires its original evidence to be inspected.
                self._state.release_claim(turn.turn_id)
            if isinstance(error, NativeStorageDeferred) and not withdrawn:
                # Storage refused before any provider started: the turn never
                # happened, so its source stays queued and starts it afresh.
                try:
                    if not keep_unclaimed:
                        self._state.withdraw_turn(turn.turn_id)
                    withdrawn = True
                except RuntimeError:
                    pass  # Linked or claimed after all: interrupt it below.
            if (not withdrawn and self._state.prepared_turn(event_id) is None
                    and not self._claimed(event_id)):
                self._state.interrupt_turn(
                    turn.turn_id, str(error) or type(error).__name__
                )
            raise
        finally:
            release_input()

    def _claimed(self, event_id: str) -> sqlite3.Row | None:
        return next((row for row in self._state.claimed_turns() if row["turn_id"] == event_id), None)

    def _capture_lease(self, owner: ConversationId, *, timeout: float = 5) -> Lease:
        """One capturer per owner: the finishing turn, or recovery, never both."""
        root = self._state.path.with_suffix(".captures")
        root.mkdir(mode=0o700, exist_ok=True)
        name = hashlib.sha256(str(owner).encode()).hexdigest()
        return Lease(root, lock_name=f"{name}.lock", timeout_seconds=timeout)

    def _prepare_completion(self, event_id: str) -> None:
        row = self._claimed(event_id)
        if row is None:
            return
        if row["output"] is None:
            raise ConversationBusy(
                f"turn {event_id} has no retained provider completion; inspect its native records"
            )
        checkpoint = self._workspace if isinstance(self._workspace, WorldTurnCheckpoint) else None
        if row["world_root"] != (str(checkpoint.world.root) if checkpoint else None):
            raise ValueError("completed output belongs to another world")
        assert checkpoint is not None  # a worldless turn is prepared once its output is retained
        candidate_sha = checkpoint.retain(
            WorldTurnWorktree(checkpoint.workspace(ConversationId(row["conversation_id"]).workspace),
                              row["base_sha"]),
            event_id, row["episode_input"], _retired_task_markers(row["output"])[0],
            row["source_event_key"],
        )
        self._state.record_candidate(event_id, candidate_sha)

    def recover_completion(self, owner: ConversationId) -> None:
        """Finish checkpointing old output before this owner receives new work."""
        with self._input_lock:
            if owner in self._native_inputs:
                return
        claimed = [row for row in self._state.claimed_turns()
                   if row["conversation_id"] == str(owner)]
        if not claimed:
            return
        event_id = claimed[0]["turn_id"]
        with self._capture_lease(owner, timeout=0):
            try:
                self._prepare_completion(event_id)
            except ConversationBusy:
                raise
            except Exception as error:
                raise WorldUpdatePending(f"completed turn for {owner} awaits capture: {error}") from error
        # The claim was read before the lease: a provider failure may have
        # released it since, leaving nothing to accept.
        if self._state.prepared_turn(event_id) is not None:
            self.accept_prepared(event_id)

    def recover_completions(self) -> None:
        """Startup recovery uses retained output, never repeats native cognition."""
        for row in self._state.claimed_turns():
            try:
                self.recover_completion(ConversationId(row["conversation_id"]))
            except (WorldUpdatePending, WorldContentConflict, Busy, ConversationBusy) as error:
                logging.getLogger(__name__).warning("completed turn retained for recovery: %s", error)

    def deliver_task_result(
        self, conversation_id: ConversationId, *, send: Callable[[str, str], None],
    ) -> str | None:
        """Send retained evidence without acquiring a conversation or cognition slot."""
        receipt = self._state.retain_pending_result(conversation_id)
        if receipt is None:
            return None
        if "reply" not in receipt:
            # A legacy receipt without a frozen message is not a send decision
            # for a quiet automatic task. Only explicit notify receipts carry one.
            quiet = bool(receipt.get("task_id") and receipt["source_key"].endswith(":done")
                         and self._state.tasks.get(
                TaskId(receipt["task_id"])).quiet)
            receipt["reply"] = "" if quiet else receipt["result_text"]
            # Freeze the delivery decision before attempting external transport.
            receipt["assess"] = bool(receipt.get("task_id")) and not quiet
            self._state.save_result_receipt(receipt)
        if receipt["reply"]:
            send(receipt["reply"], receipt["source_key"])
        receipt.pop("delivery_error", None)
        receipt["done"] = True
        self._state.save_result_receipt(receipt)
        return receipt["reply"]

    def assess_task_result(self, conversation_id: ConversationId) -> None:
        """Optional judgment after delivery; final prose creates no second send."""
        receipt = next((r for r in self._state.pending_result_assessments()
                        if r["owner"] == str(conversation_id)), None)
        if receipt is None:
            return
        task = self._state.tasks.get(TaskId(receipt["task_id"]))
        current_source = f"task_result:{task.task_id}:{task.outcome}:{task.status.value}"
        if task.owner != str(conversation_id) or (
            receipt["source_key"].startswith("task_result:")
            and receipt["source_key"] != current_source
        ):
            receipt["assessment_done"] = True
            receipt["assessment_skipped"] = "task outcome or owner changed"
            self._state.save_result_receipt(receipt)
            return
        try:
            self._assess_task_result(
                conversation_id, TaskId(receipt["task_id"]),
                receipt["result_text"], receipt["source_key"],
            )
        except (RuntimeExecutionError, RuntimeUnavailable) as error:
            receipt["assessment_error"] = str(error)
        # Busy/world-acceptance failures propagate and remain pending. Accepted
        # world turns replay their own receipt if this write is interrupted.
        receipt["assessment_done"] = True
        self._state.save_result_receipt(receipt)

    def _assess_task_result(
        self, conversation_id: ConversationId, task_id: TaskId,
        result_text: str, source_event_key: str,
    ) -> str:
        task = self._state.tasks.get(task_id)
        target_result = source_event_key.startswith("target_result:")
        self._state.open_conversation(conversation_id,
                                      provider=self._state.tasks.default_provider,
                                      profile=self._state.tasks.default_profile)
        conversation = self._state.get_conversation(conversation_id)
        prior = self._state.turn_for_source(conversation_id, source_event_key)
        text = prior.input_text if prior is not None else build_result_assessment_request(
            task.brief, result_text,
        )
        result = self.run_turn(
            transport=conversation.transport,
            transport_key=conversation.transport_key,
            source_event_key=source_event_key,
            operator_id="harness:task-result",
            automatic=True,
            text=text,
            allow_empty_output=True,
            # The episode names the result; it does not restate it. Everything
            # `text` adds — the controller preamble, the brief, the worker's
            # findings — is either a constant or already committed on the
            # task's own branch, so an episode carrying it records the
            # harness's own instructions as though they were an observation.
            episode_input=(result_text if target_result else
                           f"Harness task result for {task_id} in {task.repository}."),
        )
        return result.reply_text

    def accept_prepared(self, event_id: str) -> ConversationTurnResult | Turn:
        row = self._state.prepared_turn(event_id)
        if row is None:
            raise ValueError("world-session output is not prepared")
        turn = self._state.get_turn(TurnId(event_id))
        fresh = row["state"] != "completed"

        def finalize():
            current = self._state.prepared_turn(event_id)
            if current["state"] == "completed":
                return current
            visible, rejection = _retired_task_markers(current["output"])
            return self._state.accept_turn(
                event_id, visible_reply=visible, spec=None, rejection=rejection,
            )

        if fresh and row["world_root"] is not None:
            if not isinstance(self._workspace, WorldTurnCheckpoint):
                raise RuntimeError("pending turn requires its configured Git world")
            row = self._workspace.apply(event_id, finalize)
        else:
            row = finalize()
        admission = None
        if row["task_id"]:
            admission = TaskAdmission(TaskId(row["task_id"]), fresh)
        return ConversationTurnResult(
            turn.conversation_id,
            TurnId(event_id),
            row["reply_text"],
            admission,
            row["rejection"],
        )

    def switch_provider(
        self, conversation_id: ConversationId, provider: ProviderFamily
    ) -> Conversation:
        """Change this conversation's provider independently of its tasks.

        Switching to the provider already in use is not a rotation, so it is
        refused here rather than made a case inside the lineage transition.
        """
        if provider not in self._provider_order:
            raise ValueError(f"provider {provider!r} is not configured")
        current = self._state.get_conversation(conversation_id)
        if current.provider == provider:
            return current
        return self._state.bind_conversation_provider(conversation_id, provider, None)

    def conversation_for(self, transport: Transport, transport_key: str) -> Conversation:
        """Resolve command routing through the same real transport identity as turns."""
        return self._state.get_or_create_conversation(
            transport,
            transport_key,
            provider=(self._desk_provider if transport == "desk" else None) or self._provider_order[0],
            profile=(self._desk_profile if transport == "desk" else None) or self._profile,
        )

    def set_profile(
        self, conversation_id: ConversationId, profile: ProviderProfile
    ) -> Conversation:
        """Change this conversation's quality independently of its tasks."""
        return self._state.set_conversation_profile(conversation_id, profile)

    def _order_from(self, current: str) -> tuple[ProviderFamily, ...]:
        if current not in self._provider_order:
            raise RuntimeError(f"conversation selected unconfigured provider {current!r}")
        selected = cast(ProviderFamily, current)
        return (selected, *(provider for provider in self._provider_order if provider != selected))
