"""
Turn on AgentCore's own telemetry for the Memory and the Gateway:
  - TRACES           -> X-Ray (needs CloudWatch Transaction Search on in the region)
  - APPLICATION_LOGS -> a CloudWatch log group per resource (can be forwarded to Dash0)

Records what it creates in runtime_config.json so cleanup.py removes it.
Usage: python enable_observability.py
"""

import json

import boto3

with open("runtime_config.json") as f:
    config = json.load(f)

region, account = config["region"], config["account_id"]
logs = boto3.client("logs", region_name=region)

resources = {
    "memory": f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{config['memory_id']}",
    "gateway": config["gateway_arn"],
}

created = config.setdefault("observability", {"deliveries": [], "sources": [], "destinations": [], "log_groups": []})


def remember(kind, value):
    if value not in created[kind]:
        created[kind].append(value)
    with open("runtime_config.json", "w") as f:
        json.dump(config, f, indent=2)


def destination(name, dtype, arn=None):
    kwargs = {"name": name, "deliveryDestinationType": dtype}
    if arn:
        kwargs = {"name": name, "deliveryDestinationConfiguration": {"destinationResourceArn": arn}}
    d = logs.put_delivery_destination(**kwargs)["deliveryDestination"]
    remember("destinations", name)
    return d["arn"]


def deliver(source_name, resource_arn, log_type, dest_arn):
    logs.put_delivery_source(name=source_name, resourceArn=resource_arn, logType=log_type)
    remember("sources", source_name)
    try:
        d = logs.create_delivery(deliverySourceName=source_name, deliveryDestinationArn=dest_arn)["delivery"]
        remember("deliveries", d["id"])
        print(f"  {source_name}: {log_type} -> {dest_arn.split(':')[-1]}")
    except logs.exceptions.ConflictException:
        print(f"  {source_name}: already delivering")


xray_dest = destination("reinvent-agentcore-xray", "XRAY")

for kind, arn in resources.items():
    print(f"{kind}: {arn}")
    deliver(f"reinvent-{kind}-traces", arn, "TRACES", xray_dest)

    group = f"/aws/vendedlogs/bedrock-agentcore/reinvent-travel-{kind}"
    try:
        logs.create_log_group(logGroupName=group, tags={"project": "reinvent"})
        logs.put_retention_policy(logGroupName=group, retentionInDays=14)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    remember("log_groups", group)
    group_dest = destination(f"reinvent-{kind}-logs", "CWL", f"arn:aws:logs:{region}:{account}:log-group:{group}")
    deliver(f"reinvent-{kind}-logs", arn, "APPLICATION_LOGS", group_dest)

print("\nDone. Traces appear in CloudWatch → Transaction Search; logs in the log groups above.")
