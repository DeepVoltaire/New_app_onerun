from __future__ import annotations

import os
import re
import json
import uuid
import hashlib
import pathlib
import subprocess
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, Field

import streamlit as st
import asyncio

# ===== Pfade / Repo-Layout ====================================================
BASE_DIR = pathlib.Path(__file__).parent.resolve()
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
PROMPTS_DIR = KNOWLEDGE_DIR / "prompts"
META_INDEX_PATH = KNOWLEDGE_DIR / "meta" / "layer1_index.yml"
POLICY_PATH = KNOWLEDGE_DIR / "policy.json"
USECASES_DIR = KNOWLEDGE_DIR / "usecases"

RUNNER_DIR = BASE_DIR / "runner"
SANDBOX_DIR = RUNNER_DIR / "sandbox"
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

# ===== Agents SDK import ======================================================
AGENTS_OK = True
try:
    from agents import Agent, Runner, function_tool, SQLiteSession, AgentOutputSchema
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI
    openai_client = AsyncOpenAI() if AGENTS_OK else None
except Exception as e:
    AGENTS_OK = False
    AGENTS_IMPORT_ERROR = str(e)
    openai_client = None

# ===== Prompt laden ===========================================================
def load_text_file(path: pathlib.Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return fallback

MEGA_PROMPT = load_text_file(
    PROMPTS_DIR / "mega_prompt.md",
    fallback=(
        "SYSTEM: Du bist ein einzelner Gesprächs-Agent, der Mini-Apps baut (GEE-first, UI optional). "
        "Arbeite L1.1→L1.2→L2→L3, nutze die Tools tool_get_meta, tool_get_policy, tool_get_uc_sections, "
        "tool_bundle_components, tool_run_python. AOI als strukturierte Spec, kein Textparser. "
        "Erzeuge vor Code eine PLAN_SPEC (JSON) und bundle dann exakt die benötigten Komponenten."
    )
)

# ===== Util ===================================================================
def _sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)

def ensure_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# ===== Earth Engine (Host-Init) ===============================================
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
        pass
    try:
        sa = st.secrets.get("EE_SERVICE_ACCOUNT")
        key = st.secrets.get("EE_PRIVATE_KEY")
        proj = st.secrets.get("EE_PROJECT")
        if not (sa and key and proj):
            return False

        import json as _json
        key_json_str: Optional[str] = None
        if isinstance(key, dict):
            key_json_str = _json.dumps(key)
        elif isinstance(key, str) and key.strip():
            key_json_str = key.strip()

        if key_json_str and key_json_str.startswith("{"):
            from google.oauth2 import service_account as _sa_mod
            scopes = [
                "https://www.googleapis.com/auth/earthengine",
                "https://www.googleapis.com/auth/devstorage.read_only",
            ]
            creds = _sa_mod.Credentials.from_service_account_info(_json.loads(key_json_str), scopes=scopes)
            ee.Initialize(credentials=creds, project=proj)
        else:
            import tempfile as _tf
            with _tf.NamedTemporaryFile("w", delete=False, suffix=".json") as fp:
                if key_json_str:
                    fp.write(key_json_str)
                key_path = fp.name
            creds = ee.ServiceAccountCredentials(sa, key_path)
            ee.Initialize(credentials=creds, project=proj)

        ee.Number(1).getInfo()
        return True
    except Exception:
        return False

_EE_READY = ee_maybe_init()

# ===== Minimal-Sanitizer (wie in der großen app.py) ===========================
_COMPONENT_BEGIN_RE = re.compile(r"^\s*#\s*====\s*BEGIN\s+COMPONENT:", re.MULTILINE)
_REGION_RE = re.compile(r"^\s*#\s*region\s+BLOCK\s+id\s*=", re.MULTILINE)
_FROM_BLOCKS_IMPORT_RE = re.compile(
    r"^\s*from\s+blocks\.components\.[^\n]+\s+import\s+[^\n]+$",
    re.MULTILINE
)

_SANITIZE_SIMPLE_RULES: Tuple[Tuple[str, str], ...] = (
    # falscher main-Guard (alle Quotes erlauben)
    (r"\bif\s+name\s*==\s*[\"']main[\"']\s*:", 'if __name__ == "__main__":'),
)

