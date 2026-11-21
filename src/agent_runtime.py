"""LangGraph orchestration for one ScholarGuide turn.

The graph is deliberately thin around the existing ``ChatSession`` state
machine: SQLite remains the source of truth for task/candidates/assessments,
while LangGraph owns the explicit Planner -> Retriever/Assessor -> Critic ->
Verifier -> Finalize transition and stores an inspectable checkpoint.

No graph node invents papers or bypasses ChatSession's budget, evidence and
reuse invariants. A graph failure is recorded and converted into the same
partial result the legacy loop would produce.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sqlite3
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from common import GuideError
from controller import Decision, allowed_actions, record_decision

MAX_GRAPH_STEPS = 18


class TurnState(TypedDict, total=False):
    session_id: str
    revision: int
    step: int
    stage: str
    decision: dict[str, Any]
    reflected: bool
    result: dict[str, Any] | None
    stop_reason: str
    error: str


class LangGraphRuntime:
    """Compile and run the bounded single-agent role graph for one turn."""

    def __init__(self, chat: Any):
        self.chat = chat
        self.last_thread = ''
        self.last_resumed = False
        self.graph = self._build()

    # -- graph construction ----------------------------------------------
    def _build(self):
        graph = StateGraph(TurnState)
        graph.add_node('planner', self.planner)
        graph.add_node('decide', self.decide)
        graph.add_node('retrieve', self.retrieve)
        graph.add_node('assess', self.assess)
        graph.add_node('critic', self.critic)
        graph.add_node('verifier', self.verifier)
        graph.add_node('finalize', self.finalize)

        graph.add_edge(START, 'planner')
        graph.add_conditional_edges('planner', self._after_planner,
                                    {'decide': 'decide', 'finalize': 'finalize'})
        graph.add_conditional_edges('decide', self._after_decide,
                                    {'retrieve': 'retrieve', 'assess': 'assess',
                                     'verifier': 'verifier', 'finalize': 'finalize'})
        graph.add_edge('retrieve', 'critic')
        graph.add_edge('assess', 'critic')
        graph.add_conditional_edges('critic', self._after_critic,
                                    {'retrieve': 'retrieve', 'decide': 'decide',
                                     'finalize': 'finalize'})
        graph.add_conditional_edges('verifier', self._after_verifier,
                                    {'decide': 'decide', 'finalize': 'finalize'})
        graph.add_edge('finalize', END)
        return graph.compile()

    # -- helpers ----------------------------------------------------------
    def _record(self, node: str, **detail: Any) -> None:
        self.chat._event('agent_node', {'node': node, **detail})

    @staticmethod
    def _decision(state: TurnState) -> Decision:
        raw = state.get('decision') or {}
        return Decision(action=str(raw.get('action') or 'FINISH'),
                        args=dict(raw.get('args') or {}),
                        accepted=bool(raw.get('accepted', True)),
                        reason=str(raw.get('reason') or ''),
                        attempted=str(raw.get('attempted') or ''),
                        error=str(raw.get('error') or ''),
                        fallback=raw.get('fallback'))

    # -- nodes ------------------------------------------------------------
    def planner(self, state: TurnState) -> TurnState:
        task = self.chat.task()
        control = self.chat.control_state()
        self._record('planner', revision=state.get('revision'),
                     topic=task.get('topic') or '', goal=task.get('reading_goal') or '',
                     candidates=control.candidates, stale=control.stale,
                     gaps=len(control.gaps))
        terminal = self.chat.prepare_turn()
        if terminal is not None:
            self._record('planner', terminal=terminal.get('status'))
            return {'result': terminal, 'stage': 'finalize'}
        return {'stage': 'decide'}

    def decide(self, state: TurnState) -> TurnState:
        step = int(state.get('step') or 0)
        if step >= MAX_GRAPH_STEPS:
            self._record('decide', stop='step_budget_exhausted')
            return {'stop_reason': 'action_budget_exhausted', 'stage': 'finalize'}
        control = self.chat.control_state()
        legal = list(allowed_actions(control))
        self._record('decide', legal_actions=legal, candidates=control.candidates,
                     stale=control.stale, gaps=len(control.gaps),
                     budget={'llm_left': control.budget.llm_left,
                             'searches_left': control.budget.searches_left,
                             'seconds_left': round(control.budget.seconds_left, 1)})
        try:
            decision, stop = self.chat.next_decision()
        except GuideError as exc:
            if self.chat.cancel_requested:
                self._record('decide', stop='user_cancelled')
            else:
                self.chat._warnings.append(str(exc))
                self._record('decide', error=str(exc))
            return {'error': str(exc), 'stop_reason': self.chat._error_stop_reason(exc),
                    'stage': 'finalize'}
        if decision is None:
            self._record('decide', stop=stop)
            return {'stop_reason': stop, 'stage': 'finalize'}
        self._record('decide', action=decision.action, reason=decision.reason,
                     accepted=decision.accepted, attempted=decision.attempted,
                     error=decision.error, fallback=decision.fallback)
        return {'decision': asdict(decision), 'stage': decision.action.lower(),
                'step': step + 1}

    def retrieve(self, state: TurnState) -> TurnState:
        decision = self._decision(state)
        self._record('retrieve', query=decision.args.get('query', ''),
                     tool='search', filters=self.chat.task().get('venue') or None)
        try:
            self.chat.apply_decision(decision)
        except GuideError as exc:
            self._record('retrieve', **({'stop': 'user_cancelled'} if self.chat.cancel_requested
                                       else {'error': str(exc)}))
            return {'error': str(exc), 'stop_reason': self.chat._error_stop_reason(exc),
                    'stage': 'finalize'}
        if self.chat._native_operation_open():
            return {'stop_reason': 'pending_operation_not_replayed', 'stage': 'finalize'}
        return {'stage': 'critic'}

    def assess(self, state: TurnState) -> TurnState:
        decision = self._decision(state)
        self._record('assess', paper_ids=list(decision.args.get('paper_ids') or []))
        try:
            self.chat.apply_decision(decision)
        except GuideError as exc:
            self._record('assess', **({'stop': 'user_cancelled'} if self.chat.cancel_requested
                                     else {'error': str(exc)}))
            return {'error': str(exc), 'stop_reason': self.chat._error_stop_reason(exc),
                    'stage': 'finalize'}
        if self.chat._native_operation_open():
            return {'stop_reason': 'pending_operation_not_replayed', 'stage': 'finalize'}
        return {'stage': 'critic'}

    def critic(self, state: TurnState) -> TurnState:
        if self.chat.cancel_requested:
            return {'stop_reason': 'user_cancelled', 'stage': 'finalize', 'decision': {}}
        if state.get('stop_reason'):
            return {'stage': 'finalize', 'decision': {}}
        control = self.chat.control_state()
        gaps = self.chat.gaps()
        self._record('critic', gaps=gaps, candidates=control.candidates,
                     stale=control.stale, unassessed=control.unassessed)
        # Do not reflect before the candidates gathered by this action have been
        # assessed; otherwise every first search would burn a reflection call.
        if control.unassessed or control.stale:
            return {'stage': 'decide', 'decision': {}}
        # One bounded reflection: only after an execution step, only with a gap,
        # and only if it produces a genuinely new query. The controller still owns
        # the normal budget/action gate.
        limits = self.chat.limits()
        can_reflect = ('SEARCH' in allowed_actions(control)
                       and limits['actions_left'] > 0 and limits['llm_left'] > 0
                       and limits['searches_left'] > 0
                       and limits['searches_left_phase'] > 0
                       and limits['candidates_left'] > 0
                       and int(state.get('step') or 0) < MAX_GRAPH_STEPS)
        if gaps and not state.get('reflected') and can_reflect:
            previous = {row['query'].casefold() for row in self.chat.store.query_history(self.chat.session_id)}
            try:
                query = self.chat._refine_query()
            except Exception as exc:  # reflection must never abort a turn
                self._record('critic', reflection='error', error=f'{type(exc).__name__}: {exc}')
                query = ''
            if query and query.casefold() not in previous:
                # Refinement itself can exhaust the time/request budget. Recheck
                # the action gate after that call, and charge this search as an
                # action just like one proposed by the normal controller.
                current = self.chat.control_state()
                if 'SEARCH' in allowed_actions(current) and self.chat.limits()['actions_left'] > 0:
                    decision = Decision('SEARCH', {'query': query}, True, 'critic reflection')
                    if getattr(self.chat.config, 'action_protocol', 'json') == 'responses_native':
                        self.chat._authorize_program_decision(decision)
                    with self.chat.store.tx():
                        self.chat._consume(actions_left=1)
                        record_decision(self.chat.store, self.chat.session_id, decision,
                                        revision=self.chat.revision)
                    self._record('critic', reflection='new_query', query=query)
                    return {'decision': asdict(decision), 'reflected': True,
                            'step': int(state.get('step') or 0) + 1, 'stage': 'retrieve'}
        return {'stage': 'decide', 'decision': {}}

    def verifier(self, state: TurnState) -> TurnState:
        decision = self._decision(state)
        self._record('verifier', action=decision.action)
        try:
            result = self.chat.apply_decision(decision)
        except GuideError as exc:
            self._record('verifier', **({'stop': 'user_cancelled'} if self.chat.cancel_requested
                                       else {'error': str(exc)}))
            return {'error': str(exc), 'stop_reason': self.chat._error_stop_reason(exc),
                    'stage': 'finalize'}
        if result is not None:
            return {'result': result, 'stage': 'finalize'}
        return {'stage': 'decide'}

    def finalize(self, state: TurnState) -> TurnState:
        result = state.get('result')
        self._record('finalize', status=(result or {}).get('status') if isinstance(result, dict) else None)
        if result is not None:
            return {'result': result, 'stage': 'finalize'}
        reason = str(state.get('stop_reason') or 'action_budget_exhausted')
        self._record('finalize', reason=reason)
        try:
            result = self.chat._partial(reason)
        except GuideError as exc:
            self._record('finalize', error=str(exc))
            result = self.chat._partial('model_or_tool_error')
        return {'result': result, 'stage': 'finalize'}

    # -- routing ----------------------------------------------------------
    @staticmethod
    def _after_planner(state: TurnState) -> str:
        return 'finalize' if state.get('result') is not None else 'decide'

    @staticmethod
    def _after_decide(state: TurnState) -> str:
        if state.get('result') is not None or state.get('stop_reason'):
            return 'finalize'
        action = (state.get('decision') or {}).get('action')
        if action == 'SEARCH':
            return 'retrieve'
        if action == 'ASSESS':
            return 'assess'
        if action in ('ASK', 'FINISH'):
            return 'verifier'
        return 'finalize'

    @staticmethod
    def _after_verifier(state: TurnState) -> str:
        return 'finalize' if state.get('result') is not None else 'decide'

    @staticmethod
    def _after_critic(state: TurnState) -> str:
        if state.get('stop_reason'):
            return 'finalize'
        return 'retrieve' if state.get('decision') and (state.get('decision') or {}).get('action') == 'SEARCH' else 'decide'

    # -- execution --------------------------------------------------------
    def run_turn(self) -> dict[str, Any]:
        if self.chat._native_operation_open():
            return self.chat._partial('pending_operation_not_replayed')
        checkpoint = Path(self.chat.store.path).with_name('agent-checkpoints.sqlite3')
        conn = sqlite3.connect(checkpoint, check_same_thread=False)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            app = self._build_with_saver(conn)
            thread = f'{self.chat.session_id}:{self.chat.revision}'
            self.last_thread = thread
            config = {'configurable': {'thread_id': thread}, 'recursion_limit': 80}
            state: TurnState = {'session_id': self.chat.session_id,
                                'revision': self.chat.revision, 'step': 0,
                                'result': None, 'stage': 'planner'}
            existing = app.get_state(config)
            values = getattr(existing, 'values', None) or {}
            if (getattr(self.chat.config, 'action_protocol', 'json') == 'responses_native'
                    and getattr(existing, 'next', ())):
                self.last_resumed = True
                self._record('checkpoint', resumed='pending_not_replayed', thread=thread,
                             next=list(existing.next))
                return self.chat._partial('pending_operation_not_replayed')
            if values.get('result') is not None:
                self.last_resumed = True
                self._record('checkpoint', resumed='complete', thread=thread)
                return values['result']
            if getattr(existing, 'next', ()):  # interrupted/pending node
                self.last_resumed = True
                pending = list(existing.next)
                nodes = [event for event in self.chat.store.events(self.chat.session_id)
                         if event['action'] == 'agent_node']
                last_node = (nodes[-1]['payload'].get('node') if nodes else '')
                if last_node in pending:
                    # A node was entered; its external outcome cannot be inferred
                    # from the graph checkpoint, even if a socket returned.
                    self._record('checkpoint', resumed='pending_not_replayed', thread=thread, next=pending)
                    return self.chat._partial('pending_operation_not_replayed')
                self._record('checkpoint', resumed='pending_not_started', thread=thread, next=pending)
                out = app.invoke(None, config)
            else:
                out = app.invoke(state, config)
            result = out.get('result') if isinstance(out, dict) else None
            if isinstance(result, dict):
                return result
            self.chat._warnings.append('LangGraph completed without a result; partial delivery.')
            return self.chat._partial('model_or_tool_error')
        except Exception as exc:  # noqa: BLE001 - graph failure must not lose the turn
            if self.chat.cancel_requested:
                return self.chat._partial('user_cancelled')
            message = f'LangGraph runtime failed: {type(exc).__name__}: {exc}'
            self.chat._warnings.append(message)
            self.chat._event('error', {'error': message, 'type': type(exc).__name__})
            return self.chat._partial('model_or_tool_error')
        finally:
            conn.close()

    def _build_with_saver(self, conn):
        graph = StateGraph(TurnState)
        graph.add_node('planner', self.planner)
        graph.add_node('decide', self.decide)
        graph.add_node('retrieve', self.retrieve)
        graph.add_node('assess', self.assess)
        graph.add_node('critic', self.critic)
        graph.add_node('verifier', self.verifier)
        graph.add_node('finalize', self.finalize)
        graph.add_edge(START, 'planner')
        graph.add_conditional_edges('planner', self._after_planner,
                                    {'decide': 'decide', 'finalize': 'finalize'})
        graph.add_conditional_edges('decide', self._after_decide,
                                    {'retrieve': 'retrieve', 'assess': 'assess',
                                     'verifier': 'verifier', 'finalize': 'finalize'})
        graph.add_edge('retrieve', 'critic')
        graph.add_edge('assess', 'critic')
        graph.add_conditional_edges('critic', self._after_critic,
                                    {'retrieve': 'retrieve', 'decide': 'decide',
                                     'finalize': 'finalize'})
        graph.add_conditional_edges('verifier', self._after_verifier,
                                    {'decide': 'decide', 'finalize': 'finalize'})
        graph.add_edge('finalize', END)
        return graph.compile(checkpointer=SqliteSaver(conn))
