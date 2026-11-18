"""
Invoke the deployed travel agent to show Memory and Gateway working.

Session 1: the traveler states preferences.
(wait while Memory extracts them in the background)
Session 2: a new session plans a trip; the agent uses the remembered
preferences and calls search_flights / get_weather through the Gateway.

Usage: python invoke.py [--user maria] [--skip-wait]
"""

import argparse
import json
import time
import uuid

import boto3

with open("runtime_config.json") as f:
    config = json.load(f)

client = boto3.client("bedrock-agentcore", region_name=config["region"])

parser = argparse.ArgumentParser()
parser.add_argument("--user", default="maria")
parser.add_argument("--skip-wait", action="store_true")
args = parser.parse_args()


def ask(session_id: str, prompt: str):
    print(f"\n[{session_id[:8]}] {args.user}: {prompt}\n" + "-" * 60)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=config["runtime_arn"],
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt, "user_id": args.user}).encode(),
    )
    body = resp["response"].read().decode()
    try:
        print(json.loads(body))
    except json.JSONDecodeError:
        print(body)


def new_session() -> str:
    return f"reinvent-{uuid.uuid4()}"  # runtimeSessionId must be at least 33 characters


s1 = new_session()
ask(s1, "Hi! I always fly window seat, I hate layovers, and my budget for flights is under $600.")
ask(s1, "I'm also vegetarian and I love walking tours.")

if not args.skip_wait:
    print("\nWaiting 90s while AgentCore Memory extracts preferences...")
    time.sleep(90)

s2 = new_session()
ask(s2, "Plan a 3-day trip from Seattle to Las Vegas on 2026-12-01. Find me a flight and check the weather.")
