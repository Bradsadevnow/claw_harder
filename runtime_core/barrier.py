from __future__ import annotations

import uuid
from dataclasses import dataclass
from time import time


@dataclass(frozen=True)
class PlannedTool:
    name: str
    args: dict


@dataclass(frozen=True)
class ExecutionPlan:
    batch_id: str
    tools: tuple  # tuple[PlannedTool, ...]
    estimated_cost: float
    contract_verdict: str  # "ALLOW" | "DENY" | "NUKE_TRIGGERED"
    created_at: float

    @staticmethod
    def from_tool_plans(plans: list, estimated_cost: float, contract_verdict: str) -> "ExecutionPlan":
        return ExecutionPlan(
            batch_id=str(uuid.uuid4()),
            tools=tuple(
                PlannedTool(p.call.name, dict(p.call.arguments))
                for p in plans
            ),
            estimated_cost=estimated_cost,
            contract_verdict=contract_verdict,
            created_at=time(),
        )


class ExecutionToken:
    __slots__ = ("token_id",)

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id


class PrecommitBarrier:
    """
    Single-use execution validator. No token → no execution.

    Invariants:
    - Token is issued once per precommit.
    - Token is consumed destructively; reuse raises RuntimeError.
    - NUKE_TRIGGERED, killswitch, and budget overflow block issuance.
    """

    def __init__(self) -> None:
        self._active_tokens: dict[str, ExecutionPlan] = {}

    def precommit(
        self,
        plan: ExecutionPlan,
        context,  # duck: .killswitch_active bool, .snapshot_cost float, .sandbox_file_budget float
    ) -> tuple[bool, str | None, ExecutionToken | None]:
        if plan.contract_verdict == "NUKE_TRIGGERED":
            return False, "nuke_triggered", None

        if context.killswitch_active:
            return False, "killswitch_active", None

        total_cost = plan.estimated_cost + context.snapshot_cost
        if total_cost > context.sandbox_file_budget:
            return False, "budget_exceeded", None

        token_id = str(uuid.uuid4())
        token = ExecutionToken(token_id)
        self._active_tokens[token_id] = plan
        return True, None, token

    def consume(self, token: ExecutionToken) -> ExecutionPlan:
        plan = self._active_tokens.pop(token.token_id, None)
        if plan is None:
            raise RuntimeError("Invalid or already-consumed token.")
        return plan
