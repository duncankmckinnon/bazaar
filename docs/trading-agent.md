# Bounded trading decision (#24)

`bazaar_agent.trading.run_decision` implements one PydanticAI invocation with a bounded
model/tool loop, not a period runner. Registration remains nonexecuting. The caller supplies a
trusted `MarketIdentity`, immutable `StrategyVersion`, fixed `ResearchContext`, an owned HTTPX
client, a runner-reserved `client_order_id`, and optionally `RuntimeConfig`, `DecisionBudget`,
`ModelFactory`, and the existing `PrivateHistoryReader`.
Identity/version/context must agree; inputs are
revalidated and copied before use. No API endpoint, CLI execution path, artifact loading,
database access, approval implementation or scheduler is added.

## Models and fixture usage

With no `model_factory`, the model comes from the operator's environment: `BAZAAR_AGENT_MODEL`,
default `gateway/openai:gpt-5.6-sol`, built with Pydantic AI's `infer_model` (from
**pydantic-ai-slim[anthropic,openai] 2.54.0**). Gateway models read `PYDANTIC_AI_GATEWAY_API_KEY`
themselves; this code never reads, logs or echoes it. A model that cannot be built (for example a
missing key or an unknown model) is a structured `unsupported` error naming the setting, never its
value. Tests inject local `TestModel`/`FunctionModel` factories, as in the example below. Every
model is wrapped by the tool-call id check. The `news` and `filings` tools ask the market for at
most 5 items per page, whatever the model requests, to keep real articles inside the token budget.
Code Mode dependencies are pinned at **pydantic-ai-harness[codemode] 0.54.0** and
**pydantic-monty 1.0.0**. `RuntimeConfig` accepts
only `harness="builtin"`, with `model_ref="fixture"` by default. Its supported model settings
are `temperature` (default 0, range 0–2), `max_tokens` (default 4,000, range 1–100,000), and
optional integer `seed`. These are converted to SDK `ModelSettings` and passed to `Agent`,
independently of strategy text. Unknown runtime/settings fields are rejected. Model references
are passed to an explicitly trusted factory, never parsed as gateway routes, URLs or credentials.
Tests need no API key. The trusted `RuntimeConfig.code_mode` boolean defaults to `False`;
set `RuntimeConfig(code_mode=True)` to enable the SDK's `CodeMode` capability. This is a runtime
choice, not a strategy flag or a new harness value.

```python
from pydantic_ai.models.test import TestModel
from bazaar_agent.trading import DecisionBudget, MarketIdentity, RuntimeConfig, run_decision

# The trusted runner supplies a market binding, not a local registration record.
identity = MarketIdentity(
    agent_id=context.experiment.agent_id,
    account_id=context.experiment.account_id,
    experiment_id=context.experiment.experiment_id,
    strategy_version_id=context.experiment.strategy_version_id,
)
# version/context/client/order_id are runner-owned synthetic fixture inputs.
# MockTransport must serve valid, coherent account and portfolio snapshots first.
result = await run_decision(
    identity=identity,
    version=version,
    context=context,
    client=client,  # fixed MockTransport base URL, bounded timeout, no logging hooks
    client_order_id=order_id,  # reserve once; preserve across recovery
    runtime=RuntimeConfig(model_settings={"temperature": 0, "max_tokens": 4_000}),
    budget=DecisionBudget(model_requests=4, tool_calls=20, total_tokens=16_000),
    model_factory=lambda ref: TestModel(
        call_tools=[], custom_output_args={"action": "hold"}
    ),
)
assert result.error is None
assert result.decision.action == "hold"
```

Immutable sample definitions (proposals, **not approved experiments**):

```python
from bazaar_protocol.registry import StrategyDefinition
baseline = StrategyDefinition(
    instructions="Inspect account and eligible prices; hold or trade once."
)
research = StrategyDefinition(
    instructions="Compare eligible archived news and prior-cycle filings; hold or trade once."
)
```

Both variants use the same fixed builtin tool surface, runtime settings and bounded loop, not
nested planners. New `StrategyDefinition` accepts **only instructions**; harness, model reference,
tools, artifacts and model settings are rejected. Persisted `StrategyVersion` definitions may
still contain `LegacyStrategyDefinition`, but execution reads only its instructions and ignores
all legacy runtime fields, including unsupported harnesses and artifact references.

