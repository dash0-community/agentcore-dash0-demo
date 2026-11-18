"""
Deploy the re:Invent travel agent: AgentCore Memory + Gateway (Lambda tools) + Runtime,
with the agent's traces, metrics and logs sent to Dash0 over OTLP.

Every AWS resource is named reinvent-* / reinvent_* and listed in runtime_config.json,
which cleanup.py uses to delete them. Re-running reuses what already exists.

Usage:
    cp .env.example .env  # fill in your Dash0 auth token
    python deploy.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

import boto3
from bedrock_agentcore.memory import MemoryClient
from dotenv import load_dotenv

load_dotenv()

AGENT_NAME = "reinvent_travel_agent"
MEMORY_NAME = "reinvent_travel_memory"
GATEWAY_NAME = "reinvent-travel-gateway"
TARGET_NAME = "travel-tools"
TOOLS_FUNCTION = "reinvent-travel-tools"
RUNTIME_ROLE = "reinvent-travel-agent-runtime"
GATEWAY_ROLE = "reinvent-travel-gateway"
TOOLS_ROLE = "reinvent-travel-tools"
TAGS = {"project": "reinvent"}

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
session = boto3.Session(region_name=REGION)
ACCOUNT_ID = session.client("sts").get_caller_identity()["Account"]
S3_BUCKET = f"reinvent-agentcore-code-{ACCOUNT_ID}-{REGION}"
S3_KEY = f"{AGENT_NAME}/code.zip"

iam = session.client("iam")
control = session.client("bedrock-agentcore-control")
lam = session.client("lambda")
s3 = session.client("s3")

CONFIG_FILE = "runtime_config.json"
config = json.load(open(CONFIG_FILE)) if os.path.exists(CONFIG_FILE) else {}
config.update({"region": REGION, "account_id": ACCOUNT_ID, "s3_bucket": S3_BUCKET, "s3_key": S3_KEY})


def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def ensure_role(name: str, service: str, policy: dict | None, managed: list[str] = ()) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}},
            }
        ],
    }
    try:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )["Role"]["Arn"]
        print(f"  created role {name}")
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        created = False
    if policy:
        iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    config.setdefault("roles", [])
    if name not in config["roles"]:
        config["roles"].append(name)
    save_config()
    if created:
        time.sleep(10)  # IAM propagation
    return arn


# ── Memory ─────────────────────────────────────────────────────────────────────


def ensure_memory() -> str:
    print("\n[1/4] AgentCore Memory")
    for m in control.list_memories().get("memories", []):
        if m["id"].startswith(MEMORY_NAME):
            print(f"  exists: {m['id']}")
            config["memory_id"] = m["id"]
            save_config()
            return m["id"]
    mem = MemoryClient(region_name=REGION).create_memory_and_wait(
        name=MEMORY_NAME,
        description="re:Invent travel agent: remembers traveler preferences",
        strategies=[
            {
                "userPreferenceMemoryStrategy": {
                    "name": "traveler_preferences",
                    "namespaces": ["/users/{actorId}/preferences"],
                }
            }
        ],
        event_expiry_days=30,
    )
    config["memory_id"] = mem["id"]
    save_config()
    print(f"  created: {mem['id']}")
    return mem["id"]


# ── Tools Lambda ───────────────────────────────────────────────────────────────


def ensure_tools_lambda() -> str:
    print("\n[2/4] Tool Lambda")
    role_arn = ensure_role(
        TOOLS_ROLE,
        "lambda.amazonaws.com",
        None,
        managed=[
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess",
        ],
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write("utils/tools_lambda.py", "tools_lambda.py")
    code = buf.getvalue()
    try:
        arn = lam.get_function(FunctionName=TOOLS_FUNCTION)["Configuration"]["FunctionArn"]
        lam.update_function_code(FunctionName=TOOLS_FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=TOOLS_FUNCTION)
        lam.update_function_configuration(FunctionName=TOOLS_FUNCTION, TracingConfig={"Mode": "Active"})
        print(f"  updated: {arn}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                arn = lam.create_function(
                    FunctionName=TOOLS_FUNCTION,
                    Runtime="python3.13",
                    Architectures=["arm64"],
                    Role=role_arn,
                    Handler="tools_lambda.handler",
                    Code={"ZipFile": code},
                    Timeout=10,
                    TracingConfig={"Mode": "Active"},  # Lambda spans go to aws/spans via X-Ray
                    Tags=TAGS,
                )["FunctionArn"]
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)  # role not assumable yet
        lam.get_waiter("function_active_v2").wait(FunctionName=TOOLS_FUNCTION)
        print(f"  created: {arn}")
    config["tools_function"] = TOOLS_FUNCTION
    save_config()
    return arn


# ── Gateway ────────────────────────────────────────────────────────────────────


def ensure_gateway(tools_arn: str) -> dict:
    print("\n[3/4] AgentCore Gateway")
    from importlib import util

    spec = util.spec_from_file_location("tools_lambda", "utils/tools_lambda.py")
    tools_mod = util.module_from_spec(spec)
    spec.loader.exec_module(tools_mod)

    role_arn = ensure_role(
        GATEWAY_ROLE,
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": tools_arn}],
        },
    )
    gw = next((g for g in control.list_gateways().get("items", []) if g["name"] == GATEWAY_NAME), None)
    if gw:
        gw_id = gw["gatewayId"]
        print(f"  exists: {gw_id}")
    else:
        gw_id = control.create_gateway(
            name=GATEWAY_NAME,
            description="re:Invent travel tools (flights, weather)",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            tags=TAGS,
        )["gatewayId"]
        print(f"  created: {gw_id}")
    while (g := control.get_gateway(gatewayIdentifier=gw_id))["status"] != "READY":
        if g["status"] == "FAILED":
            sys.exit(f"gateway failed: {g.get('statusReasons')}")
        time.sleep(5)
    config.update({"gateway_id": gw_id, "gateway_arn": g["gatewayArn"], "gateway_url": g["gatewayUrl"]})
    save_config()

    targets = control.list_gateway_targets(gatewayIdentifier=gw_id).get("items", [])
    if not any(t["name"] == TARGET_NAME for t in targets):
        control.create_gateway_target(
            gatewayIdentifier=gw_id,
            name=TARGET_NAME,
            description="Flight search and weather forecast",
            targetConfiguration={
                "mcp": {"lambda": {"lambdaArn": tools_arn, "toolSchema": {"inlinePayload": tools_mod.TOOL_SCHEMAS}}}
            },
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        print(f"  target {TARGET_NAME} added")
    return config


# ── Runtime ────────────────────────────────────────────────────────────────────


def build_and_upload_package():
    try:
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=S3_BUCKET)
        else:
            s3.create_bucket(Bucket=S3_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_tagging(Bucket=S3_BUCKET, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})
    except (s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
        pass
    pkg_dir, zip_file = "deployment_package", "deployment_package.zip"
    shutil.rmtree(pkg_dir, ignore_errors=True)
    if os.path.exists(zip_file):
        os.remove(zip_file)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python-platform", "aarch64-manylinux2014", "--python-version", "3.13",
         "--target", pkg_dir, "--only-binary", ":all:", "-r", "requirements.txt"],
        check=True,
    )
    subprocess.run(["zip", "-qr", f"../{zip_file}", "."], cwd=pkg_dir, check=True)
    subprocess.run(["zip", "-qj", zip_file, "utils/travel_agent.py"], check=True)
    s3.upload_file(zip_file, S3_BUCKET, S3_KEY)
    shutil.rmtree(pkg_dir)
    os.remove(zip_file)
    print(f"  package uploaded to s3://{S3_BUCKET}/{S3_KEY}")


def ensure_runtime():
    print("\n[4/4] AgentCore Runtime")
    mem_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:memory/{config['memory_id']}"
    role_arn = ensure_role(
        RUNTIME_ROLE,
        "bedrock-agentcore.amazonaws.com",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                    "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*",
                },
                {"Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*"},
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{ACCOUNT_ID}:*"],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:CreateEvent",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:GetEvent",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:ListMemoryRecords",
                    ],
                    "Resource": mem_arn,
                },
                {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": config["gateway_arn"]},
            ],
        },
    )
    build_and_upload_package()
    endpoint = os.getenv("DASH0_OTLP_ENDPOINT", "https://ingress.us-west-2.aws.dash0.com").rstrip("/")
    dataset = os.getenv("DASH0_DATASET", "default")
    env = {
        # Standard OpenTelemetry exporter settings, pointing at Dash0
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer%20{os.environ['DASH0_AUTH_TOKEN']},Dash0-Dataset={dataset}",
        "OTEL_SERVICE_NAME": os.getenv("OTEL_SERVICE_NAME", "reinvent-travel-agent"),
        # Keep AgentCore from injecting its own ADOT configuration
        "DISABLE_ADOT_OBSERVABILITY": "true",
        "BEDROCK_MODEL_ID": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "MEMORY_ID": config["memory_id"],
        "GATEWAY_URL": config["gateway_url"],
    }
    args = dict(
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {"s3": {"bucket": S3_BUCKET, "prefix": S3_KEY}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["travel_agent.py"],
            }
        },
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=env,
        description="re:Invent travel agent with Memory, Gateway and Dash0 observability",
    )
    existing = next(
        (r for r in control.list_agent_runtimes().get("agentRuntimes", []) if r["agentRuntimeName"] == AGENT_NAME),
        None,
    )
    if existing:
        runtime_id = existing["agentRuntimeId"]
        control.update_agent_runtime(agentRuntimeId=runtime_id, **args)
        print(f"  updating: {runtime_id}")
    else:
        runtime_id = control.create_agent_runtime(agentRuntimeName=AGENT_NAME, tags=TAGS, **args)["agentRuntimeId"]
        print(f"  created: {runtime_id}")
    while (r := control.get_agent_runtime(agentRuntimeId=runtime_id))["status"] != "READY":
        if r["status"].endswith("FAILED"):
            sys.exit(f"runtime failed: {r.get('failureReason')}")
        time.sleep(10)
    config.update({"runtime_id": runtime_id, "runtime_arn": r["agentRuntimeArn"], "agent_name": AGENT_NAME})
    save_config()
    print(f"  ready: {r['agentRuntimeArn']}")


def main():
    if not os.getenv("DASH0_AUTH_TOKEN"):
        sys.exit("ERROR: DASH0_AUTH_TOKEN not set. Copy .env.example to .env and fill it in.")
    print(f"Region {REGION}, account {ACCOUNT_ID}")
    ensure_memory()
    tools_arn = ensure_tools_lambda()
    ensure_gateway(tools_arn)
    ensure_runtime()
    print("\nDone. Next: python invoke.py, then open Dash0 → Tracing")


if __name__ == "__main__":
    main()