def _normalize_region_markers(text: str) -> str:
    text = re.sub(r"(?m)^(?P<pre>\s*)(region\s+BLOCK\s+id=)", r"\g<pre># \2", text)
    text = re.sub(r"(?m)^(?P<pre>\s*)(endregion\s+BLOCK\s+id=)", r"\g<pre># \2", text)
    return text

def _strip_page_config_lines(text: str) -> str:
    # Entfernt jede Zeile mit st.set_page_config(...). (Einzeiler genügt hier)
    return re.sub(r"(?m)^\s*st\.set_page_config\([^\n]*\)\s*$", "# [sanitized] st.set_page_config(...) removed", text)

def _ensure_future_annotations_first(text: str) -> str:
    """
    Entfernt ALLE Varianten von (from future import annotations | from __future__ import annotations)
    und setzt genau eine korrekte Zeile GANZ nach oben.
    """
    # kompletten Future-Import in der Datei entfernen
    text_wo = re.sub(
        r"(?m)^\s*from\s+(?:__)?future\s+import\s+annotations\s*$",
        "",
        text,
    ).lstrip("\n")
    return "from __future__ import annotations\n\n" + text_wo

def sanitize_code(code_text: Any) -> str:
    """Sanitizer: Marker, main-Guard, Future-Import, doppeltes page_config; optional Import-Bereinigung."""
    s = _extract_source(code_text)

    # 1) Marker normalisieren
    s = _normalize_region_markers(s)

    # 2) Regeln anwenden (main-Guard etc.)
    for pat, repl in _SANITIZE_SIMPLE_RULES:
        try:
            s = re.sub(pat, repl, s)
        except re.error:
            pass

    # 3) Future-Import ganz nach oben
    s = _ensure_future_annotations_first(s)

    # 4) Doppeltes set_page_config entfernen (Host setzt es bereits)
    s = _strip_page_config_lines(s)

    # 5) Redundante Komponenten-Imports entfernen, falls Bundle-Blöcke vorhanden
    if _COMPONENT_BEGIN_RE.search(s):
        s = _FROM_BLOCKS_IMPORT_RE.sub("", s)

    return s

def _extract_source(maybe_code: Any) -> str:
    if isinstance(maybe_code, str):
        return maybe_code
    if isinstance(maybe_code, (bytes, bytearray)):
        try:
            return maybe_code.decode("utf-8")
        except Exception:
            return maybe_code.decode("latin-1", errors="ignore")
    if isinstance(maybe_code, (list, tuple)):
        for item in maybe_code:
            if isinstance(item, str) and item.strip():
                return item
        if maybe_code:
            return str(maybe_code[0])
    if isinstance(maybe_code, dict):
        for key in ("code", "new_code", "source", "text", "content"):
            val = maybe_code.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return json.dumps(maybe_code, ensure_ascii=False)
    return str(maybe_code)

