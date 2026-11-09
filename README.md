# Debate Bro

A local, research-first assessment app. Two equally configured AI teams investigate a broad question, challenge each other's cases, and correct errors before a substantive judge and decision analyst produce an inspectable assessment.

The application includes six debater roles, an independent-context fact checker, a judge, and a decision analyst. Framing, coverage review, investigation planning, consistency checking, and suite comparison are bounded helper calls. LangGraph controls execution; agents cannot choose their own permissions or bypass budgets.

## Start locally

Requires **Python 3.12**, **uv**, and **Node.js 22.12+** (Node 26 also works).

```sh
uv sync
cd frontend
npm ci
cd ..
uv run debate-bro types
cd frontend
npm run build
cd ..
uv run debate-bro serve
```

Open **http://127.0.0.1:8000**. `make setup` and `make run` provide the same setup and launch steps. Use one backend worker. The built React application is served by FastAPI.

Choose **Explore a demo** to exercise the entire pipeline without an API key or any API charges. The demo always uses an explicitly fictional library scenario. It includes a misleading aggregate statistic, a check that rejects it, a corrected claim version, opposing arguments, a hybrid verdict, and a reversible recommendation. All demo documents and findings are synthetic; the demo is not evidence of real-world accuracy.

### Use your existing ChatGPT Plus plan

1. Open the app at **http://127.0.0.1:8000** and choose **Continue with ChatGPT**. Select the account with Plus and approve Debate Bro's plan-usage permission on OpenAI's screen. A Free account in your browser can differ from the account signed into Codex.
2. Open **Manage usage** in the app. In ChatGPT Usage settings, set Debate Bro's **credits allowance to zero** to use included usage only. Account settings, rather than this app, govern credit spending. Do not purchase credits or enable automatic reload to use this route.
3. Enter a question and start research. Both lead researchers use **GPT-6 Astra at xhigh**; judgment uses **Astra high**, fact checking/final consistency use **GPT-6.1 Sol xhigh**, debate/decision analysis/dispute planning use **Sol high**, and framing/coverage use **Sol medium**. Both teams receive identical settings. Astra and Sol availability is checked against your account before starting; no model is silently substituted. The Connection disclosure shows the assignments.

This is OpenAI's [official local personal-project integration](https://developers.openai.com/siwc/token-sharing-open-source). No API key is required. OAuth credentials are saved in `data/chatgpt/credentials.json` with owner-only permissions. They are never exposed to the browser, reports, application records, or source control. Sign-out attempts remote session revocation and clears local tokens while retaining the account registration for reconnection. Do not share this file or include it in support requests.

**Plus does not receive the Pro API-credit benefit.** Plan usage is a separate supported route. Plus's five-hour allowance is shared across apps, including Codex; this application has no separate quota. Check [ChatGPT Usage settings](https://chatgpt.com/settings/usage) for actual availability and per-app controls. A plan-limit error preserves partial work and pauses the run; Resume retries later without switching to API billing. No reset time is inferred from the error.

