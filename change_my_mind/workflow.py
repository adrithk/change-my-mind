import asyncio
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .config import STAGES
from .errors import BudgetExceeded, EvidenceError, NeedsReview
from .evidence import Evidence
from .providers import Gateway
from .schemas import (
    CheckBatch,
    ConsistencyReview,
    CoverageReview,
    Frame,
    Investigation,
    Judgment,
    Recommendation,
    ResearchPlan,
    Submission,
)
from .store import Store


class State(TypedDict):
    run_id: str


class Workflow:
    def __init__(self, store: Store, gateway: Gateway, evidence: Evidence):
        self.store, self.gateway, self.evidence = store, gateway, evidence

    async def current_frame(self, run_id):
        frames = await self.store.records(run_id, audience="observer", kinds=["frame"])
        return frames[-1] if frames else None

    async def context(self, run_id, *, team=None, role=None, exclude_stage=None):
        run = await self.store.get_run(run_id, records=False)
        rows = await self.store.records(run_id, audience=team or "observer")
        rows = [
            r
            for r in rows
            if r.kind not in {"invocation", "snapshot"} and r.key != "question" and r.stage != exclude_stage
        ]
        claims = [r for r in rows if r.kind == "claim" and not r.superseded]
        claims.sort(key=lambda r: (not r.data.get("decisive"), r.created_at))
        sources = [r for r in rows if r.kind == "source"]
        passages = await self.evidence.relevant_passages(run_id, run.question, team or "observer", limit=48)
        selected = claims[:100] + sources[:60] + passages
        for kind in [
            "frame",
            "research_agenda",
            "briefing",
            "coverage",
            "submission",
            "argument",
            "objection",
            "response",
            "check",
            "finding",
            "judgment",
            "recommendation",
            "limitation",
            "investigation",
        ]:
            selected += [r for r in rows if r.kind == kind][-50:]
        items = []
        for record in selected:
            data = dict(record.data)
            data.pop("raw_path", None)
            data.pop("text_path", None)
            data.pop("sections_path", None)
            if record.kind in {"submission", "briefing"} and record.stale:
                data["synthesis"] = (
                    "Superseded working synthesis: reconsider using current claims and checks below. Original remains in the audit record."
                )
            if role == "judge":
                data.pop("origin", None)
                data.pop("speaker", None)
            items.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "case": record.team,
                    "stale": record.stale,
                    "superseded": record.superseded,
                    "data": data,
                }
            )
        responded = {r.data["objection_id"] for r in rows if r.kind == "response" and r.team == team}
        own_ids = {r.id for r in rows if r.team == team and r.kind in {"claim", "argument"}}
        required = [
            r.id
            for r in rows
            if r.kind == "objection" and r.data["target_id"] in own_ids and r.id not in responded
        ]
        current_frame = await self.current_frame(run_id)
        presentation_stage = exclude_stage or run.stage
        round_index = STAGES.index(presentation_stage) if presentation_stage in STAGES else 0
        case_order = ["A", "B"] if round_index % 2 == 0 else ["B", "A"]
        if run.config.get("variant") == "reverse":
            case_order.reverse()
        # Order is controlled by the application, independent of which API request finishes first.
        items.sort(key=lambda item: case_order.index(item["case"]) + 1 if item["case"] in case_order else 0)
        return {
            "original_question": run.question,
            "team": team,
            "stage": exclude_stage or run.stage,
            "records": items,
            "record_ids": [r["id"] for r in items],
            "required_objections": required,
            "mode": run.config["mode"],
            "variant": run.config.get("variant", "standard"),
            "evidence_omitted": max(0, len(rows) - len(selected)),
            "retrieval_instructions": "Use evidence_requests with a visible source_id and query to inspect omitted source passages.",
            "synthetic": run.config["model_config"]["provider"] == "demo",
            "current_framing": current_frame.data if current_frame else None,
            "case_presentation_order": case_order,
            "correction_history": [
                {
                    "id": r.id,
                    "text": r.data["text"],
                    "status": r.data.get("status"),
                    "superseded": r.superseded,
                }
                for r in rows
                if r.kind == "claim" and (r.superseded or r.data.get("status") == "contradicted")
            ],
        }

    async def invoke(self, run_id, key, role, context, schema, *, team=None, final=False):
        for attempt in range(3):
            output = await self.gateway.invoke(
                run_id, f"{key}:read:{attempt}", role, context, schema, team=team, final=final
            )
            if not output.evidence_requests:
                return output
            if schema in {Frame, ResearchPlan}:
                # These stages precede retrieval. Source requests are questions for research,
                # not a reason to replay an already-complete framing/planning response.
                await self.store.put(
                    run_id,
                    "research_agenda",
                    key,
                    {
                        "queries": [
                            request.query for request in output.evidence_requests if request.query.strip()
                        ],
                        "origin_role": role,
                    },
                    stage="framing" if schema is Frame else "research",
                    team=team,
                    public=team is None,
                )
                return output.model_copy(update={"evidence_requests": []})
            if attempt == 2:
                raise NeedsReview(
                    "Specialist still needs source details after the bounded retrieval allowance"
                )
            extra = []
            for request in output.evidence_requests[:3]:
                passages = await self.evidence.relevant_passages(
                    run_id, request.query, team or "observer", source_id=request.source_id, limit=6
                )
                extra += [p.model_dump() for p in passages]
            context = {
                **context,
                "additional_passages": extra,
                "instruction": context.get("instruction", "")
                + "\nUse the requested evidence and produce a final output; disclose remaining gaps.",
            }
            await self.store.emit(run_id, "retrieval", {"role": role, "passages": len(extra)})

    async def execute(self, run_id):
        builder = StateGraph(State)
        for stage in STAGES[:-1]:

            async def node(state, stage=stage):
                await self.store.ensure_active(state["run_id"])
                await self.store.set_status(state["run_id"], "running", stage=stage)
                await getattr(self, stage)(state["run_id"])
                return {"run_id": state["run_id"]}

            builder.add_node(stage, node)
        builder.add_edge(START, "framing")
        builder.add_edge("framing", "research")
        builder.add_edge("research", "dossier")

        async def after_dossier(state):
            run = await self.store.get_run(state["run_id"], records=False)
            return (
                "judgment"
                if run.config["mode"] in {"single", "synthesis"} or run.config.get("reuse_parent")
                else "openings"
            )

        async def after_responses(state):
            run = await self.store.get_run(state["run_id"], records=False)
            return "closings" if run.config["mode"] == "reduced" else "rebuttals"

        builder.add_conditional_edges("dossier", after_dossier, ["judgment", "openings"])
        builder.add_edge("openings", "responses")
        builder.add_conditional_edges("responses", after_responses, ["closings", "rebuttals"])
        for before, after in [
            ("rebuttals", "follow_up"),
            ("follow_up", "closings"),
            ("closings", "judgment"),
            ("judgment", "insight"),
            ("insight", "consistency"),
        ]:
            builder.add_edge(before, after)
        builder.add_edge("consistency", END)
        async with AsyncSqliteSaver.from_conn_string(
            str(self.store.data_dir / "checkpoints.db")
        ) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}
            snapshot = await graph.aget_state(config)
            await graph.ainvoke(None if snapshot.next else {"run_id": run_id}, config, durability="sync")
        await self.store.ensure_active(run_id)
        await self.store.set_status(run_id, "completed", stage="complete")

    async def framing(self, run_id):
        existing = await self.current_frame(run_id)
        answers = await self.store.records(run_id, audience="observer", kinds=["clarification"])
        if existing and not existing.data.get("clarification_needed"):
            return
        if existing and len(answers) <= existing.data.get("clarification_count", 0):
            raise NeedsReview(f"Clarification needed: {existing.data['clarification_needed']}")
        run = await self.store.get_run(run_id, records=False)
        frame = await self.invoke(
            run_id,
            f"frame:{len(answers)}",
            "framer",
            {
                "original_question": run.question,
                "clarifications": [a.data["answer"] for a in answers],
                "variant": run.config.get("variant", "standard"),
                "instruction": "This is provisional framing BEFORE research, not a factual assessment. No sources have been retrieved yet. Map the question and working positions without establishing disputed facts. Return evidence_requests=[]; phrase source needs as issues for the research stage. clarification_needed is only for missing user intent that prevents useful progress, never for missing research evidence. For paraphrase variant, use an equivalent working question while preserving the original. For reverse variant, reverse the working side assignments.",
            },
            Frame,
        )
        await self.store.put(
            run_id,
            "frame",
            "initial" if not existing else f"revision:{len(answers)}",
            {**frame.model_dump(), "clarification_count": len(answers)},
            stage="framing",
            public=True,
            logical_id=existing.logical_id if existing else None,
            version=existing.version + 1 if existing else 1,
        )
        if frame.clarification_needed:
            raise NeedsReview(f"Clarification needed: {frame.clarification_needed}")

    async def research_task(self, run_id, team, track, stage, *, queries=None):
        key = f"{stage}:{team}:{track}"
        if await self.store.is_committed(run_id, "submission", key):
            return
        run = await self.store.get_run(run_id, records=False)
        frame = (await self.current_frame(run_id)).data
        default_query = f"{frame['working_question']} {track} original evidence systematic review contrary findings limitations"
        deep = bool(run.config.get("role_configs"))
        plan = await self.store.by_key(run_id, "research_plan", team) if deep else None
        track_plan = (
            next((item for item in plan.data["tracks"] if item["track"] == track), None) if plan else None
        )
        queries = queries or (track_plan["search_queries"][:2] if track_plan else [default_query])
        # Up to three evidence-led cycles; adequacy and diminishing returns can stop earlier.
        cycles = 3 if deep and stage == "research" else 2
        for cycle in range(cycles):
            stop = False
            for query_index, query in enumerate(queries[:2]):
                try:
                    hits = await self.gateway.search(
                        run_id,
                        f"{key}:search:{cycle}:{query_index}",
                        query,
                        team=team,
                        model_role="researcher" if deep else "search",
                    )
                    await asyncio.gather(
                        *(
                            self.evidence.retrieve(run_id, h.url, team=team, stage=stage, query=query)
                            for h in hits.hits[:3]
                        )
                    )
                except BudgetExceeded:
                    await self.store.put(
                        run_id,
                        "limitation",
                        f"{key}:budget",
                        {
                            "text": f"Team {team} research stopped at its equal budget allowance; unresolved gaps remain."
                        },
                        team=team,
                        stage=stage,
                    )
                    stop = True
                    break
            context = await self.context(run_id, team=team, exclude_stage=None)
            context.update(
                {
                    "stage": stage,
                    "track": track,
                    "assigned_position": frame[f"position_{team.lower()}"]
                    if run.config["mode"] != "single"
                    else "Neutral researcher: assess both positions and alternatives",
                    "instruction": "Build a rigorous research briefing, not a persuasive essay. Trace major claims to original studies, datasets, or authoritative reviews; inspect their methods and dates. Actively search for the strongest disconfirming evidence and plausible competing explanations. Compare effect sizes, base rates, denominators, populations, and implementation constraints. Treat repeated reports of one study as one evidence source. Separate causal evidence from associations, predictions, and values. Explain why sources deserve different weights and what evidence would reverse the conclusion. Name important access gaps. Request another cycle only when a specific unresolved question could materially change the assessment; stop on adequate coverage or diminishing material findings, not a citation count. Include scoped claims and registered passage IDs. Research limits must be disclosed.",
                    "research_plan": plan.data if plan else None,
                    "track_plan": track_plan,
                    "model_role": "researcher" if deep else "debater",
                }
            )
            tracks = ["background", "competing explanations", "consequences"]
            debater = 1 if deep else tracks.index(track) + 1 if track in tracks else 2
            role = "single_researcher" if run.config["mode"] == "single" else f"{team}_debater_{debater}"
            result = await self.invoke(run_id, f"{key}:cycle:{cycle}", role, context, Submission, team=team)
            if cycle < cycles - 1 and not stop and result.stop_reason == "continue" and result.next_queries:
                await self.commit_submission(
                    run_id, f"{key}:interim" if cycle == 0 else f"{key}:interim:{cycle}", result, team, stage
                )
                queries = result.next_queries
                continue
            if result.stop_reason == "continue":
                result.stop_reason = "budget_limit"
                result.gaps.append(
                    "The bounded research iteration allowance was reached; further requested searches were not executed."
                )
            await self.commit_submission(run_id, key, result, team, stage)
            break

    async def research(self, run_id):
        run = await self.store.get_run(run_id, records=False)
        if run.config.get("reuse_parent"):
            await self.import_parent(run_id, run.config["reuse_parent"])
            return
        teams = ["A"] if run.config["mode"] == "single" else ["A", "B"]
        if run.config.get("role_configs"):

            async def plan_research(team):
                if await self.store.by_key(run_id, "research_plan", team):
                    return
                frame = (await self.current_frame(run_id)).data
                output = await self.invoke(
                    run_id,
                    f"research_plan:{team}",
                    f"{team}_debater_1",
                    {
                        "original_question": run.question,
                        "team": team,
                        "stage": "research",
                        "current_framing": frame,
                        "research_agenda": [
                            r.data
                            for r in await self.store.records(
                                run_id, audience=team, kinds=["research_agenda"]
                            )
                        ],
                        "assigned_position": frame[f"position_{team.lower()}"]
                        if run.config["mode"] != "single"
                        else "Assess both positions neutrally",
                        "model_role": "researcher",
                        "instruction": "Plan an independent adversarial research investigation before drawing conclusions. Cover each track exactly once: background, competing explanations, consequences. Form precise searches for primary evidence, strong contrary findings, and external-validity/implementation limits. Identify decisive falsifiable tests, evidence standards, methodological pitfalls, and stopping conditions. Do not assume your assigned side is correct; seek the best case against it. Do not fabricate findings or citations. Prioritize queries by how much they could change the eventual decision.",
                    },
                    ResearchPlan,
                    team=team,
                )
                if {track.track for track in output.tracks} != {
                    "background",
                    "competing explanations",
                    "consequences",
                }:
                    raise EvidenceError("Research plan must cover each track exactly once")
                await self.store.put(
                    run_id, "research_plan", team, output.model_dump(), team=team, stage="research"
                )

            results = await asyncio.gather(*(plan_research(team) for team in teams), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        for track in ["background", "competing explanations", "consequences"]:
            results = await asyncio.gather(
                *(self.research_task(run_id, team, track, "research") for team in teams),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        # First debaters independently synthesize their own team's research before the barrier.
        for team in teams:
            if await self.store.by_key(run_id, "briefing", team):
                continue
            context = await self.context(run_id, team=team)
            context["instruction"] = (
                "Synthesize your independent team's research into a complete initial briefing. Address the strongest contrary evidence and reversal conditions. Cite existing claims rather than duplicating them."
            )
            if run.config.get("role_configs"):
                context["model_role"] = "researcher"
            briefing = await self.invoke(
                run_id, f"briefing:{team}", f"{team}_debater_1", context, Submission, team=team
            )
            submission = await self.commit_submission(run_id, f"briefing:{team}", briefing, team, "research")
            await self.store.put(
                run_id,
                "briefing",
                team,
                {**briefing.model_dump(), "submission_id": submission.id},
                stage="research",
                team=team,
            )
        await self.store.publish_stage(run_id, "research")
        await self.check_new(run_id, "initial")

    async def import_parent(self, run_id, parent_id):
        if await self.store.by_key(run_id, "limitation", "evidence_reuse"):
            return
        rows = await self.store.records(parent_id, audience="observer")
        rows = [
            r
            for r in rows
            if r.kind not in {"frame", "judgment", "finding", "recommendation", "consistency", "snapshot"}
        ]
        mapping = {}
        for row in rows:
            copy = await self.store.put(
                run_id, row.kind, f"import:{row.id}", {}, stage="import", team=row.team, public=True
            )
            mapping[row.id] = copy.id

        def remap(value):
            if isinstance(value, str):
                return mapping.get(value, value)
            if isinstance(value, list):
                return [remap(x) for x in value]
            if isinstance(value, dict):
                return {k: remap(v) for k, v in value.items()}
            return value

        from sqlalchemy import select

        from .store import Dependency, Record

        async with self.store.lock, self.store.sessions.begin() as session:
            for row in rows:
                copy = await session.get(Record, mapping[row.id])
                copy.data = {**remap(row.data), "reused_from": {"run_id": parent_id, "record_id": row.id}}
                copy.stale, copy.superseded = row.stale, row.superseded
            edges = (await session.scalars(select(Dependency).where(Dependency.upstream.in_(mapping)))).all()
            for edge in edges:
                if edge.downstream in mapping:
                    session.add(
                        Dependency(upstream=mapping[edge.upstream], downstream=mapping[edge.downstream])
                    )
        await self.store.put(
            run_id,
            "limitation",
            "evidence_reuse",
            {
                "text": "This value-sensitivity assessment reuses the parent's evidence and debate. Agreement is not independent corroboration."
            },
            public=True,
            stage="import",
        )

    async def dossier(self, run_id):
        if await self.store.is_committed(run_id, "coverage", "dossier"):
            return
        context = await self.context(run_id)
        result = await self.invoke(run_id, "coverage", "coverage", context, CoverageReview)
        await self.store.put(run_id, "coverage", "dossier", result.model_dump(), stage="dossier", public=True)
        frame = await self.current_frame(run_id)
        if result.missing_questions or result.added_perspectives:
            await self.store.put(
                run_id,
                "frame",
                "coverage_revision",
                {
                    **frame.data,
                    "issues": list(dict.fromkeys(frame.data["issues"] + result.missing_questions)),
                    "overlooked_perspectives": list(
                        dict.fromkeys(frame.data["overlooked_perspectives"] + result.added_perspectives)
                    ),
                    "revision_reason": "Neutral coverage review incorporated omissions and team framing challenges without deciding the winner.",
                },
                stage="dossier",
                public=True,
                logical_id=frame.logical_id,
                version=frame.version + 1,
            )
        for i, query in enumerate(result.research_queries[:2]):
            try:
                hits = await self.gateway.search(run_id, f"coverage:search:{i}", query)
                await asyncio.gather(
                    *(
                        self.evidence.retrieve(run_id, h.url, stage="dossier", query=query)
                        for h in hits.hits[:2]
                    )
                )
            except BudgetExceeded:
                await self.store.put(
                    run_id,
                    "limitation",
                    "coverage:budget",
                    {"text": "Neutral coverage supplement limited by budget."},
                    public=True,
                )
                break

        await self.store.mark_committed(run_id, "coverage", "dossier")

    async def commit_submission(self, run_id, key, output: Submission, team, stage):
        if await self.store.is_committed(run_id, "submission", key):
            return await self.store.by_key(run_id, "submission", key)
        # Validate every reference before committing any proposed claims or arguments.
        visible = {r.id: r for r in await self.store.records(run_id, audience=team)}
        for claim in output.claims:
            for reference in claim.supporting_passage_ids + claim.conflicting_passage_ids:
                if reference not in visible or visible[reference].kind != "passage":
                    raise EvidenceError("Submission contains a fabricated or inaccessible passage citation")
        for argument in output.arguments:
            if any(i < 0 or i >= len(output.claims) for i in argument.claim_indexes):
                raise EvidenceError("Argument references an invalid proposed claim")
            for claim_id in argument.existing_claim_ids:
                record = visible.get(claim_id)
                if (
                    not record
                    or record.kind != "claim"
                    or record.superseded
                    or record.data.get("status") == "contradicted"
                ):
                    raise EvidenceError("Arguments must use current, defensible claims")
        for objection in output.objections:
            target = visible.get(objection.target_id)
            if not target or target.kind not in {"argument", "claim"}:
                raise EvidenceError("Objection must identify a visible claim or argument")
        for response in output.responses:
            target = visible.get(response.objection_id)
            if not target or target.kind != "objection":
                raise EvidenceError("Response must identify an existing objection")
        claims = [
            await self.evidence.register_claim(run_id, f"{key}:claim:{i}", claim, team, stage)
            for i, claim in enumerate(output.claims)
        ]
        arguments = []
        for i, argument in enumerate(output.arguments):
            claim_ids = [claims[j].id for j in argument.claim_indexes] + argument.existing_claim_ids
            record = await self.store.put(
                run_id,
                "argument",
                f"{key}:argument:{i}",
                {
                    "conclusion": argument.conclusion,
                    "claim_ids": claim_ids,
                    "assumptions": argument.assumptions,
                },
                team=team,
                stage=stage,
            )
            for claim_id in claim_ids:
                await self.store.link(run_id, claim_id, record.id)
            arguments.append(record)
        result = await self.store.put(
            run_id,
            "submission",
            key,
            {
                **output.model_dump(),
                "claim_ids": [c.id for c in claims],
                "argument_ids": [a.id for a in arguments],
                "speaker": f"{team}_debater_{3 if stage == 'closings' else 1 if stage in {'research', 'openings'} else 2}",
            },
            team=team,
            stage=stage,
        )
        for record in claims + arguments:
            await self.store.link(run_id, record.id, result.id)
        for i, objection in enumerate(output.objections):
            await self.store.put(
                run_id, "objection", f"{key}:objection:{i}", objection.model_dump(), team=team, stage=stage
            )
        for i, response in enumerate(output.responses):
            await self.store.put(
                run_id, "response", f"{key}:response:{i}", response.model_dump(), team=team, stage=stage
            )
        await self.store.mark_committed(run_id, "submission", key)
        return result

    async def paired(self, run_id, stage, debater):
        snapshot = await self.store.by_key(run_id, "snapshot", stage)
        if not snapshot:
            contexts = {
                team: await self.context(run_id, team=team, exclude_stage=stage) for team in ["A", "B"]
            }
            snapshot = await self.store.put(run_id, "snapshot", stage, contexts, stage="snapshots")

        async def generate(team):
            key = f"{stage}:{team}"
            if await self.store.is_committed(run_id, "submission", key):
                return
            frame = snapshot.data[team].get("current_framing") or {}
            context = {
                **snapshot.data[team],
                "assigned_position": frame.get(f"position_{team.lower()}"),
                "instruction": f"Deliver the {stage}. Respond explicitly to EVERY required_objections ID. Reconsider stale premises, concede verified errors, and compare surviving arguments. Use existing claims when possible. Add new claims only with scoped retrieved evidence. Record substantive framing challenges explicitly.",
            }
            for repair in range(3):
                output = await self.invoke(
                    run_id,
                    f"{key}:repair:{repair}",
                    f"{team}_debater_{debater}",
                    context,
                    Submission,
                    team=team,
                )
                try:
                    answered = {r.objection_id for r in output.responses}
                    if set(context["required_objections"]) - answered:
                        raise EvidenceError("Strong unanswered objections must receive explicit responses")
                    await self.commit_submission(run_id, key, output, team, stage)
                    return
                except EvidenceError as error:
                    context = {**context, "validation_error": str(error)}
                    if repair == 2:
                        raise

        run = await self.store.get_run(run_id, records=False)
        order = ["B", "A"] if run.config.get("variant") == "reverse" else ["A", "B"]
        results = await asyncio.gather(*(generate(team) for team in order), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await self.store.publish_stage(run_id, stage)
        await self.check_new(run_id, stage)

    async def openings(self, run_id):
        await self.paired(run_id, "openings", 1)

    async def responses(self, run_id):
        await self.paired(run_id, "responses", 2)

    async def rebuttals(self, run_id):
        await self.paired(run_id, "rebuttals", 2)

    async def closings(self, run_id):
        await self.paired(run_id, "closings", 3)

    async def check_new(self, run_id, stage, *, final=False, recheck_ids=None):
        plan = await self.store.by_key(run_id, "check_plan", stage)
        if not plan:
            claims = [
                r
                for r in await self.store.records(run_id, audience="observer", kinds=["claim"])
                if (r.data.get("status", "unchecked") == "unchecked" or r.id in (recheck_ids or set()))
                and not r.superseded
            ]
            claims.sort(key=lambda r: not r.data.get("decisive"))
            plan = await self.store.put(
                run_id, "check_plan", stage, {"claim_ids": [c.id for c in claims]}, stage="check_plans"
            )
        claims = [await self.store.get_record(run_id, id_) for id_ in plan.data["claim_ids"]]
        for batch_start in range(0, len(claims), 4):
            batch = claims[batch_start : batch_start + 4]
            key = f"check:{stage}:{batch_start}"
            if all([await self.store.by_key(run_id, "check", f"{key}:{c.id}") for c in batch]):
                continue
            empirical = [c for c in batch if c.data["type"] != "normative"]
            try:
                if empirical:
                    query = f"{empirical[0].data['text']} original data contradictory evidence methodological critique"
                    hits = await self.gateway.search(
                        run_id, f"{key}:independent_search", query, final=final, model_role="checker"
                    )
                    await asyncio.gather(
                        *(
                            self.evidence.retrieve(run_id, h.url, stage="checks", query=query)
                            for h in hits.hits[:3]
                        )
                    )
                passages = await self.evidence.relevant_passages(
                    run_id, " ".join(c.data["text"] for c in batch), "observer", limit=35
                )
                context = {
                    "original_question": (await self.store.get_run(run_id, records=False)).question,
                    "claims": [
                        {
                            "id": c.id,
                            **{k: v for k, v in c.data.items() if k not in {"status", "latest_check_id"}},
                        }
                        for c in batch
                    ],
                    "passages": [{"id": p.id, **p.data} for p in passages],
                    "instruction": "Check each supplied claim exactly once, using independently retrieved evidence and cited source context. Do not see or grade team speeches. Do not infer causation from association or generalize an unrepresentative sample.",
                }
                output = await self.invoke(run_id, key, "checker", context, CheckBatch, final=final)
                if {c.claim_id for c in output.checks} != {c.id for c in batch} or len(output.checks) != len(
                    batch
                ):
                    raise EvidenceError("Checker must return exactly one assessment per requested claim")
                for check in output.checks:
                    if not await self.store.by_key(run_id, "check", f"{key}:{check.claim_id}"):
                        await self.store.apply_check(
                            run_id, check.claim_id, check.model_dump(), f"{key}:{check.claim_id}"
                        )
            except BudgetExceeded:
                await self.store.put(
                    run_id,
                    "limitation",
                    key,
                    {
                        "text": "Checking budget exhausted. Remaining claims are unverified and cannot establish decisive findings."
                    },
                    public=True,
                    stage="checks",
                )
                return

    async def follow_up(self, run_id):
        for cycle in range(2):
            context = await self.context(run_id)
            investigation = await self.invoke(
                run_id, f"investigate:{cycle}", "investigator", context, Investigation
            )
            await self.store.put(
                run_id,
                "investigation",
                str(cycle),
                investigation.model_dump(),
                public=True,
                stage="follow_up",
            )
            queries = [
                d.research_query
                for d in investigation.disputes
                if d.expected_value == "high" and d.type != "normative" and d.research_query
            ]
            if not queries:
                return
            stage = f"follow_up:{cycle}"
            results = await asyncio.gather(
                *(
                    self.research_task(run_id, team, "decisive disputes", stage, queries=queries[:2])
                    for team in ["A", "B"]
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            await self.store.publish_stage(run_id, stage)
            targets = set()
            for dispute in investigation.disputes:
                for id_ in dispute.target_ids:
                    record = await self.store.get_record(run_id, id_, audience="observer")
                    if record.kind == "claim":
                        targets.add(record.id)
                    elif record.kind == "argument":
                        targets.update(record.data.get("claim_ids", []))
            await self.check_new(run_id, stage, recheck_ids=targets)

    async def validate_findings(self, run_id, output):
        if not output.findings:
            if not output.provisional or output.confidence != "low":
                raise EvidenceError(
                    "A judgment without supported findings must be provisional and low-confidence"
                )
        for finding in output.findings:
            if finding.decisive and not finding.claim_ids:
                raise EvidenceError("A decisive empirical finding requires registered claims")
            for claim_id in finding.claim_ids:
                claim = await self.store.get_record(run_id, claim_id, audience="observer")
                if claim.kind != "claim" or claim.superseded or claim.stale:
                    raise EvidenceError("Judgment references stale or superseded evidence")
                if finding.decisive and claim.data.get("status") not in {"supported", "partly_supported"}:
                    raise EvidenceError(
                        "Unchecked, contradicted, or normative claims cannot establish an empirical decisive finding"
                    )
                if (
                    finding.decisive
                    and claim.data.get("status") == "partly_supported"
                    and not finding.uncertainty.strip()
                ):
                    raise EvidenceError("Partly supported findings require explicit qualification")

    async def judgment(self, run_id):
        if await self.store.is_committed(run_id, "judgment", "final"):
            return
        error_text = ""
        for repair in range(3):
            context = await self.context(run_id, role="judge")
            context["instruction"] = (
                "Assess individual disputes, then select the best-supported position. Use neutral case labels. Explicitly distinguish value priorities, factual findings, and evidence gaps. Reference only current checked claim IDs."
            )
            run = await self.store.get_run(run_id, records=False)
            if run.config.get("variant") == "values":
                context["instruction"] += (
                    " Reassess using a reasonable alternative ordering of the stated value principles; state exactly how the priorities differ."
                )
            if repair:
                context["validation_error"] = error_text
            output = await self.invoke(run_id, f"judgment:{repair}", "judge", context, Judgment, final=True)
            if output.new_claims:
                for i, claim in enumerate(output.new_claims):
                    await self.evidence.register_claim(
                        run_id, f"judge:{repair}:{i}", claim, None, "judgment", public=True
                    )
                await self.check_new(run_id, f"judgment:{repair}", final=True)
                error_text = "New claims are now registered and checked. Reassess using their IDs; do not introduce further unregistered assertions."
                continue
            try:
                await self.validate_findings(run_id, output)
            except EvidenceError as error:
                error_text = str(error)
                continue
            findings = []
            for i, finding in enumerate(output.findings):
                record = await self.store.put(
                    run_id, "finding", str(i), finding.model_dump(), stage="judgment", public=True
                )
                for claim_id in finding.claim_ids:
                    await self.store.link(run_id, claim_id, record.id)
                findings.append(record)
            judgment = await self.store.put(
                run_id,
                "judgment",
                "final",
                {**output.model_dump(), "finding_ids": [r.id for r in findings]},
                stage="judgment",
                public=True,
            )
            for finding in findings:
                await self.store.link(run_id, finding.id, judgment.id)
            await self.store.mark_committed(run_id, "judgment", "final")
            return
        raise NeedsReview("Judgment could not satisfy evidence checks after two correction attempts")

    async def insight(self, run_id):
        if await self.store.is_committed(run_id, "recommendation", "final"):
            return
        for attempt in range(3):
            context = await self.context(run_id)
            output = await self.invoke(
                run_id, f"analyst:{attempt}", "analyst", context, Recommendation, final=True
            )
            if output.new_claims:
                for i, claim in enumerate(output.new_claims):
                    await self.evidence.register_claim(
                        run_id, f"analyst:{attempt}:{i}", claim, None, "insight", public=True
                    )
                await self.check_new(run_id, f"analyst:{attempt}", final=True)
                continue
            if not output.supporting_finding_ids:
                raise EvidenceError("A recommendation must identify its supporting findings")
            dependencies = output.supporting_finding_ids + output.claim_ids
            for reference in dependencies:
                record = await self.store.get_record(run_id, reference, audience="observer")
                if record.stale or record.superseded or record.kind not in {"finding", "claim"}:
                    raise EvidenceError("Recommendation relies on invalid or stale evidence")
                if record.kind == "claim" and record.data.get("status") not in {
                    "supported",
                    "partly_supported",
                }:
                    raise EvidenceError("Recommendation relies on unchecked or rejected facts")
            recommendation = await self.store.put(
                run_id, "recommendation", "final", output.model_dump(), stage="insight", public=True
            )
            for reference in dependencies:
                await self.store.link(run_id, reference, recommendation.id)
            await self.store.mark_committed(run_id, "recommendation", "final")
            return
        raise NeedsReview("Recommendation introduced unchecked facts after bounded correction attempts")

    async def consistency(self, run_id):
        judgment = await self.store.by_key(run_id, "judgment", "final")
        recommendation = await self.store.by_key(run_id, "recommendation", "final")
        if not judgment or not recommendation or judgment.stale or recommendation.stale:
            raise NeedsReview("Final assessment has missing or stale dependencies")
        await self.validate_findings(
            run_id, Judgment.model_validate({k: v for k, v in judgment.data.items() if k != "finding_ids"})
        )
        context = await self.context(run_id)
        output = await self.invoke(
            run_id, "consistency", "consistency", context, ConsistencyReview, final=True
        )
        await self.store.put(
            run_id, "consistency", "final", output.model_dump(), stage="consistency", public=True
        )
        if not output.consistent or not output.recommendation_consistent:
            raise NeedsReview("Final consistency review requires attention: " + "; ".join(output.issues))
