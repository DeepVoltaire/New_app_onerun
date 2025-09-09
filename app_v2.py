import os
import json
os.environ["OPENAI_API_KEY"] = json.load(open("openai_key.json"))["OPENAI_API_KEY"] 
import streamlit as st
import ee
import time
import traceback
from datetime import datetime
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

os.makedirs("changed_codes", exist_ok=True)
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
    if st.button("🧹 Verlauf leeren"):
        st.session_state.messages = []
        st.session_state.turns = []
        st.session_state.render_slot = st.empty()
        # remove last_code from st.session_state to reset the app
        if "last_code" in st.session_state:
            del st.session_state["last_code"]
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
            return False
            # try:
            #     entry(st)
            # except:
            #     st.write(f"entry(st) failed")
            #     st.write(traceback.format_exc())  # prints full traceback to stdout
    return True
    

USE_CASES = [
    UseCase(
        key="heatmap",
        title="Heatmap using Landsat",
        description="Creates a heatmap of a typical summer with Landsat.",
        param_schema={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "Where to focus the map"}, "year": {"type": "integer", "description": "Year"}},
            "additionalProperties": True,
        },
    ),
    UseCase(
        key="air_pollution",
        title="Air pollution using Sentinel 5P",
        description="Uses Sentinel5P to show NO2 and other pollutants.",
        param_schema={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "Where to focus the map"}, "year": {"type": "integer", "description": "Year"}},
            "additionalProperties": True,
        },
    ),
    # NEW: let the model decide to route here when the user asks to "change", "modify", "adjust", etc.
    UseCase(
        key="modify_app",
        title="Modify App",
        description="Takes the last rendered code and applies your natural-language changes.",
        param_schema={
            "type": "object",
            "properties": {
                "instructions": {"type": "string", "description": "What to change in the current mini-app"}
            },
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
                    "description": "Selected Use Case",
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
    "You are a router that assigns user input to a specific use case or to change the current app. "
    "Return only a JSON object that matches the provided JSON schema. "
    "Use params when appropriate by extracting them from the user input (e.g., location). "
    "If the user asks to change/modify/tweak the current app, e.g. change the location, colormap, etc. route to 'modify_app'."
)


def call_router(model: str, message: str, responses_api: bool):
    """Ask GPT to route to a use case."""
    # Build a compact description of available cases for the model
    uc_descriptions = "\n".join([f"- {uc.key}: {uc.title} – {uc.description}" for uc in USE_CASES])

    router_schema = build_router_schema()

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
    
# ----------------------------- LLM CODE EDITOR ------------------------------

CODER_SYSTEM_PROMPT = """You are an expert Python+Streamlit code editor.
You're given the CURRENT mini-app code and a user request describing desired changes.
Return ONLY a JSON object with the new, complete Python source (no markdown).
Constraints:
- Provide a fully runnable file that defines a function main() as the entry point.
- Use 'import streamlit as st' within the file (assume st is NOT injected).
- Import any other libraries you use (e.g., ee, geemap), but avoid re-initializing Earth Engine if not needed.
- Do NOT call st.set_page_config (the host app already sets it).
- Keep the app self-contained; avoid file I/O, network calls, or secrets beyond what the current code already uses.
- Prefer minimal changes to satisfy the request; preserve working behavior.
"""

def call_coder(model: str, base_code: str, user_request: str, responses_api: bool):
    coder_schema = {
        "name": "CodeEdit",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Complete Python source for the modified Streamlit mini-app. Must define main()."
                },
                "notes": {
                    "type": "string",
                    "description": "Short summary of what changed (bullets)."
                }
            },
            "required": ["code", "notes"]
        },
        "strict": True
    }

    # Build the single user message the model will see
    user_payload = (
        "### CURRENT CODE\n"
        f"{base_code}\n\n"
        "### CHANGE REQUEST\n"
        f"{user_request}\n"
    )

    client = st.session_state.client or OpenAI()  # reuse existing client if set
    raw = None

    if responses_api:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": CODER_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            response_format={"type": "json_schema", "json_schema": coder_schema},
            reasoning={"effort": "medium"},
            temperature=0.2,
        )
        raw = getattr(resp, "output_text", str(resp))
    else:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CODER_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            response_format={"type": "json_schema", "json_schema": coder_schema},
            temperature=0.2,
        )
        raw = resp.choices[0].message.content

    # Be robust if the model returns non-JSON accidentally
    try:
        payload = json.loads(raw)
        code = payload.get("code", "")
        notes = payload.get("notes", "")
    except Exception:
        # crude fallback: strip code fences if any
        text = raw or ""
        if "```" in text:
            code = text.split("```")[1]
            # handle language fences like ```python
            code = code.split("\n", 1)[1] if code.startswith("python") else code
        else:
            code = text
        notes = ""

    return code, notes


