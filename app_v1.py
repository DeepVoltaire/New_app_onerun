import os
import json
os.environ["OPENAI_API_KEY"] = json.load(open("openai_key.json"))["OPENAI_API_KEY"] 
import streamlit as st
import ee
import time
import traceback
from dataclasses import dataclass
from openai import OpenAI

from code_examples import heatmap_code, no2_code

ee.Initialize(project="cloudtostreet-294422")

# ----------------------------- UI SETUP -------------------------------------
st.set_page_config(
    page_title="Talk2Earth",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 Talk2Earth")

# Zentraler Render-Platzhalter, in den die Mini-App *immer* gerendert wird
if "render_slot" not in st.session_state:
    st.session_state.render_slot = st.empty()
# ----------------------------- STATE ----------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []  # [{role, content}]
if "turns" not in st.session_state:
    st.session_state.turns = []  # [{user, router, use_case, output}]
if "client" not in st.session_state:
    st.session_state.client = None
if "_runner_autorun_done" not in st.session_state:
    st.session_state._runner_autorun_done = False
if "skip_agent_on_next_run" not in st.session_state:
    st.session_state.skip_agent_on_next_run = False
if "queued_input" not in st.session_state:
    st.session_state.queued_input = None

# ----------------------------- CONFIG SIDEBAR -------------------------------
with st.sidebar:
    st.subheader("⚙️ Konfiguration")
    api_key = st.text_input("OPENAI_API_KEY", type="password", value=os.getenv("OPENAI_API_KEY", ""))
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
        if st.session_state.client is None:
            st.session_state.client = OpenAI()
    model = st.selectbox("Modell", ["gpt-5", "gpt-4o-2024-08-06", "gpt-5-mini", "gpt-5-nano", "gpt-5-chat-latest"], index=1)
    use_responses_api = st.toggle("Responses API verwenden (statt Chat Completions)", value=False, help="Die Responses API ist neuer; Chat Completions ist weiterhin stabil.")
    # show_debug = st.toggle("Debug-Details anzeigen", value=True)
    if st.button("🧹 Verlauf leeren"):
        st.session_state.messages = []
        st.session_state.turns = []
        slot = st.session_state.render_slot
        slot.empty()  # alte Mini-App entfernen
        st.rerun()


# ----------------------------- USE CASES ------------------------------------
@dataclass
class UseCase:
    key: str
    title: str
    description: str
    # Optional parameter schema the router may extract
    param_schema: dict


def run_mini_app(): #code: str):
    """Run the given code in the render slot."""
    slot = st.session_state.render_slot
    slot.empty()  # alte Mini-App entfernen
    with slot.container():
        ns = {"__name__": "__generated__", "st": st}
        compiled = compile(st.session_state.last_code, "<autorender-now>", "exec")
        # st.write(f"calling exec()")
        exec(compiled, ns, ns)

        entry = None
        fn = ns.get("main")
        if callable(fn):
            entry = fn
        if entry is None:
            raise RuntimeError("Autorender-now: Kein Entry-Point (t2e_app/render/main) gefunden.")
        try:
            entry()
        except Exception as e:
            st.write(f"entry() failed with exception {e}")
            st.write(traceback.format_exc())  # prints full traceback to stdout
            try:
                entry(st)
            except:
                st.write(f"entry(st) failed")
                st.write(traceback.format_exc())  # prints full traceback to stdout
    # st.session_state._runner_autorun_done = True
    return True
    

USE_CASES = [
    UseCase(
        key="heatmap",
        title="Heatmap erstellen",
        description="Creates a heatmap of a typical summer with Landsat.",
        param_schema={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "Where to focus the map"}, "year": {"type": "integer", "description": "Year"}},
            "additionalProperties": True,
        },
    ),
    UseCase(
        key="air pollution",
        title="Show air pollution globally",
        description="Uses Sentinel5P to show NO2 and other pollutants.",
        param_schema={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "Where to focus the map"}, "year": {"type": "integer", "description": "Year"}},
            "additionalProperties": True,
        },
    ),
]

USE_CASE_LOOKUP = {uc.key: uc for uc in USE_CASES}

def build_router_schema():
    return {
        "name": "RoutingResult",
        "schema": {
            "type": "object",
            "additionalProperties": False,   # <-- REQUIRED
            "properties": {
                "use_case": {
                    "type": "string",
                    "enum": [uc.key for uc in USE_CASES],
                    "description": "Ausgewählter Use Case-Key",
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
                "params": {
                    "type": "object", 
                    "additionalProperties": False,
                    "properties": {},
                },
            },
            "required": ["use_case", "confidence", "reason", "params"],
        },
        "strict": True,
    }

# ----------------------------- ROUTER ---------------------------------------
ROUTER_SYSTEM_PROMPT = (
    "You are a router that assigns user input to a specific use case. "
    "Return only a JSON object that matches the provided JSON schema. "
    "Use params when appropriate by extracting them from the user input (e.g., location)."
)


def call_router(model: str, message: str, responses_api: bool):
    """Ask GPT‑5 to route to a use case. Fallback to keyword routing without API key."""
    # Build a compact description of available cases for the model
    uc_descriptions = "\n".join([f"- {uc.key}: {uc.title} – {uc.description}" for uc in USE_CASES])

    router_schema = build_router_schema()

    # responses_api = False
    if responses_api:
        resp = st.session_state.client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT + "\n\nVerfügbare Use Cases:\n" + uc_descriptions},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_schema", "json_schema": router_schema},
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
        )
        raw = getattr(resp, "output_text", str(resp))
    else:
        resp = st.session_state.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT + "\n\nVerfügbare Use Cases:\n" + uc_descriptions},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_schema", "json_schema": router_schema},
        )
        raw = resp.choices[0].message.content

    try:
        return json.loads(raw)
    except Exception:
        # Be robust against any formatting hiccups
        return {"use_case": "summarize", "confidence": 0.2, "reason": f"Parsing-Fehler, Rohantwort: {raw[:200]}...", "params": {}}
    
