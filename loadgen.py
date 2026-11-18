"""
Keep the demo busy: run a short conversation every few minutes so dashboards
and AgentCore pages always have fresh data. About one in five conversations asks
about Atlantis, whose weather tool deliberately fails.

Usage: python loadgen.py [--minutes 40] [--interval 120]
"""

import argparse
import json
import random
import time
import uuid

import boto3

with open("runtime_config.json") as f:
    config = json.load(f)
client = boto3.client("bedrock-agentcore", region_name=config["region"])

parser = argparse.ArgumentParser()
parser.add_argument("--minutes", type=float, default=40)
parser.add_argument("--interval", type=float, default=120)
args = parser.parse_args()

TRAVELERS = ["maria", "jordan", "sam", "alex", "robin", "casey", "devon", "kim"]
CITIES = ["Las Vegas", "Lisbon", "Tokyo", "Denver", "Austin", "Berlin", "Mexico City"]
ORIGINS = ["Seattle", "New York", "Chicago", "San Francisco"]


def conversation():
    user = random.choice(TRAVELERS)
    if random.random() < 0.2:
        city = "Atlantis"
    else:
        city = random.choice(CITIES)
    prompt = (
        f"I'm flying from {random.choice(ORIGINS)} to {city} on 2026-12-0{random.randint(1, 9)} "
        "for re:Invent-style sightseeing. Find a flight and check the weather."
    )
    session = f"reinvent-load-{uuid.uuid4()}"
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=config["runtime_arn"],
        qualifier="DEFAULT",
        runtimeSessionId=session,
        payload=json.dumps({"prompt": prompt, "user_id": user}).encode(),
    )
    resp["response"].read()
    return user, city


end = time.time() + args.minutes * 60
while time.time() < end:
    try:
        user, city = conversation()
        print(f"{time.strftime('%H:%M:%S')} ok {user} -> {city}", flush=True)
    except Exception as e:
        print(f"{time.strftime('%H:%M:%S')} error {e}", flush=True)
    time.sleep(args.interval)
