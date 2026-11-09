import { useEffect, useState } from "react";
import {
  ArrowUp,
  LoaderCircle,
  MessageSquare,
  SquarePen,
  X,
} from "lucide-react";
import {
  api,
  request,
  type RecordView,
  type RunView,
  type Settings,
} from "./api";

type Doc = Record<string, any>;
const human = (value: string) => value.replaceAll("_", " ");
const done = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "needs_review",
  "budget_exhausted",
]);
const stages: Record<string, string> = {
  framing: "Mapping the question",
  research: "Researching both sides",
  dossier: "Reviewing the evidence",
  openings: "Opening arguments",
  responses: "Challenging the arguments",
  rebuttals: "Rebuttals",
  follow_up: "Checking unresolved points",
  closings: "Corrected closing arguments",
  judgment: "Weighing the evidence",
  insight: "Finding the practical takeaway",
  consistency: "Checking the outcome",
  complete: "Complete",
};

function reveal(id: string) {
  const element = document.getElementById(id);
  let parent: HTMLElement | null = element;
  while (parent) {
    if (parent instanceof HTMLDetailsElement) parent.open = true;
    parent = parent.parentElement;
  }
  element?.scrollIntoView({ behavior: "smooth", block: "center" });
}
function Refs({
  ids = [],
  records,
}: {
  ids?: string[];
  records: RecordView[];
}) {
  return (
    <span className="refs">
      {ids.map((id) => {
        const record = records.find((r) => r.id === id);
        return record ? (
          <a
            key={id}
            href={`#${id}`}
            onClick={(e) => {
              e.preventDefault();
              reveal(id);
            }}
          >
            {human(record.kind)} {id.slice(-5)}
          </a>
        ) : null;
      })}
    </span>
  );
}
function Lines({ items = [] }: { items?: string[] }) {
  return items.length ? (
    <ul>
      {items.map((text, i) => (
        <li key={i}>{text}</li>
      ))}
    </ul>
  ) : null;
}