Separately billed API calls are **disabled by default**, even if `OPENAI_API_KEY` exists. An operator must explicitly set both `DEBATE_ALLOW_PAID_API=true` and `OPENAI_API_KEY` to use the API provider through the typed API/CLI. There is no automatic billing fallback. Eligible Pro/Business subscribers can separately investigate the [API credit benefit](https://developers.openai.com/api/docs/guides/chatgpt-api-credits); it does not apply to Plus.

### Development

Run `uv run debate-bro serve` in one terminal and `npm run dev` from `frontend/` in another. Vite proxies `/api` to port 8000.

Settings and defaults:

| Setting | Default |
| --- | --- |
| Data directory | `./data` (`DEBATE_DATA_DIR`) |
| Backend address | `127.0.0.1:8000` (`serve --host/--port`) |
| Plan request ceiling | 80 requests/topic, including retries; 12 reserved for completion |
| API spending ceiling (only when explicitly enabled) | $15/topic |
| API cost target | $2–5, unmeasured until real pilots are performed |
| Follow-up investigation | At most two cycles |
| Initial research per track | Up to three evidence-led search-and-read cycles; up to two queries per cycle, within equal request/budget allowances |
| Retry policy | At most two automatic retries for retryable failures |
| Source fetch | 25-second request timeout, public HTTP(S), up to 20 MB |
| Robustness suite | Separate, explicitly initiated 180 plan requests or $15 API aggregate ceiling |

The role runner supports bounded requests for additional registered source passages. Sources that require JavaScript, sign-in, a paywall, unsupported media, or OCR remain access gaps. There is no browser-rendering or OCR fallback in v1.

Each side has one lead researcher who first creates a private search plan, then covers background, competing explanations, and consequences. Plans identify primary-source standards, methodological pitfalls, decisive tests, and stopping conditions. Researchers actively seek disconfirming evidence, trace repeated coverage back to underlying studies, and distinguish causal support from associations and value judgments. They stop early on adequate coverage or diminishing material findings; stronger models do not guarantee accurate research. Higher reasoning consumes more shared plan allowance and can take longer. Role configurations and model-specific API price snapshots are frozen per run; old runs retain their prior settings. The typed API supports `model_policy: "uniform"` for explicit uniform-model comparisons.

## Evidence and execution guarantees

- **Independent research:** run- and team-scoped retrieval prevents either team from reading its opponent's private work. Both independent briefings are committed before publication.
- **Paired rounds:** frozen round inputs and atomic publication keep a completed draft private if its counterpart fails. Successful drafts and completed provider responses are reused on recovery.
- **Registered evidence:** source files, extracted text, passage locations, retrieval dates, and hashes are retained. Quotes must occur verbatim in saved source text. Claims cannot cite invented or inaccessible passage IDs.
- **Corrections:** versioned claims preserve their original wording. Claim → argument → finding → judgment/recommendation dependencies propagate stale flags. Working contexts suppress stale briefing summaries while retaining original transcripts for inspection.
- **Checking dimensions:** citation support, methodological reliability, and normative consistency are recorded separately. Normative premises cannot receive an empirical “supported” status.
- **Judgment gates:** decisive empirical findings require current checked claims. Recommendations must reference registered findings. New factual claims from the judge or analyst go through verification; a failed consistency review prevents a completed status.
- **Topic isolation:** records and tool access are scoped to one run. The robustness value-priority variant explicitly imports public evidence with remapped IDs and a reuse disclosure.
- **Recovery:** durable LangGraph checkpoints contain references and execution state. Running jobs become interrupted after a server restart, and Resume continues from the durable stage. Committed artifacts are idempotent; this is not an exactly-once guarantee for remote API billing.
- **Cancellation:** stops new work, cancels the active workflow, and retains partial artifacts. Requests with unknown remote outcomes retain their maximum cost reservation as an uncertain charge.

Retrieved content is treated as untrusted evidence. Public-source retrieval rejects local/private addresses and validates redirect destinations. The API is intended for one user on localhost, not public hosting.

## Spending controls

ChatGPT-plan runs record tokens, searches, attempts, and uncertainty, with **zero separately billed API dollars**. Dollar values do not measure how much ChatGPT plan allowance or credits were consumed. App request limits are additional bounds, not a promise of remaining plan quota. Concurrent requests reserve a request slot atomically; both teams have equal request allowances. Optional suites have their own aggregate request ceiling. The preview plan route does not accept a server-enforced `max_output_tokens` or `max_tool_calls`; its stream is bounded by the request timeout and account policy, and request counts do not bound tokens within a request. Per-app credit controls in ChatGPT remain essential for included-only usage.

All paid model and search calls pass through the same gateway. It reserves estimated maximum input, output, and tool costs before dispatch, including concurrent in-flight calls. It reconciles reported input, cached input, output (including reasoning), and search usage afterward. Unknown outcomes retain their reservation rather than being treated as free.

Each run freezes its pricing configuration and model settings. Both teams receive equal allowances. Completion funding is withheld from optional research; the single-researcher baseline receives the combined team allowance. Suite calls additionally check their aggregate ceiling. Model prices must be configured before an unfamiliar model can be used.

Costs are application estimates of gross API charges **before subscription credits**, not a replacement for the provider's bill. The initial table uses [OpenAI standard pricing](https://developers.openai.com/api/docs/pricing) checked on November 8, 2026. Update the table and price version when pricing changes. Reconcile a small real pilot with provider billing before relying on cost predictions at scale.

## Review and export

The minimal interface has one topic box, the outcome directly underneath, opposing points, and a research summary. Expand evidence/corrections or the transcript when needed. Click a claim or passage reference to open the linked evidence. Saved full source text remains available. Optional suite controls stay in the API while the UI is kept simple.

Reports export as standalone HTML, Markdown, or JSON, including the issue map, synthesis, competing submissions, disputes, checked findings, substantive winner, opposing points, practical step, reversal conditions, limitations, usage, and evidence appendix. Partial reports retain their incomplete status.

Robustness tests cover reversed side/order assignment, equivalent question framing, and alternative value priorities. The values variant reuses evidence; the other variants research anew. A comparison helper assesses substantive positions rather than team letters. Unfinished or budget-limited comparisons are marked incomplete. Shared evidence or model agreement is never presented as independent corroboration.

## API and extension points

Interactive OpenAPI documentation: **http://127.0.0.1:8000/docs**.

Core routes:

```text
POST /api/runs
GET  /api/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/events              # SSE; Last-Event-ID supported
POST /api/runs/{id}/cancel
POST /api/runs/{id}/resume
POST /api/runs/{id}/clarify             # answer a blocking framing question
GET  /api/runs/{id}/records[?kind=claim]
GET  /api/runs/{id}/records/{record_id}
GET  /api/runs/{id}/sources/{source_id}/text
GET  /api/runs/{id}/usage
GET  /api/runs/{id}/report?format=html|markdown|json
POST /api/runs/{id}/robustness          # separate aggregate budget_usd
POST /api/runs/{id}/evaluations         # separate aggregate budget_usd
```

Frontend DTOs are generated from FastAPI's OpenAPI schema with `uv run debate-bro types`. Regenerate them when public types change.

To add a provider, implement `ModelProvider.generate` and `WebDiscovery.discover`, returning typed output and measured usage. Register its capabilities, pricing, configuration validation, and factory. Workflow and evidence code do not depend on an OpenAI client. `SourceFetcher` is independently replaceable. Keep these trust boundaries and add contract tests for the new adapter.

SQLite stores application records and usage separately from LangGraph's checkpoint database. Raw evidence lives in `data/evidence/<run-id>/`. Back up the entire data directory together. The initial database schema is created automatically; future incompatible schema changes require an explicit migration.

## Validation

```sh
uv run ruff check debate_bro tests
uv run pytest -q
cd frontend
npm run build
npm run test:e2e
```

The browser suite uses locally installed Google Chrome. It starts its own backend on port 8001 with an isolated temporary data directory. Routine tests use deterministic sources and mocked API responses, never paid calls.

Run the offline evaluation tool:

```sh
uv run debate-bro evaluate --demo
```

It compares the full workflow against a single neutral researcher, independent research synthesized without debate, and fewer debate rounds. The HTML report includes per-variant usage and correction/checking metrics. A deliberately explicit `--no-demo` starts a paid evaluation: its original-topic allowance and separate suite allowance are displayed before execution. Both can be limited using `--budget`.

Automated metrics report checking coverage, source-reference integrity, correction activity, elapsed time, and gross cost. Factual accuracy, completeness of opposing evidence, and recommendation usefulness require independent adjudication; they are not inferred from model confidence. Same-family checkers can share blind spots. Controlled benchmark improvements do not establish that debate reliably resolves broad policy or moral questions.

The initial acceptance suite covers private contexts, fabricated citations and quotes, transitive corrections, normative claims, concurrent budget reservations, uncertain charges, restart, cancellation, paired failure recovery, SDK request contracts, source provenance, exports, mobile layout, and an end-to-end synthetic assessment.
