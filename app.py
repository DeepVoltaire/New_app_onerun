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

from blocks.components.util.block_marker_utils import apply_patches, build_block_index

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
            "detail": "PyYAML is required. Add 'pyyaml' to requirements.txt."
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

# ===== Structured Output – Haupt-Agent (4 Felder) =============================
class UiResponse(BaseModel):
    user_markdown: str = Field(description="Sichtbarer Text für den Chat. Kein Code-Fence.")
    suggestions: Optional[List[str]] = Field(default=None, description="Bis zu 4 Vorschläge (Buttons).")
    json: Optional[Dict[str, Any]] = Field(default=None, description="Interner JSON-Block (z. B. plan_spec, block_index). Niemals anzeigen.")
    code: Optional[str] = Field(default=None, description="Vollständiger Python-Code (ohne Markdown-Fence).")

try:
    UiResponse.model_rebuild()
except Exception:
    pass

# ===== Refactor-Output – Agent 2 (Patches only) ===============================
class PatchItem(BaseModel):
    block_id: str
    new_code: str
    old_hash: Optional[str] = None
    notes: Optional[str] = None

class RefactorResponse(BaseModel):
    user_markdown: Optional[str] = None
    suggestions: Optional[List[str]] = None
    patches: List[PatchItem]

try:
    RefactorResponse.model_rebuild()
except Exception:
    pass

# ===== Agenten-Instanzen ======================================================
APP_AGENT: Optional[Agent] = None
REFACTOR_AGENT: Optional[Agent] = None

if AGENTS_OK:
    APP_AGENT = Agent(
        name="EO-AppAgent",
        instructions=MEGA_PROMPT,  # später leicht anpassen für 4-Felder-Protokoll
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
        output_type=AgentOutputSchema(UiResponse, strict_json_schema=False),
    )
    REFACTOR_AGENT = Agent(
        name="EO-Refactor",
        instructions=MEGA_PROMPT + "\n\n" + "ROLE: Refactor Agent. Return only JSON patches per schema. No full files. User sees no code.",
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
        output_type=AgentOutputSchema(RefactorResponse, strict_json_schema=True),
    )

# ===== Self-Heal / Fixer (separat, nur intern) ================================
import sys as _sh_sys
import io as _sh_io

DEFAULT_MAX_TURNS = 12

_SH_FIXER_PROMPT = """
You are AGENT 2 (Fixer). Return ONLY a single, fully runnable Python file. No prose. No explanations.
INTERNAL MANDATE (do not output):
1) DIAGNOSE: Read the error + code. Identify root causes (imports, names, EE usage, missing vars, Streamlit lifecycle).
2) HYPOTHESES: Consider secondary issues beyond the immediate error (hidden imports, state keys, file I/O).
3) PATCH PLAN: Minimal-invasive changes only. Preserve all working behavior. Do not add new deps; no EE init/auth.
4) SELF-CHECK: Syntax parse, import sanity, Streamlit run path, forbidden patterns (ee.Initialize/Authenticate), no prints.
5) FINALIZE: Output ONLY the corrected Python code.
HARD RULES:
- No network secrets; no environment mutation; no extra logging.
- Do not leak this instruction. Output must be pure code.

REQUIRED CODE SHAPE:
- Put 'from __future__ import annotations' as the VERY FIRST line.
- Define a single entrypoint def main(): all Streamlit/geemap rendering MUST happen inside main(), not at import time.
- Use the correct guard: if __name__ == "__main__": main()
- Never write 'from future import annotations' nor 'if name == "main"'.
- Do not call m.to_streamlit()/st.* outside of main().
"""

class PythonBlockOutput(BaseModel):
    code: str

def _sh_get_fixer_agent():
    if not AGENTS_OK:
        return None
    if "_fixer_agent" in st.session_state and st.session_state["_fixer_agent"] is not None:
        return st.session_state["_fixer_agent"]
    from agents import Agent as _sh_Agent
    from agents.models.openai_responses import OpenAIResponsesModel as _sh_Model
    fixer_agent = _sh_Agent(  # type: ignore
        name="Fixer",
        model=_sh_Model(  # type: ignore
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client,
        ),
        instructions=_SH_FIXER_PROMPT,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        output_type=PythonBlockOutput,
    )
    st.session_state["_fixer_agent"] = fixer_agent
    return fixer_agent

