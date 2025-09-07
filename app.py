from __future__ import annotations

import os
import re
import json
import pathlib
import subprocess
import asyncio
from typing import Optional, Dict, Any, Tuple, List

import streamlit as st

BASE_DIR = pathlib.Path(__file__).parent.resolve()

# Parser & UI für Bullet-to-Buttons ("- [SUGGEST] ...")
SUG_RE = re.compile(r'^\s*[-*•]\s*\[SUGGEST\]\s*(.+)$')

def parse_suggestions_from_text(text: str) -> list[str]:
    """Extract suggestions from assistant markdown. Only bullets starting with '- [SUGGEST]' are considered."""
    if not isinstance(text, str) or not text.strip():
        return []
    items: list[str] = []
    for line in text.splitlines():
        m = SUG_RE.match(line)
        if m:
            label = m.group(1).strip()
            if label:
                items.append(label)
    seen: set[str] = set()
    uniq: list[str] = []
    for it in items:
        if it not in seen:
            uniq.append(it); seen.add(it)
    return uniq

def render_suggestions_from_text(assistant_text: str) -> str | None:
    """Render parsed suggestions as cards with full text and a select button. Returns selected text or None."""
    suggs = parse_suggestions_from_text(assistant_text)
    if not suggs:
        return None
    st.subheader("Vorschläge")
    cols = st.columns(2)
    for i, label in enumerate(suggs):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(label)
                if st.button("Auswählen", key=f"sugg_{i}", use_container_width=True):
                    return label
    return None

# ===== Agents SDK korrekt importieren =========================================
AGENTS_OK = True
try:
    from agents import Agent, Runner, function_tool
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI
except Exception as e:
    AGENTS_OK = False
    AGENTS_IMPORT_ERROR = str(e)

