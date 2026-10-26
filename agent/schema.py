"""Structured output schema for one Nemotron step.

Each model turn must be exactly one `AgentStep`: either a `QueryStep` (run SQL through the
cutoff-bound tool) or a `DecisionStep` (commit to orders). The two are discriminated on the
`type` field. The policy:

  * sends `AgentStep.model_json_schema()` to the endpoint as guided_json (so the server
    constrains generation to this shape when it supports it), and
  * validates the model's `content` with `AgentStep.model_validate_json(...)`.

The model only supplies `symbol / side / quantity` per order; the runner-side contract builds
the full, official order (order_id, execute_at, order_type, limit_price) deterministically.
Imports no simulator and no model code.
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Union

from pydantic import BaseModel, Field, RootModel


class QueryStep(BaseModel):
    type: Literal["query"]
    goal: str = Field(description="Why this query helps the decision")
    sql: str = Field(description="A single SELECT/WITH over tables `prices` and `news`")


class OrderIntent(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, description="Positive integer share count")


class DecisionStep(BaseModel):
    type: Literal["decision"]
    orders: List[OrderIntent] = Field(default_factory=list, description="Empty list means hold")
    reason: str
    evidence_query_ids: List[str] = Field(default_factory=list)


AgentStepUnion = Annotated[Union[QueryStep, DecisionStep], Field(discriminator="type")]


class AgentStep(RootModel[AgentStepUnion]):
    """One model turn: a QueryStep or a DecisionStep (discriminated on `type`)."""
