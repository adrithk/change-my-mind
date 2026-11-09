from datetime import datetime


def metrics(run):
    claims = [r for r in run.records if r.kind == "claim" and not r.superseded]
    empirical = [r for r in claims if r.data.get("type") != "normative"]
    checked = [r for r in empirical if r.data.get("status", "unchecked") != "unchecked"]
    return {
        "current_empirical_claims": len(empirical),
        "checked_claim_fraction": round(len(checked) / len(empirical), 3) if empirical else None,
        "supported_claims": sum(r.data.get("status") == "supported" for r in empirical),
        "contradicted_claims": sum(r.data.get("status") == "contradicted" for r in empirical),
        "unchecked_claims": len(empirical) - len(checked),
        "claims_with_opposing_passages": sum(bool(r.data.get("conflicting_passage_ids")) for r in claims),
        "superseded_claim_versions": sum(r.kind == "claim" and r.superseded for r in run.records),
        "flagged_dependent_records": sum(r.stale for r in run.records),
        "gross_cost_usd": run.cost_usd,
        "elapsed_seconds_including_queue": round(
            (datetime.fromisoformat(run.updated_at) - datetime.fromisoformat(run.created_at)).total_seconds(),
            2,
        ),
        "independent_factual_accuracy": "Not measured; requires adjudicated ground truth.",
        "opposing_evidence_coverage": "Passage counts are a proxy, not a measure of completeness.",
        "recommendation_usefulness": "Requires independent human review.",
    }
