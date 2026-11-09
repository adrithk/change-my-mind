import html
import json

from .metrics import metrics
from .store import Store


async def report_data(store: Store, run_id):
    run = await store.get_run(run_id)
    usage = await store.usage(run_id)
    return {
        **run.model_dump(),
        "usage": usage,
        "metrics": metrics(run),
        "limitations": [
            "Model-based verification is fallible. Shared model family and evidence are not independent corroboration.",
            (
                "Requests used the selected ChatGPT plan or its app credits allowance. No API-key billing fallback. ChatGPT Usage settings control credit spending and shared limits."
                if run.config["model_config"]["provider"] == "chatgpt"
                else "Cost estimates are gross API usage before subscription credits."
            ),
        ],
    }


def readable_sections(data):
    records = data["records"]

    def latest(kind):
        return next((r["data"] for r in reversed(records) if r["kind"] == kind), None)

    sections = [("Original question", data["question"])]
    if data["config"]["model_config"]["provider"] == "demo":
        sections.insert(
            0,
            (
                "Synthetic demonstration",
                "All source documents, numerical findings, and recommendations in this example are fictional. This is not research about the real world.",
            ),
        )
    for kind, title in [
        ("frame", "Issue map"),
        ("coverage", "Research synthesis and coverage"),
        ("judgment", "Substantive assessment"),
        ("recommendation", "Practical insight"),
        ("consistency", "Consistency review"),
    ]:
        if result := latest(kind):
            sections.append((title, result))
    sections.append(("Robustness", data["suites"] or "Untested. No robustness assessment has been run."))
    sections.append(("Validation metrics", data["metrics"]))
    sections.append(
        ("Limitations", data["limitations"] + [r["data"] for r in records if r["kind"] == "limitation"])
    )
    return sections


def usage_label(data):
    if data["config"]["model_config"]["provider"] == "chatgpt":
        return f"Using ChatGPT plan · {data['requests_used']}/{data['config'].get('request_limit', 80)} application requests (including retries); plan limits also apply"
    return f"${data['cost_usd']:.4f} gross API usage"


def markdown_report(data):
    lines = [f"# Research-first assessment\n\nStatus: **{data['status']}** · {usage_label(data)}\n"]
    for title, value in readable_sections(data):
        lines += [
            f"\n## {title}\n",
            value
            if isinstance(value, str)
            else "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```",
        ]
    lines.append("\n## Evidence and debate appendix\n")
    for record in data["records"]:
        if record["kind"] == "snapshot":
            continue
        flags = " · STALE" if record["stale"] else ""
        flags += " · SUPERSEDED" if record["superseded"] else ""
        lines += [
            f"\n### {record['id']} · {record['kind']}{flags}\n",
            "```json\n" + json.dumps(record["data"], indent=2, ensure_ascii=False) + "\n```",
        ]
    return "\n".join(lines)


def html_report(data):
    def escape(value):
        return html.escape(str(value))

    ids = {r["id"] for r in data["records"]}

    def render(value):
        if isinstance(value, dict):
            return (
                "<dl>"
                + "".join(
                    f"<dt>{escape(k.replace('_', ' '))}</dt><dd>{render(v)}</dd>"
                    for k, v in value.items()
                    if not k.endswith("_path")
                )
                + "</dl>"
            )
        if isinstance(value, list):
            return "<ul>" + "".join(f"<li>{render(v)}</li>" for v in value) + "</ul>"
        if isinstance(value, str) and value in ids:
            return f'<a href="#{escape(value)}">{escape(value)}</a>'
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return f'<a href="{escape(value)}" rel="noopener noreferrer">{escape(value)}</a>'
        return escape(value)

    body = "".join(
        f"<section><h2>{escape(title)}</h2>{render(value)}</section>"
        for title, value in readable_sections(data)
    )
    appendix = "".join(
        f'<details id="{escape(r["id"])}"><summary>{escape(r["kind"])} · {escape(r["id"])}{" · STALE" if r["stale"] else ""}{" · SUPERSEDED" if r["superseded"] else ""}</summary>{render(r["data"])}</details>'
        for r in data["records"]
        if r["kind"] != "snapshot"
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Research-first assessment</title>
<style>body{{font:16px/1.6 system-ui;max-width:980px;margin:40px auto;padding:0 24px;background:#faf9f5;color:#242b29}}h1,h2{{line-height:1.2}}section,details{{padding:20px;background:white;border:1px solid #ddd;border-radius:12px;margin:16px 0}}dt{{font-weight:600;margin-top:12px}}dd{{margin:4px 0 14px}}a{{color:#276452;overflow-wrap:anywhere}}summary{{cursor:pointer}}:target{{outline:3px solid #82b39a}}.meta{{color:#56605a}}</style>
<h1>Research-first assessment</h1><p class="meta">{escape(data["status"])} · {escape(usage_label(data))}</p>{body}<h2>Evidence and debate appendix</h2>{appendix}</html>"""
