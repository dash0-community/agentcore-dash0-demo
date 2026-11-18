"""
Travel tools served to the agent through AgentCore Gateway (Lambda target).

Gateway passes the tool name in the Lambda client context as
"<targetName>___<toolName>" and the tool arguments as the event.
Data is generated, not live, so the demo is deterministic and free.
"""

import hashlib
import json

TOOL_SCHEMAS = [
    {
        "name": "get_weather",
        "description": "Get the weather forecast for a city on a date (YYYY-MM-DD).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "date": {"type": "string", "description": "Date as YYYY-MM-DD"},
            },
            "required": ["city"],
        },
    },
    {
        "name": "search_flights",
        "description": "Search flights between two cities on a date (YYYY-MM-DD). Returns options with price, stops and seat type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Departure city"},
                "destination": {"type": "string", "description": "Arrival city"},
                "date": {"type": "string", "description": "Date as YYYY-MM-DD"},
            },
            "required": ["origin", "destination"],
        },
    },
]


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(p.lower() for p in parts).encode()).hexdigest(), 16)


def get_weather(city: str, date: str = "next week") -> dict:
    if city.strip().lower() == "atlantis":
        # Deliberate failure, so error panels and failed tool calls can be demonstrated.
        raise RuntimeError("weather provider timeout for Atlantis")
    s = _seed(city, date)
    conditions = ["sunny", "partly cloudy", "light rain", "windy", "clear"]
    return {
        "city": city,
        "date": date,
        "condition": conditions[s % len(conditions)],
        "high_c": 12 + s % 18,
        "low_c": 4 + s % 10,
    }


def search_flights(origin: str, destination: str, date: str = "flexible") -> dict:
    s = _seed(origin, destination, date)
    airlines = ["SkyLark", "Atlas Air", "Blue Heron", "Northwind"]
    options = []
    for i in range(3):
        k = (s >> (i * 8)) % 1000
        options.append(
            {
                "airline": airlines[(s >> i) % len(airlines)],
                "departure": f"{6 + (k % 14):02d}:{(k * 7) % 60:02d}",
                "stops": k % 3,
                "price_usd": 180 + k % 900,
                "seats_left": {"window": k % 5, "aisle": (k // 5) % 5},
            }
        )
    return {"origin": origin, "destination": destination, "date": date, "options": options}


TOOLS = {"get_weather": get_weather, "search_flights": search_flights}


def handler(event, context):
    full_name = context.client_context.custom.get("bedrockAgentCoreToolName", "")
    tool_name = full_name.split("___", 1)[-1]
    if tool_name not in TOOLS:
        return {"error": f"unknown tool {full_name!r}"}
    result = TOOLS[tool_name](**event)
    print(json.dumps({"tool": tool_name, "args": event}))
    return result
