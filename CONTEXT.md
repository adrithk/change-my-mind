# Change My Mind — working context

Updated November 8, 2026. This file is the handoff for continued iteration. Read it alongside README.md; do not rely on an old chat being available.

## User intent and latest decisions

- Build a local, single-user research-first debate system that handles broad questions, researches opposing positions, checks material claims, judges substantive support, and offers one practical takeaway with inspectable evidence.
- Keep iteration quick and changes simple. Use focused smoke checks; avoid adding test infrastructure or running paid benchmarks without a separate explicit action.
- Product name: **Change My Mind**. Latest UI preference: primarily navy, with white text, navy input surfaces, and restrained purple/red accents. One topic box asking **What topic do you want to debate?**. Put the outcome directly underneath, then the opposing points and research summary. Evidence and transcripts stay expandable. Use a ChatGPT/Codex-style sidebar and rounded composer with a clean system font.
- Use the user's existing **ChatGPT Plus** plan. Earlier references to Pro were clarified to mean Plus. Do not assume Pro benefits or spend API funds.
- GitHub repository: `adrithk/change-my-mind`. It is now **public**, as explicitly requested after creation. GitHub repository names cannot contain spaces.

## Running locally

```sh
uv sync
cd frontend
npm ci
npm run build
cd ..
uv run change-my-mind serve
```

Open http://127.0.0.1:8000. Use one backend process. The backend serves the built frontend. `make setup` / `make run` are convenience commands. Frontend development uses Vite's proxy for both `/api` and `/auth`.

## Connection and spending

- **Continue with ChatGPT** starts the official public-client OAuth flow for local personal projects. No API key is needed. The user must sign into the account that actually has Plus and approve plan usage on OpenAI's screen.
- A browser account may differ from the paid account in Codex. Do not infer a paid plan from a logged-in browser alone.
- Sign-in is implemented in `change_my_mind/chatgpt_auth.py`: stable host UUID, state + nonce + PKCE, issued dynamic client registration, validated OIDC signature/issuer/audience/expiry/nonce, granted-scope checks, serialized rotating refresh, protected atomic storage, saved account selection, and session revocation on sign-out.
- Credentials live only in ignored `data/chatgpt/credentials.json` with directory mode 0700 and file mode 0600. Never commit, print, share, or put them into app records. HTTP access logging is disabled in the normal launch command so callback codes are not logged.
- The OAuth provider calls public `/v1/responses`, with streaming and `store:false`. It waits for `response.completed`, uses `instructions` instead of system messages, and omits unsupported plan-route fields. Model catalog comes from the selected account's public `/v1/models` response.
- **Paid API-key calls are disabled by default**, even when an API key is present. No automatic billing fallback exists. Both `DEBATE_ALLOW_PAID_API=true` and `OPENAI_API_KEY` would be needed for a separately billed API run. Do not enable them while the user's no-extra-spend constraint remains.
- Plus does not qualify for the separate Pro/Business API-credit benefit. Its five-hour plan allowance is shared across apps, including Codex. A usage-limit error may be per-app or plan-wide; do not invent a reset time.
- ChatGPT can also allow use of an app's credits balance. **For included usage only, the user must set Change My Mind's credits allowance to zero in ChatGPT Settings → Usage.** The app cannot change that account setting. Never purchase credits or enable auto-reload without separate authorization.
- Plan mode counts request attempts (including retries) rather than pretending API dollar prices measure subscription usage. Default: 80/topic, 12 reserved for finalization, equal team allowances. Separately initiated suites default to 180 aggregate plan requests. Account limits still apply and a full debate can exhaust them.
- The plan preview does not support server-enforced output-token or hosted-search-call caps. The timeout and request ceiling are additional bounds, not a guarantee of token usage within one request.
- Interrupted/limited runs preserve evidence and successful private drafts. Resume does not switch billing paths.

Official integration references:
- https://developers.openai.com/siwc/token-sharing-open-source
- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://developers.openai.com/api/docs/guides/chatgpt-api-credits

## Model and role configuration

