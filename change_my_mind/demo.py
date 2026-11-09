"""Deterministic synthetic scenario. No network, billing, or real-world findings."""

from .evidence import Page
from .providers import ModelResult
from .schemas import (
    ArgumentDraft,
    Challenge,
    ChallengeResponse,
    CheckBatch,
    CheckItem,
    ClaimDraft,
    ConsistencyReview,
    CoverageReview,
    Dispute,
    Finding,
    Frame,
    Investigation,
    Judgment,
    Recommendation,
    ResearchPlan,
    SearchHit,
    SearchResults,
    Submission,
    SuiteComparison,
)

DEMO_QUESTION = "Should our city library extend its evening opening hours?"
CORPUS = {
    "https://fixtures.changemymind.test/pilot": (
        "Fictional evening-hours pilot",
        """SYNTHETIC DEMONSTRATION DATA — not a real study.

The fictional library pilot recorded 1,200 additional visits across twelve evening sessions. This is an aggregate total, not 1,200 visits per evening. No randomized comparison group was used.

The pilot could establish observed attendance, but could not establish whether a permanent extension would cause the same increase. The report does not include staffing costs.
""",
    ),
    "https://fixtures.changemymind.test/finance": (
        "Fictional staffing and access memo",
        """SYNTHETIC DEMONSTRATION DATA — not a real financial record.

The fictional pilot incurred $9,600 in additional staffing costs across twelve evening sessions. These are staffing costs only, not a complete economic evaluation.

The last connecting bus departs at 20:00. Staffing availability, disability access, safety, and the distribution of benefits should be assessed before expansion.
""",
    ),
    "https://fixtures.changemymind.test/survey": (
        "Fictional visitor survey",
        """SYNTHETIC DEMONSTRATION DATA — not a real survey.

Of 100 evening visitors surveyed, 80 favored extending library opening hours. Respondents were recruited inside the library during the pilot, so this is not a representative sample of city residents.

The local newsletter repeats the pilot's 1,200 visits figure. It is dependent coverage of the same pilot, not independent corroboration.
""",
    ),
}


class FixtureFetcher:
    async def fetch(self, url):
        title, text = CORPUS[url]
        return Page(url, title, text.encode(), [("synthetic scenario", text)], "text/plain", True)


