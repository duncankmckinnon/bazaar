# Monty calculations (#22)

The harness exposes two built-in model-callable tools, independently of strategy:

- `monty_inputs()` inspects the runner's fixed calculation snapshot.
- `monty_calculate(code)` executes Python in **pydantic-monty 0.0.14**. The last
  expression is the result, which must be JSON-serializable.

The model supplies **code only**, not a dataset, account identity, clock, filesystem
path, URL or host callback. The runner supplies a `MontyCalculator` to `run_decision`.
There is no Monty strategy flag or Monty-specific harness. Trusted harness/runner
configuration owns the model and eligible data. Models remain local injected
PydanticAI fixtures; Gateway binding is separate work.

## Inputs and calculations

```python
from bazaar_agent.monty import CalculationSnapshot, MontyCalculator

# Context and validated historical DTOs come from the trusted runner/research layer.
calculator = MontyCalculator(CalculationSnapshot(
    context=research_context.experiment,
    prices=(price_history,),
    account=account_snapshot,
    portfolio=portfolio_snapshot,
))
# Pass calculator=calculator to run_decision; strategy contains instructions only.
```

`inputs` is a JSON-compatible dictionary containing `context`, `prices`, `account`
and `portfolio`. Optional account/portfolio values can be null. Histories contain
ordered `observations`; prices, cash, quantities and marks retain the wire contract's
Decimal strings. Dates retain their historical UTC timestamps. The model can convert
strings to numbers for analytics; market accounting never uses these calculations.

Example model code for returns, volatility, moving averages and a naive forecast:

```python
import math
p = [float(point['price']) for point in inputs['prices'][0]['observations']]
r = [p[i] / p[i - 1] - 1 for i in range(1, len(p))]
mean = sum(r) / len(r)
variance = sum((x - mean) ** 2 for x in r) / len(r)
{
    'mean_return': mean,
    'volatility': math.sqrt(variance),
    'moving_average': sum(p) / len(p),
    'naive_next_price': p[-1] * (1 + mean),
}
```

A forecast is a hypothesis derived from permitted data, not an observed future price.
The same tool can compute a hypothetical allocation or a retrospective backtest
statistic **within the supplied historical window**. It does not run the experiment,
schedule historical periods, fetch another bar or perform market settlement.

Snapshots are revalidated, including objects constructed with Pydantic validation
bypasses. Experiment/account/agent/version identity, data version and historical
cutoffs must agree; account and portfolio state must agree when both are supplied.
The entire context must match the trading decision. Each decision requires a fresh
calculator, so audit trails cannot accidentally combine separate decisions.

## Execution and failures

Each calculation runs in a fresh isolated Python subprocess hosting the actual Monty
VM—not host `eval`/`exec`. The child ignores Python path/user-site overrides, receives
no inherited credentials, and has no host function, mount or OS handler. Host-access
snapshots are never resumed. File/network/OS access and current-clock lookups are
unavailable. Monty supports a subset of Python; arbitrary third-party imports are
not available. Input mutations affect only the worker's copy.

**There are intentionally no Monty-specific code/data/output size ceilings,
truncation, allocation/recursion quotas, memory quotas or execution deadlines.**
This follows the requested design: SDK compilation/runtime failures and ordinary
JSON serialization failures are reported with complete private diagnostics, rather
than invented resource rejections. In the decision loop, syntax/runtime/serialization
failures return their full immutable calculation record to the model so it can correct
code within the existing overall budget. Host-denial, invalid worker boundaries and
scope failures still stop the decision; ambiguous orders remain fatal. Finite JSON
is required by the result format. Printed output is retained,
not emitted to ordinary application stdout or silently truncated.

The pre-existing trading-agent model/tool/token/decision budgets are unchanged;
Monty tools participate in that loop like other tools. Explicit cancellation,
including cancellation by the decision deadline, kills and reaps the child rather
than leaving a background CPU thread. Standalone calculations can run indefinitely
or consume substantial resources unless their caller cancels them. Monty's sandbox
is an experimental SDK boundary, not an independently audited security guarantee.

## Audit and monitoring

`CalculationRecord` is immutable and includes execution UUID, context, SDK version,
complete code, canonical inputs, code/snapshot digests, status, complete JSON result,
captured prints, SDK failure diagnostics and elapsed duration. Records are owned by
the caller through `calculator.records`; `DecisionResult.calculations` retains that
decision's records, including failures. Cancellation raises instead of returning a
`DecisionResult`: the cancelled execution record remains in the caller's calculator
only. No new database or storage API is introduced. Validation failures before
execution are not execution records.

Ordinary Logfire spans contain only digests, status and duration. Source, inputs,
results, prints and error diagnostics are private audit data, not telemetry. The
existing decision's payload-safe PydanticAI/HTTPX instrumentation remains unchanged.
Registration still stores definitions only and never creates calculators or executes
code. Market APIs, approvals, full historical runs and live model export are not added.
