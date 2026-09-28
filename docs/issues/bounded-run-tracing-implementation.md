# Bounded Run tracing (#282)

Tracing observes the Agent MCP, Agent gateway, Runtime observation and execution,
Gate submission, Effect worker, and optional host capture boundaries. Runtime
events, Gate decisions, Effect settlement, and Outcome remain authoritative.
Tracing never adds a preflight or changes MCP result fields.

## Enable or disable

Tracing is off unless `OPENUBMC_TRACE_ENABLED=1` is set in the MCP process.
`OPENUBMC_TRACE_ENABLED=0` (or removing the variable) explicitly disables it,
even when an endpoint remains configured. The disabled path imports no
OpenTelemetry package. Invalid tracing configuration or missing optional SDK
also leaves the Runtime operational with tracing disabled.

The canonical Python package offers `pip install '.[tracing]'` from
`openubmc-target-runtime`. A packaged plugin vendors the Runtime source; install
the same `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http`
packages into the Python interpreter used by that plugin before enabling it.

With tracing enabled and no endpoint, spans stay in a bounded local collector
accessible through `service.tracing.local_spans()`. This makes offline diagnosis
possible without network traffic. Remote export requires an explicit
`OPENUBMC_TRACE_OTLP_ENDPOINT` containing the full HTTP(S) OTLP traces path,
such as `https://collector.example/v1/traces`. URLs with embedded credentials,
query strings or fragments are rejected. Generic `OTEL_EXPORTER_OTLP_ENDPOINT`
does not enable this adapter's remote export.

Optional bounds are `OPENUBMC_TRACE_QUEUE_SIZE` (default 128, maximum 1024),
`OPENUBMC_TRACE_MAX_SPANS_PER_RUN` (default 64, maximum 128), and
`OPENUBMC_TRACE_EXPORT_TIMEOUT_SECONDS` (default 1, maximum 5). The local
collector keeps at most one queue's worth of finished spans; the trace adapter
tracks at most 256 Run or task scopes. `service.tracing.stats()` reports
`dropped_spans` and its budget, queue, export, local eviction, and internal
parts. Each recorded span also carries the drop count observed when it began.
The exporter worker is a daemon and never calls a domain operation. Its queue
is nonblocking; a stuck custom exporter can fill it, after which spans are
dropped and the Runtime proceeds. The built-in OTLP HTTP exporter receives the
configured request timeout.

## Data contract

Only the fixed span names in `tracing.py` and four attributes are emitted:
`task.ref`, `run.ref`, `effect.ref`, and `trace.dropped_before`. The references
are HMAC digests with a process-local key, so they are stable within one MCP
process and deliberately cannot be joined across restarts. Attributes are
limited to 64 bytes; span events and links are disabled. No command text,
prompts, target addresses, credentials, raw output, artifact contents,
application exception messages, or ambient OpenTelemetry baggage are copied
into spans. A direct, private `TracerProvider` avoids changing any global
provider used by the host application.

## Verification

Run the focused offline tests with the optional tracing packages installed:

```sh
python -m unittest discover -s openubmc-target-runtime/tests -p 'test_bounded_tracing.py' -v
```

The tests compare canonical MCP reply and persisted Run event bytes with tracing
disabled, normally enabled, exporter failure, and a full queue. They also
check observe-to-execute trace ancestry, Gate and Effect child spans, bounded
drop counts, a synthetic secret input, and vendored Skill packaging. No BMC or
remote collector is contacted.

The implementation uses the current OpenTelemetry Python
[manual instrumentation API](https://opentelemetry.io/docs/languages/python/instrumentation/),
[SDK span limits](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html),
and [OTLP HTTP exporter](https://opentelemetry-python.readthedocs.io/en/latest/exporter/otlp/otlp.html).
The SDK's current [BatchSpanProcessor source](https://opentelemetry-python.readthedocs.io/en/latest/_modules/opentelemetry/sdk/trace/export.html)
notes that its `export_timeout_millis` constructor argument is not used to
bound the exporter call. This adapter therefore owns its bounded queue and
passes the timeout directly to the OTLP exporter.
