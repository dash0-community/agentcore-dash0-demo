"""Delete every resource deploy.py created, as listed in runtime_config.json."""

import json
import time

import boto3

with open("runtime_config.json") as f:
    config = json.load(f)

session = boto3.Session(region_name=config["region"])
control = session.client("bedrock-agentcore-control")
iam = session.client("iam")
lam = session.client("lambda")
s3 = session.client("s3")


def attempt(label, fn):
    print(label)
    try:
        fn()
    except Exception as e:
        print(f"  {e}")


cfn = session.client("cloudformation")
attempt("Deleting stack reinvent-agentcore-spans-to-dash0 (Firehose, subscription filter)",
        lambda: (cfn.delete_stack(StackName="reinvent-agentcore-spans-to-dash0"),
                 cfn.get_waiter("stack_delete_complete").wait(StackName="reinvent-agentcore-spans-to-dash0")))

obs = config.get("observability", {})
if obs:
    logs = session.client("logs")
    for d in obs.get("deliveries", []):
        attempt(f"Deleting delivery {d}", lambda d=d: logs.delete_delivery(id=d))
    for s in obs.get("sources", []):
        attempt(f"Deleting delivery source {s}", lambda s=s: logs.delete_delivery_source(name=s))
    for d in obs.get("destinations", []):
        attempt(f"Deleting delivery destination {d}", lambda d=d: logs.delete_delivery_destination(name=d))
    for g in obs.get("log_groups", []):
        attempt(f"Deleting log group {g}", lambda g=g: logs.delete_log_group(logGroupName=g))

if rid := config.get("runtime_id"):
    attempt(f"Deleting runtime {rid}", lambda: control.delete_agent_runtime(agentRuntimeId=rid))

if gid := config.get("gateway_id"):
    for t in control.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
        attempt(f"Deleting gateway target {t['name']}",
                lambda: control.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"]))
    for _ in range(30):
        if not control.list_gateway_targets(gatewayIdentifier=gid).get("items"):
            break
        time.sleep(5)
    attempt(f"Deleting gateway {gid}", lambda: control.delete_gateway(gatewayIdentifier=gid))

if mid := config.get("memory_id"):
    attempt(f"Deleting memory {mid}", lambda: control.delete_memory(memoryId=mid))

if fn := config.get("tools_function"):
    attempt(f"Deleting Lambda {fn}", lambda: lam.delete_function(FunctionName=fn))

for role in config.get("roles", []):
    def drop(role=role):
        for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
    attempt(f"Deleting IAM role {role}", drop)

if bucket := config.get("s3_bucket"):
    def empty_and_delete():
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=bucket)
    attempt(f"Deleting bucket {bucket}", empty_and_delete)

print("Cleanup complete.")
