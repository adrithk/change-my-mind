"""Freeze role assignments per run; old runs without a policy retain their configuration."""

from .schemas import ModelConfig

ROLE_PROFILES = {
    "researcher": {
        "model": "gpt-6-astra",
        "reasoning": "xhigh",
        "max_output_tokens": 12000,
        "timeout_seconds": 420,
    },
    "debater": {"model": "gpt-6.1-sol", "reasoning": "high"},
    "checker": {
        "model": "gpt-6.1-sol",
        "reasoning": "xhigh",
        "max_output_tokens": 10000,
        "timeout_seconds": 300,
    },
    "judge": {
        "model": "gpt-6-astra",
        "reasoning": "high",
        "max_output_tokens": 10000,
        "timeout_seconds": 300,
    },
    "analyst": {"model": "gpt-6.1-sol", "reasoning": "high"},
    "investigator": {"model": "gpt-6.1-sol", "reasoning": "high"},
    "helper": {"model": "gpt-6.1-sol", "reasoning": "medium"},
    "search": {"model": "gpt-6.1-sol", "reasoning": "high"},
}


def freeze_roles(config):
    if config.get("model_policy") != "research_first":
        return {}
    base = ModelConfig.model_validate(config["model_config"])
    return {role: base.model_copy(update=values).model_dump() for role, values in ROLE_PROFILES.items()}


def invocation_config(run_config, role, context):
    group = context.get("model_role")
    if not group:
        if "_debater_" in role:
            group = "debater"
        elif role in {"consistency"}:
            group = "checker"
        elif role == "comparator":
            group = "judge"
        elif role in {"single_researcher", "researcher"}:
            group = "researcher"
        else:
            group = role if role in ROLE_PROFILES else "helper"
    return ModelConfig.model_validate(
        run_config.get("role_configs", {}).get(group, run_config["model_config"])
    )
