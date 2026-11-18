# Amazon Bedrock AgentCore + Dash0

A travel agent on Amazon Bedrock AgentCore that shows one request as one trace in [Dash0](https://www.dash0.com/): the agent's model and tool calls, plus the spans AWS records inside AgentCore Gateway, AgentCore Memory, and the Lambda function behind the tools.

The agent is built with [Strands Agents](https://strandsagents.com/). It remembers each traveler's preferences in AgentCore Memory and calls two tools, `get_weather` and `search_flights`, through AgentCore Gateway. The tools return generated data. `get_weather` fails on purpose for the city "Atlantis", so you can see a failed tool call in Dash0.

For the full setup guide, see [Set Up Amazon Bedrock AgentCore Monitoring](https://www.dash0.com/docs/dash0/infrastructure/aws/bedrock-agentcore/setup) in the Dash0 docs.

## How Telemetry Reaches Dash0

```
AgentCore Runtime (utils/travel_agent.py)
  ├── OpenTelemetry SDK ──── OTLP ──────────────────────────────────────────► Dash0
  │     agent, chat, and execute_tool spans; metrics; logs
  ├── traceparent + X-Amzn-Trace-Id ──► AgentCore Gateway ──► Lambda tools
  └── X-Amzn-Trace-Id ────────────────► AgentCore Memory
                                          │
        Gateway, Memory, and Lambda spans ▼
        X-Ray → CloudWatch Transaction Search (aws/spans)
          → subscription filter → Amazon Data Firehose ─────────────────────► Dash0

AWS/Bedrock-AgentCore CloudWatch metrics ── Dash0 AWS integration ──────────► Dash0
```

- `DISABLE_ADOT_OBSERVABILITY=true` turns off the default CloudWatch pipeline, and the standard `OTEL_EXPORTER_OTLP_*` variables point the agent at Dash0.
- AgentCore records Gateway and Memory spans only for requests that carry a sampled trace context. The agent wraps each invocation in a root span and sends that context to Gateway and Memory, so all spans share one trace ID.

## Prerequisites

- Python 3.10 or later
- AWS credentials for an account and region where AgentCore is available, for example `us-east-1`
- A Dash0 auth token with ingest permission, from **Settings → Auth Tokens**
- For the AWS-side spans: [CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html) turned on in the region. This is an account-wide setting, so check with the account owner first.
- For the metrics: your AWS account connected to Dash0 through the [AWS integration](https://www.dash0.com/docs/dash0/infrastructure/aws/setup)

## Quick Start

### 1. Deploy the Agent

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set DASH0_AUTH_TOKEN, DASH0_OTLP_ENDPOINT, and DASH0_DATASET
python deploy.py
```

`deploy.py` creates the Memory, the Lambda tools, the Gateway, and the Runtime. Every resource is named `reinvent-*` or `reinvent_*` and recorded in `runtime_config.json`. Running it again reuses what exists.

### 2. Talk to the Agent

```bash
python invoke.py              # two sessions: the second one recalls preferences from the first
python loadgen.py --minutes 30   # optional: a conversation every two minutes
```

Open **Tracing** in Dash0 and filter by your `OTEL_SERVICE_NAME`. At this point you see the agent's own spans.

### 3. Add Gateway, Memory, and Lambda Spans

With Transaction Search on, turn on tracing for the Memory and the Gateway:

```bash
python enable_observability.py
```

Then forward the `aws/spans` log group to Dash0. Use the **AWS CloudWatch Spans via Firehose** endpoint from **Settings → Endpoints**, and export `DASH0_AUTH_TOKEN` in your shell first:

```bash
aws cloudformation deploy \
  --template-file spans-to-dash0.yaml \
  --stack-name reinvent-agentcore-spans-to-dash0 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    Dash0AuthToken="$DASH0_AUTH_TOKEN" \
    Dash0SpansEndpoint=https://ingress.us-west-2.aws.dash0.com/firehose/cwspans \
    Dash0Dataset=default
```

The stack's subscription filter forwards only spans that contain `reinvent`, because `aws/spans` holds the spans of every traced service in the account and region. Change `FilterPattern` to forward more.

### 4. Add the Metrics and Dashboard

In Dash0, open your AWS integration, select the **Metrics** tab, and turn on the **Bedrock-AgentCore** namespace. Then install the **Amazon Bedrock AgentCore** dashboard from the Dash0 Integration Hub.

## Clean Up

```bash
python cleanup.py
```

`cleanup.py` deletes the spans stack, the tracing deliveries and log groups, the Runtime, Gateway, Memory, Lambda function, IAM roles, and the code bucket. It leaves account-wide settings alone: Transaction Search, the X-Ray destination, and the CloudWatch Logs resource policies.

## Files

| File | Description |
| --- | --- |
| `utils/travel_agent.py` | The agent: OpenTelemetry setup, Memory, Gateway tools, and trace context propagation |
| `utils/tools_lambda.py` | The Lambda function behind the Gateway tools |
| `deploy.py` | Creates the Memory, Lambda, Gateway, and Runtime |
| `invoke.py` | Runs a two-session conversation to show Memory |
| `loadgen.py` | Runs conversations on a timer, including failing tool calls |
| `enable_observability.py` | Turns on tracing and application logs for the Memory and Gateway |
| `spans-to-dash0.yaml` | CloudFormation stack that forwards `aws/spans` to Dash0 through Firehose |
| `cleanup.py` | Deletes everything the scripts created |

## Credits

Based on the Dash0 observability sample in [awslabs/agentcore-samples](https://github.com/awslabs/agentcore-samples/tree/main/03-integrations/3p-observability/dash0), licensed under Apache 2.0. See [NOTICE](NOTICE) for the changes.
