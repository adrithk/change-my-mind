from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    provider: Literal["chatgpt", "openai", "demo"] = "chatgpt"
    model: str = "gpt-6.1-sol"
    reasoning: Literal["low", "medium", "high"] = "medium"
    max_output_tokens: int = Field(default=6000, ge=1000, le=12000)


class RunCreate(StrictModel):
    question: str = Field(min_length=5, max_length=6000)
    model_config_: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    budget_usd: float = Field(default=15, ge=0.50, le=15)
    mode: Literal["full", "single", "synthesis", "reduced"] = "full"
    request_limit: int = Field(default=80, ge=20, le=120)


class SuiteCreate(StrictModel):
    budget_usd: float = Field(default=15, ge=0.50, le=15)
    request_limit: int = Field(default=180, ge=20, le=360)


class ChatGPTLogin(StrictModel):
    profile_id: str | None = None


class ChatGPTProfile(StrictModel):
    profile_id: str


class ClarificationRequest(StrictModel):
    answer: str = Field(min_length=1, max_length=4000)


class EvidenceRequest(StrictModel):
    source_id: str
    query: str


class AgentOutput(StrictModel):
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)


class Frame(AgentOutput):
    working_question: str
    position_a: str
    position_b: str
    definitions: list[str]
    issues: list[str]
    principles: list[str]
    assumptions: list[str]
    overlooked_perspectives: list[str]
    clarification_needed: str | None


class ClaimDraft(StrictModel):
    text: str
    type: Literal["factual", "causal", "predictive", "interpretive", "normative"]
    scope: str
    supporting_passage_ids: list[str]
    conflicting_passage_ids: list[str]
    limitations: list[str]
    underlying_evidence_ids: list[str]
    decisive: bool
    revises_claim_id: str | None = None


class ArgumentDraft(StrictModel):
    conclusion: str
    claim_indexes: list[int]
    existing_claim_ids: list[str]
    assumptions: list[str]


class Challenge(StrictModel):
    target_id: str
    objection: str


class ChallengeResponse(StrictModel):
    objection_id: str
    disposition: Literal["concede", "qualify", "contest"]
    explanation: str


class Submission(AgentOutput):
    synthesis: str
    claims: list[ClaimDraft]
    arguments: list[ArgumentDraft]
    objections: list[Challenge]
    responses: list[ChallengeResponse]
    strongest_opposing_points: list[str]
    gaps: list[str]
    next_queries: list[str]
    stop_reason: Literal["adequate_coverage", "diminishing_returns", "continue", "budget_limit"]
    framing_challenges: list[str] = Field(default_factory=list)


class CoverageReview(AgentOutput):
    synthesis: str
    missing_questions: list[str]
    added_perspectives: list[str]
    research_queries: list[str]


CheckStatus = Literal[
    "supported", "contradicted", "partly_supported", "insufficient_evidence", "not_fact_checkable"
]


class CheckItem(StrictModel):
    claim_id: str
    status: CheckStatus
    explanation: str
    passage_ids: list[str]
    citation_support: Literal["supports", "contradicts", "partial", "unverified", "not_applicable"]
    methodological_reliability: Literal["strong", "mixed", "weak", "unknown", "not_applicable"]
    normative_consistency: str | None
    corrected_text: str | None


class CheckBatch(AgentOutput):
    checks: list[CheckItem]


class Dispute(StrictModel):
    question: str
    type: Literal["factual", "causal", "predictive", "interpretive", "normative"]
    target_ids: list[str]
    resolution_test: str
    research_query: str | None
    expected_value: Literal["high", "low"]


class Investigation(AgentOutput):
    disputes: list[Dispute]
    stop_reason: str


class Finding(StrictModel):
    text: str
    claim_ids: list[str]
    assumptions: list[str]
    uncertainty: str
    decisive: bool


class Judgment(AgentOutput):
    winner: Literal["A", "B", "hybrid", "alternative"]
    best_supported_position: str
    margin: Literal["narrow", "moderate", "substantial", "indeterminate"]
    confidence: Literal["low", "medium", "high"]
    provisional: bool
    findings: list[Finding]
    opposing_points: list[str]
    rejected_claim_ids: list[str]
    value_assumptions: list[str]
    alternative_assumptions: list[str]
    reversal_conditions: list[str]
    new_claims: list[ClaimDraft]


class Recommendation(AgentOutput):
    insight: str
    actor: str
    next_step: str
    supporting_finding_ids: list[str]
    claim_ids: list[str]
    costs_risks_tradeoffs: list[str]
    reversibility: str
    success_measure: str
    review_trigger: str
    change_conditions: list[str]
    conditional_on_goals: str
    new_claims: list[ClaimDraft]


class ConsistencyReview(AgentOutput):
    consistent: bool
    issues: list[str]
    recommendation_consistent: bool


class SuiteComparison(AgentOutput):
    stability: Literal["stable", "sensitive", "incomplete"]
    explanation: str
    changes: list[str]
    value_sensitivity: str
    limitations: list[str]


class SearchHit(StrictModel):
    url: str
    title: str
    description: str = ""


class SearchResults(StrictModel):
    hits: list[SearchHit]


class RecordView(StrictModel):
    id: str
    kind: str
    key: str
    stage: str
    team: str | None
    visibility: str
    stale: bool
    superseded: bool
    version: int
    logical_id: str
    data: dict[str, Any]
    created_at: str


class RunView(StrictModel):
    id: str
    question: str
    status: str
    stage: str
    config: dict[str, Any]
    budget_usd: float
    cost_usd: float
    reserved_usd: float
    requests_used: int = 0
    created_at: str
    updated_at: str
    error: str | None
    records: list[RecordView] = Field(default_factory=list)
    suites: list[dict[str, Any]] = Field(default_factory=list)


class EventView(StrictModel):
    id: int
    run_id: str
    type: str
    data: dict[str, Any]
    created_at: str
