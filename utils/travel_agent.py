"""
Travel agent for AgentCore Runtime with Dash0 observability,
AgentCore Memory (remembers traveler preferences) and AgentCore Gateway
(flight and weather tools served by a Lambda, reached over MCP with IAM auth).

Telemetry uses the standard OpenTelemetry environment variables, so any
OTLP backend works. For Dash0 (set by deploy.py on the agent runtime):
    OTEL_EXPORTER_OTLP_ENDPOINT  — e.g. https://ingress.us-west-2.aws.dash0.com
    OTEL_EXPORTER_OTLP_HEADERS   — Authorization=Bearer%20<token>,Dash0-Dataset=<dataset>
    OTEL_EXPORTER_OTLP_PROTOCOL  — http/protobuf
    OTEL_SERVICE_NAME            — service name shown in Dash0
    DISABLE_ADOT_OBSERVABILITY   — "true", so AgentCore does not inject its own ADOT setup
Optional:
    SECONDARY_OTLP_ENDPOINT      — a second OTLP backend that gets a copy of all telemetry
    SECONDARY_OTLP_HEADERS       — headers for it, in the OTEL_EXPORTER_OTLP_HEADERS format
    MEMORY_ID                    — AgentCore Memory id
    GATEWAY_URL                  — AgentCore Gateway MCP URL
"""

import logging
import os

logging.basicConfig(level=logging.ERROR, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("AGENT_RUNTIME_LOG_LEVEL", "INFO").upper())

# ── OpenTelemetry setup ────────────────────────────────────────────────────────
# Exporters read endpoint and headers from OTEL_EXPORTER_OTLP_*; the resource
# reads OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES. Configure before the agent imports.