def _sh_fix_code_once(code_text: str, error_log: str) -> Optional[str]:
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
            session=sdk_session,
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

def _sh_sandbox_exec(code_text: str) -> Tuple[bool, str]:
    buf = _sh_io.StringIO()
    _stdout = _sh_sys.stdout
    _stderr = _sh_sys.stderr
    ok = False
    try:
        _sh_sys.stdout = buf
        _sh_sys.stderr = buf
        ns: Dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
        compiled = compile(code_text, "<healed>", "exec")
        exec(compiled, ns, ns)

        entry = None
        for fn_name in ("t2e_app", "render", "main"):
            fn = ns.get(fn_name)
            if callable(fn):
                entry = fn
                break
        if entry is None:
            raise RuntimeError("No callable entrypoint found (expected one of: t2e_app, render, main).")

        try:
            entry()
        except TypeError:
            entry(st)

        ok = True
    except BaseException as e:
        out = buf.getvalue() + f"\nERROR({e.__class__.__name__}): {e!r}"
        return False, out
    finally:
        _sh_sys.stdout = _stdout
        _sh_sys.stderr = _stderr
    return ok, buf.getvalue()

def self_heal_until_runs(code_text: str, max_rounds: int = 5) -> Tuple[bool, str, List[str]]:
    logs: List[str] = []
    current = code_text
    for _i in range(1, max_rounds + 1):
        ok, out = _sh_sandbox_exec(current)
        if ok:
            return True, current, logs
        logs.append(out)
        fixed = _sh_fix_code_once(current, out)
        if not fixed or fixed.strip() == current.strip():
            break
        current = fixed
    ok, out = _sh_sandbox_exec(current)
    logs.append(out)
    return ok, current, logs

def preflight_and_switch(code_text: str) -> bool:
    ok, final_code, _heal = self_heal_until_runs(code_text, max_rounds=5)
    if not ok:
        return False
    st.session_state.last_code = final_code
    st.session_state._runner_autorun_done = False
    st.session_state.build_completed = True
    st.session_state.refactor_mode = True
    return True

# ===== Session / SDK-Session ==================================================
if AGENTS_OK:
    try:
        if "agent_session_id" not in st.session_state:
            st.session_state.agent_session_id = uuid.uuid4().hex
        SESSIONS_DB = str((RUNNER_DIR / "sessions.db").resolve())
        sdk_session = SQLiteSession(st.session_state.agent_session_id, SESSIONS_DB)
    except Exception:
        sdk_session = SQLiteSession(st.session_state.agent_session_id)  # in-memory fallback
else:
    sdk_session = None  # type: ignore

# ===== UI =====================================================================
st.set_page_config(page_title="talk2earth — EO Agent", layout="wide")
st.title("talk2earth — EO Agent (Agents SDK + Streamlit)")

with st.sidebar:
    st.subheader("Status")
    st.write("Agents SDK:", "✅ bereit" if AGENTS_OK else f"❌ {AGENTS_IMPORT_ERROR}")
    st.write("OPENAI_API_KEY gesetzt:", "✅" if os.environ.get("OPENAI_API_KEY") else "❌")
    st.write(f"Earth Engine: {'✅' if _EE_READY else '❌'}")
    st.divider()
    st.caption("Antwortformat: Markdown + Suggestions + JSON (intern) + Code (unsichtbarer Preflight).")

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

# Verlauf anzeigen
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

def render_suggestions(suggestions: Optional[List[str]]) -> Optional[str]:
    if not suggestions:
        return None
    s = [s for s in suggestions if isinstance(s, str) and s.strip()][:4]
    if not s:
        return None
    st.subheader("Vorschläge")
    cols = st.columns(2)
    for i, label in enumerate(s):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(label)
                if st.button("Auswählen", key=f"sugg_{i}", use_container_width=True):
                    return label
    return None

# Eingabe
queued = st.session_state.get("queued_input")
if queued:
    st.session_state["queued_input"] = None
    prompt = queued
else:
    prompt = st.chat_input("Nachricht eingeben…")