st.session_state._runner_autorun_done = False
# --- UI-only Rerun Handling (einmalige Entkopplung des Autorender-Reruns) ---
ui_only_rerun = False
if st.session_state.get("skip_agent_on_next_run"):
    st.session_state["skip_agent_on_next_run"] = False
    ui_only_rerun = True
    # st.write("ui_only_rerun active.")
    # time.sleep(4)

# # ----------------------------- CHAT UI --------------------------------------
# chat_col, info_col = st.columns([0.62, 0.38], gap="large")

# with chat_col:
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

user_text = st.chat_input("Nachricht eingeben…")

# # user_text = st.chat_input("Schreibe hier deine Anfrage…")
# if ui_only_rerun and user_text:
#     st.sidebar.write("User text inputed during ui_only_rerun, re-queueing.")
#     time.sleep(3)
#     st.session_state["queued_input"] = user_text
#     st.session_state["skip_agent_on_next_run"] = False
#     st.rerun()

if user_text and not ui_only_rerun:
    st.session_state.messages.append({"role": "user", "content": user_text})

    # Route
    router_res = call_router(model, user_text, use_responses_api)
    # for key, value in router_res.items():
    #     st.write(f"Router {key}: {value}")
    uc_key = router_res.get("use_case", "heatmap")
    # st.write(f"🔧 Use Case: {uc_key} ({router_res.get('confidence', 0):.2f})")
    uc = USE_CASE_LOOKUP.get(uc_key, USE_CASE_LOOKUP["heatmap"]) # type: ignore
    params = router_res.get("params", {}) or {}

    # Render assistant message
    assistant_message = f"Führe Use Case **{uc.title}** aus… (Confidence: {router_res.get('confidence', 0):.2f})"
    with st.chat_message("assistant"):
        st.markdown(assistant_message)

    # Run selected use case
    if uc_key == "heatmap":
        code_to_run = heatmap_code
        year = params.get("year", 2024)
        location = params.get("location", "Berlin, Germany")
        params = {"year": year, "location": location}
    elif uc_key == "air pollution":
        code_to_run = no2_code
        year = params.get("year", 2024)
        location = params.get("location", "Berlin, Germany")
        params = {"year": year, "location": location}
    else:
        # st.write(f"Use Case '{uc_key}' nicht implementiert, führe Heatmap aus.")
        code_to_run = heatmap_code
        year = params.get("year", 2024)
        location = params.get("location", "Berlin, Germany")
        params = {"year": year, "location": location}
    # st.write(f"Parameters: {params}")
    handoff_done = False
    # time.sleep(1)
    st.session_state.last_code = code_to_run
    # run_mini_app(code_to_run)
    # handoff_done = True

    # Save turn
    st.session_state.messages.append({"role": "assistant", "content": assistant_message})
    # st.session_state.turns.append({
    #     "user": user_text,
    #     "router": router_res,
    #     "use_case": uc_key,
    # })
    # st.rerun()

    # if handoff_done:
    st.session_state["skip_agent_on_next_run"] = True
    # st.write("skipping agent on next run.")
    # time.sleep(1)
    st.rerun()

# st.write(f'LAST CODE: {st.session_state.get("last_code")}')
# st.write(f'_runner_autorun_done: {st.session_state.get("_runner_autorun_done")}')
if st.session_state.get("last_code") and not st.session_state.get("_runner_autorun_done"):
    # st.write("Run miniapp with last_code.")
    # time.sleep(1)
    ok = run_mini_app()
    if ok:
        st.session_state._runner_autorun_done = True

# with info_col:
#     st.subheader("🧭 Router & Verlauf")
#     if st.session_state.turns:
#         for i, t in enumerate(reversed(st.session_state.turns), 1):
#             with st.expander(f"Turn {len(st.session_state.turns) - i + 1}: {t['use_case']}"):
#                 st.markdown("**User:**\n\n" + t["user"])
#                 st.markdown("**Use Case:** `" + t["use_case"] + "`")
#                 st.json(t["router"]) if show_debug else st.caption("(Debug ausgeblendet)")
#     else:
#         st.info("Noch keine Turns – schreibe unten deine erste Anfrage.")