The user replaced the original uniform model setup with **research-first role scaling** and explicitly confirmed **GPT-6 Astra at xhigh** for the two initial lead researchers. Both models were verified in the connected account's model catalog. Both teams must use identical configuration and resource limits.

| Work | Model | Reasoning |
| --- | --- | --- |
| Two lead researchers, research planning, initial searches/briefings, targeted research follow-up | GPT-6 Astra | xhigh |
| Debate rounds | GPT-6.1 Sol | high |
| Fact checker, checking searches, final evidence-consistency check | GPT-6.1 Sol | xhigh |
| Substantive judge and optional suite comparison | GPT-6 Astra | high |
| Decision analyst and dispute planner | GPT-6.1 Sol | high |
| Framing and coverage helpers | GPT-6.1 Sol | medium |

`model_policy.py` defines the presets. `Store.create_run` freezes `role_configs` and `pricing_by_model`. Gateway resolves each call by role/context and logs the exact configuration, reasoning, tokens and actual model. Do not change settings mid-run. Old runs without a role map retain their original uniform configuration; API `model_policy: "uniform"` remains available for comparisons. Required account models are checked before queuing, without silent substitution or billing fallback. The minimal UI puts assignments inside Connection. High-effort work can use substantially more shared allowance; request limits remain unchanged.

Nine specialist roles:
1. Team A: lead researcher and opening debater.
2. Team A: responses/rebuttals debater.
3. Team A: corrected closing debater.
4. Team B: lead researcher and opening debater.
5. Team B: responses/rebuttals debater.
6. Team B: corrected closing debater.
7. Independent-context fact checker.
8. Substantive judge.
9. Decision analyst.

Framing, neutral coverage review, investigation planning, consistency checking, and suite comparison are bounded helpers with the assignments above. These are roles/context boundaries within the application, not nine independently trained models. Shared model-family limitations must remain disclosed.

## Architecture and files

- Python 3.12, uv, FastAPI; React/TypeScript + Vite.
- `config.py`, `schemas.py`: configuration, Pydantic structured outputs and API records.
- `store.py`: SQLAlchemy + SQLite records, dependencies, visibility, publication barriers, atomic usage reservations and corrections.
- `chatgpt_auth.py`: protected OAuth sessions, account catalog.
- `providers.py`: provider-neutral model/research interfaces, OpenAI API adapter, ChatGPT plan adapter, invocation caching and accounting gateway.
- `model_policy.py`: per-role model/reasoning presets, frozen assignment resolution and legacy uniform fallback.
- `evidence.py`: public HTTP source retrieval, readable HTML/PDF extraction, exact quotations, passage registration, local original files.
- `workflow.py`: persistent LangGraph workflow. Checkpoints reference records, not full source documents.
- `jobs.py`: durable queue, one active topic, paired team tasks, cancellation/recovery, optional suites.
- `reports.py`, `metrics.py`: HTML/Markdown/JSON exports and inspectable measurements.
- `demo.py`: deterministic offline provider and explicitly fictional library evidence.
- `api.py`, `cli.py`: local API, SSE, lifecycle, command-line setup/types/evaluation.
- `frontend/src/App.tsx`, `styles.css`: intentionally simple single-page interface.
- `frontend/src/api.generated.ts`: generated with `uv run change-my-mind types` after public schema changes.

## Workflow and evidence invariants

1. Preserve original question; make provisional issue map, working positions, evaluation principles.
2. Two independent lead researchers first create private search plans, then each covers background, explanations, and consequences. Plans specify primary-source standards, disconfirming searches, methodological pitfalls, decisive tests, and stopping conditions. Up to three evidence-led cycles per initial track, within equal existing resource limits; adequate coverage or diminishing material findings can stop earlier.
3. Commit both private briefings before either can see the other.
4. Neutral coverage review creates a shared dossier preserving disagreements and access gaps.
5. Paired openings, responses, and one rebuttal exchange. Freeze inputs and publish paired valid drafts atomically. Retain successful private drafts if the other fails.
6. Check material claims from research and every round; publish corrections and reconsider affected arguments.
7. Investigate decisive unresolved disputes, at most two follow-up cycles; share checked evidence.
8. Corrected closings and checks on new claims.
9. Point-by-point substantive judgment, separate decision analysis, final evidence-consistency gate.

