"""The Snugban dispatches of one webhook route and profile: the agent runs they start, the receiver's own
acks and heartbeats for them, and the pause / resume / abort / status_request / decision webhooks that
steer them. Runs are gateway webhook sessions; their delivery ids name the dispatch
(``snugban:dispatch:<id>``, ``snugban:resume:<id>:...``, ``snugban:answer:<id>:...``), so a dispatch's
sessions can be found and stopped from the session keys alone after a gateway restart."""

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from gateway.platforms.webhook_snugban import (
    _ACTED_ON_TTL_SECONDS, _DEFAULT_HEARTBEAT_SECONDS, _DELIVERY_HEADER, _DELIVERY_TTL_SECONDS, _ID_RE,
    _MAX_REMEMBERED, _SIGNATURE_HEADER, ANSWERED_INTRO, RESUMED_INTRO, SnugbanClient, _decision_prompt,
    _decision_text, _dispatch_prompt, _Invalid, _json, _require_dispatch_shape, _valid_id, snugban_block)

logger = logging.getLogger(__name__)

# How often a stopped run is stopped again until it settles: a run still queued when pause or abort
# arrived has no agent to interrupt yet.
_STOP_ENFORCE_INTERVAL_SECONDS = 1.0
_STOP_ENFORCE_LIMIT_SECONDS = 3600.0
# A session found running for a dispatch this receiver no longer knows (a gateway restart) has no
# settlement to wait for: it is stopped again whenever found running, for this long.
_ORPHAN_ENFORCE_SECONDS = 300.0
_LIVE_ADMISSION = frozenset({"queued", "started"})
# Dispatch states in which the receiver heartbeats it: a run works it, or it waits for Keith's answer.
_HEARTBEATING = frozenset({"running", "waiting_answer"})
_DELIVERY_RE = re.compile(r"snugban:(?:dispatch|resume|answer):([A-Za-z0-9_-]{1,100})(?::.*)?")
_CHAT_TOKEN_RE = re.compile(r"webhook:v2:([A-Za-z0-9_-]+)")
_QUESTION_ID_RE = re.compile(r'"questionId"\s*:\s*"([A-Za-z0-9_-]{1,100})"')


def _dispatch_of_delivery(delivery_id: str) -> Optional[str]:
    match = _DELIVERY_RE.fullmatch(delivery_id or "")
    return match.group(1) if match else None


def _delivery_of_session_key(session_key: str) -> Optional[tuple]:
    """``(profile, route, delivery_id)`` of a webhook session key (its chat id is a ``webhook:v2:`` token)."""
    match = _CHAT_TOKEN_RE.search(session_key)
    if match is None:
        return None
    token = match.group(1)
    try:
        profile, route, delivery_id = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError):
        return None
    return str(profile), str(route), str(delivery_id)


