# Telemetry

Runs send spans to Logfire when `LOGFIRE_TOKEN` is set (the runner and the market both use
`send_to_logfire="if-token-present"`). This page lists the names to filter and chart on.

## Strategy attributes

Every span and log of a run carries these attributes. The runner sets them on `runner.run` and
as Logfire baggage around the run, so the decisions, orders, marks, the agent's own spans and the
evaluator's spans all inherit them. Values are strings.

The baggage also leaves the process: pydantic-ai's Gateway provider injects the OpenTelemetry
context into every model request, so these attributes (including `bazaar.handle`) reach the
Pydantic AI Gateway as a W3C `baggage` header. They reach the market the same way once the
runner's market clients propagate the trace (`instrument_market_client`, T2a). So the strategy's
instruction text, an IP address or its hash, and any secret must never be put in baggage; a test
pins the allow-list (`test_the_baggage_allow_list_is_exactly_the_strategy_attributes`).

| Attribute | Value |
| --- | --- |
| `bazaar.strategy_name` | The submission's name; for demo launches, the policy_ref (for example `baseline-cash-only`) |
| `bazaar.submission_id` | The web submission id. Absent for demo launches |
| `bazaar.experiment_id` | The run's market experiment (UUID) |
| `bazaar.run_id` | The run id, which is also the run's directory name (UUID) |
| `bazaar.policy_kind` | `baseline` for `baseline-*` policies, otherwise `agent` |
| `bazaar.handle` | The submitter's optional handle. Absent when none was given |

## Spans

| Span | Where | Attributes beyond `bazaar.*` |
| --- | --- | --- |
| `runner.run` | One per run, the root | `experiment_id`, `policy_ref`, `status`; when failed, `failure_code`, `failure` at error level |
| `runner.decision` | One per decision (each session open) | `event_sequence`, `simulated_at`, `bazaar.action` (`hold`, `ordered` or `failed`); when ordered, `bazaar.symbol`, `bazaar.side`, `bazaar.quantity` and, if filled, `bazaar.fill_price` (the decision's last order). Agent decisions add `agent`, `client_order_id`, `reconcile`, `fiscal_cycles`, `fiscal_cycles_read`, `model_requests`, `tool_calls`, `total_tokens` and, on error, `decision_error` |
| `runner.order` | One per order the runner submits (scripted policies and baselines) | `symbol`, `side`, `quantity`, `status`, `error_code` |
| `runner.mark` | One per session close | `event_sequence`, `simulated_at`, `portfolio_value` |
| `runner.evaluate` | Around the evaluator call | Parent of the evaluator's own spans (`evaluate run {experiment_id}`, `trade {symbol} {side} {status}`) |
| `bazaar.run scored` | Log inside `runner.run`, after scoring | `return_pct`, `ending_value`, `fills`, `decisions_exhausted`; `excess_vs_buy_and_hold_pct` only when known (the runner does not know it) |
| `trading.decision` | The agent harness, inside `runner.decision` | `experiment_id`, `account_id`, `agent_id`, `strategy_version_id` |
| `invoke_agent ...` | The agent run, inside `trading.decision` | `gen_ai.agent.name`, `gen_ai.aggregated_usage.input_tokens`, `gen_ai.aggregated_usage.output_tokens`, `pydantic_ai.all_messages`, `final_result`, `model_name` |
| `chat ...` | One per model request | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions` |
| `execute_tool ...` | One per tool call | `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |
| `account tool`, `portfolio tool`, `news tool`, `order tool`, `research.tool` | The market reads and the order behind each tool | `bazaar.*` and the agent ids |

The agent spans (`invoke_agent`, `chat`, `execute_tool`) appear for web submissions and the demo's
agent launch, which set `RuntimeConfig(instrument=True)`. They include message content: prompts,
the submitter's strategy text, tool arguments and results, and outputs. Other callers of
`run_decision` keep the default `instrument=False`, which suppresses them.

## Configuration

Every service configures Logfire through `bazaar_protocol.telemetry.configure(service_name)`. It
configures once per process (`send_to_logfire="if-token-present"`, distributed tracing, the shared
scrubbing) and never replaces a configuration that is already in place, whether it came from this
helper or from a direct `logfire.configure` call. So a submission run inside the web app keeps the
web app's service name. Library code (`run_submission`, `record_run`, `run_demo`) never takes over
a configured process: `run_submission` configures only a process nobody has configured.

## What is never sent

The runner token, the Gateway key, the admin token and the Logfire token never enter the agent's
messages or any span. Tests check this with sentinel values (`test_observability.py`).

Scrubbing (`bazaar_protocol.telemetry.scrubbing_options()`) keeps Logfire's default patterns and
adds `runner[._ -]?token`, `admin[._ -]?token` and `x[._ -]?bazaar[._ -]?approval`, so the
`X-Bazaar-Runner-Token`, `X-Bazaar-Admin-Token` and `X-Bazaar-Approval` headers are scrubbed if
headers are ever captured. It is never a bare "token" (that would scrub `gen_ai.usage.*_tokens`).
One exception to the defaults: a value whose only match is the word "session" (as in "trading
session") is kept (`keep_trading_sessions`).

Logfire's patterns match secret names, not secret values, and Logfire never scrubs
`exception.message` or `exception.stacktrace`. So exception text is redacted before a span records
it. `redact` replaces the values of `PYDANTIC_AI_GATEWAY_API_KEY`, `BAZAAR_RUNNER_TOKEN`,
`BAZAAR_ADMIN_TOKEN` and `LOGFIRE_TOKEN` (read once, kept in memory), the runner token it was
given, and any value written as `name=value` or `name: value` under a secret-like name, with
`[REDACTED]`. In the runner:

- every runner span (`runner.run`, `runner.decision`, `runner.mark`, `runner.order`,
  `runner.evaluate`) wraps its body in `redacted_exceptions()`: an error whose text, or printed
  chain, holds a secret leaves as a `RedactedError` raised `from None`; any other error keeps its
  type;
- the agent's model is wrapped the same way (`redacting_model_factory`), because the agent's own
  `chat` and `invoke_agent` spans record a model error before the runner sees it;
- market errors, the run's `failure` text and `SubmissionFailed` messages are redacted;
- the strategy judge (`bazaar_agent.strategy_evaluation`) runs inside `redacted_exceptions()`,
  because pydantic-evals records a judge failure's text on its `evaluator` span and evaluation
  event. The judge's spans and events carry the `bazaar.*` attributes like the rest of the run.

The market uses the shared configuration and scrubbing; redacting its own exceptions is the market
team's work.