Application retrieval permissions enforce privacy. Citations must resolve to registered retrieved passages. Snippets and inaccessible documents remain unverified. Exact quotation matching does not establish a source's truth. Distinguish citation support, methodological reliability, causal support, denominators, repeated underlying studies, normative disagreement, and inconclusive evidence.

Claims are versioned; invalidation propagates through argument → finding → judgment/recommendation dependencies. Stale and superseded artifacts remain inspectable and must not support decisive findings. A hybrid/overlooked alternative may win. Judge outputs include confidence, margin, provisional status, surviving opposition, value assumptions and reversal conditions. Analyst outputs include one insight, actor, next step, tradeoffs, success measure and review conditions.

## Interfaces and deferred scope

Core API: create/list/inspect runs; persisted SSE events; cancel/resume; clarification; records/source text; usage; HTML/Markdown/JSON export. `/api/runs/{id}/robustness` and `/evaluations` remain available for separately initiated comparison suites. The minimal UI intentionally leaves suite controls out for now.

Robustness: side/order reversal, equivalent phrasing, alternative values (explicit evidence reuse). Evaluation baselines: one researcher, research without debate, reduced rounds. Do not assume debate improves accuracy; this requires measurement.

Deferred: hosted deployment, accounts for app users, authenticated/paywalled research, uploads, OCR, browser-rendered retrieval, additional model vendors. Keep it local and single-user.

## Validation and next iteration

- Earlier backend/workflow suite: 22 tests passed before the plan integration.
- ChatGPT integration: 9 deterministic focused checks passed (OAuth bindings/scopes, refresh rotation, model catalog, streaming contract, quota stop, concurrent request accounting, paid-call guard).
- The current UI receives a production build and one browser smoke flow (topic input, connection setup, complete demo, evidence link, export, mobile fit). Routine checks use mocks/synthetic evidence and incur no model charges.
- **Real account authorization and a live research pilot are not established by these offline tests.** Complete the user's OAuth grant, verify the account model catalog and included-only account setting, then try one bounded topic. Report real failures rather than claiming the synthetic demo validates research quality.
- Keep future changes focused. Do not rerun paid robustness suites or extensive benchmarks without a separately initiated aggregate allowance.
- Role-scaling validation: 17 focused offline checks passed, including a complete synthetic workflow that verifies exactly two Astra-xhigh lead research roles, role-specific debate/check/judge settings, original-run fallback and persisted search routing. Production frontend build passed. No live inference or paid evaluation was needed for this change.

## Read-only developer trace

`uv run change-my-mind trace` starts a separate inspector on http://127.0.0.1:8002; append `#<run-id>` to select a debate. It reads the same SQLite records and usage, including private submitted outputs, without starting jobs/providers or changing agent visibility. Calls show role/model/effort, query, token usage, errors and completed structured responses; artifacts expose arguments, objections, corrections and findings. Output is available only after calls complete; hidden chain-of-thought and full original prompt bodies are not stored/shown. Keep it bound to loopback. Source: `change_my_mind/developer.py`; focused visibility/access test: `tests/test_developer.py`. It can be started without interrupting the main backend.

## Low-usage validation before live runs

The user asked to verify the full system without consuming substantial account usage before another live run. Run deterministic/offline workflow and browser checks first. The latest check passed 42 backend tests and one full browser flow using fictional demo evidence; the production frontend build also passed. Preserve the distinction between engine validation and factual/live-provider validation. The single short Sol-low connectivity probe was blocked by `subscription_sharing_usage_limit_exceeded`; live discovery is still unverified. Do not restart a full expensive debate to test connectivity. Keep the saved run stopped, retain partial records, and use one bounded small source-discovery check when account access permits. No credit/billing settings were changed. Local validation details are in ignored `data/validation/offline-validation.md`.
