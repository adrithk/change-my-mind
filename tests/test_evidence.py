import pytest

from debate_bro.demo import FixtureFetcher
from debate_bro.errors import EvidenceError
from debate_bro.evidence import Evidence, canonical_url, validate_public_url
from debate_bro.schemas import ClaimDraft, RunCreate


async def test_private_evidence_and_topic_boundaries(store, run_id):
    evidence = Evidence(store, FixtureFetcher())
    source = await evidence.retrieve(run_id, "https://fixtures.debatebro.test/pilot", team="A")
    passages = await store.records(run_id, audience="A", kinds=["passage"])
    assert passages
    assert not await store.records(run_id, audience="B")
    assert not await store.records(run_id, audience="observer")
    claim = ClaimDraft(
        text="An observed pilot result",
        type="factual",
        scope="Synthetic pilot",
        supporting_passage_ids=[passages[0].id],
        conflicting_passage_ids=[],
        limitations=[],
        underlying_evidence_ids=[],
        decisive=True,
    )
    with pytest.raises(EvidenceError):
        await evidence.register_claim(run_id, "bad", claim, "B", "research")
    other = await store.create_run(RunCreate(question="An unrelated topic"))
    with pytest.raises(EvidenceError):
        await store.get_record(other, source.id, audience="A")
    await store.publish_stage(run_id, "research")
    assert await store.records(run_id, audience="B", kinds=["source"])


async def test_fabricated_quotes_and_unknown_citations_are_rejected(store, run_id):
    evidence = Evidence(store, FixtureFetcher())
    source = await evidence.retrieve(run_id, "https://fixtures.debatebro.test/pilot")
    with pytest.raises(EvidenceError, match="verbatim"):
        await evidence.register_passage(
            run_id, source, "The study proved permanent benefits for everyone.", "page 1", "fabricated"
        )
    claim = ClaimDraft(
        text="A false citation",
        type="factual",
        scope="Unknown",
        supporting_passage_ids=["passage_invented"],
        conflicting_passage_ids=[],
        limitations=[],
        underlying_evidence_ids=[],
        decisive=True,
    )
    with pytest.raises(EvidenceError):
        await evidence.register_claim(run_id, "fake", claim, None, "research")


async def test_correction_invalidates_transitive_dependents(store, run_id):
    evidence = Evidence(store, FixtureFetcher())
    await evidence.retrieve(run_id, "https://fixtures.debatebro.test/pilot")
    passage = (await store.records(run_id, kinds=["passage"]))[1]
    claim = await store.put(
        run_id,
        "claim",
        "claim",
        {"text": "1,200 per evening", "type": "factual", "status": "supported"},
        public=True,
    )
    previous = claim
    downstream = []
    for kind in ["argument", "finding", "judgment", "recommendation"]:
        item = await store.put(run_id, kind, kind, {}, public=True)
        await store.link(run_id, previous.id, item.id)
        previous = item
        downstream.append(item.id)
    await store.apply_check(
        run_id,
        claim.id,
        {
            "status": "contradicted",
            "passage_ids": [passage.id],
            "explanation": "Aggregate is not per evening",
        },
        "correction",
    )
    assert all([(await store.get_record(run_id, id_)).stale for id_ in downstream])
    assert (await store.get_record(run_id, claim.id)).data["status"] == "contradicted"


async def test_values_are_not_empirical_facts(store, run_id):
    claim = await store.put(
        run_id, "claim", "value", {"type": "normative", "text": "Access should be prioritized"}, public=True
    )
    with pytest.raises(EvidenceError, match="Normative"):
        await store.apply_check(run_id, claim.id, {"status": "supported", "passage_ids": []}, "bad")


async def test_private_urls_and_redirect_targets_are_rejected():
    for url in [
        "file:///etc/passwd",
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/",
        "https://example.com:8000/",
    ]:
        with pytest.raises(EvidenceError):
            await validate_public_url(url)
    assert canonical_url("https://Example.com/a?utm_source=test&x=1#section") == "https://example.com/a?x=1"
