import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import PRICE_VERSION, PRICING, SEARCH_PRICE, TERMINAL
from .errors import BudgetExceeded, Cancelled, EvidenceError
from .model_policy import freeze_roles
from .schemas import EventView, RecordView, RunCreate, RunView


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    question: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    stage: Mapped[str] = mapped_column(default="framing")
    config: Mapped[dict] = mapped_column(JSON)
    budget_usd: Mapped[float]
    error: Mapped[str | None]
    suite_id: Mapped[str | None] = mapped_column(ForeignKey("suites.id"), index=True)
    created_at: Mapped[str] = mapped_column(default=now)
    updated_at: Mapped[str] = mapped_column(default=now)


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("run_id", "kind", "key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(index=True)
    key: Mapped[str]
    stage: Mapped[str]
    team: Mapped[str | None]
    visibility: Mapped[str] = mapped_column(default="private")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(default=1)
    logical_id: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (UniqueConstraint("upstream", "downstream"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    upstream: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    downstream: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (UniqueConstraint("run_id", "operation"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    operation: Mapped[str]
    team: Mapped[str | None]
    state: Mapped[str] = mapped_column(default="reserved")
    reserved: Mapped[float]
    cost: Mapped[float] = mapped_column(default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    type: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Suite(Base):
    __tablename__ = "suites"
    id: Mapped[str] = mapped_column(primary_key=True)
    parent_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str]
    budget_usd: Mapped[float]
    status: Mapped[str] = mapped_column(default="queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(default=now)


def record_view(r: Record) -> RecordView:
    return RecordView(**{k: getattr(r, k) for k in RecordView.model_fields})


class Store:
    """One local process; the lock serializes reservations and SQLite writes."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{data_dir / 'debate.db'}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.lock, self.sessions.begin() as session:
            for run in (await session.scalars(select(Run).where(Run.status == "running"))).all():
                run.status, run.error = "interrupted", "Interrupted; resume from the last durable stage."
            for usage in (await session.scalars(select(Usage).where(Usage.state == "reserved"))).all():
                usage.state, usage.cost = "uncertain", usage.reserved
                usage.details = {
                    **usage.details,
                    "limitation": "Outcome unknown after interruption; maximum reserved cost retained.",
                }

    async def close(self):
        await self.engine.dispose()

    async def create_run(self, request: RunCreate, *, suite_id=None, extra=None) -> str:
        config = request.model_dump(by_alias=True)
        config.update(extra or {})
        config.setdefault("role_configs", freeze_roles(config))
        config.setdefault(
            "model_policy_version", "research-first-v1" if config["role_configs"] else "uniform-v1"
        )
        config["pricing_by_model"] = config.get("pricing_by_model") or {
            model: dict(rates) for model, rates in PRICING.items()
        }
        config["pricing_version"] = PRICE_VERSION
        config["pricing"] = dict(PRICING.get(request.model_config_.model, PRICING["gpt-6.1-sol"]))
        config["search_price"] = SEARCH_PRICE
        config["finalization_reserve"] = min(2.0, request.budget_usd * 0.25)
        async with self.lock, self.sessions.begin() as session:
            run = Run(
                id=uid("run"),
                question=request.question.strip(),
                config=config,
                budget_usd=request.budget_usd,
                suite_id=suite_id,
            )
            session.add(run)
            await session.flush()
            session.add(Event(run_id=run.id, type="queued", data={"question": run.question}))
            return run.id

    async def get_run(self, run_id: str, *, records=True) -> RunView:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            usages = (await session.scalars(select(Usage).where(Usage.run_id == run_id))).all()
            view = RunView(
                id=run.id,
                question=run.question,
                status=run.status,
                stage=run.stage,
                config=run.config,
                budget_usd=run.budget_usd,
                cost_usd=round(sum(u.cost for u in usages), 6),
                reserved_usd=round(sum(u.reserved for u in usages if u.state == "reserved"), 6),
                requests_used=len(usages),
                created_at=run.created_at,
                updated_at=run.updated_at,
                error=run.error,
            )
            if records:
                view.records = await self.records(run_id, audience="observer")
                view.suites = await self.suites(run_id)
            return view

    async def list_runs(self) -> list[RunView]:
        async with self.sessions() as session:
            ids = (await session.scalars(select(Run.id).order_by(Run.created_at.desc()))).all()
        return [await self.get_run(i, records=False) for i in ids]

    async def set_status(self, run_id: str, status: str, *, stage=None, error=None):
        async with self.lock, self.sessions.begin() as session:
            run = await session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            if run.status == "cancelled" and status not in {"queued", "cancelled"}:
                raise Cancelled("Run cancelled")
            run.status, run.error, run.updated_at = status, error, now()
            if stage:
                run.stage = stage
            session.add(
                Event(
                    run_id=run_id, type="status", data={"status": status, "stage": run.stage, "error": error}
                )
            )

    async def ensure_active(self, run_id):
        run = await self.get_run(run_id, records=False)
        if run.status == "cancelled":
            raise Cancelled("Run cancelled")

    async def emit(self, run_id, type_, data):
        async with self.lock, self.sessions.begin() as session:
            session.add(Event(run_id=run_id, type=type_, data=data))

    async def events(self, run_id: str, after: int = 0) -> list[EventView]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Event)
                    .where(Event.run_id == run_id, Event.id > after)
                    .order_by(Event.id)
                    .limit(200)
                )
            ).all()
            return [
                EventView(id=r.id, run_id=r.run_id, type=r.type, data=r.data, created_at=r.created_at)
                for r in rows
            ]

    async def put(
        self,
        run_id: str,
        kind: str,
        key: str,
        data: dict,
        *,
        stage="",
        team=None,
        public=False,
        logical_id=None,
        version=1,
    ) -> RecordView:
        async with self.lock, self.sessions.begin() as session:
            old = await session.scalar(
                select(Record).where(Record.run_id == run_id, Record.kind == kind, Record.key == key)
            )
            if old:
                return record_view(old)
            record_id = uid(kind)
            record = Record(
                id=record_id,
                run_id=run_id,
                kind=kind,
                key=key,
                data=data,
                stage=stage,
                team=team,
                visibility="public" if public else "private",
                logical_id=logical_id or record_id,
                version=version,
            )
            session.add(record)
            await session.flush()
            return record_view(record)

    async def by_key(self, run_id, kind, key) -> RecordView | None:
        async with self.sessions() as session:
            r = await session.scalar(
                select(Record).where(Record.run_id == run_id, Record.kind == kind, Record.key == key)
            )
            return record_view(r) if r else None

    async def is_committed(self, run_id, kind, key):
        return bool(await self.by_key(run_id, "commit", f"{kind}:{key}"))

    async def mark_committed(self, run_id, kind, key):
        artifact = await self.by_key(run_id, kind, key)
        if not artifact:
            raise EvidenceError("Cannot commit a missing artifact")
        await self.put(run_id, "commit", f"{kind}:{key}", {"record_id": artifact.id}, stage="commits")

    async def records(
        self, run_id: str, *, audience="controller", kinds=None, stage=None
    ) -> list[RecordView]:
        query = select(Record).where(Record.run_id == run_id)
        if audience != "controller":
            if audience in {"A", "B"}:
                query = query.where((Record.visibility == "public") | (Record.team == audience))
            else:
                query = query.where(Record.visibility == "public")
            query = query.where(Record.kind != "invocation")
        if kinds:
            query = query.where(Record.kind.in_(kinds))
        if stage:
            query = query.where(Record.stage == stage)
        async with self.sessions() as session:
            return [
                record_view(r)
                for r in (await session.scalars(query.order_by(Record.created_at, Record.id))).all()
            ]

    async def get_record(self, run_id, record_id, audience="controller") -> RecordView:
        records = await self.records(run_id, audience=audience)
        for r in records:
            if r.id == record_id:
                return r
        raise EvidenceError(f"Record {record_id} is not visible in this topic and stage")

    async def publish_stage(self, run_id, stage):
        async with self.lock, self.sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(Record).where(
                        Record.run_id == run_id, Record.stage == stage, Record.kind != "invocation"
                    )
                )
            ).all()
            for r in rows:
                r.visibility = "public"
            session.add(Event(run_id=run_id, type="published", data={"stage": stage, "records": len(rows)}))

    async def link(self, run_id, upstream: str, downstream: str):
        async with self.lock, self.sessions.begin() as session:
            for record_id in [upstream, downstream]:
                r = await session.get(Record, record_id)
                if not r or r.run_id != run_id:
                    raise EvidenceError("Dependencies cannot cross topics or reference unknown records")
            if not await session.scalar(
                select(Dependency).where(Dependency.upstream == upstream, Dependency.downstream == downstream)
            ):
                session.add(Dependency(upstream=upstream, downstream=downstream))

    async def _invalidate(self, session, claim_id):
        todo, visited = [claim_id], {claim_id}
        while todo:
            parent = todo.pop()
            for child in (
                await session.scalars(select(Dependency.downstream).where(Dependency.upstream == parent))
            ).all():
                if child in visited:
                    continue
                visited.add(child)
                record = await session.get(Record, child)
                record.stale = True
                todo.append(child)
        return list(visited - {claim_id})

    async def revise_claim(self, run_id, old_id, key, data, team, stage):
        existing = await self.by_key(run_id, "claim", key)
        if existing:
            return existing
        old = await self.get_record(run_id, old_id, audience=team or "observer")
        if old.kind != "claim" or old.superseded or (team and old.team != team):
            raise EvidenceError("Only a team's current claims can be revised by that team")
        async with self.lock, self.sessions.begin() as session:
            record = Record(
                id=uid("claim"),
                run_id=run_id,
                kind="claim",
                key=key,
                data={**data, "supersedes": old_id, "status": "unchecked"},
                team=team,
                stage=stage,
                visibility="private",
                logical_id=old.logical_id,
                version=old.version + 1,
            )
            session.add(record)
            previous = await session.get(Record, old_id)
            previous.superseded = True
            affected = await self._invalidate(session, old_id)
            session.add(
                Event(
                    run_id=run_id,
                    type="correction",
                    data={"claim_id": old_id, "replacement_id": record.id, "affected_ids": affected},
                )
            )
            await session.flush()
            return record_view(record)

    async def apply_check(self, run_id, claim_id, check_data, key):
        claim = await self.get_record(run_id, claim_id)
        if claim.kind != "claim":
            raise EvidenceError("Checks must target claims")
        if claim.data["type"] == "normative" and check_data["status"] != "not_fact_checkable":
            raise EvidenceError("Normative premises cannot be marked empirically verified")
        for passage_id in check_data["passage_ids"]:
            passage = await self.get_record(run_id, passage_id, audience="observer")
            if passage.kind != "passage":
                raise EvidenceError("Check evidence must reference retrieved passages")
        if (
            check_data["status"] in {"supported", "contradicted", "partly_supported"}
            and not check_data["passage_ids"]
        ):
            raise EvidenceError("A substantive fact check requires retrieved evidence")
        async with self.lock, self.sessions.begin() as session:
            result = await session.scalar(
                select(Record).where(Record.run_id == run_id, Record.kind == "check", Record.key == key)
            )
            if result:
                return record_view(result)
            result_id = uid("check")
            result = Record(
                id=result_id,
                run_id=run_id,
                kind="check",
                key=key,
                data=check_data,
                stage="checks",
                team=None,
                visibility="public",
                logical_id=result_id,
            )
            session.add(result)
            row = await session.get(Record, claim_id)
            old_status = row.data.get("status", "unchecked")
            row.data = {**row.data, "status": check_data["status"], "latest_check_id": result.id}
            affected = []
            if (
                check_data["status"] in {"contradicted", "insufficient_evidence", "partly_supported"}
                and old_status != check_data["status"]
            ):
                affected = await self._invalidate(session, claim_id)
            session.add(
                Event(
                    run_id=run_id,
                    type="checked",
                    data={"claim_id": claim_id, "status": check_data["status"], "affected_ids": affected},
                )
            )
            await session.flush()
            return record_view(result)

    async def reserve(self, run_id, operation, amount: float, *, team=None, final=False, details=None):
        async with self.lock, self.sessions.begin() as session:
            run = await session.get(Run, run_id)
            if run.status == "cancelled":
                raise Cancelled("Run cancelled")
            old = await session.scalar(
                select(Usage).where(Usage.run_id == run_id, Usage.operation == operation)
            )
            if old:
                raise EvidenceError(f"Operation {operation} already has an accounting entry")
            rows = (await session.scalars(select(Usage).where(Usage.run_id == run_id))).all()

            def charged(u):
                return u.reserved if u.state == "reserved" else u.cost

            spent = sum(charged(u) for u in rows)
            plan = run.config["model_config"]["provider"] == "chatgpt"
            if plan:
                request_limit = run.config.get("request_limit", 80)
                available = request_limit - (0 if final else 12)
                if len(rows) + 1 > available:
                    raise BudgetExceeded(
                        "Application request limit reached; remaining requests are reserved for completion. No API fallback."
                    )
                if team:
                    team_limit = int(
                        (request_limit - 12) * (0.70 if run.config["mode"] == "single" else 0.35)
                    )
                    if sum(u.team == team for u in rows) + 1 > team_limit:
                        raise BudgetExceeded(f"Team {team} reached its equal plan request allowance")
            limit = run.budget_usd - (0 if final else run.config["finalization_reserve"])
            if spent + amount > limit + 1e-9:
                raise BudgetExceeded("Remaining budget is reserved for completion or already committed")
            if team:
                team_limit = (run.budget_usd - run.config["finalization_reserve"]) * (
                    0.70 if run.config["mode"] == "single" else 0.35
                )
                if sum(charged(u) for u in rows if u.team == team) + amount > team_limit + 1e-9:
                    raise BudgetExceeded(f"Team {team} reached its equal resource allowance")
            if run.suite_id:
                suite = await session.get(Suite, run.suite_id)
                suite_rows = (
                    await session.scalars(
                        select(Usage).join(Run, Run.id == Usage.run_id).where(Run.suite_id == run.suite_id)
                    )
                ).all()
                if plan and len(suite_rows) + 1 > suite.result.get("request_limit", 180):
                    raise BudgetExceeded("Aggregate suite plan request limit reached; no API fallback")
                if sum(charged(u) for u in suite_rows) + amount > suite.budget_usd + 1e-9:
                    raise BudgetExceeded("Aggregate suite budget exhausted")
            usage = Usage(
                id=uid("usage"),
                run_id=run_id,
                operation=operation,
                team=team,
                reserved=amount,
                details=details or {},
            )
            session.add(usage)
            return usage.id

    async def settle(self, usage_id, cost, details, *, uncertain=False):
        async with self.lock, self.sessions.begin() as session:
            row = await session.get(Usage, usage_id)
            row.state = "uncertain" if uncertain else "settled"
            row.cost = max(cost, row.reserved) if uncertain else cost
            row.details = {**row.details, **details}
            session.add(
                Event(run_id=row.run_id, type="usage", data={"cost_usd": row.cost, "uncertain": uncertain})
            )

    async def usage(self, run_id):
        async with self.sessions() as session:
            rows = (await session.scalars(select(Usage).where(Usage.run_id == run_id))).all()
            return [
                {
                    "operation": r.operation,
                    "team": r.team,
                    "state": r.state,
                    "cost_usd": r.cost,
                    "reserved_usd": r.reserved,
                    "details": r.details,
                }
                for r in rows
            ]

    async def mark_uncertain(self, run_id):
        async with self.lock, self.sessions.begin() as session:
            rows = (
                await session.scalars(select(Usage).where(Usage.run_id == run_id, Usage.state == "reserved"))
            ).all()
            for row in rows:
                row.state, row.cost = "uncertain", row.reserved
                row.details = {
                    **row.details,
                    "limitation": "Cancelled or interrupted request; maximum reservation retained.",
                }

    async def create_suite(self, parent_run_id, kind, budget, request_limit=180):
        async with self.lock, self.sessions.begin() as session:
            suite = Suite(
                id=uid("suite"),
                parent_run_id=parent_run_id,
                kind=kind,
                budget_usd=budget,
                result={"request_limit": request_limit},
            )
            session.add(suite)
            await session.flush()
            return suite.id

    async def suites(self, parent_run_id):
        async with self.sessions() as session:
            rows = (await session.scalars(select(Suite).where(Suite.parent_run_id == parent_run_id))).all()
            return [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "status": r.status,
                    "budget_usd": r.budget_usd,
                    "result": r.result,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def refresh_suites(self):
        async with self.lock, self.sessions.begin() as session:
            suites = (
                await session.scalars(select(Suite).where(Suite.status.in_(["queued", "running"])))
            ).all()
            for suite in suites:
                children = (await session.scalars(select(Run).where(Run.suite_id == suite.id))).all()
                if not children or any(c.status not in TERMINAL | {"interrupted"} for c in children):
                    suite.status = "running"
                    continue
                outcomes = []
                for run in children:
                    judgment = await session.scalar(
                        select(Record)
                        .where(Record.run_id == run.id, Record.kind == "judgment")
                        .order_by(Record.created_at.desc())
                    )
                    outcomes.append(
                        {
                            "run_id": run.id,
                            "variant": run.config.get("variant", run.config["mode"]),
                            "status": run.status,
                            "winner": judgment.data["best_supported_position"] if judgment else None,
                            "confidence": judgment.data["confidence"] if judgment else None,
                        }
                    )
                suite.status = "completed" if all(c.status == "completed" for c in children) else "incomplete"
                suite.result = {
                    **suite.result,
                    "outcomes": outcomes,
                    "evidence_reuse": any(c.config.get("reuse_parent") for c in children),
                    "limitation": "Shared evidence and model family are not independent corroboration. Compare positions and assumptions, not team labels.",
                }