st.session_state._runner_autorun_done = False
# --- UI-only Rerun Handling (einmalige Entkopplung des Autorender-Reruns) ---
ui_only_rerun = False
if st.session_state.get("skip_agent_on_next_run"):
    st.session_state["skip_agent_on_next_run"] = False
    ui_only_rerun = True

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

user_text = st.chat_input("Nachricht eingeben…")

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
    assistant_message = f"Creating Use Case **{uc.title}** aus… (Confidence: {router_res.get('confidence', 0):.2f}, Reason: {router_res.get('reason', '')})"
    with st.chat_message("assistant"):
        st.markdown(assistant_message)

    if uc_key == "modify_app":
        # Use the very last code as base
        base_code = st.session_state.get("last_code")

        # The user's full message is the best source of instructions
        instructions = user_text
        new_code, notes = call_coder(model, base_code, instructions, use_responses_api)

        # Keep a safe fallback if the model failed to return code
        if not new_code or "def main" not in new_code:
            st.warning("Changes did not work, fallback to earlier code.")
            time.sleep(2)
            new_code = base_code

        st.session_state.last_code = new_code   # hand off to the autorender

        changed_code = assistant_message + f"\n\nÄnderungen:\n{notes}\n\n" + new_code + "\n\n#################OLDCODE:\n\n" + base_code
        time_string = datetime.now().strftime("%H:%M:%S")
        with open(f"changed_codes/{model}_{time_string}.py", "w", encoding="utf-8") as fp:
            fp.write(changed_code)
        st.session_state.messages.append({"role": "assistant", "content": assistant_message + (f"\n\nÄnderungen:\n{notes}")})
        st.session_state["skip_agent_on_next_run"] = True
        st.rerun()

    # Run selected use case
    if uc_key == "heatmap":
        code_to_run = heatmap_code
    elif uc_key == "air_pollution":
        code_to_run = no2_code
    else:
        raise ValueError(f"Use Case {uc_key} not implemented.")

    st.session_state.last_code = code_to_run

    # Save turn
    st.session_state.messages.append({"role": "assistant", "content": assistant_message})
    # st.session_state.turns.append({
    #     "user": user_text,
    #     "router": router_res,
    #     "use_case": uc_key,
    # })

    st.session_state["skip_agent_on_next_run"] = True
    st.rerun()

# st.write(f'LAST CODE: {st.session_state.get("last_code")}')
# st.write(f'_runner_autorun_done: {st.session_state.get("_runner_autorun_done")}')
if st.session_state.get("last_code") and not st.session_state.get("_runner_autorun_done"):
    # st.write("Run miniapp with last_code.")
    time.sleep(0.5)
    ok = run_mini_app()
    if ok:
        st.session_state._runner_autorun_done = True
    else:
        st.warning("Changes did not work, fallback to earlier code.")
        st.session_state.last_code = base_code
        ok = run_mini_app()
        if ok:
            st.session_state._runner_autorun_done = True
        else:
            st.error("Earlier code failed, we are stuck here.")