The trusted trading role comes from the Logfire managed variable `bazaar_trading_role`. At the
start of each submission, the runner forces one refresh, resolves the variable with the submission
id as its targeting key, and holds that exact value for all ten decisions. The next submission
refreshes independently, so a published variable change takes effect without an application
redeploy. If Logfire is unavailable or the remote value is missing or invalid, Logfire retains the
last valid cached value when available and otherwise uses the baked `TRADING_ROLE` fallback.

The role tells the agent to act as a simulated stock trader maximizing market-authoritative
portfolio value **NET of all trading fees**, within the supplied strategy.
Strategy instructions are labeled **user input**, never appended to agent instructions. The
initial validated market snapshots are also labeled JSON user input, including `portfolio_value`.
Strategy text cannot change scope, budgets, tool admission, factory, settings, simulated time or
order identity. News, filings and private text are explicitly marked untrusted evidence;
prompt-injection resistance is enforced by fixed capabilities and scoped DTO validation, not a
claim that models ignore malicious prose.

Managed-variable reads use `LOGFIRE_API_KEY` with `project:read_variables`; this is separate from
the write-only telemetry token in `LOGFIRE_TOKEN`. Direct `run_decision` callers resolve the same
variable once for their individual decision unless a trusted role was already supplied by the
submission runner.

## Online strategy adherence evaluation