export default function App() {
  const [settings, setSettings] = useState<Settings>();
  const [models, setModels] = useState<
    Array<{ slug: string; display_name: string }>
  >([]);
  const model = "gpt-6.1-sol";
  const [question, setQuestion] = useState("");
  const [selected, setSelected] = useState(location.hash.slice(1));
  const [run, setRun] = useState<RunView>();
  const [history, setHistory] = useState<RunView[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [welcome, setWelcome] = useState(false);
  const [clarification, setClarification] = useState("");
  const connected = Boolean(settings?.chatgpt.active?.plan_enabled);
  const profiles = settings?.role_profiles || {};
  const modelsReady =
    Object.values(profiles).length > 0 &&
    Object.values(profiles).every((p) =>
      models.some((m) => m.slug === p.model),
    );
  const records = run?.records || [];
  const latest = (kind: string): Doc | undefined =>
    [...records]
      .reverse()
      .find((r) => r.kind === kind && !r.stale && !r.superseded)?.data;
  const frame = latest("frame"),
    judgment = latest("judgment"),
    insight = latest("recommendation");
  const active = Boolean(run && !done.has(run.status));
  const synthetic = (run?.config.model_config as Doc)?.provider === "demo";

  useEffect(() => {
    const syncHash = () => setSelected(location.hash.slice(1));
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, []);

  async function refreshSettings() {
    setSettings(await api.settings());
  }
  useEffect(() => {
    const load = () => refreshSettings().catch((e) => setError(e.message));
    load();
    window.addEventListener("focus", load);
    return () => window.removeEventListener("focus", load);
  }, []);
  useEffect(() => {
    const load = () =>
      api
        .runs()
        .then(setHistory)
        .catch((e) => setError(e.message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    setModels([]);
    if (!connected) return;
    let disposed = false;
    request<{ models: Array<{ slug: string; display_name: string }> }>(
      "/api/auth/chatgpt/models",
    )
      .then(({ models }) => {
        if (!disposed) setModels(models);
      })
      .catch((e) => {
        if (!disposed) setError(e.message);
      });
    const id = settings?.chatgpt.active?.id;
    if (id && !localStorage.getItem(`chatgpt-welcome-${id}`)) setWelcome(true);
    return () => {
      disposed = true;
    };
  }, [connected, settings?.chatgpt.active?.id]);
  useEffect(() => {
    if (!selected) {
      setRun(undefined);
      return;
    }
    let disposed = false;
    api
      .run(selected)
      .then((r) => {
        if (!disposed) {
          setRun(r);
          setQuestion(r.question);
        }
      })
      .catch((e) => setError(e.message));
    const refresh = () =>
      api
        .run(selected)
        .then((r) => {
          if (!disposed) setRun(r);
        })
        .catch((e) => {
          if (!disposed) setError(e.message);
        });
    const stream = new EventSource(`/api/runs/${selected}/events`);
    stream.onmessage = refresh;
    const timer = setInterval(refresh, 2500);
    return () => {
      disposed = true;
      stream.close();
      clearInterval(timer);
    };
  }, [selected]);

  function select(id: string) {
    setSelected(id);
    setError("");
    historyReplace(id);
  }
  function historyReplace(id: string) {
    window.history.replaceState(null, "", id ? `#${id}` : location.pathname);
  }
  async function connect(profileId?: string) {
    setBusy(true);
    setError("");
    try {
      const response = await request<{ authorization_url: string }>(
        "/api/auth/chatgpt/start",
        { profile_id: profileId || null },
      );
      location.assign(response.authorization_url);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  async function start(demo = false) {
    setBusy(true);
    setError("");
    try {
      const created = await api.create({
        question: demo
          ? settings?.demo_question || "Library evening hours"
          : question,
        model_config: {
          provider: demo ? "demo" : "chatgpt",
          model,
          reasoning: "medium",
          max_output_tokens: 6000,
        },
        budget_usd: 15,
        request_limit: 80,
        mode: "full",
        model_policy: "research_first",
      });
      setRun(created);
      select(created.id);
      setHistory(await api.runs());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function action(name: string, body: unknown = {}) {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      await api.action(run.id, name, body);
      setRun(await api.run(run.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function switchAccount(id: string) {
    try {
      await request("/api/auth/chatgpt/select", { profile_id: id });
      await refreshSettings();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function disconnect() {
    try {
      const result = await request<{ message: string }>(
        "/api/auth/chatgpt/disconnect",
        { profile_id: settings?.chatgpt.active?.id },
      );
      await refreshSettings();
      if (result.message !== "Signed out.") setError(result.message);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function dismissWelcome() {
    const id = settings?.chatgpt.active?.id;
    if (id) localStorage.setItem(`chatgpt-welcome-${id}`, "1");
    setWelcome(false);
  }

  return (
    <>
      <aside className="sidebar" aria-label="Debate navigation">
        <a
          className="sidebar-brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            select("");
            setQuestion("");
          }}
        >
          <MessageSquare size={20} /> Change My Mind
        </a>
        <button
          className="new-debate"
          onClick={() => {
            select("");
            setQuestion("");
            document.getElementById("topic")?.focus();
          }}
        >
          <SquarePen size={17} /> New debate
        </button>
        <div className="history-label">Your debates</div>
        <nav className="debate-history" aria-label="Recent debates">
          {history.length ? (
            history.map((r) => (
              <button
                key={r.id}
                className={selected === r.id ? "selected" : ""}
                onClick={() => select(r.id)}
                title={r.question}
              >
                {r.question}
              </button>
            ))
          ) : (
            <p className="history-empty">Your debates will appear here.</p>
          )}
        </nav>
        <div className="sidebar-footer">
          <span className="status-dot" /> A little more perspective.
        </div>
      </aside>
      <header className="header">
        <a
          className="wordmark"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            select("");
            setQuestion("");
          }}
        >
          Change My Mind
        </a>
        <div className="header-actions">
          {history.length > 0 && (
            <select
              aria-label="Past debates"
              value={selected}
              onChange={(e) => select(e.target.value)}
            >
              <option value="">Past debates</option>
              {history.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.question.slice(0, 65)}
                </option>
              ))}
            </select>
          )}
          <button
            className="connect"
            disabled={busy}
            onClick={() => connect(settings?.chatgpt.active?.id)}
          >
            {connected ? "ChatGPT connected" : "Continue with ChatGPT"}
          </button>
        </div>
      </header>
      <main className={run ? "has-debate" : "empty-state"}>
        <section className="composer" aria-label="Start a debate">
          <h1>
            <label htmlFor="topic">What topic do you want to debate?</label>
          </h1>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              start();
            }}
          >
            <textarea
              id="topic"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask a big question…"
              maxLength={6000}
              rows={3}
            />
            <div className="composer-bottom">
              <span>Research both sides. Follow the evidence.</span>
              <button
                className="primary"
                disabled={
                  busy ||
                  active ||
                  !connected ||
                  !modelsReady ||
                  question.trim().length < 5
                }
              >
                {busy ? (
                  <LoaderCircle size={16} className="spin" />
                ) : (
                  <>
                    Debate <ArrowUp size={17} />
                  </>
                )}
              </button>
            </div>
          </form>
          <div className="composer-meta">
            <span>
              {connected
                ? "Using ChatGPT plan"
                : "Connect your ChatGPT Plus account to start."}
            </span>
            {connected && (
              <a
                href="https://chatgpt.com/settings/usage"
                target="_blank"
                rel="noreferrer"
              >
                Manage usage ↗
              </a>
            )}
            <button onClick={() => start(true)} disabled={busy || active}>
              Try a demo
            </button>
          </div>
          <details className="settings">
            <summary>Connection</summary>
            <div className="settings-content">
              <p>
                Two independent lead researchers use Astra at xhigh to plan
                searches, inspect original sources, and investigate contrary
                evidence. Both teams use identical settings and limits.
              </p>
              <table className="role-models">
                <thead>
                  <tr>
                    <th>Role</th>
                    <th>Model</th>
                    <th>Reasoning</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ["Two lead researchers", "researcher"],
                    ["Debaters", "debater"],
                    ["Fact checker & final check", "checker"],
                    ["Judge", "judge"],
                    ["Decision analyst", "analyst"],
                    ["Dispute planner", "investigator"],
                    ["Framing & coverage", "helper"],
                  ].map(([label, role]) => (
                    <tr key={role}>
                      <td>{label}</td>
                      <td>
                        {profiles[role]?.model.replace("gpt-", "GPT-") || "…"}
                      </td>
                      <td>{profiles[role]?.reasoning || "…"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p>
                Higher reasoning can take longer and consume more of your shared
                plan allowance.
              </p>
              {connected && (
                <p>
                  {settings?.chatgpt.active?.label} · 80 requests maximum per
                  debate. Set this app’s credit allowance to zero in{" "}
                  <a
                    href="https://chatgpt.com/settings/usage"
                    target="_blank"
                    rel="noreferrer"
                  >
                    ChatGPT Usage
                  </a>{" "}
                  for included usage only. Paid API requests are disabled.
                </p>
              )}
              {connected && !modelsReady && (
                <p className="notice">
                  This configuration needs GPT-6 Astra and GPT-6.1 Sol access.
                  Available models are checked before starting; there is no
                  silent model substitution.
                </p>
              )}
              {!!settings?.chatgpt.profiles.length && (
                <label>
                  Account{" "}
                  <select
                    value={settings.chatgpt.active?.id || ""}
                    onChange={(e) => switchAccount(e.target.value)}
                    disabled={active}
                  >
                    <option value="" disabled>
                      Choose saved account
                    </option>
                    {settings.chatgpt.profiles.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.label} · {p.registration}
                        {!p.connected ? " · signed out" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="inline-actions">
                <button onClick={() => connect()}>
                  Connect another account
                </button>
                {connected && (
                  <button disabled={active} onClick={disconnect}>
                    Sign out
                  </button>
                )}
              </div>
            </div>
          </details>
        </section>
        {error && (
          <div className="notice" role="alert">
            <span>{error}</span>
            <button aria-label="Dismiss error" onClick={() => setError("")}>
              <X size={16} />
            </button>
          </div>
        )}
        {run && (
          <article className="debate-result" aria-label="Debate summary">
            <div className="run-status">
              <span>
                {active && <LoaderCircle className="spin" size={14} />}{" "}
                {active
                  ? stages[run.stage] || human(run.stage)
                  : human(run.status)}
              </span>
              <span>
                {synthetic
                  ? "Fictional demo · no charges"
                  : `${run.requests_used}/80 plan requests`}
              </span>
              {active && (
                <button onClick={() => action("cancel")} disabled={busy}>
                  Stop
                </button>
              )}
              {(["interrupted", "failed", "cancelled"].includes(run.status) || (run.status === "needs_review" && ["framing", "research"].includes(run.stage) && run.error === "Specialist still needs source details after the bounded retrieval allowance")) && (
                <button onClick={() => action("resume")} disabled={busy}>
                  Resume
                </button>
              )}
            </div>
            {synthetic && (
              <p className="notice">
                This demo uses fictional sources and findings to show how the
                system works.
              </p>
            )}
            {run.error && (
              <p className="notice">
                {run.error}{" "}
                {(run.error.includes("subscription_sharing_") || run.error.includes("ChatGPT usage limit") || run.error.includes("sharing limit")) && <a
                  href="https://chatgpt.com/settings/usage"
                  target="_blank"
                  rel="noreferrer"
                >
                  Manage usage ↗
                </a>}
              </p>
            )}
            {judgment ? (
              <section className="outcome">
                <span className="eyebrow">
                  {run.status === "completed"
                    ? "Final outcome"
                    : "Provisional outcome"}
                </span>
                <h2>{judgment.best_supported_position}</h2>
                <p className="muted">
                  {judgment.confidence} confidence · {judgment.margin} margin
                  {judgment.provisional ? " · provisional" : ""}
                </p>
                {insight && (
                  <div className="takeaway">
                    <h3>The takeaway</h3>
                    <p>{insight.insight}</p>
                    <p>
                      <strong>Next step:</strong> {insight.next_step}
                    </p>
                    <Refs
                      ids={[
                        ...(insight.supporting_finding_ids || []),
                        ...(insight.claim_ids || []),
                      ]}
                      records={records}
                    />
                  </div>
                )}
                {(judgment.findings || []).map((finding: Doc, i: number) => (
                  <p key={i}>
                    {finding.text}{" "}
                    <Refs ids={finding.claim_ids} records={records} />
                  </p>
                ))}
              </section>
            ) : (
              <section className="outcome">
                <span className="eyebrow">
                  {active ? "Debate in progress" : "Partial assessment"}
                </span>
                <h2>
                  {active
                    ? "Following the evidence."
                    : "The outcome is not ready yet."}
                </h2>
                <p className="muted">
                  Completed work is saved as the debate progresses.
                </p>
              </section>
            )}
            {frame && (
              <section className="points">
                <h2>What’s being debated</h2>
                <p>{frame.working_question}</p>
                <div className="sides">
                  {(["A", "B"] as const).map((side) => {
                    const arguments_ = records.filter(
                      (r) =>
                        r.kind === "argument" &&
                        r.team === side &&
                        !r.stale &&
                        !r.superseded,
                    );
                    const closing = arguments_.filter(
                      (r) => r.stage === "closings",
                    );
                    return (
                      <div key={side}>
                        <span className="eyebrow">Position {side}</span>
                        <h3>
                          {side === "A" ? frame.position_a : frame.position_b}
                        </h3>
                        <ul>
                          {(closing.length ? closing : arguments_)
                            .slice(-4)
                            .map((r) => (
                              <li key={r.id}>
                                {String(r.data.conclusion)}{" "}
                                <Refs
                                  ids={r.data.claim_ids as string[]}
                                  records={records}
                                />
                              </li>
                            ))}
                        </ul>
                      </div>
                    );
                  })}
                </div>
              </section>
            )}
            {latest("coverage") && (
              <section>
                <h2>Research summary</h2>
                <p>{latest("coverage")?.synthesis}</p>
              </section>
            )}
            {judgment && (
              <details>
                <summary>Uncertainty & what could change the outcome</summary>
                <h3>Surviving opposing points</h3>
                <Lines items={judgment.opposing_points} />
                <h3>Reversal conditions</h3>
                <Lines items={judgment.reversal_conditions} />
                <h3>Value assumptions</h3>
                <Lines items={judgment.value_assumptions} />
                {insight && (
                  <>
                    <h3>Tradeoffs</h3>
                    <Lines items={insight.costs_risks_tradeoffs} />
                    <p>
                      <strong>Review when:</strong> {insight.review_trigger}
                    </p>
                  </>
                )}
              </details>
            )}
            {frame?.clarification_needed && run.status === "needs_review" && (
              <form
                className="clarification"
                onSubmit={(e) => {
                  e.preventDefault();
                  action("clarify", { answer: clarification });
                }}
              >
                <label htmlFor="clarification">
                  {frame.clarification_needed}
                </label>
                <input
                  id="clarification"
                  value={clarification}
                  onChange={(e) => setClarification(e.target.value)}
                />
                <button
                  className="primary"
                  disabled={busy || !clarification.trim()}
                >
                  Continue
                </button>
              </form>
            )}
            <details className="evidence">
              <summary>
                Evidence & corrections (
                {records.filter((r) => r.kind === "claim").length} claims)
              </summary>
              {records
                .filter((r) =>
                  ["claim", "passage", "finding", "source", "check"].includes(
                    r.kind,
                  ),
                )
                .map((r) => (
                  <details id={r.id} key={r.id}>
                    <summary>
                      {human(r.kind)} ·{" "}
                      {String(
                        r.data.text ||
                          r.data.title ||
                          r.data.explanation ||
                          r.id,
                      ).slice(0, 150)}
                      {r.stale ? " · stale" : ""}
                      {r.superseded ? " · superseded" : ""}
                      {r.data.status
                        ? ` · ${human(String(r.data.status))}`
                        : ""}
                    </summary>
                    {r.kind === "source" ? (
                      <>
                        <a
                          href={String(r.data.url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {String(r.data.title || r.data.url)} ↗
                        </a>
                        <p>
                          {String(r.data.access_status)} ·{" "}
                          {String(r.data.retrieved_at || "")}
                        </p>
                        {r.data.text_path ? (
                          <a
                            href={`/api/runs/${run.id}/sources/${r.id}/text`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Read saved source text
                          </a>
                        ) : null}
                      </>
                    ) : r.kind === "passage" ? (
                      <>
                        <blockquote>{String(r.data.quote)}</blockquote>
                        <p>{String(r.data.location)}</p>
                        <Refs
                          ids={[String(r.data.source_id)]}
                          records={records}
                        />
                      </>
                    ) : (
                      <>
                        <p>{String(r.data.text || r.data.explanation || "")}</p>
                        {r.data.citation_support ? (
                          <p>
                            Citation: {human(String(r.data.citation_support))} ·
                            Methodology:{" "}
                            {human(String(r.data.methodological_reliability))}
                          </p>
                        ) : null}
                        <Lines items={r.data.limitations as string[]} />
                        <Refs
                          ids={[
                            ...((r.data.supporting_passage_ids as string[]) ||
                              []),
                            ...((r.data.conflicting_passage_ids as string[]) ||
                              []),
                            ...((r.data.passage_ids as string[]) || []),
                            ...((r.data.claim_ids as string[]) || []),
                            ...(r.data.claim_id
                              ? [String(r.data.claim_id)]
                              : []),
                            ...(r.data.latest_check_id
                              ? [String(r.data.latest_check_id)]
                              : []),
                          ]}
                          records={records}
                        />
                      </>
                    )}
                  </details>
                ))}
            </details>
            <details>
              <summary>Read the debate</summary>
              {records
                .filter((r) => r.kind === "submission")
                .map((r) => (
                  <section key={r.id}>
                    <h3>
                      {human(r.stage)} · Position {r.team}
                    </h3>
                    {r.stale && (
                      <span className="muted">Affected by a correction</span>
                    )}
                    <p>{String(r.data.synthesis)}</p>
                    <Refs
                      ids={r.data.claim_ids as string[]}
                      records={records}
                    />
                  </section>
                ))}
            </details>
            <footer className="report-actions">
              <span>Export</span>
              {["html", "markdown", "json"].map((format) => (
                <a
                  key={format}
                  href={`/api/runs/${run.id}/report?format=${format}`}
                >
                  {format === "markdown" ? "Markdown" : format.toUpperCase()}
                </a>
              ))}
              <span className="muted">
                Robustness untested unless separately assessed.
              </span>
            </footer>
          </article>
        )}
      </main>
      {welcome && (
        <div className="overlay">
          <section
            role="dialog"
            aria-modal="true"
            aria-label="Using your ChatGPT plan"
          >
            <h2>You’re using your ChatGPT plan</h2>
            <p>
              Eligible requests use your plan or its credits allowance. Plus
              limits are shared across apps.
            </p>
            <p>
              For included usage only, set Change My Mind’s credit allowance to zero
              in ChatGPT Usage settings. Paid API requests are disabled.
            </p>
            <a
              href="https://chatgpt.com/settings/usage"
              target="_blank"
              rel="noreferrer"
            >
              Manage usage ↗
            </a>
            <button className="primary" autoFocus onClick={dismissWelcome}>
              Got it
            </button>
          </section>
        </div>
      )}
    </>
  );
}
