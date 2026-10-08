# Telemetry

Runs send spans to Logfire when `LOGFIRE_TOKEN` is set (the runner and the market both use
`send_to_logfire="if-token-present"`). This page lists the names to filter and chart on.

## Strategy attributes

Every span and log of a run carries these attributes. The runner sets them on `runner.run` and
as Logfire baggage around the run, so the decisions, orders, marks, the agent's own spans and the
evaluator's spans all inherit them. Values are strings.

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

## What is never sent

The runner token, the Gateway key, the admin token and the Logfire token never enter the agent's
messages or any span. Tests check this with sentinel values (`test_observability.py`). Logfire's
default scrubbing stays on in the runner and the market, with one exception: a value whose only
match is the word "session" (as in "trading session") is kept (`keep_trading_sessions`). Logfire's
patterns match secret names (`api_key`, `auth`, `secret`), not bare secret values, so keeping
secrets out of spans in the first place is the protection that matters.
