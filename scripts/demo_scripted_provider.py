#!/usr/bin/env python3
"""Exercise one scripted outcome using a saved public NWL23 view, entirely offline."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from scrabble_harness.domain import ModelSettings
from scrabble_harness.harness import DecisionView, HarnessConfig, build_model_request
from scrabble_harness.providers import (
    ProviderCapabilities, ProviderToolCall, ScriptedProvider, ScriptedReply, ScriptedStep,
    ScriptedTransportError, ScriptedTransportFailure, TokenUsage, comparison_call, selection_call,
)

SCENARIOS = ("select", "compare", "refuse", "incomplete", "malformed", "multiple",
             "missing-usage", "transport-error", "timeout")


async def demonstrate(view: DecisionView, scenario: str) -> dict:
    config = HarnessConfig()
    model = ModelSettings(provider="scripted", model="offline-v1", max_output_tokens=1024)
    capabilities = ProviderCapabilities(provider=model.provider, model=model.model, max_output_tokens=1024)
    request = build_model_request(view, attempt_id="scripted-demo-attempt", model=model, capabilities=capabilities,
        config=config, remaining_limits=config.decision_limits, force_selection=scenario != "compare")
    handles = tuple(candidate.handle for candidate in view.candidates)
    calls = (selection_call("scripted-demo-call", handles[0]),)
    status = "completed"
    if scenario == "compare":
        calls = (comparison_call("scripted-demo-call", handles[:2]),)
    elif scenario in {"refuse", "incomplete"}:
        status = "refused" if scenario == "refuse" else "incomplete"
        calls = ()
    elif scenario == "malformed":
        calls = (ProviderToolCall(call_id="scripted-demo-call", name="select_move", arguments_json="{malformed"),)
    elif scenario == "multiple":
        calls += (selection_call("scripted-demo-extra-call", handles[1]),)
    outcome = ScriptedTransportFailure(error_category="connection_error", request_id="scripted-demo-request") \
        if scenario == "transport-error" else ScriptedReply(response_id="scripted-demo-response", request_id="scripted-demo-request",
            status=status, tool_calls=calls, usage=None if scenario == "missing-usage" else TokenUsage(input_tokens=100, output_tokens=10))
    provider = ScriptedProvider((ScriptedStep(attempt_id=request.attempt_id, outcome=outcome,
        delay_seconds=60.0 if scenario == "timeout" else 0.0),), capabilities=capabilities)
    result = {"purpose": "Scripted provider demonstration; synthetic usage; no tools executed or game committed",
              "scenario": scenario, "api_requests": 0, "api_cost_microusd": 0}
    try:
        async with asyncio.timeout(0.02 if scenario == "timeout" else 1.0):
            reply = await provider.complete(request)
        result["reply"] = reply.model_dump(mode="json")
    except ScriptedTransportError as error:
        result["error_category"] = error.error_category
    except TimeoutError:
        result["error_category"] = "caller_timeout"
    result["call_state"] = provider.calls[0].state
    result["remaining_script_steps"] = provider.remaining_steps
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=Path, default=PROJECT / "docs/decision-context-demo.json",
                        help="Saved demonstration containing model_view, or a standalone DecisionView JSON")
    parser.add_argument("--scenario", choices=SCENARIOS, default="select")
    args = parser.parse_args()
    payload = json.loads(args.view.read_text())
    view = DecisionView.model_validate_json(json.dumps(payload.get("model_view", payload)))
    print(json.dumps(asyncio.run(demonstrate(view, args.scenario)), indent=2))


if __name__ == "__main__":
    main()