if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from urllib.parse import unquote

    resource = Resource.create()

    # Optional second destination, for example a second Dash0 organization.
    secondary = os.getenv("SECONDARY_OTLP_ENDPOINT", "").rstrip("/")
    secondary_headers = dict(
        (unquote(k.strip()), unquote(v.strip()))
        for k, v in (pair.split("=", 1) for pair in os.getenv("SECONDARY_OTLP_HEADERS", "").split(",") if "=" in pair)
    )

    def exporters(cls, path):
        found = [cls()]
        if secondary:
            found.append(cls(endpoint=f"{secondary}/v1/{path}", headers=secondary_headers))
        return found

    trace_provider = TracerProvider(resource=resource)
    for exporter in exporters(OTLPSpanExporter, "traces"):
        trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(trace_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(e) for e in exporters(OTLPMetricExporter, "metrics")],
        )
    )

    log_provider = LoggerProvider(resource=resource)
    for exporter in exporters(OTLPLogExporter, "logs"):
        log_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(log_provider)
    logging.getLogger().addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=log_provider))

    logger.info("OpenTelemetry configured, exporting to %s", os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
    if secondary:
        logger.info("Also exporting to %s", secondary)
else:
    logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set, telemetry is not exported")

# ── Agent ──────────────────────────────────────────────────────────────────────

from bedrock_agentcore.memory import MemoryClient  # noqa: E402
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402
from ddgs import DDGS  # noqa: E402
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client  # noqa: E402
from opentelemetry import trace as otel_trace  # noqa: E402
from strands import Agent, tool  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from strands.tools.mcp import MCPClient  # noqa: E402

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
MEMORY_ID = os.getenv("MEMORY_ID", "")
GATEWAY_URL = os.getenv("GATEWAY_URL", "")

app = BedrockAgentCoreApp()
tracer = otel_trace.get_tracer("reinvent.travel_agent")
memory = MemoryClient(region_name=REGION) if MEMORY_ID else None


def xray_trace_header() -> str | None:
    """X-Ray header for the current OTel span, so AgentCore records its own spans
    for Memory and Gateway calls (it only does for sampled requests) in the same trace."""
    ctx = otel_trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    tid = f"{ctx.trace_id:032x}"
    return f"Root=1-{tid[:8]}-{tid[8:]};Parent={ctx.span_id:016x};Sampled=1"


def w3c_traceparent() -> str | None:
    ctx = otel_trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"


def _add_trace_header(request, **kwargs):
    header = xray_trace_header()
    if header:
        request.headers["X-Amzn-Trace-Id"] = header


if memory:
    memory.gmdp_client.meta.events.register("before-sign.bedrock-agentcore.*", _add_trace_header)


def preferences_namespace(user_id: str) -> str:
    return f"/users/{user_id}/preferences"


@tool
def web_search(query: str) -> str:
    """Search the web for current travel information."""
    try:
        ddgs = DDGS()
        results = ddgs.text(query, max_results=5)
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(
                f"{i}. {r.get('title', 'No title')}\n"
                f"   {r.get('body', 'No summary')}\n"
                f"   Source: {r.get('href', 'No URL')}\n"
            )
        return "\n".join(formatted) if formatted else "No results found."
    except Exception as e:
        return f"Search error: {str(e)}"


def recall_preferences(user_id: str, prompt: str) -> list[str]:
    """Read what Memory has learned about this traveler."""
    if not memory:
        return []
    with tracer.start_as_current_span("agentcore.memory.retrieve") as span:
        span.set_attribute("aws.agentcore.memory.id", MEMORY_ID)
        span.set_attribute("aws.agentcore.memory.namespace", preferences_namespace(user_id))
        try:
            records = memory.retrieve_memories(
                memory_id=MEMORY_ID, namespace=preferences_namespace(user_id), query=prompt, top_k=5
            )
        except Exception as e:
            span.record_exception(e)
            logger.warning("memory retrieve failed: %s", e)
            return []
        prefs = [r.get("content", {}).get("text", "") for r in records]
        prefs = [p for p in prefs if p]
        span.set_attribute("aws.agentcore.memory.records", len(prefs))
        return prefs


def load_session_history(user_id: str, session_id: str) -> list[dict]:
    """Earlier turns of this conversation (short-term memory), oldest first."""
    if not memory:
        return []
    with tracer.start_as_current_span("agentcore.memory.list_events") as span:
        span.set_attribute("aws.agentcore.memory.id", MEMORY_ID)
        try:
            events = memory.gmdp_client.list_events(
                memoryId=MEMORY_ID, actorId=user_id, sessionId=session_id, includePayloads=True, maxResults=20
            ).get("events", [])
        except Exception as e:
            span.record_exception(e)
            logger.warning("memory list_events failed: %s", e)
            return []
        messages = []
        for event in sorted(events, key=lambda e: e["eventTimestamp"]):
            for item in event.get("payload", []):
                conv = item.get("conversational")
                if conv:
                    role = "user" if conv["role"] == "USER" else "assistant"
                    messages.append({"role": role, "content": [{"text": conv["content"]["text"]}]})
        span.set_attribute("aws.agentcore.memory.messages", len(messages))
        return messages


def remember_turn(user_id: str, session_id: str, prompt: str, answer: str) -> None:
    """Store the turn; Memory extracts preferences from it in the background."""
    if not memory:
        return
    with tracer.start_as_current_span("agentcore.memory.create_event") as span:
        span.set_attribute("aws.agentcore.memory.id", MEMORY_ID)
        try:
            memory.create_event(
                memory_id=MEMORY_ID,
                actor_id=user_id,
                session_id=session_id,
                messages=[(prompt, "USER"), (answer, "ASSISTANT")],
            )
        except Exception as e:
            span.record_exception(e)
            logger.warning("memory create_event failed: %s", e)


def create_agent(preferences: list[str], extra_tools: list, history: list[dict]) -> Agent:
    model_id = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    model = BedrockModel(model_id=model_id, region_name=REGION, temperature=0.0, max_tokens=1024)
    system_prompt = (
        "You are an experienced travel agent. Use search_flights and get_weather for trip "
        "planning, and web_search for destination research. Give concise recommendations."
    )
    if preferences:
        system_prompt += "\n\nWhat you know about this traveler from earlier conversations:\n" + "\n".join(
            f"- {p}" for p in preferences
        )
    return Agent(model=model, system_prompt=system_prompt, tools=[web_search, *extra_tools], messages=history)


@app.entrypoint
def invoke(payload, context=None):
    with tracer.start_as_current_span("travel_agent.invoke") as span:
        span.set_attribute("enduser.id", payload.get("user_id", "anonymous"))
        span.set_attribute("session.id", getattr(context, "session_id", None) or "local")
        return handle(payload, context)


def handle(payload, context=None):
    user_input = payload.get("prompt", "")
    user_id = payload.get("user_id", "anonymous")
    session_id = getattr(context, "session_id", None) or "local"
    logger.info("[%s] user=%s %s", session_id, user_id, user_input)

    preferences = recall_preferences(user_id, user_input)
    history = load_session_history(user_id, session_id)

    if GATEWAY_URL:
        trace_headers = {"X-Amzn-Trace-Id": xray_trace_header(), "traceparent": w3c_traceparent()}
        trace_headers = {k: v for k, v in trace_headers.items() if v}
        gateway = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                GATEWAY_URL,
                aws_service="bedrock-agentcore",
                aws_region=REGION,
                headers=trace_headers or None,
            )
        )
        with gateway:
            agent = create_agent(preferences, gateway.list_tools_sync(), history)
            answer = str(agent(user_input))
    else:
        agent = create_agent(preferences, [], history)
        answer = str(agent(user_input))

    remember_turn(user_id, session_id, user_input, answer)
    return answer


if __name__ == "__main__":
    app.run()
