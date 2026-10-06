# Bounded trading decision (#24)

`bazaar_agent.trading.run_decision` implements one PydanticAI invocation with a bounded
model/tool loop, not a period runner. Registration remains nonexecuting. The caller supplies a
registered `AgentRecord`, immutable `StrategyVersion`, fixed `ResearchContext`, an owned HTTPX
client, a runner-reserved `client_order_id`, and optionally `DecisionBudget`, `ModelFactory`
and the existing `PrivateHistoryReader` or a runner-bound `MontyCalculator`.
Identity/version/context must agree; inputs are
revalidated and copied before use. No API endpoint, CLI execution path, artifact loading,
database access, approval implementation or scheduler is added.

## Fixture usage

The only accepted models today are explicitly injected local `TestModel`/`FunctionModel`
instances (from **pydantic-ai-slim 1.70.0**, no provider extras). Missing factories, non-fixture
models, artifact references and `orchestrated` harnesses return structured `unsupported`
errors before model execution. The `monty` harness/capability requires a fresh runner-bound
calculator; see [Monty calculations](monty-calculations.md). Model references are passed to a
trusted factory, never parsed as gateway routes, URLs or credentials. No API key is needed.

```python
from pydantic_ai.models.test import TestModel
from bazaar_agent.trading import DecisionBudget, run_decision

# identity/version/context/client/order_id are runner-owned synthetic fixture inputs.
result = await run_decision(
    identity=identity,
    version=version,
    context=context,
    client=client,  # fixed MockTransport base URL, bounded timeout, no logging hooks
    client_order_id=order_id,  # reserve once; preserve across recovery
    budget=DecisionBudget(model_requests=4, tool_calls=12, total_tokens=16_000),
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
    model_ref="fixture", instructions="Inspect account and eligible prices; hold or trade once."
)
research = StrategyDefinition(
    harness="research", model_ref="fixture",
    instructions="Compare eligible archived news and prior-cycle filings; hold or trade once.",
    tools=("account", "market_history", "news", "reports", "private_history", "orders"),
)
```

Both variants use the same bounded baseline; research composes capabilities, not nested planners.
`definition.instructions` influences the model but cannot change runtime scope, budgets, tools,
factory, simulated time or order identity. News, filings and private text are explicitly marked
untrusted evidence; prompt-injection resistance is enforced by available capabilities and scoped
DTO validation, not a claim that models ignore malicious prose.

## Public tool mapping and failure semantics

| Definition capability | Exposed tools |
| --- | --- |
| `account` | `account`, `portfolio`, `account_history`, `portfolio_history` |
| `market_history` | `prices` |
| `news` | `news` |
| `reports` | `filings` (requires trusted `FiscalCycle`) |
| `private_history` | `private_history` (default unsupported adapter) |
| `orders` | `orders` (own order history), `market_order` (structured buy/sell) |
| `monty` | `monty_inputs`, `monty_calculate` (runner-owned historical snapshots) |

Tools reuse #21/shared DTO signatures and `ToolResult`/`ToolError`. PydanticAI flattens a single
Pydantic argument into the tool's top-level JSON object: `market_order` accepts the exact
`OrderRequest` fields, **not** a nested `request` object. No prices, account IDs, SQL, arbitrary
HTTP URLs, simulation clocks or prompt settings can be supplied to the order tool. Fiscal cycles,
provenance, cutoff and cursor validation remain in `ResearchTools`; server authorization and
account settlement remain authoritative. See [research tools](agent-research-tools.md) and
[market agreement](market-agent-api.md) for the proposed, not live, HTTP endpoints.

Each invocation creates fresh messages and research cursor state. There is no inherited model
history, cache, automatic pagination or automatic data/order retry. Any scoped tool error ends
that decision immediately, returning a readable fixed error, never silently advancing time or
calling the model again with invalid evidence. Invalid tool arguments/unknown tools and invalid
final outputs can use **one** SDK validation retry, within the model-request budget. The final
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

Default hard limits: 4 model requests (including validation retries), 12 executed tools, 16,000
reported total tokens and 30 seconds. Tool execution is sequential; SDK batch checks reject an
over-budget validated tool batch before executing it. Local failed tool executions count too.
Schema-invalid/unknown calls do not execute tools; their retries consume model requests. Token
limits are checked **after** responses using SDK-reported/fixture-estimated usage, not exact
preflight cost or billing guarantees. Monty calls use this same decision loop and cancellation
boundary; Monty itself adds no resource quotas or truncation. Async timeouts cannot preempt
a malicious synchronous factory/function: injections are trusted test fixtures. Monty runs in a
separate worker that is killed and reaped on cancellation.

## Monitoring and next interfaces

A safe `trading.decision` operation span is emitted now. SDK content/GenAI instrumentation is
explicitly disabled even when enabled globally; nested model/HTTP instrumentation is suppressed.
Exceptions are caught inside the safe span and only fixed messages leave it. No messages, inputs,
instructions, research/private contents, order payloads, response bodies, cursors or credentials
are logged. Production HTTPX configuration explicitly sets `capture_all=False`, header/request/
response capture false; do not add independently logging hooks. Payload-marker tests configure
monitored HTTPX and globally enabled PydanticAI instrumentation. Full GenAI metadata and actual
AI Gateway/private SDK binding are **#23**, not implemented or gateway-ready here.

The **#22** [Monty integration](monty-calculations.md) uses real SDK execution, a runner-bound
point-in-time snapshot and caller-owned immutable calculation records. It denies host access
without inventing a separate forecasting API or accepting model-provided datasets. No real historical experiment was run; fixtures prove
client behavior, not server authorization, source completeness, live grants or model quality.