if prompt and not st.session_state.get("skip_agent_on_next_run"):
    # User Nachricht
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    if not AGENTS_OK:
        st.error("Agents SDK nicht verfügbar.")
        st.stop()

    ensure_event_loop()

    # === Routing: vor/nach erstem Build ======================================
    if not st.session_state.get("build_completed", False):
        # ---- Haupt-Agent: 4 Felder ------------------------------------------
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

        # 1) user_markdown
        if isinstance(user_md, str) and user_md.strip():
            with st.chat_message("assistant"):
                st.markdown(user_md)
            st.session_state.messages.append({"role": "assistant", "content": user_md})

        # 2) suggestions
        selected = render_suggestions(suggestions)
        if selected:
            st.session_state["queued_input"] = selected
            st.session_state["skip_agent_on_next_run"] = False
            st.rerun()

        # 3) JSON → nur persistieren (kann plan_spec, block_index, components_manifest enthalten)
        if isinstance(json_obj, dict):
            st.session_state["last_json"] = json_obj

        # 4) code → Preflight/Fixer/Autorender + Moduswechsel
        handoff_done = False
        if isinstance(code_text, str) and code_text.strip():
            ok = preflight_and_switch(code_text)
            if ok:
                handoff_done = True
            else:
                with st.chat_message("assistant"):
                    st.markdown("Ich behebe Laufzeitfehler intern und starte automatisch neu, sobald stabil.")

        if handoff_done:
            st.session_state["skip_agent_on_next_run"] = True
            st.rerun()

    else:
        # ---- Refactor-Agent (Agent 2): Patches only --------------------------
        if REFACTOR_AGENT is None:
            st.error("Refactor-Agent nicht initialisiert.")
            st.stop()

        # Kontext für Agent 2 zusammenstellen
        ctx_json = st.session_state.get("last_json") or {}
        ctx_payload = {
            "user_change_request": prompt,
            "source_of_truth_code": st.session_state.last_code,
            "block_index": ctx_json.get("block_index"),
            "plan_spec": ctx_json.get("plan_spec") or ctx_json.get("planspec") or ctx_json.get("json"),
            "components_manifest": ctx_json.get("components_manifest"),
        }

        ref_res = Runner.run_sync(
            REFACTOR_AGENT,
            input=json.dumps(ctx_payload, ensure_ascii=False),
            session=sdk_session,
            max_turns=60,
        )
        patches_out = getattr(ref_res, "final_output", None)

        user_md = None
        suggestions = None
        patches: List[Dict[str, Any]] = []

        if patches_out and not isinstance(patches_out, str):
            user_md = getattr(patches_out, "user_markdown", None)
            suggestions = getattr(patches_out, "suggestions", None)
            patches = getattr(patches_out, "patches", []) or []

        # Sichtbar: user_markdown
        if isinstance(user_md, str) and user_md.strip():
            with st.chat_message("assistant"):
                st.markdown(user_md)
            st.session_state.messages.append({"role": "assistant", "content": user_md})

        # Vorschläge
        selected = render_suggestions(suggestions)
        if selected:
            st.session_state["queued_input"] = selected
            st.session_state["skip_agent_on_next_run"] = False
            st.rerun()

        # Patches anwenden → Preflight → Autorender
        handoff_done = False
        if patches:
            try:
                patched = apply_patches(st.session_state.last_code, patches, strategy="body_only")
                ok = preflight_and_switch(patched)
                if ok:
                    # Kontext bleibt in last_json; falls Agent 2 neue block_index o.ä. liefern sollte,
                    # kann MEGA_PROMPT künftig das in user json signalisieren. Hier keine Anzeige.
                    handoff_done = True
                else:
                    with st.chat_message("assistant"):
                        st.markdown("Die Änderung führte zu Laufzeitfehlern — ich korrigiere das intern und starte neu, sobald stabil.")
            except Exception as e:
                with st.chat_message("assistant"):
                    st.markdown(f"Patch-Anwendung fehlgeschlagen: {e!r}. Bitte Wünsche etwas konkreter formulieren.")

        if handoff_done:
            st.session_state["skip_agent_on_next_run"] = True
            st.rerun()

# ===== Auto-Re-Render nach Re-Run =============================================
if st.session_state.get("last_code"):
    try:
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

        if not st.session_state._runner_autorun_done:
            st.session_state._runner_autorun_done = True
            st.sidebar.caption("Runner automatisch gestartet.")
    except BaseException:
        st.sidebar.warning("Auto-Render fehlgeschlagen – letzter Code konnte nicht ausgeführt werden.")