def _tool_calls(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [call for call in raw if isinstance(call, dict)] if isinstance(raw, list) else []


def questions_asked(db, session_id: str) -> set:
    """The ids of the questions the run of *session_id* asked Keith with Snugban's ``ask_keith`` tool, read
    from its transcript (the tool's results carry the ``questionId`` Snugban gave each)."""
    rows = db.get_messages(session_id, include_ancestors=True)
    ask_calls = {call.get("id") for row in rows for call in _tool_calls(row.get("tool_calls"))
                 if str((call.get("function") or {}).get("name", "")).endswith("ask_keith")}
    asked = set()
    for row in rows:
        if row.get("role") == "tool" and (row.get("tool_call_id") in ask_calls
                                          or str(row.get("tool_name") or "").endswith("ask_keith")):
            asked.update(_QUESTION_ID_RE.findall(str(row.get("content") or "")))
    return asked


def _admitted(payload: dict) -> dict:
    """The webhook as admitted: without its per-attempt ``deliveryId``, so Snugban's resend (same
    dispatchId or questionId, new deliveryId) is the same delivery to durable admission."""
    return {key: value for key, value in payload.items() if key != "deliveryId"}


class _Run:
    """One agent run of a dispatch, followed through its admission (``WebhookAdapter._admit_agent_run``)."""

    def __init__(self, delivery_id: str, on_end=None):
        self.delivery_id = delivery_id
        self.session_id: Optional[str] = None
        self.db = None
        self.status: Optional[str] = None  # its admission's status: when committed, then when it ended
        self.stopped = False  # the receiver stopped it (pause or abort): its end is not a failure
        self.ended = asyncio.Event()
        self._on_end = on_end

    def admitted(self, session_id: str, db, status: Optional[str] = None) -> None:
        self.session_id, self.db, self.status = session_id, db, status

    def settled(self, status: Optional[str]) -> None:
        if self.ended.is_set():
            return
        self.status = status
        self.ended.set()
        if self._on_end is not None:
            self._on_end(self)


@dataclass
class _Dispatch:
    id: str
    payload: Optional[dict]  # None for an abort of a dispatch never received here
    state: str = "running"  # running | waiting_answer | paused | aborted | ended
    run: Optional[_Run] = None
    pending_decisions: list = field(default_factory=list)
    open_questions: set = field(default_factory=set)
    heartbeat: Optional[asyncio.Task] = None
    ended_at: Optional[float] = None


class SnugbanReceiver:
    """The Snugban webhooks of one route and profile, and the dispatches they started."""

    def __init__(self, adapter, route_name: str, profile):
        self._adapter, self._route_name, self._profile = adapter, route_name, profile
        self._deliveries: dict[str, float] = {}
        self._signatures: dict[str, float] = {}
        self._dispatches: dict[str, _Dispatch] = {}
        self._questions: dict[str, float] = {}
        self._handlers = {"dispatch": self._on_dispatch, "decision": self._on_decision, "pause": self._on_pause,
                          "resume": self._on_resume, "abort": self._on_abort,
                          "status_request": self._on_status_request}

    def _client(self) -> SnugbanClient:
        block = snugban_block(self._route_config())
        return SnugbanClient(block["base_url"], block["agent_key"])

    def _route_config(self) -> dict:
        return self._adapter._routes.get(self._route_name) or {}

    # --- bookkeeping ---

    def _prune(self, now: float) -> None:
        for seen in (self._deliveries, self._signatures, self._questions):
            ttl = _ACTED_ON_TTL_SECONDS if seen is self._questions else _DELIVERY_TTL_SECONDS
            for key in [k for k, at in seen.items() if now - at > ttl]:
                seen.pop(key, None)
        for key in [k for k, d in self._dispatches.items()
                    if d.ended_at is not None and now - d.ended_at > _ACTED_ON_TTL_SECONDS]:
            self._dispatches.pop(key, None)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._adapter._background_tasks.add(task)
        task.add_done_callback(self._adapter._background_tasks.discard)
        return task

    def _report(self, dispatch_id: str, step: str, body: Optional[dict] = None) -> None:
        self._spawn(self._client().report(dispatch_id, step, body), f"snugban-report-{step}-{dispatch_id}")

    def _finish(self, dispatch: _Dispatch, state: str) -> None:
        """The dispatch is over here: nothing heartbeats it any more."""
        dispatch.state, dispatch.ended_at = state, time.time()
        self._stop_heartbeat(dispatch)

    # --- entry point ---

    async def handle(self, headers, payload: Any):
        now = time.time()
        self._prune(now)
        if not isinstance(payload, dict):
            return _json({"error": "Body must be a JSON object"}, 400)
        refusal = self._refuse_replay(headers, payload, now)
        if refusal is not None:
            return refusal
        delivery_id = payload["deliveryId"]
        handler = self._handlers.get(payload.get("type"))
        if handler is None:
            logger.info("[webhook] Snugban route %s ignored webhook type %r", self._route_name, payload.get("type"))
            return _json({"status": "ignored"})
        try:
            return await handler(payload, delivery_id)
        except _Invalid as exc:
            return _json({"error": f"Invalid {payload.get('type')} webhook: {exc}"}, 400)

    def _refuse_replay(self, headers, payload: dict, now: float):
        """Refuse a delivery whose signed ``deliveryId`` differs from its header, or whose id or signature
        was seen in the last ten minutes; otherwise remember both. The header is not signed, so the body's
        id is the one that counts."""
        delivery_id, header_id = payload.get("deliveryId"), headers.get(_DELIVERY_HEADER, "")
        if not isinstance(delivery_id, str) or not _ID_RE.fullmatch(delivery_id):
            return _json({"error": "deliveryId must be the X-Snugban-Delivery id"}, 400)
        if header_id != delivery_id:
            logger.warning("[webhook] Snugban route %s: signed deliveryId differs from %s", self._route_name,
                           _DELIVERY_HEADER)
            return _json({"error": "deliveryId does not match the X-Snugban-Delivery header"}, 401)
        signature = headers.get(_SIGNATURE_HEADER, "")
        if delivery_id in self._deliveries or signature in self._signatures:
            logger.warning("[webhook] Snugban route %s refused replayed delivery %s", self._route_name, delivery_id)
            return _json({"error": "Replayed delivery"}, 409)
        if len(self._deliveries) >= _MAX_REMEMBERED:
            return _json({"error": "Too many deliveries in flight; retry"}, 503)
        self._deliveries[delivery_id] = self._signatures[signature] = now
        return None

    # --- runs ---

    async def _admit(self, prompt: str, payload: dict, delivery_id: str, event_type: str,
                     watch: Optional[_Run] = None) -> Optional[str]:
        return await self._adapter._admit_agent_run(
            _admitted(payload), prompt, delivery_id, time.time(), route_config=self._route_config(),
            route_name=self._route_name, profile=self._profile, event_type=event_type, watch=watch)

    def _runner(self):
        return self._adapter.gateway_runner or getattr(self._adapter._message_handler, "__self__", None)

    def _session(self, delivery_id: str):
        source = self._adapter._admission_source(self._route_name, self._profile, delivery_id)
        return self._runner()._session_key_for_source(source), source

    def _running_agent(self, run: _Run):
        from gateway.run import _AGENT_PENDING_SENTINEL
        key, _source = self._session(run.delivery_id)
        agent = self._runner()._snapshot_running_agents().get(key)
        return None if agent is _AGENT_PENDING_SENTINEL else agent

    async def _start_run(self, dispatch: _Dispatch, prompt: str, delivery_id: str) -> Optional[str]:
        """Admit a run of *dispatch*: ``"accepted"``; ``"duplicate"`` when an earlier process admitted it and
        its run is still live (it is adopted); ``"ended"`` when that earlier run is over; None when admission
        failed. The run is the dispatch's before admission, so a pause or abort arriving meanwhile stops it."""
        run = _Run(delivery_id, on_end=lambda ended: self._spawn(
            self._after_run(dispatch, ended), f"snugban-ended-{dispatch.id}"))
        previous = (dispatch.run, dispatch.state)
        dispatch.run, dispatch.state = run, "running"
        outcome = await self._admit(prompt, dispatch.payload, delivery_id, "dispatch", watch=run)
        if outcome is None or (outcome == "duplicate" and run.status not in _LIVE_ADMISSION):
            if dispatch.run is run:
                dispatch.run, dispatch.state = previous
            return None if outcome is None else "ended"
        if dispatch.state == "running" and not run.ended.is_set():
            self._start_heartbeat(dispatch)
        return outcome

    async def _after_run(self, dispatch: _Dispatch, run: _Run) -> None:
        """A run of *dispatch* ended: keep the dispatch while it waits for Keith, else fail it if the run left
        it open. An outcome that is not known (the run crashed, its finalization failed) fails nothing."""
        if dispatch.run is not run:
            return
        if run.stopped or dispatch.state != "running":
            if dispatch.state == "aborted":
                self._finish(dispatch, "aborted")
            return
        if run.status != "terminal":
            logger.warning("[webhook] Snugban dispatch %s: its run ended with outcome %r; leaving the dispatch to "
                           "Snugban's stall handling", dispatch.id, run.status)
            self._finish(dispatch, "ended")
            return
        asked = await asyncio.to_thread(questions_asked, run.db, run.session_id) if run.db else set()
        open_questions = {question for question in asked if question not in self._questions}
        if dispatch.run is not run or dispatch.state != "running":
            return
        if open_questions:
            logger.info("[webhook] Snugban dispatch %s waits for Keith's answer to %s", dispatch.id,
                        ", ".join(sorted(open_questions)))
            dispatch.state, dispatch.open_questions = "waiting_answer", open_questions
            if dispatch.heartbeat is None:
                self._start_heartbeat(dispatch)
            return
        self._finish(dispatch, "ended")
        # Snugban answers 409 when the run already completed or failed it; otherwise nothing works it now.
        self._report(dispatch.id, "fail", {"reason": "The Hermes run ended without completing or failing "
                                                     "the dispatch."})

    async def _run_again(self, dispatch: _Dispatch, intro: str, delivery_id: str) -> Optional[str]:
        """Start a new run of *dispatch* on its branch, carrying the answers held for it."""
        decisions, dispatch.pending_decisions = tuple(dispatch.pending_decisions), []
        prompt = _dispatch_prompt(dispatch.payload, intro=intro, decisions=decisions)
        outcome = await self._start_run(dispatch, prompt, delivery_id)
        if outcome is None:
            dispatch.pending_decisions[:0] = decisions
        return outcome

    # --- stopping ---

    async def _stop_run(self, run: _Run, reason: str) -> None:
        run.stopped = True
        await self._stop_delivery(run.delivery_id, reason, until=run.ended, limit=_STOP_ENFORCE_LIMIT_SECONDS)

    async def _stop_delivery(self, delivery_id: str, reason: str, *, until: Optional[asyncio.Event],
                             limit: float) -> None:
        """Interrupt the session of *delivery_id* as ``/stop`` does, now and whenever it is found running
        until *until* is set (its run settled) or *limit* seconds pass."""
        from gateway.run import _INTERRUPT_REASON_STOP
        key, source = self._session(delivery_id)
        runner = self._runner()

        async def _interrupt() -> None:
            await runner._interrupt_and_clear_session(key, source, interrupt_reason=_INTERRUPT_REASON_STOP,
                                                      invalidation_reason=reason)

        if runner._is_session_running(key):
            await _interrupt()

        async def _enforce() -> None:
            deadline = time.monotonic() + limit
            while not (until is not None and until.is_set()) and time.monotonic() < deadline:
                await asyncio.sleep(_STOP_ENFORCE_INTERVAL_SECONDS)
                if not (until is not None and until.is_set()) and runner._is_session_running(key):
                    logger.info("[webhook] Snugban run %s is running after it was stopped; stopping it", delivery_id)
                    await _interrupt()

        self._spawn(_enforce(), f"snugban-stop-{delivery_id}")

    def _sessions_of(self, dispatch_id: str) -> set:
        """Delivery ids of the sessions of *dispatch_id*: its first run's, and any running one's."""
        deliveries = {f"snugban:dispatch:{dispatch_id}"}
        profile = self._profile or "default"
        for key in self._runner()._snapshot_running_agents():
            found = _delivery_of_session_key(key)
            if found and found[:2] == (profile, self._route_name) and _dispatch_of_delivery(found[2]) == dispatch_id:
                deliveries.add(found[2])
        return deliveries

    async def _stop_unknown(self, dispatch_id: str, reason: str) -> None:
        """Stop the sessions of a dispatch this receiver holds no run for (it was taken before a restart)."""
        for delivery_id in sorted(self._sessions_of(dispatch_id)):
            await self._stop_delivery(delivery_id, reason, until=None, limit=_ORPHAN_ENFORCE_SECONDS)

    # --- heartbeats ---

    def _start_heartbeat(self, dispatch: _Dispatch) -> None:
        self._stop_heartbeat(dispatch)
        every = (dispatch.payload.get("return") or {}).get("heartbeatEverySeconds")
        interval = float(every) if isinstance(every, (int, float)) and every > 0 else _DEFAULT_HEARTBEAT_SECONDS
        dispatch.heartbeat = self._spawn(self._keep_alive(dispatch, interval), f"snugban-heartbeat-{dispatch.id}")

    @staticmethod
    def _stop_heartbeat(dispatch: _Dispatch) -> None:
        if dispatch.heartbeat is not None and dispatch.heartbeat is not asyncio.current_task():
            dispatch.heartbeat.cancel()
        dispatch.heartbeat = None

    async def _keep_alive(self, dispatch: _Dispatch, interval: float) -> None:
        """Heartbeat the dispatch while it is worked or waits for Keith, so stall detection does not depend on
        the model."""
        while dispatch.state in _HEARTBEATING:
            await asyncio.sleep(interval)
            if dispatch.state not in _HEARTBEATING:
                return
            try:
                if not await self._heartbeat_once(dispatch):
                    if dispatch.state == "waiting_answer":
                        self._finish(dispatch, "ended")
                    return
            except Exception:
                # One broken tick must not end the heartbeats: Snugban would then see a stall.
                logger.exception("[webhook] Snugban heartbeat of dispatch %s failed; trying again next tick",
                                 dispatch.id)

    async def _heartbeat_once(self, dispatch: _Dispatch) -> bool:
        """Deliver held answers and send one heartbeat; False once Snugban says the dispatch is not worked here."""
        self._deliver_pending_decisions(dispatch)
        status, answer = await self._client().report(dispatch.id, "heartbeat")
        if status in (404, 409) or (answer or {}).get("state") == "waiting_input":
            logger.info("[webhook] Snugban dispatch %s is no longer worked here (%s); heartbeats stop",
                        dispatch.id, status)
            return False
        return True

    # --- webhook types ---

    async def _on_dispatch(self, payload: dict, delivery_id: str):
        dispatch_id = _valid_id(payload, "dispatchId")
        _require_dispatch_shape(payload)
        known = self._dispatches.get(dispatch_id)
        if known is not None:
            logger.info("[webhook] Snugban dispatch %s sent again; already taken", dispatch_id)
            if known.state in _HEARTBEATING:
                self._report(dispatch_id, "ack")  # the resend means Snugban missed the first ack
            return _json({"status": "duplicate", "dispatchId": dispatch_id})
        dispatch = self._dispatches[dispatch_id] = _Dispatch(dispatch_id, payload)
        outcome = await self._start_run(dispatch, _dispatch_prompt(payload), f"snugban:dispatch:{dispatch_id}")
        if outcome is None:
            self._dispatches.pop(dispatch_id, None)  # not admitted: Snugban's resend must be taken
            return _json({"error": "Admission unavailable; retry this delivery"}, 503)
        if outcome == "ended":
            # Taken before a restart and its run is over: nothing works it here, so it is not held.
            self._dispatches.pop(dispatch_id, None)
            logger.info("[webhook] Snugban dispatch %s was run here before and that run has ended", dispatch_id)
            return _json({"status": "duplicate", "dispatchId": dispatch_id})
        self._report(dispatch_id, "ack")
        if outcome == "duplicate":
            logger.info("[webhook] Snugban dispatch %s: adopted its run from before a restart", dispatch_id)
            return _json({"status": "duplicate", "dispatchId": dispatch_id})
        logger.info("[webhook] Snugban dispatch %s accepted on route %s", dispatch_id, self._route_name)
        return _json({"status": "accepted", "dispatchId": dispatch_id}, 202)

    async def _on_abort(self, payload: dict, delivery_id: str):
        dispatch_id = _valid_id(payload, "dispatchId")
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is None:
            # Unknown here (never sent, or taken before a restart): stop any session it has, and refuse it later.
            dispatch = self._dispatches[dispatch_id] = _Dispatch(dispatch_id, None)
            self._finish(dispatch, "aborted")
            await self._stop_unknown(dispatch_id, "snugban_abort")
        else:
            self._finish(dispatch, "aborted")
            if dispatch.run is not None and not dispatch.run.ended.is_set():
                await self._stop_run(dispatch.run, "snugban_abort")
        logger.info("[webhook] Snugban aborted dispatch %s", dispatch_id)
        return _json({"status": "aborted", "dispatchId": dispatch_id})

    async def _on_pause(self, payload: dict, delivery_id: str):
        dispatch_id = _valid_id(payload, "dispatchId")
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is None or dispatch.state not in _HEARTBEATING:
            # The protocol's answer for a dispatch not running here: Snugban then records no pause.
            return _json({"error": f"Dispatch {dispatch_id} is not running here"}, 404)
        dispatch.state = "paused"
        self._stop_heartbeat(dispatch)
        if dispatch.run is not None and not dispatch.run.ended.is_set():
            await self._stop_run(dispatch.run, "snugban_pause")
        logger.info("[webhook] Snugban paused dispatch %s", dispatch_id)
        return _json({"status": "paused", "dispatchId": dispatch_id})

    async def _on_resume(self, payload: dict, delivery_id: str):
        dispatch_id = _valid_id(payload, "dispatchId")
        dispatch = self._dispatches.get(dispatch_id)
        if dispatch is None or dispatch.state != "paused" or dispatch.payload is None:
            return _json({"error": f"Dispatch {dispatch_id} is not paused here"}, 404)
        outcome = await self._run_again(dispatch, RESUMED_INTRO, f"snugban:resume:{dispatch_id}:{delivery_id}")
        if outcome != "accepted":
            return _json({"error": "Admission unavailable; retry this delivery"}, 503)
        logger.info("[webhook] Snugban resumed dispatch %s", dispatch_id)
        return _json({"status": "resumed", "dispatchId": dispatch_id}, 202)

    async def _on_status_request(self, payload: dict, delivery_id: str):
        dispatch_id = _valid_id(payload, "dispatchId")
        dispatch = self._dispatches.get(dispatch_id)
        # A dispatch with no run here gets no heartbeat: that would hide a stall from Snugban.
        if dispatch is None or dispatch.state not in _HEARTBEATING:
            return _json({"status": "not_running", "dispatchId": dispatch_id})
        self._report(dispatch_id, "heartbeat")
        return _json({"status": "heartbeat", "dispatchId": dispatch_id})

    # --- decisions ---

    def _dispatch_for_decision(self, question_id: str, task_id: Any) -> Optional[_Dispatch]:
        live = [d for d in self._dispatches.values()
                if d.payload is not None and d.state in ("running", "waiting_answer", "paused")]
        asked = next((d for d in live if question_id in d.open_questions), None)
        if asked is not None or not isinstance(task_id, str):
            return asked
        return next((d for d in live if d.payload["task"].get("id") == task_id), None)

    def _steer(self, dispatch: _Dispatch, text: str) -> bool:
        if dispatch.state != "running" or dispatch.run is None or dispatch.run.ended.is_set():
            return False
        agent = self._running_agent(dispatch.run)
        return agent is not None and self._runner()._steer_running_agent(agent, text)

    def _deliver_pending_decisions(self, dispatch: _Dispatch) -> None:
        while dispatch.pending_decisions and self._steer(dispatch, dispatch.pending_decisions[0]):
            dispatch.pending_decisions.pop(0)

    async def _on_decision(self, payload: dict, delivery_id: str):
        question_id = _valid_id(payload, "questionId")
        if question_id in self._questions:
            return _json({"status": "duplicate", "questionId": question_id})
        self._questions[question_id] = time.time()
        dispatch = self._dispatch_for_decision(question_id, payload.get("taskId"))
        if dispatch is not None:
            return await self._decide(dispatch, payload, question_id, delivery_id)
        outcome = await self._admit(_decision_prompt(payload), payload, f"snugban:decision:{question_id}", "decision")
        if outcome is None:
            self._questions.pop(question_id, None)
            return _json({"error": "Admission unavailable; retry this delivery"}, 503)
        if outcome == "duplicate":
            return _json({"status": "duplicate", "questionId": question_id})
        return _json({"status": "accepted", "questionId": question_id}, 202)

    async def _decide(self, dispatch: _Dispatch, payload: dict, question_id: str, delivery_id: str):
        """Keith's answer for a dispatch held here: a new run when the dispatch waits for it, into the live run
        otherwise, or held for the dispatch's next run."""
        text = _decision_text(payload)
        dispatch.open_questions.discard(question_id)
        if dispatch.state == "waiting_answer":
            dispatch.pending_decisions.append(text)
            outcome = await self._run_again(dispatch, ANSWERED_INTRO, f"snugban:answer:{dispatch.id}:{delivery_id}")
            if outcome != "accepted":
                self._questions.pop(question_id, None)
                dispatch.open_questions.add(question_id)
                dispatch.pending_decisions.remove(text)
                return _json({"error": "Admission unavailable; retry this delivery"}, 503)
            return _json({"status": "continued", "questionId": question_id}, 202)
        if self._steer(dispatch, text):
            return _json({"status": "delivered", "questionId": question_id})
        # Its run is paused or not started: the answer goes with the next delivery to it.
        dispatch.pending_decisions.append(text)
        return _json({"status": "held", "questionId": question_id})