class DemoProvider:
    async def discover(self, query, config):
        return ModelResult(
            SearchResults(
                hits=[
                    SearchHit(url=url, title=title, description="Synthetic fixture")
                    for url, (title, _) in CORPUS.items()
                ]
            )
        )

    async def generate(self, role, context, schema, config):
        records = context.get("records", [])
        passages = [r for r in records if r["kind"] == "passage"]
        claims = [r for r in records if r["kind"] == "claim" and not r.get("superseded")]
        team = context.get("team")

        def passage_for(term):
            return next((p["id"] for p in passages if term in p["data"]["quote"]), None)

        if schema is Frame:
            a, b = (
                "Extend evening access through a carefully evaluated pilot.",
                "Prioritize affordability and stronger evidence before permanent expansion.",
            )
            if context.get("variant") == "reverse":
                a, b = b, a
            output = Frame(
                working_question=DEMO_QUESTION,
                position_a=a,
                position_b=b,
                definitions=["Synthetic library scenario; all numbers and documents are fictional."],
                issues=[
                    "Attendance and causal attribution",
                    "Staffing cost",
                    "Representativeness",
                    "Access and reversibility",
                ],
                principles=[
                    "Equitable access",
                    "Responsible use of limited funds",
                    "Evidence proportional to commitment",
                ],
                assumptions=["The intended actor is a library administrator considering a reversible pilot."],
                overlooked_perspectives=["Residents who cannot reach the library in the evening"],
                clarification_needed=None,
            )
        elif schema is ResearchPlan:
            output = ResearchPlan(
                strategy="Inspect the synthetic pilot, survey, and costs; actively test the assigned position.",
                tracks=[
                    {
                        "track": track,
                        "search_queries": [
                            f"Fictional library {track} primary evidence",
                            f"Fictional library {track} contrary findings",
                        ],
                        "evidence_standard": "Use the original fictional records; do not infer causal effects.",
                        "pitfalls": [
                            "Aggregate denominator",
                            "Unrepresentative survey",
                            "Repeated pilot coverage",
                        ],
                    }
                    for track in ["background", "competing explanations", "consequences"]
                ],
                decisive_tests=[
                    "Would observed attendance justify a reversible pilot given unknown generalizability?"
                ],
                stop_criteria=[
                    "All fixture limitations are recorded; extra fixture searches add no evidence."
                ],
            )
        elif schema is Submission:
            stage, track = context.get("stage"), context.get("track", "")
            proposed = []
            existing = [
                c["id"] for c in claims if c.get("case") == team and c["data"].get("status") != "contradicted"
            ][:3]
            if track:
                if track == "background" and team == "A":
                    text, term = (
                        "The fictional pilot recorded 1,200 additional visits per evening.",
                        "aggregate total",
                    )
                elif track == "competing explanations":
                    text, term = (
                        "80 of 100 surveyed evening visitors favored an extension; the sample does not represent all residents.",
                        "80 favored",
                    )
                elif track == "consequences":
                    text, term = (
                        "The fictional pilot incurred $9,600 in additional staffing costs across twelve evening sessions.",
                        "$9,600",
                    )
                else:
                    text, term = (
                        "The fictional pilot recorded 1,200 additional visits across twelve evening sessions.",
                        "aggregate total",
                    )
                pid = passage_for(term)
                if pid:
                    proposed = [
                        ClaimDraft(
                            text=text,
                            type="factual",
                            scope="Fictional twelve-session local pilot; synthetic demonstration only.",
                            supporting_passage_ids=[pid],
                            conflicting_passage_ids=[],
                            limitations=[
                                "Synthetic example; no causal comparison and no generalization to real libraries."
                            ],
                            underlying_evidence_ids=[
                                "fictional-pilot-1" if term != "80 favored" else "fictional-survey-1"
                            ],
                            decisive=True,
                        )
                    ]
            elif stage == "openings" and team == "A":
                rejected = next(
                    (
                        c
                        for c in claims
                        if c.get("case") == team
                        and "per evening" in c["data"]["text"]
                        and c["data"].get("status") == "contradicted"
                    ),
                    None,
                )
                pid = passage_for("aggregate total")
                if rejected and pid:
                    proposed = [
                        ClaimDraft(
                            text="The fictional pilot recorded 1,200 additional visits across twelve evening sessions.",
                            type="factual",
                            scope="Fictional twelve-session pilot, aggregate visits.",
                            supporting_passage_ids=[pid],
                            conflicting_passage_ids=[],
                            limitations=["No randomized comparison; attendance is not a causal estimate."],
                            underlying_evidence_ids=["fictional-pilot-1"],
                            decisive=True,
                            revises_claim_id=rejected["id"],
                        )
                    ]
            objections = []
            if stage in {"openings", "responses"}:
                target = next(
                    (
                        c
                        for c in claims
                        if c.get("case") and c["case"] != team and c["data"].get("status") == "supported"
                    ),
                    None,
                )
                if target:
                    objections = [
                        Challenge(
                            target_id=target["id"],
                            objection="Explain how the pilot's scope and sampling limitations constrain this argument.",
                        )
                    ]
            output = Submission(
                synthesis="Synthetic assessment: observed use supports a reversible experiment, while limited causal evidence, staffing costs, and access constraints weaken the case for an immediate permanent expansion.",
                claims=proposed,
                arguments=[
                    ArgumentDraft(
                        conclusion="A bounded pilot can balance access and evidence gathering."
                        if team == "A"
                        else "Permanent expansion needs stronger evidence and an explicit staffing plan.",
                        claim_indexes=list(range(len(proposed))),
                        existing_claim_ids=existing,
                        assumptions=[
                            "Value equitable access and avoid irreversible commitments on weak evidence."
                        ],
                    )
                ],
                objections=objections,
                responses=[
                    ChallengeResponse(
                        objection_id=i,
                        disposition="qualify",
                        explanation="Concede the scope limitation: the pilot is observational and the visitor sample is not representative. Narrow the conclusion to a reversible test.",
                    )
                    for i in context.get("required_objections", [])
                ],
                strongest_opposing_points=[
                    "Attendance alone cannot demonstrate net social benefit.",
                    "Costs and access differ across residents.",
                ],
                gaps=["No credible counterfactual for attendance", "User priorities are unspecified"],
                next_queries=[],
                stop_reason="adequate_coverage",
            )
        elif schema is CheckBatch:
            evidence = context.get("passages", [])
            checks = []
            for claim in context["claims"]:
                bad = "per evening" in claim["text"]
                terms = (
                    ["aggregate total"]
                    if bad or "1,200" in claim["text"]
                    else ["$9,600"]
                    if "$9,600" in claim["text"]
                    else ["80 favored"]
                )
                pids = [p["id"] for p in evidence if any(term in p["quote"] for term in terms)]
                normative = claim["type"] == "normative"
                status = (
                    "not_fact_checkable"
                    if normative
                    else "contradicted"
                    if bad and pids
                    else "supported"
                    if pids
                    else "insufficient_evidence"
                )
                checks.append(
                    CheckItem(
                        claim_id=claim["id"],
                        status=status,
                        explanation="The source reports an aggregate over twelve sessions, not a per-evening count."
                        if bad
                        else "Supported only within the explicitly fictional scope; this does not establish causation or population representativeness.",
                        passage_ids=pids[:2],
                        citation_support="not_applicable"
                        if normative
                        else "contradicts"
                        if bad
                        else "supports"
                        if pids
                        else "unverified",
                        methodological_reliability="not_applicable" if normative else "mixed",
                        normative_consistency="Value premise requires consistency assessment"
                        if normative
                        else None,
                        corrected_text="1,200 additional visits across twelve evening sessions."
                        if bad
                        else None,
                    )
                )
            output = CheckBatch(checks=checks)
        elif schema is CoverageReview:
            output = CoverageReview(
                synthesis="Both sides need to account for residents excluded by evening transport schedules.",
                missing_questions=["Who cannot use the extended hours?"],
                added_perspectives=["Non-visitors and transport-dependent residents"],
                research_queries=[],
            )
        elif schema is Investigation:
            previous = [r for r in records if r["kind"] == "investigation"]
            output = Investigation(
                disputes=[]
                if previous
                else [
                    Dispute(
                        question="Does observational attendance justify a permanent expansion?",
                        type="causal",
                        target_ids=[c["id"] for c in claims[:2]],
                        resolution_test="A comparable, prospectively evaluated pilot with explicit costs and access measures.",
                        research_query="fictional library pilot counterfactual evaluation limits",
                        expected_value="high",
                    )
                ],
                stop_reason="The next useful evidence requires a real evaluation; the synthetic source set cannot resolve causality.",
            )
        elif schema is Judgment:
            unique = {}
            for claim in claims:
                if claim["data"].get("status") == "supported":
                    unique.setdefault(claim["data"]["text"], claim)
            selected = list(unique.values())[:3]
            output = Judgment(
                winner="hybrid",
                best_supported_position="Run a limited evening-hours pilot with explicit staffing and access measures before deciding on permanent expansion.",
                margin="narrow",
                confidence="medium",
                provisional=True,
                findings=[
                    Finding(
                        text=c["data"]["text"],
                        claim_ids=[c["id"]],
                        assumptions=["Observed utilization matters, but cannot alone establish net benefit."],
                        uncertainty="Synthetic, observational example; shared evidence is not independent corroboration.",
                        decisive=True,
                    )
                    for c in selected
                ],
                opposing_points=[
                    "The cautionary case establishes that costs and non-representative sampling constrain the conclusion."
                ],
                rejected_claim_ids=list(
                    dict.fromkeys(
                        [c["id"] for c in claims if c["data"].get("status") == "contradicted"]
                        + [
                            c["id"]
                            for c in context.get("correction_history", [])
                            if c.get("status") == "contradicted"
                        ]
                    )
                ),
                value_assumptions=["Prefer reversible learning while protecting access and limited funds."],
                alternative_assumptions=[
                    "A strict short-term cost priority could favor maintaining existing hours."
                ],
                reversal_conditions=[
                    "Representative demand and credible cost-effectiveness evidence could support a permanent extension."
                ],
                new_claims=[],
            )
        elif schema is Recommendation:
            findings = [r for r in records if r["kind"] == "finding"]
            output = Recommendation(
                insight="Make the next decision a reversible test with a pre-agreed review rule.",
                actor="Library administrator (fictional demonstration)",
                next_step="Specify a short evening-hours pilot, a staffing allowance, and an access assessment; review results before committing permanently.",
                supporting_finding_ids=[r["id"] for r in findings],
                claim_ids=[],
                costs_risks_tradeoffs=[
                    "Staffing effort",
                    "Possible exclusion of transport-dependent residents",
                    "Opportunity cost",
                ],
                reversibility="Retain an explicit end date and return to existing hours if the review rule is not met.",
                success_measure="Incremental use and equitable access relative to staffing expenditure.",
                review_trigger="Review at the pilot's agreed end date or earlier if staffing or access problems arise.",
                change_conditions=[
                    "Unmanageable costs",
                    "Little incremental use",
                    "Evidence of unequal access",
                ],
                conditional_on_goals="Appropriate if the decision-maker values access and learning within a bounded budget.",
                new_claims=[],
            )
        elif schema is ConsistencyReview:
            output = ConsistencyReview(consistent=True, issues=[], recommendation_consistent=True)
        elif schema is SuiteComparison:
            output = SuiteComparison(
                stability="stable",
                explanation="In this deterministic synthetic scenario, the variants retain the recommendation for a reversible pilot. This is not empirical validation of real-world accuracy.",
                changes=[],
                value_sensitivity="A strict short-term cost priority could favor existing hours; that conditional disagreement remains explicit.",
                limitations=[
                    "Synthetic fixtures",
                    "Shared model family",
                    "The value variant reuses the original evidence",
                    "Repeated agreement is not independent corroboration",
                ],
            )
        else:
            raise ValueError(f"Unsupported synthetic schema: {schema.__name__}")
        return ModelResult(output)
