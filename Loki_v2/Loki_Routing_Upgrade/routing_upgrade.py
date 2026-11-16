# ============================================================
# LOKI CONFIGURATION + ROUTING UPGRADE
# ============================================================

from pathlib import Path
from dataclasses import dataclass
import json
import logging
import re

CONFIG_DIR = Path(__file__).resolve().parent / "config"
ROUTER_LOGGER = logging.getLogger("loki.router")


def load_json_config(filename):
    path = CONFIG_DIR / filename

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing Loki configuration file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{filename} must contain a JSON object.")

    return data


MODELS_CONFIG = load_json_config("models.json")
ROUTER_CONFIG = load_json_config("router.json")
PROMPTS_CONFIG = load_json_config("prompts.json")
TOOLS_CONFIG = load_json_config("tools.json")
APP_CONFIG = load_json_config("apps.json")

OLLAMA_URL = MODELS_CONFIG["ollama_url"]
FAST_MODEL = MODELS_CONFIG["models"]["fast"]["name"]
REASONING_MODEL = MODELS_CONFIG["models"]["reasoning"]["name"]
CLASSIFIER_MODEL = MODELS_CONFIG["models"]["classifier"]["name"]

ROUTE_SIMPLE = "simple"
ROUTE_COMPLEX = "reasoning"
ROUTE_TOOL = "tool"


@dataclass
class RouteDecision:
    route: str
    confidence: float
    reason: str
    scores: dict
    source: str


def _score_route(user_text, route):
    text = user_text.lower()
    indicators = ROUTER_CONFIG["routes"].get(route, {}).get("indicators", {})
    return sum(
        float(weight)
        for indicator, weight in indicators.items()
        if indicator.lower() in text
    )


def _heuristic_route(user_text):
    scores = {
        ROUTE_SIMPLE: 0.0,
        ROUTE_COMPLEX: _score_route(user_text, ROUTE_COMPLEX),
        ROUTE_TOOL: _score_route(user_text, ROUTE_TOOL),
    }

    length = len(user_text.strip())

    if length >= ROUTER_CONFIG["settings"]["long_request_threshold"]:
        scores[ROUTE_COMPLEX] += 4

    if length <= ROUTER_CONFIG["settings"]["very_short_request_threshold"]:
        scores[ROUTE_SIMPLE] += 1.5

    # Tool intent takes precedence when it is clearly strong.
    if scores[ROUTE_TOOL] >= ROUTER_CONFIG["settings"]["strong_tool_score"]:
        route = ROUTE_TOOL
    else:
        route = max(scores, key=scores.get)

    total = sum(scores.values())
    confidence = (
        scores[route] / total
        if total > 0
        else 0.55
    )

    if route == ROUTE_TOOL:
        reason = "Strong computer-action or live-PC-information indicators."
    elif route == ROUTE_COMPLEX:
        reason = "Coding, project, reasoning or multi-step indicators."
    else:
        reason = "No strong tool or reasoning indicators."

    return RouteDecision(
        route=route,
        confidence=round(confidence, 3),
        reason=reason,
        scores=scores,
        source="weighted-rules",
    )


def _extract_json(text):
    # Models occasionally wrap JSON in markdown. Recover the first object.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _ai_route(user_text):
    prompt = (
        "Classify the following request.\n\n"
        f"USER REQUEST:\n{user_text}\n\n"
        "Return ONLY JSON."
    )

    try:
        raw = ask_ollama(
            prompt,
            CLASSIFIER_MODEL,
            system=PROMPTS_CONFIG["classifier"]["system"],
            stream=False,
            timeout=MODELS_CONFIG["timeouts"]["classifier_seconds"],
        )

        data = _extract_json(raw)
        if not data:
            return None

        route = str(data.get("route", "")).lower().strip()
        confidence = float(data.get("confidence", 0))
        reason = str(data.get("reason", "AI classifier decision.")).strip()

        if route not in {ROUTE_SIMPLE, ROUTE_COMPLEX, ROUTE_TOOL}:
            return None

        return RouteDecision(
            route=route,
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            scores={},
            source="ai-classifier",
        )

    except Exception as exc:
        ROUTER_LOGGER.warning("AI classifier unavailable: %s", exc)
        return None


def choose_route(user_text):
    heuristic = _heuristic_route(user_text)

    settings = ROUTER_CONFIG["settings"]

    # Strong tool requests do not need another model call.
    if (
        heuristic.route == ROUTE_TOOL
        and heuristic.scores.get(ROUTE_TOOL, 0)
        >= settings["strong_tool_score"]
    ):
        return heuristic

    # Use the fast classifier only when the rule engine is uncertain.
    total = sum(heuristic.scores.values())
    use_classifier = (
        settings["use_ai_classifier_for_ambiguous_requests"]
        and (
            total == 0
            or heuristic.confidence < settings["classifier_min_confidence"]
            or abs(
                heuristic.scores.get(ROUTE_TOOL, 0)
                - heuristic.scores.get(ROUTE_COMPLEX, 0)
            ) <= settings["ambiguous_score_gap"]
        )
    )

    if use_classifier:
        classified = _ai_route(user_text)
        if classified is not None:
            return classified

    return heuristic


def build_model_prompt(user_text, route):
    system_prompt = PROMPTS_CONFIG[route]["system"]

    return (
        f"{system_prompt}\n\n"
        f"{memory.build_context()}\n\n"
        f"CURRENT USER MESSAGE:\n{user_text}\n\n"
        "Respond naturally."
    )


def model_for_route(route):
    if route == ROUTE_COMPLEX:
        return REASONING_MODEL

    return FAST_MODEL