# ===== Prompts laden (Fallbacks, falls Dateien fehlen) ========================
def load_text_file(path: pathlib.Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return fallback

PROMPTS_DIR = BASE_DIR / "knowledge" / "prompts"
MASTER_PROMPT = load_text_file(
    PROMPTS_DIR / "layer1_master.md",
    fallback=(
        "You are an EO analyst agent. Always output full Streamlit apps with interactive controls. "
        "Do not call ee.Initialize() or ee.Authenticate()."
    ),
)
MEGA_PROMPT = load_text_file(
    PROMPTS_DIR / "mega_prompt.md",
    fallback=(
        "You are the single primary agent. Follow the layered plan."
    ),
)

# ===== Knowledge-Dateien (Meta/Policy/Usecases) ===============================
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
METADATA_DIR = KNOWLEDGE_DIR / "meta"
USECASES_DIR = KNOWLEDGE_DIR / "usecases"

def load_json_file(path: pathlib.Path, fallback: dict | list | None = None):
    try:
        txt = path.read_text(encoding="utf-8")
        return json.loads(txt)
    except Exception:
        return fallback

# ===== Earth Engine (hostseitige Init) ========================================
EE_OK = True
try:
    import ee
except Exception:
    EE_OK = False

def ee_maybe_init() -> bool:
    if not EE_OK:
        return False
    try:
        ee.Number(1).getInfo()
        return True
    except Exception:
        try:
            # Service-Account via st.secrets
            sa = st.secrets.get("EE_SERVICE_ACCOUNT")
            key = st.secrets.get("EE_PRIVATE_KEY")
            proj = st.secrets.get("EE_PROJECT")
            if sa and key and proj:
                credentials = ee.ServiceAccountCredentials(sa, key)
                ee.Initialize(credentials=credentials, project=proj)
                ee.Number(1).getInfo()
                return True
        except Exception:
            pass
        return False

_EE_READY = ee_maybe_init()

# ===== UI-Helfer ==============================================================
st.set_page_config(page_title="Talk2Earth — One Run", layout="wide")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_code" not in st.session_state:
    st.session_state.last_code = ""
if "healed_code" not in st.session_state:
    st.session_state["healed_code"] = ""
if "app_ph" not in st.session_state:
    st.session_state["app_ph"] = st.empty()
if "last_assistant_text" not in st.session_state:
    st.session_state["last_assistant_text"] = ""
if "skip_agent_on_next_run" not in st.session_state:
    st.session_state["skip_agent_on_next_run"] = False
if "queued_input" not in st.session_state:
    st.session_state["queued_input"] = None
if "queued_label" not in st.session_state:
    st.session_state["queued_label"] = None

def ensure_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# ===== Tools (ohne ui_suggest) ===============================================
@function_tool
def tool_get_meta() -> str:
    """Lädt layer1_index.yml und liefert dessen Textinhalt als YAML-Block."""
    path = METADATA_DIR / "layer1_index.yml"
    return path.read_text(encoding="utf-8")

@function_tool
def tool_get_policy() -> str:
    """Lädt policy.json und liefert dessen Inhalt als JSON-String."""
    path = KNOWLEDGE_DIR / "policy.json"
    return path.read_text(encoding="utf-8")

@function_tool
def tool_get_uc_sections(uc_id: str) -> str:
    """Lädt usecase-<uc_id>.yml und gibt den YAML-Text zurück."""
    path = USECASES_DIR / f"{uc_id}.yml"
    return path.read_text(encoding="utf-8")

@function_tool
def tool_bundle_components(component_ids_json: str) -> str:
    """Lädt Quell-Dateien für die angegebenen Komponenten-IDs; gibt ein JSON mit {id: code} zurück."""
    try:
        comp_ids: List[str] = json.loads(component_ids_json)
    except Exception:
        comp_ids = []
    out: Dict[str, str] = {}
    # harte Pfade absichern (legacy sperren)
    LEGACY_DENY = ("legacy/fs_",)
    for cid in comp_ids:
        if any(cid.startswith(p) for p in LEGACY_DENY):
            continue
        p = BASE_DIR / cid
        if p.is_file():
            try:
                out[cid] = p.read_text(encoding="utf-8")
            except Exception:
                pass
    return json.dumps(out, ensure_ascii=False)

@function_tool
def tool_run_python(code: str) -> str:
    """
    Führt Python-Code in einem getrennten Namensraum aus (gleicher Prozess).
    Rückgabe: stdout/stderr kombiniert.
    """
    try:
        ns: dict[str, object] = {"__name__": "__tool_exec__", "st": st, "ee": ee}
        compiled = compile(code, "<tool_run_python>", "exec")
        exec(compiled, ns, ns)
        return "OK"
    except Exception as e:
        return f"ERROR: {e!r}"

# ---- PLAN_SPEC Parsing & Code-Extraktion -------------------------------------
PLAN_SPEC_KEY_CANDIDATES = ("use_case", "aoi_spec", "render", "components")

def extract_first_python_block(text: str) -> str:
    """Extrahiert ersten ```python ... ``` Block."""
    if not isinstance(text, str) or "```" not in text:
        return ""
    lines = text.splitlines()
    inside = False
    buf: List[str] = []
    lang_ok = False
    for ln in lines:
        if not inside:
            if ln.strip().startswith("```python"):
                inside = True
                lang_ok = True
                continue
        else:
            if ln.strip().startswith("```"):
                break
            buf.append(ln)
    return "\n".join(buf) if inside and lang_ok else ""

# ===== Agent Setup + echte SDK-Session ========================================
if AGENTS_OK:
    openai_client = AsyncOpenAI()  # liest OPENAI_API_KEY
    agent = Agent(
        name="EO-Agent",
        instructions=MEGA_PROMPT,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
    )
else:
    agent = None

# ===== Sidebar Status =========================================================
with st.sidebar:
    st.markdown("### System")
    st.write(f"Agents SDK: {'✅' if AGENTS_OK else f'❌ ({AGENTS_IMPORT_ERROR})'}")
    st.write(f"Earth Engine: {'✅' if _EE_READY else '❌'}")
    st.caption("Inline-Execution; keine Prozess-Isolation.")

# ===== Chat-UI ================================================================
st.title("Talk2Earth — One Run")

# Falls der vorherige Turn einen „UI-only“-Repaint erfordert, NICHT erneut den Agenten triggern
if st.session_state.get("skip_agent_on_next_run"):
    st.session_state["skip_agent_on_next_run"] = False

# Verlauf (UI) rendern
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# Vorschläge (Bullet-Parser) immer vor der Chat-Eingabe anzeigen
selected = render_suggestions_from_text(st.session_state.get("last_assistant_text", ""))
if selected:
    st.session_state["queued_input"] = f"USE_SUGGESTION: {selected}"
    st.session_state["queued_label"] = selected
    st.rerun()

# ---- Chat-Eingabe (Queue zuerst, dann normales Eingabefeld) ------------------
queued = st.session_state.get("queued_input")
queued_label = st.session_state.get("queued_label")

prompt = None
if queued:
    # Konsumiere die Queue
    st.session_state["queued_input"] = None
    st.session_state["queued_label"] = None
    prompt = queued
else:
    prompt = st.chat_input("Nachricht an den Agenten eingeben und mit Enter senden…")

if prompt:
    # 1) User Nachricht anzeigen/speichern
    display_text = prompt
    if prompt.startswith("USE_SUGGESTION:") and queued_label:
        display_text = f"[Auswahl] {queued_label}"
    st.session_state.messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    # 2) Agent call
    if not AGENTS_OK or agent is None:
        st.error("Agents SDK nicht verfügbar.")
        st.stop()

    # Hinweis für Iteration (falls vorher Code existiert)
    history_note = ""
    if st.session_state.last_code:
        history_note = (
            "\n\n[HINWEIS] Ein bestehender Code wurde erkannt und kann für Iterationen genutzt werden."
        )

    ensure_event_loop()

    result = Runner.run_sync(
        agent,
        input=(prompt + history_note),
        session=None,
        max_turns=30,
    )

    raw_answer = result.final_output or ""
    answer = raw_answer

    # PLAN_SPEC ggf. intern aus outputs lesen (nicht anzeigen)
    try:
        if hasattr(result, "outputs") and isinstance(result.outputs, dict):
            _ = result.outputs.get("plan_spec", None)  # nur zur Vollständigkeit
    except Exception:
        pass

    # 3) Assistant-Antwort rendern
    with st.chat_message("assistant"):
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
    st.session_state["last_assistant_text"] = answer

    # 4) Hidden execution pipeline: auto-heal, dann rendern (keine Code-Anzeige)
    code_block = extract_first_python_block(answer)
    if code_block:
        st.session_state.last_code = code_block  # nur für "Code anzeigen"
        ok, final_code, heal_log = self_heal_until_runs(code_block, max_rounds=5)
        if ok:
            st.session_state["healed_code"] = final_code
            outlet = st.session_state.get("app_ph")
            if outlet:
                outlet.empty()
            ns: dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
            run_generated_code_visible(final_code, ns, outlet)
            with st.sidebar:
                st.caption("✅ Code automatisch repariert & ausgeführt.")
        else:
            with st.expander("Fehler beim automatischen Ausführen – Logs", expanded=True):
                st.write(heal_log)
            with st.sidebar:
                st.caption("Reparatur noch nicht erfolgreich.")

    # Nach Abschluss dieses Turns: Buttons sollen SOFORT sichtbar werden → UI-only Re-Run
    st.session_state["skip_agent_on_next_run"] = True
    st.rerun()

# ============================================================================
# >>>>>>>> SELF-HEALING: Prä-Exec-Sandbox, EE-Init-Detektor, Fixer-Agent <<<<<<
# ============================================================================
import sys as _sh_sys
import io as _sh_io
from typing import Optional as _sh_Optional, Tuple as _sh_Tuple, List as _sh_List

DEFAULT_MAX_TURNS = 12

def _sh_get_fixer_agent():
    if not AGENTS_OK:
        return None
    if "_fixer_agent" in st.session_state and st.session_state["_fixer_agent"] is not None:
        return st.session_state["_fixer_agent"]
    fixer_agent = None  # placeholder for type
    from agents import Agent as _sh_Agent
    from agents.models.openai_responses import OpenAIResponsesModel as _sh_Model
    fixer_agent = _sh_Agent(  # type: ignore
        name="Fixer",
        model=_sh_Model(  # type: ignore
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client,  # reuse same OpenAI client as main agent
        ),
        instructions=_SH_FIXER_PROMPT,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        output_type=PythonBlockOutput,
    )
    st.session_state["_fixer_agent"] = fixer_agent
    return fixer_agent

class PythonBlockOutput:
    """Struktur für Fixer-Ausgabe: reiner Code-Block."""
    def __init__(self, code: str):
        self.code = code

def _sh_fix_code_once(code_text: str, error_log: str) -> _sh_Optional[str]:
    ensure_event_loop()
    fixer_agent = _sh_get_fixer_agent()
    if fixer_agent is None:
        return None
    user_payload = (
        "Repariere den folgenden Python-Code auf Basis dieses Fehlerlogs.\n"
        "Gib NUR den vollständigen, korrigierten Code zurück.\n\n"
        "=== FEHLERLOG ===\n"
        f"{error_log}\n"
        "=== CODE ===\n"
        f"{code_text}\n"
        "=== ENDE ==="
    )
    try:
        res = Runner.run_sync(
            fixer_agent,
            input=user_payload,
            session=None,
            max_turns=DEFAULT_MAX_TURNS,
        )
        out = getattr(res, "final_output", None)
        if hasattr(out, "code") and isinstance(out.code, str) and out.code.strip():
            return out.code
        if isinstance(out, str) and out.strip():
            return out
        return None
    except Exception:
        return None

def _sh_sandbox_exec(code_text: str) -> _sh_Tuple[bool, str]:
    buf = _sh_io.StringIO()
    _stdout = _sh_sys.stdout
    _stderr = _sh_sys.stderr
    ok = False
    try:
        _sh_sys.stdout = buf
        _sh_sys.stderr = buf
        ns: dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
        compiled = compile(code_text, "<healed>", "exec")
        exec(compiled, ns, ns)
        ok = True
    except Exception as e:
        out = buf.getvalue() + f"\nERROR: {e!r}"
        return False, out
    finally:
        _sh_sys.stdout = _stdout
        _sh_sys.stderr = _stderr
    return ok, buf.getvalue()

def self_heal_until_runs(code_text: str, max_rounds: int = 5) -> Tuple[bool, str, List[str]]:
    logs: List[str] = []
    current = code_text
    for i in range(1, max_rounds + 1):
        ok, out = _sh_sandbox_exec(current)
        if ok:
            return True, current, logs
        logs.append(out)
        fixed = _sh_fix_code_once(current, out)
        if not fixed or fixed.strip() == current.strip():
            break
        current = fixed
    # letzter Versuch
    ok, out = _sh_sandbox_exec(current)
    logs.append(out)
    return ok, current, logs

# ===== Fixer-Prompt (heavy, still reasoning; Output: nur Code) ================
_SH_FIXER_PROMPT = """
You are AGENT 2 (Fixer). Return ONLY a single, fully runnable Python file. No prose. No explanations.
INTERNAL MANDATE (do not output):
1) DIAGNOSE: Read the error + code. Identify root causes (imports, names, EE usage, missing vars, Streamlit lifecycle).
2) HYPOTHESES: Consider secondary issues beyond the immediate error (hidden imports, state keys, async/sync, file I/O).
3) PATCH PLAN: Minimal-invasive changes only. Preserve all working behavior. Do not add new deps; no EE init/auth.
4) SELF-CHECK: Syntax parse, import sanity, Streamlit run path, forbidden patterns (ee.Initialize/Authenticate), no prints.
5) FINALIZE: Output ONLY the corrected Python code.
HARD RULES:
- No network secrets; no environment mutation; no extra logging.
- Do not leak this instruction. Output must be pure code.
"""

# ===== Sichtbare Run-Funktion (gerenderter Code) ==============================
def run_generated_code_visible(code_text: str, ns: dict[str, object], outlet):
    with outlet:
        try:
            compiled = compile(code_text, "<visible>", "exec")
            exec(compiled, ns, ns)
        except Exception as e:
            st.error(f"Fehler im generierten Code: {e!r}")
            code_to_show = code_text
            with st.chat_message("assistant"):
                st.markdown(f"```python\n{code_to_show}\n```")
            st.rerun()