Every completed `run_decision` invocation is wrapped with Pydantic Evals'
[`OnlineEvalConfig.evaluate`](https://pydantic.dev/docs/ai/evals/online-evaluation/).
`bazaar_agent.strategy_evaluation.StrategyAdherence` uses `LLMJudge` to assess the supplied
strategy against the initial account/portfolio, fixed simulated time, model conversation,
research tool observations (including nested Code Mode reads), and actual decision/order evidence.
The rubric assesses research requirements, entry/exit conditions, sizing and risk constraints,
without using later market outcomes. Jev returns a binary verdict rather than a rationale.

Logfire receives `gen_ai.evaluation.result` events under target `trading.decision`:

- `strategy_adherence`: `1.0` for adherence or `0.0` for a violation.
- `strategy_adherence_pass`: the same verdict as a pass/fail assertion.
- `strategy_adherence_confidence`: Jev's confidence in that verdict, when reported.
- `strategy_adherence_status=not_evaluated`: no decision or attempted order was produced.

An attempted order is still evaluated when the final model output fails. Judge errors and
the judge's 30-second timeout are reported as evaluation failures, without changing trade
results or triggering another order. Cancellation propagates; a cancelled invocation has no
returned decision to evaluate. Judge usage is separate from the trading decision's budget.
There is no evaluation database, dataset file, or change to `DecisionResult`, `record.json`,
or the period-scoring `evaluation.json`. The SDK sends the results through the application's
existing Logfire configuration; view them in Logfire's **Live Evaluations**.

The operator can set `BAZAAR_JUDGE_MODEL` as `<Gateway route>:<Jev model>` (default
`jev-duncan:jev-latest`). The judge uses `PYDANTIC_AI_GATEWAY_API_KEY` and sends requests to that
Gateway route's Jev endpoint. Evaluation is enabled by default for every decision, including
fixture/demo decisions. Set `BAZAAR_STRATEGY_EVAL_ENABLED=0` to disable it. Strategy text cannot
configure the judge. Each decision gets a separate online wrapper so the SDK's shared evaluator
concurrency limit does not drop calls; judge concurrency therefore scales with the number of
decisions in flight.

The CLI and submission runner use `strategy_evaluation_session()` to let background judges
finish before closing their event loop. Direct callers should do the same:

```python
from bazaar_agent.strategy_evaluation import strategy_evaluation_session

async with strategy_evaluation_session():
    result = await run_decision(...)  # returns without waiting for the judge
# This run's evaluations have finished and their OTel events have been emitted.
```

Only completion signals and per-decision evidence are held temporarily in memory. Sessions
are isolated across concurrent submissions and worker threads. Tests disable paid judges by
default and exercise this path with local model fixtures and captured Logfire events.

## Market binding and initial protected reads

`MarketIdentity` is a strict wire DTO containing UUID `agent_id`, `account_id`, `experiment_id`
and `strategy_version_id`, plus `status="active"` (or `"inactive"`). Execution requires the exact
DTO type: `AgentRecord`, mappings, subclasses/local metadata and inactive bindings are rejected.
There is no local strategy ID or agent name in this binding. All four IDs must agree with the
trusted experiment context, and the version ID must agree with `StrategyVersion.version_id`.

Before invoking the factory or model, the harness uses `ResearchTools` to make exactly these
protected reads in order:

1. `GET /experiments/{experiment_id}/accounts/{account_id}`
2. `GET /experiments/{experiment_id}/accounts/{account_id}/portfolio`

Existing scope, simulated-time and data-version checks apply. The pair must also agree on
state version, currency, cash, holding symbols/quantities and snapshot time. Failures stop before
factory/model dispatch or any order. There is **no provisioning request**. These two reads do
not consume the model-tool budget, but share the decision's wall-time timeout.

The current account schema has no status field and the portfolio schema uses `portfolio_value`,
not `total_value`. Active eligibility therefore means an **active trusted binding plus successful
protected reads**, not an invented account-status response or fabricated status endpoint. This
client corroboration does not replace market-side authorization, approval or settlement.

## Public tool mapping and failure semantics

The fixed tool surface is registered independently of the strategy definition. With Code Mode
disabled, these are native tools; with it enabled, the research reads are exposed through
`run_code` and `market_order` remains native:

| Purpose | Exposed tools |
| --- | --- |
| Protected state/history | `account`, `portfolio`, `account_history`, `portfolio_history` |
| Market history | `prices` |
| Archived news | `news` |
| Reports | `filings` (requires trusted `FiscalCycle`) |
| Private evidence | `private_history` (default unsupported adapter) |
| Own orders | `orders` (own order history), `market_order` (structured buy/sell) |

CodeMode receives only `BUILTIN_TOOLS`: `account`, `portfolio`, `account_history`,
`portfolio_history`, `prices`, `news`, `filings`, `private_history`, and `orders`. It cannot call
`market_order`. Registration does not bypass fiscal-cycle, private-adapter, authorization or
budget requirements.

## Code Mode sandbox

Code Mode replaces the custom calculation surface. Generated Python can call the scoped research
reads and compute from their returned data in the same sandbox execution; there is no separate
runner-owned calculation snapshot or calculator injection. The custom `monty.py`,
`monty_worker.py`, and `monty-calculations.md` are removed, as are `monty_inputs`,
`monty_calculate`, and `DecisionResult.calculations`.

The SDK capability is configured with `max_retries=1`,
`max_tool_calls=budget.tool_calls + 1`, and
`resource_limits={"max_duration_secs": budget.timeout_seconds}`. It retains the SDK's default
256 MiB memory limit. No OS access, filesystem mounts, environment access or clock grants are
provided. No eager execution or speculation is enabled. Research access remains mediated by the
same scoped wrappers; Code Mode adds computation, not new market authority.

Research tools remain sequential. Inside `run_code`, their sandbox stubs are plain synchronous
functions called without `await` (for example, `account()` and `portfolio()`). There is no parallel
research within one run; call each read in order, then compute from the returned data.

## Tool execution and decision budgets

Tools reuse #21/shared DTO signatures and `ToolResult`/`ToolError`. PydanticAI flattens a single
Pydantic argument into the tool's top-level JSON object: `market_order` accepts the exact
`OrderRequest` fields, **not** a nested `request` object. No prices, account IDs, SQL, arbitrary
HTTP URLs, simulation clocks or prompt settings can be supplied to the order tool. Fiscal cycles,
provenance, cutoff and cursor validation remain in `ResearchTools`; server authorization and
account settlement remain authoritative. See [research tools](agent-research-tools.md) and
[market agreement](market-agent-api.md) for the proposed, not live, HTTP endpoints.

Each invocation creates fresh messages and research cursor state. There is no inherited model
history, cache, automatic pagination or automatic data/order retry. Ordinary research read errors
return structured `ToolResult.error` feedback instead of ending the decision: the model can correct
arguments or retry, including from Code Mode, within the same budgets. Error feedback is not valid
research data and never silently advances time. Initial protected bootstrap errors, ambiguous order
failures and budget exhaustion remain terminal. Invalid tool arguments/unknown tools and invalid
final outputs can use **one** SDK validation retry, within the model-request budget. Code Mode's
`max_retries=1` also bounds code correction; it does not grant extra decision budget. The final
`Decision.action` is only `hold` or `ordered` and must agree with actual tool settlement evidence;
model text cannot manufacture a fill. Market rejections are terminal evidence, not harness failures.
A public SDK `WrapperModel` response guard rejects empty or duplicate tool-call IDs (including
output-tool collisions) before SDK dispatch: dispatch-key collisions must never substitute an
order for an earlier scoped read. IDs may repeat across separate responses; order replay still
returns only the existing evidence.

At most one distinct order is submitted. Its ID must equal the runner-reserved ID. Repeating the
identical order within a decision returns existing evidence without another POST; a changed body
or fresh ID fails closed. Ambiguous failures abort immediately, even if #21 marks the transport
error retryable. `DecisionResult.order_request` and `order_result` retain reconciliation evidence
when final output is invalid or budgets expire after submission. **An error does not imply no
side effects.** The caller must reconcile with the market using the original ID/body; never rerun
with a fresh ID or advance the clock just because the decision failed. Cancellation propagates;
the runner already owns the reserved ID and must reconcile it even when no result is returned.
Across invocations this harness has no durable order ledger: the future runner/market own that.

Default hard limits: 4 model requests (including validation retries), 20 executed tools, 16,000
reported total tokens and 30 seconds. Tool execution is sequential; SDK batch checks reject an
over-budget validated tool batch before executing it. Local failed tool executions count too.
Schema-invalid/unknown calls do not execute tools; their retries consume model requests. Token
limits are checked **after** responses using SDK-reported/fixture-estimated usage, not exact
preflight cost or billing guarantees.

With Code Mode enabled, there are two independent tool-budget checks: a harness capability counts
model-dispatched `run_code` executions and native `market_order` executions, while the shared wrapper
counter counts nested research read executions and native order executions against `budget.tool_calls`.
The SDK's aggregate tool-call limit is disabled in this mode because it counts both outer and nested
calls; using it here would charge a research read twice. SDK model-request and token limits remain active.
`DecisionResult.usage.tool_calls` reports that wrapper counter, not the number of outer `run_code`
calls. Nested reads, including failed reads and model-requested retries, do not bypass or reset the
wrapper budget. CodeMode's reservation cap allows one excess nested call to reach the terminal
shared admission guard, rather than turning decision-budget exhaustion into retryable sandbox
feedback. Rejected over-budget calls are not counted as executions. The outer harness guard blocks
`run_code` when the decision budget is zero and counts it even if its code performs no reads. Code execution, corrections
and research retries also share the decision-wide model-request, token and wall-time limits; the
sandbox duration limit does not restart the overall deadline. Cancellation propagates, but SDK
cleanup of an active sandbox feed may wait for its configured duration limit; it is not an
instantaneous hard kill. No nested models or delegation are added. Async timeouts cannot preempt
a malicious synchronous factory/function: injections are trusted test fixtures, not sandbox code.

## Monitoring and next interfaces

`RuntimeConfig(instrument=True)` includes PydanticAI agent, model and tool instrumentation
inside each `trading.decision` span, including Code Mode, even if global SDK instrumentation is
off. The submission runner and demo agent enable this setting. Other callers default to
`instrument=False`, suppressing the model loop's SDK and nested HTTPX spans. Configure Logfire
in the hosting process and supply `LOGFIRE_TOKEN` to export traces (the runner configures it for
submissions). The model can be a Gateway model chosen by the operator.

Online strategy evaluation is independent of this tracing switch: it captures decision evidence
directly and emits its evaluation events in either mode. Use `BAZAAR_STRATEGY_EVAL_ENABLED=0`
to disable judging separately. See [telemetry](telemetry.md) for the strategy tags attached by
the runner and inherited by evaluation events.

**Telemetry includes model inputs/outputs**, including strategy instructions, market snapshots,
research/private content and tool arguments/results. Instrumented HTTP spans can include cursor
query strings and exception details. Only send runs to an appropriate, trusted Logfire project.
HTTPX header/request/response-body capture remains disabled in the placeholder process; that does
not suppress spans. Returned harness errors remain sanitized, but nested telemetry is not a
payload-free boundary. Do not put credentials in strategy text or research data.

Computation over returned research data does not authorize future observations
or bypass point-in-time validation. No real historical experiment was run; fixtures prove client
behavior, not server authorization, source completeness, live grants or model quality. Provisioning,
account-status and performance-statistics contracts remain deferred; no market service or Compose
changes accompany this harness.
