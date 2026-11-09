import pytest

from debate_bro.demo import DemoProvider, FixtureFetcher
from debate_bro.evidence import Evidence
from debate_bro.model_policy import invocation_config
from debate_bro.providers import Gateway
from debate_bro.schemas import RunCreate
from debate_bro.workflow import Workflow


async def test_two_lead_researchers_and_role_routing_complete_offline(store):
    class Capture(DemoProvider):
        calls = []

        async def generate(self, role, context, schema, config):
            self.calls.append((role, context.get("model_role"), context.get("stage"), config))
            return await super().generate(role, context, schema, config)

    run_id = await store.create_run(
        RunCreate(question="A broad research question", model_config={"provider": "demo"})
    )
    provider = Capture()
    await Workflow(store, Gateway(store, provider, provider), Evidence(store, FixtureFetcher())).execute(
        run_id
    )
    run = await store.get_run(run_id)
    assert run.status == "completed"
    research = [row for row in provider.calls if row[1] == "researcher"]
    assert {row[0] for row in research} == {"A_debater_1", "B_debater_1"}
    assert all(row[3].model == "gpt-6-astra" and row[3].reasoning == "xhigh" for row in research)
    for role, group, stage, config in provider.calls:
        if role == "judge":
            assert (config.model, config.reasoning) == ("gpt-6-astra", "high")
        elif role in {"checker", "consistency"}:
            assert (config.model, config.reasoning) == ("gpt-6.1-sol", "xhigh")
        elif "_debater_" in role and stage in {"openings", "responses", "rebuttals", "closings"}:
            assert (config.model, config.reasoning) == ("gpt-6.1-sol", "high")
    plans = [r for r in run.records if r.kind == "research_plan"]
    assert len(plans) == 2 and all(r.visibility == "public" for r in plans)
    # Policy is a per-run snapshot; legacy runs retain their original uniform settings.
    legacy = {"model_config": {"model": "gpt-6.1-sol", "reasoning": "medium"}}
    assert invocation_config(legacy, "judge", {}).reasoning == "medium"
    invocations = await store.records(run_id, kinds=["invocation"])
    searches = [r for r in invocations if r.data["role"] == "search" and r.key.startswith("research:")]
    assert searches and all(r.data["model_config"]["model"] == "gpt-6-astra" for r in searches)


@pytest.mark.parametrize("role,expected", [("framer", "medium"), ("coverage", "medium"), ("analyst", "high")])
async def test_frozen_role_configs_and_explicit_uniform_mode(store, role, expected):
    research_id = await store.create_run(RunCreate(question="Frozen role settings"))
    run = await store.get_run(research_id)
    assert invocation_config(run.config, role, {}).reasoning == expected
    uniform_id = await store.create_run(
        RunCreate(question="Uniform comparison settings", model_policy="uniform")
    )
    uniform = await store.get_run(uniform_id)
    assert uniform.config["role_configs"] == {}
    assert invocation_config(uniform.config, "judge", {}).model == "gpt-6.1-sol"