# ===== Tools ==================================================================
@function_tool
def tool_get_meta() -> str:
    if not META_INDEX_PATH.exists():
        return _safe_json({"error": f"meta index not found: {META_INDEX_PATH}"})
    return META_INDEX_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_policy() -> str:
    if not POLICY_PATH.exists():
        return _safe_json({"error": f"policy not found: {POLICY_PATH}"})
    return POLICY_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_uc_sections(uc_id: str, sections: List[str]) -> str:
    try:
        import yaml
    except Exception:
        return _safe_json({
            "error": "missing_dependency",
            "detail": "PyYAML ist erforderlich. Füge 'pyyaml' zu requirements.txt hinzu."
        })
    uc_path = USECASES_DIR / f"{uc_id}.yml"
    if not uc_path.exists():
        return _safe_json({"error": f"unknown UC '{uc_id}'"})
    try:
        data = yaml.safe_load(uc_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return _safe_json({"error": f"yaml parse error: {e}"})
    out: Dict[str, Any] = {}
    for sec in sections or []:
        if sec in data:
            out[sec] = data[sec]
    return _safe_json(out)

@function_tool
def tool_bundle_components(components: List[str]) -> str:
    bundle_parts: List[str] = []
    manifest: List[Dict[str, Any]] = []

    LEGACY_DIR = BASE_DIR / "blocks" / "components" / "legacy"
    ALLOW_PREFIXES = [
        "blocks/components/gee/",
        "blocks/components/visual/",
        "blocks/components/ui/",
        "blocks/components/util/",
    ]

    for rel in components or []:
        p = (BASE_DIR / rel).resolve()
        if not str(p).startswith(str(BASE_DIR)):
            return _safe_json({"error": f"component outside repo scope: {rel}"})
        rel_norm = str(pathlib.Path(rel).as_posix())
        if not any(rel_norm.startswith(pref) for pref in ALLOW_PREFIXES):
            return _safe_json({"error": f"component prefix not allowed: {rel}"})
        if LEGACY_DIR in p.parents or p.name.startswith("fs_"):
            return _safe_json({"error": f"legacy component not allowed: {rel}"})
        if not p.exists():
            return _safe_json({"error": f"component not found: {rel}"})

        txt = p.read_text(encoding="utf-8")
        h = _sha1_text(txt)
        header = f"\n# ==== BEGIN COMPONENT: {rel} (sha1:{h}) ====\n"
        footer = f"\n# ==== END COMPONENT: {rel} ====\n"
        bundle_parts.append(header + txt + footer)
        manifest.append({"id": rel, "sha1": h, "bytes": len(txt.encode("utf-8"))})

    return _safe_json({"bundle": "\n".join(bundle_parts), "manifest": manifest})

def _tool_run_python_impl(code: str,
                          filename: Optional[str] = None,
                          timeout_sec: int = 600,
                          mode: str = "script",
                          port: int = 8502,
                          preflight_only: bool = False) -> str:
    if not filename:
        filename = "app_run.py"

    ee_prelude = (
        "try:\n"
        "    import os, ee, json, tempfile\n"
        "    _sa=os.environ.get('EE_SERVICE_ACCOUNT')\n"
        "    _key=os.environ.get('EE_PRIVATE_KEY')\n"
        "    _proj=os.environ.get('EE_PROJECT')\n"
        "    if _sa and _key and _proj:\n"
        "        try:\n"
        "            from google.oauth2 import service_account as _sa_mod\n"
        "            scopes=[\n"
        "                'https://www.googleapis.com/auth/earthengine',\n"
        "                'https://www.googleapis.com/auth/devstorage.read_only',\n"
        "            ]\n"
        "            _key_str=_key.strip() if isinstance(_key, str) else _key\n"
        "            if isinstance(_key_str, str) and _key_str.startswith('{'):\n"
        "                creds=_sa_mod.Credentials.from_service_account_info(json.loads(_key_str), scopes=scopes)\n"
        "                ee.Initialize(credentials=creds, project=_proj)\n"
        "            else:\n"
        "                with tempfile.NamedTemporaryFile('w', delete=False, suffix='.json') as fp:\n"
        "                    fp.write(_key_str if isinstance(_key_str, str) else str(_key_str))\n"
        "                    key_path=fp.name\n"
        "                creds=ee.ServiceAccountCredentials(_sa, key_path)\n"
        "                ee.Initialize(credentials=creds, project=_proj)\n"
        "        except Exception:\n"
        "            try:\n"
        "                with tempfile.NamedTemporaryFile('w', delete=False, suffix='.json') as fp:\n"
        "                    fp.write(_key if isinstance(_key, str) else str(_key))\n"
        "                    key_path=fp.name\n"
        "                creds=ee.ServiceAccountCredentials(_sa, key_path)\n"
        "                ee.Initialize(credentials=creds, project=_proj)\n"
        "            except Exception:\n"
        "                ee.Initialize()\n"
        "    else:\n"
        "        ee.Initialize()\n"
        "except Exception:\n"
        "    pass\n\n"
    )

    def _merge_with_future_first(user_code: str) -> str:
        try:
            lines = (user_code or '').splitlines()
            if lines and lines[0].strip() == "from __future__ import annotations":
                head = lines[0] + "\n" + ee_prelude + "\n" + "\n".join(lines[1:])
                return head
            return ee_prelude + (user_code or "")
        except Exception:
            return ee_prelude + (user_code or "")

    code_to_write = (code or "")
    if not preflight_only:
        code_to_write = _merge_with_future_first(code_to_write)

    target = SANDBOX_DIR / filename
    target.write_text(code_to_write, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{str(BASE_DIR)}" + (":" + env["PYTHONPATH"] if "PYTHONPATH" in env and env["PYTHONPATH"] else "")
    try:
        sa = st.secrets.get("EE_SERVICE_ACCOUNT")
        key = st.secrets.get("EE_PRIVATE_KEY")
        proj = st.secrets.get("EE_PROJECT")
        if sa and key and proj:
            env["EE_SERVICE_ACCOUNT"] = str(sa)
            env["EE_PRIVATE_KEY"] = key if isinstance(key, str) else json.dumps(key)
            env["EE_PROJECT"] = str(proj)
    except Exception:
        pass

    if preflight_only:
        try:
            proc = subprocess.run(
                ["python", "-m", "py_compile", str(target)],
                cwd=SANDBOX_DIR,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=env,
            )
            return json.dumps({
                "ok": proc.returncode == 0,
                "stdout": proc.stdout[-15000:],
                "stderr": proc.stderr[-15000:],
                "path": str(target),
                "mode": "py_compile"
            }, ensure_ascii=False)
        except subprocess.TimeoutExpired as e:
            return json.dumps({
                "ok": False,
                "stdout": (getattr(e, "stdout", "") or "")[-15000:],
                "stderr": f"TIMEOUT after {timeout_sec}s (py_compile)",
                "path": str(target),
                "mode": "py_compile"
            }, ensure_ascii=False)

    if mode == "script":
        try:
            proc = subprocess.run(
                ["python", str(target)],
                cwd=SANDBOX_DIR,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            return json.dumps({
                "ok": False, "stdout": (getattr(e, "stdout", "") or "")[-15000:], "stderr": f"TIMEOUT after {timeout_sec}s",
                "path": str(target), "mode": "script"
            }, ensure_ascii=False)
        return json.dumps({
            "ok": proc.returncode == 0,
            "stdout": proc.stdout[-15000:], "stderr": proc.stderr[-15000:],
            "path": str(target), "mode": "script"
        }, ensure_ascii=False)

    if mode == "streamlit":
        try:
            proc = subprocess.Popen(
                ["streamlit", "run", str(target), "--server.headless", "true", "--server.port", str(port)],
                cwd=SANDBOX_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            try:
                bootstrap = proc.stdout.readline().strip() if proc.stdout else ""
            except Exception:
                bootstrap = ""
            url = f"http://localhost:{port}"
            return json.dumps({
                "ok": True, "url": url, "pid": proc.pid, "path": str(target),
                "hint": "Zweite Streamlit-Instanz ist auf Cloud-Hosts i. d. R. nicht erreichbar.",
                "mode": "streamlit", "bootstrap_log": bootstrap[-2000:]
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "ok": False, "error": f"Failed to start streamlit: {e}", "path": str(target), "mode": "streamlit"
            }, ensure_ascii=False)

    return json.dumps({"error": f"unknown mode '{mode}'"})

tool_run_python = function_tool(_tool_run_python_impl)

# ---------- Addendum: Haupt-Agent (4-Felder-Protokoll) ----------
APP_AGENT_ADDENDUM = """
[OUTPUT PROTOKOLL — HAUPT-AGENT · STRICT]
...
"""

# ---------- Addendum: Refactor-Agent (One-Run Vollcode-Output) ----------
REFACTOR_ADDENDUM = """
[REFACTOR-MODUS · ONE-RUN VOLLCODE-OUTPUT]
...
"""

# ---------- Output-Schemas ----------
class UiResponse(BaseModel):
    user_markdown: str = Field(description="Sichtbarer Text für den Chat. Kein Code-Fence.")
    suggestions: Optional[List[str]] = Field(default=None, description="Bis zu 4 Vorschläge (Buttons).")
    json: Optional[Dict[str, Any]] = Field(default=None, description="Interner JSON-Block (z. B. plan_spec, components_manifest). Niemals anzeigen.")
    code: Optional[str] = Field(default=None, description="Vollständiger Python-Code (ohne Markdown-Fences).")

try:
    UiResponse.model_rebuild()
except Exception:
    pass

# ---------- Agent-Instanzen ----------
APP_AGENT = None
REFACTOR_AGENT = None

if AGENTS_OK:
    APP_AGENT = Agent(
        name="EO-AppAgent",
        instructions=MEGA_PROMPT + "\n\n" + APP_AGENT_ADDENDUM,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client
        ),
        output_type=AgentOutputSchema(UiResponse, strict_json_schema=False),
    )

    REFACTOR_AGENT = Agent(
        name="EO-Refactor",
        instructions=MEGA_PROMPT + "\n\n" + REFACTOR_ADDENDUM,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client
        ),
        output_type=AgentOutputSchema(UiResponse, strict_json_schema=False),
    )

# ===== Preflight/Handoff (mit Sanitizer) ======================================
def preflight_and_switch(code_text: str) -> bool:
    """
    Sanitize → optionaler Preflight → Handoff in Autorender.
    """
    sanitized = sanitize_code(code_text)
    mode = st.session_state.get("self_heal_mode", "Direkt ausführen")

    if mode == "Nur Preflight":
        try:
            compile(sanitized, "<preflight>", "exec")
        except SyntaxError as e:
            with st.chat_message("assistant"):
                st.markdown(f"Preflight fehlgeschlagen (SyntaxError): {e}")
            return False
        except Exception as e:
            with st.chat_message("assistant"):
                st.markdown(f"Preflight fehlgeschlagen: {e}")
            return False

    try:
        st.session_state.last_code = sanitized
        st.session_state._runner_autorun_done = False
        st.session_state.build_completed = True
        st.session_state.refactor_mode = True
        autorender_now()
        return True
    except Exception as e:
        with st.chat_message("assistant"):
            st.markdown(f"Ausführen fehlgeschlagen: {e!r}")
        return False

# ===== UI =====================================================================
st.set_page_config(page_title="talk2earth — EO Agent", layout="wide")
st.title("talk2earth — EO Agent (Agents SDK + Streamlit)")

with st.sidebar:
    st.subheader("Status")
    st.write("Agents SDK:", "✅ bereit" if AGENTS_OK else f"❌ {AGENTS_IMPORT_ERROR}")
    st.write("OPENAI_API_KEY gesetzt:", "✅" if os.environ.get("OPENAI_API_KEY") else "❌")
    st.write(f"Earth Engine: {'✅' if _EE_READY else '❌'}")
    st.divider()
    st.caption("Antwortformat: Markdown + Suggestions + JSON (intern) + Code (Preflight/Handoff mit Sanitizer).")
    st.divider()

    mode_label = "Ausführungsmodus"
    options = ["Direkt ausführen", "Nur Preflight"]
    default_index = 0
    if "self_heal_mode" not in st.session_state:
        st.session_state["self_heal_mode"] = options[default_index]
    sel = st.selectbox(mode_label, options, index=options.index(st.session_state["self_heal_mode"]))
    st.session_state["self_heal_mode"] = sel
    if sel == "Direkt ausführen":
        st.caption("Code wird (nach Sanitizer) übernommen und gerendert.")
    else:
        st.caption("Nur Syntax-Preflight (kein Ausführen bei Fehlern).")

# Zentraler Render-Platzhalter
if "render_slot" not in st.session_state:
    st.session_state.render_slot = st.empty()

# Session-States
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_code" not in st.session_state:
    st.session_state.last_code = ""
if "skip_agent_on_next_run" not in st.session_state:
    st.session_state.skip_agent_on_next_run = False
if "queued_input" not in st.session_state:
    st.session_state.queued_input = None
if "_runner_autorun_done" not in st.session_state:
    st.session_state._runner_autorun_done = False
if "last_json" not in st.session_state:
    st.session_state.last_json = None
if "refactor_mode" not in st.session_state:
    st.session_state.refactor_mode = False
if "build_completed" not in st.session_state:
    st.session_state.build_completed = False
if "current_suggestions" not in st.session_state:
    st.session_state.current_suggestions = []
if "agent_session_id" not in st.session_state:
    st.session_state.agent_session_id = uuid.uuid4().hex
if "sdk_session" not in st.session_state and AGENTS_OK:
    try:
        SESSIONS_DB = str((RUNNER_DIR / "sessions.db").resolve())
        st.session_state.sdk_session = SQLiteSession(st.session_state.agent_session_id, SESSIONS_DB)
    except Exception:
        st.session_state.sdk_session = SQLiteSession(st.session_state.agent_session_id)  # in-memory fallback
sdk_session = st.session_state.get("sdk_session")

# Pro-Run Marker
st.session_state["_run_counter"] = st.session_state.get("_run_counter", 0) + 1
st.session_state["_ephem_rendered_run_id"] = st.session_state.get("_ephem_rendered_run_id", -1)

# ---- Rendering Helper --------------------------------------------------------
def autorender_now() -> None:
    """Führt den aktuellen last_code *im render_slot* aus (ersetzt alte Anzeige)."""
    slot = st.session_state.render_slot
    slot.empty()
    with slot.container():
        try:
            import ee  # falls oben im Scope
        except Exception:
            ee = None  # noqa: F401
        ns: Dict[str, object] = {"__name__": "__generated__", "st": st}
        if ee is not None:
            ns["ee"] = ee
        compiled = compile(st.session_state.last_code, "<autorender-now>", "exec")
        exec(compiled, ns, ns)

        entry = None
        for fn_name in ("t2e_app", "render", "main"):
            fn = ns.get(fn_name)
            if callable(fn):
                entry = fn
                break
        if entry is None:
            raise RuntimeError("Autorender-now: Kein Entry-Point (t2e_app/render/main) gefunden.")
        try:
            entry()
        except TypeError:
            entry(st)

    st.session_state._runner_autorun_done = True

# ---- Ephemere Vorschläge -----------------------------------------------------
def render_ephemeral_suggestions() -> None:
    sugg = st.session_state.get("current_suggestions") or []
    if not isinstance(sugg, list) or not sugg:
        return
    if st.session_state.get("_ephem_rendered_run_id") == st.session_state["_run_counter"]:
        return
    key_prefix = f"ep_sugg_{st.session_state['_run_counter']}_"
    st.subheader("Vorschläge")
    cols = st.columns(2)
    for i, label in enumerate(sugg[:4]):
        with cols[i % 2]:
            if st.button(label, key=f"{key_prefix}{i}", use_container_width=True):
                st.session_state["queued_input"] = label
                st.session_state["current_suggestions"] = []
                st.session_state["skip_agent_on_next_run"] = False
                st.rerun()
    st.session_state["_ephem_rendered_run_id"] = st.session_state["_run_counter"]

# Verlauf anzeigen
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

render_ephemeral_suggestions()

# Eingabe
queued = st.session_state.get("queued_input")
if queued:
    st.session_state["queued_input"] = None
    prompt = queued
else:
    prompt = st.chat_input("Nachricht eingeben…")

# UI-only Rerun Handling
ui_only_rerun = False
if st.session_state.get("skip_agent_on_next_run"):
    st.session_state["skip_agent_on_next_run"] = False
    ui_only_rerun = True

if ui_only_rerun and prompt:
    st.session_state["queued_input"] = prompt
    st.session_state["skip_agent_on_next_run"] = False
    st.rerun()

if prompt and not ui_only_rerun:
    st.session_state["current_suggestions"] = []
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    if not AGENTS_OK:
        st.error("Agents SDK nicht verfügbar.")
        st.stop()

    ensure_event_loop()

    if not st.session_state.get("build_completed", False):
        if APP_AGENT is None:
            st.error("Haupt-Agent nicht initialisiert.")
            st.stop()

        res = Runner.run_sync(
            APP_AGENT,
            input=prompt,
            session=sdk_session,
            max_turns=60,
        )
        out = getattr(res, "final_output", None)

        user_md = ""
        suggestions: Optional[List[str]] = None
        json_obj: Optional[Dict[str, Any]] = None
        code_text: Optional[str] = None

        if out and not isinstance(out, str):
            user_md = getattr(out, "user_markdown", "") or ""
            suggestions = getattr(out, "suggestions", None)
            json_obj = getattr(out, "json", None)
            code_text = getattr(out, "code", None)

        if isinstance(user_md, str) and user_md.strip():
            with st.chat_message("assistant"):
                st.markdown(user_md)
            st.session_state.messages.append({"role": "assistant", "content": user_md})

        if isinstance(suggestions, list) and suggestions:
            st.session_state["current_suggestions"] = [s for s in suggestions if isinstance(s, str) and s.strip()][:4]
            render_ephemeral_suggestions()

        if isinstance(json_obj, dict):
            st.session_state["last_json"] = json_obj

        handoff_done = False
        attempted_build = False
        ok = False

        if isinstance(code_text, str) and code_text.strip():
            attempted_build = True
            ok = preflight_and_switch(code_text)

        if attempted_build:
            if ok:
                handoff_done = True
            else:
                with st.chat_message("assistant"):
                    st.markdown("Der Code konnte nicht ausgeführt werden. Ich habe problematische Stellen bereits bereinigt – passe ggf. die Parameter an und versuche es erneut.")

        if handoff_done:
            st.session_state["skip_agent_on_next_run"] = True
            st.rerun()

    else:
        if REFACTOR_AGENT is None:
            st.error("Refactor-Agent nicht initialisiert.")
            st.stop()

        ctx_json = st.session_state.get("last_json") or {}
        ctx_payload = {
            "user_change_request": prompt,
            "source_of_truth_code": st.session_state.last_code,
            "plan_spec": (ctx_json.get("plan_spec") or ctx_json.get("planspec") or ctx_json.get("json")),
            "components_manifest": ctx_json.get("components_manifest"),
        }

        ref_res = Runner.run_sync(
            REFACTOR_AGENT,
            input=json.dumps(ctx_payload, ensure_ascii=False),
            session=sdk_session,
            max_turns=60,
        )
        out = getattr(ref_res, "final_output", None)

        user_md = ""
        suggestions: Optional[List[str]] = None
        json_obj: Optional[Dict[str, Any]] = None
        code_text: Optional[str] = None

        if out and not isinstance(out, str):
            user_md = getattr(out, "user_markdown", "") or ""
            suggestions = getattr(out, "suggestions", None)
            json_obj = getattr(out, "json", None)
            code_text = getattr(out, "code", None)

        if isinstance(user_md, str) and user_md.strip():
            with st.chat_message("assistant"):
                st.markdown(user_md)
            st.session_state.messages.append({"role": "assistant", "content": user_md})

        if isinstance(suggestions, list) and suggestions:
            st.session_state["current_suggestions"] = [s for s in suggestions if isinstance(s, str) and s.strip()][:4]
            render_ephemeral_suggestions()

        if isinstance(json_obj, dict):
            st.session_state["last_json"] = json_obj

        handoff_done = False
        attempted_build = False
        if isinstance(code_text, str) and code_text.strip():
            attempted_build = True
            ok = preflight_and_switch(code_text)
            if ok:
                handoff_done = True
            else:
                with st.chat_message("assistant"):
                    st.markdown("Die Änderung führte zu Fehlern. Die typischen Stolpersteine (Main-Guard, Future-Import, set_page_config) wurden bereits bereinigt – bitte erneut auslösen.")

        if handoff_done:
            st.session_state["skip_agent_on_next_run"] = True
            st.rerun()

# ===== Auto-Re-Render nach Re-Run =============================================
if st.session_state.get("last_code") and not st.session_state.get("_runner_autorun_done"):
    try:
        slot = st.session_state.render_slot
        slot.empty()
        with slot.container():
            ns: Dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
            compiled = compile(st.session_state.last_code, "<autorender>", "exec")
            exec(compiled, ns, ns)

            entry = None
            for fn_name in ("t2e_app", "render", "main"):
                fn = ns.get(fn_name)
                if callable(fn):
                    entry = fn
                    break
            if entry is None:
                raise RuntimeError("Auto-Render: Kein Entry-Point gefunden (t2e_app/render/main).")
            try:
                entry()
            except TypeError:
                entry(st)

        st.session_state._runner_autorun_done = True
        st.sidebar.caption("Runner automatisch gestartet.")
    except BaseException:
        st.sidebar.warning("Auto-Render fehlgeschlagen – letzter Code konnte nicht ausgeführt werden.")
