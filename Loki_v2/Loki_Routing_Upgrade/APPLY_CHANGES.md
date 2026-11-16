# Loki routing upgrade — changes to existing loki_v2.py

This is intentionally NOT a full rewrite. Keep the existing UI, MemoryManager,
system tools, audio tools, filesystem tools, Scheduler and other working code.

## 1. Remove the old hard-coded model constants

Replace:

FAST_MODEL = os.environ.get("LOKI_FAST_MODEL", "llama3")
REASONING_MODEL = os.environ.get("LOKI_REASONING_MODEL", "llama3")

with:

# The routing upgrade loads these from config/models.json.
# Keep the names available to the rest of Loki.
FAST_MODEL = MODELS_CONFIG["models"]["fast"]["name"]
REASONING_MODEL = MODELS_CONFIG["models"]["reasoning"]["name"]

The cleaner option is to paste the configuration-loading block from
routing_upgrade.py near the top of loki_v2.py, after the imports.

You can remove the old OLLAMA_URL line too; the new config loader supplies it.

## 2. Add the config loader + routing code

Copy the contents of routing_upgrade.py into loki_v2.py after the imports
and before the existing MemoryManager.

Important: there must only be ONE definition of:
- FAST_MODEL
- REASONING_MODEL
- choose_route()
- build_prompt()/build_model_prompt()
- ask_ollama()

## 3. Replace the old choose_model()

Delete the old choose_model() function.

The new routing system chooses a route first:

simple
reasoning
tool

Then the model is selected from models.json.

This fixes the current problem where:
"make a calculator project using python"
can accidentally fall through to the simple model.

The weighted rules give that request a strong reasoning score.

## 4. Replace the old choose_route()

Delete the existing large hard-coded choose_route() function containing
tool_indicators and complex_indicators.

Use the choose_route() from routing_upgrade.py.

The indicators now live entirely in:
config/router.json

You can edit that file without touching Python.

## 5. Replace the old process_request()

Use this:

def process_request(user_text):
    decision = choose_route(user_text)

    print(
        f"[LOKI ROUTER] "
        f"Route={decision.route} | "
        f"Confidence={decision.confidence:.2f} | "
        f"Source={decision.source} | "
        f"Reason={decision.reason} | "
        f"Scores={decision.scores}"
    )

    if decision.route == ROUTE_TOOL:
        return process_tool_request(user_text), decision, "tool router"

    model = model_for_route(decision.route)
    prompt = build_model_prompt(user_text, decision.route)

    reply = ask_ollama(
        prompt,
        model=model,
        stream=False
    )

    return reply, decision, model

If your existing streaming implementation is already working, keep it and pass
your existing stream callback into ask_ollama(). The important change is that
the model argument comes from model_for_route(decision.route).

## 6. IMPORTANT: fix the existing ask_ollama()

The current file has several versions of model-calling functions
(ask_ai, ask_fast, ask_smart, etc.). Keep ONE actual HTTP implementation.

Use this shape:

def ask_ollama(prompt, model, system=None, stream=False, timeout=120):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream
    }

    if system:
        payload["system"] = system

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        stream=stream,
        timeout=timeout
    )

    response.raise_for_status()

    if not stream:
        return response.json().get("response", "")

    # Keep your existing streaming parser here.

Do not keep a second ask_ollama() below this one.

## 7. Replace the old build_prompt()

Use:

def build_prompt(user_text):
    decision = choose_route(user_text)
    return build_model_prompt(user_text, decision.route)

However, it is preferable for process_request() to choose the route only ONCE
and directly call build_model_prompt(), so the router is not called twice.

## 8. Keep the existing tool execution system

Do NOT remove your existing:
- TOOLS
- safety_check()
- execute_tool()
- explicit_action_request()
- parse_action()
- Scheduler
- audio functions
- filesystem functions

They are separate from model routing.

The new config/tools.json is intended to become the central permission source.
If you want to activate it immediately, update the existing safety_check() so
that it reads TOOLS_CONFIG["tools"] instead of maintaining another hard-coded
allowed-tools set.

## 9. App registry

Your current APP_REGISTRY contains real executable paths and aliases.

Move that registry into config/apps.json.

Then have open_app() read APP_CONFIG.

Do NOT blindly copy the example paths if your installed applications are in
different locations. The example uses Windows environment variables and common
install locations so it is easier to edit.

## 10. UI model indicator

Your existing status label currently says something like:

LOCAL • ONLINE

Change the successful response status update to:

status_label.configure(
    text=f"{decision.route.upper()} • {model}"
)

You will then see:

SIMPLE • llama3

or:

REASONING • mistral-small3.2:24b

or:

TOOL • tool router

This makes testing the router much easier.

## 11. Expected tests

These should route approximately as follows:

"What's the weather like?"             -> simple
"Hello Loki"                            -> simple
"What is 25 + 37?"                      -> simple
"How much RAM do I have?"              -> tool
"Open Discord"                          -> tool
"Set my volume to 50%"                 -> tool
"Explain recursion"                     -> reasoning
"Write a Python function"               -> reasoning
"Make a calculator project using Python"-> reasoning
"Build me a Python game"                -> reasoning
"Debug this code"                       -> reasoning
"Redesign Loki's architecture"          -> reasoning

The AI classifier is only used for ambiguous cases. This keeps Loki fast while
giving it a second layer of judgement when the editable rules are uncertain.

## 12. Why this fixes the current problem

Previously Loki effectively had:

keyword match -> simple/complex

and multiple copies of routing/model-selection logic.

Now it has:

user request
    |
    v
weighted editable rules
    |
    +-- strong tool --> Python tools
    |
    +-- clear reasoning --> Mistral 24B
    |
    +-- unclear -------> llama3 classifier
                              |
                              v
                         final route
                              |
                              v
                         selected model

The important part is that all indicators and most behaviour are now in JSON.
