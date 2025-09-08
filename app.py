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

# ===== Parser & UI: Bullets → Buttons (alle: •, -, *) =========================
BULLET_RE = re.compile(r'^\s*([•\-\*])\s+(.+)$')

def parse_suggestions_from_text(text: str) -> List[str]:
    """Extrahiert alle Bullet-Zeilen (•, -, *) als Vorschläge (exakter Text)."""
    if not isinstance(text, str) or not text.strip():
        return []
    items: List[str] = []
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m:
            label = m.group(2).strip()
            if label:
                items.append(label)
    # Duplikate entfernen, Reihenfolge bewahren
    seen: set[str] = set()
    uniq: List[str] = []
    for it in items:
        if it not in seen:
            uniq.append(it)
            seen.add(it)
    return uniq

def render_suggestions_from_text(assistant_text: str) -> Optional[str]:
    """
    Rendert alle erkannten Bullets als Karten mit vollem Text und 'Auswählen'-Button.
    Rückgabe: der ausgewählte Text (oder None).
    """
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
    from agents import Agent, Runner, function_tool, SQLiteSession, AgentOutputSchema
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI
except Exception as e:
    AGENTS_OK = False
    AGENTS_IMPORT_ERROR = str(e)

# ===== Prompt laden ===========================================================
def load_text_file(path: pathlib.Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return fallback

MEGA_PROMPT = load_text_file(
# === Builder marker rules (appended to MEGA_PROMPT for builder runs) ==================


    PROMPTS_DIR / "mega_prompt.md",
    fallback=(
        "SYSTEM: Du bist ein einzelner Gesprächs-Agent, der Mini-Apps baut (GEE-first, UI optional). "
        "Arbeite L1.1→L1.2→L2→L3, nutze die Tools tool_get_meta, tool_get_policy, tool_get_uc_sections, "
        "tool_bundle_components, tool_run_python. AOI als strukturierte Spec, kein Textparser. "
        "Erzeuge vor Code eine PLAN_SPEC (JSON) und bundle dann exakt die benötigten Komponenten."
    )
)

# ===== Hilfsfunktionen ========================================================
def _sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def _safe_json(data: Any) -> str:
    from blocks.components.util.block_marker_utils import apply_patches, build_block_index
    return json.dumps(data, ensure_ascii=False)

def extract_first_python_block(text: str) -> Optional[str]:
    m = re.search(r"```(?:py|python|python3)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None

CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_\-]*\s*.*?```", re.DOTALL)

def strip_fenced_code_blocks(text: str) -> str:
    """Entfernt alle Markdown-Code-Fences (```...```) – nur für die UI-Anzeige."""
    if not isinstance(text, str) or "```" not in text:
        return text

# === Builder marker rules (safe string) ===
BUILDER_MARKER_RULES = "\n".join(["You MUST return code with explicit block annotations for every functional section.","Use Python comments as markers, exactly this format:","","# filemeta uc_id=<slug> version=1 spec_hash=<8-hex> generated_at=<YYYY-MM-DD>","","# region BLOCK id=<phase.component_id> kind=<acq|proc|viz|ui> phase=<L1|L2|L3|Acquire|Process|Visualize|UI> name=\"<human title>\" plan_ref=\"<plan.path>\" hash=<8-hex> modifiable=<yes|no>","... code for this block ...","# endregion BLOCK id=<phase.component_id>","","Rules:","- All executable code MUST lie inside regions; do NOT place executable code outside regions.","- The 'id' MUST be deterministic and stable across regenerations (e.g., \"acq.aoi_selector\").","- 'plan_ref' MUST match the node path in the planning spec (e.g., \"acquire.aoi\").","- 'hash' is an 8-hex content hash of the block body (no markers).","- If the user requests, set 'modifiable=yes' only for UI/viz or explicitly requested blocks; else 'no'.","- Do NOT include markdown fences in the code. Return plain Python only.","- Additionally, fill 'block_index' with an array of objects mirroring all blocks and their metadata."])

    return CODE_FENCE_RE.sub("", text).strip()

def ensure_event_loop() -> None:
    """Event-Loop für Streamlit-Thread sicherstellen."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# ==== Neu: PLAN_SPEC aus un-fenced JSON am Textanfang herausfiltern ===========
def _strip_leading_plan_spec(text: str) -> Tuple[str, Optional[dict]]:
    """
    Entfernt ein am Textanfang stehendes JSON-Objekt, wenn es wie eine PLAN_SPEC aussieht.
    Gibt (bereinigter_text, planspec_dict|None) zurück.
    """
    if not isinstance(text, str) or not text.strip():
        return text, None
    m = re.match(r'^\s*(\{.*?\})\s*(?:\n|$)', text, flags=re.DOTALL)
    if not m:
        return text, None
    blob = m.group(1)
    try:
        obj = json.loads(blob)
        return text[m.end():].lstrip(), obj
    except Exception:
        return text, None

# ===== Earth Engine (Host-Init) ===============================================
EE_OK = True
try:
    import ee
except Exception:
    EE_OK = False

def ee_maybe_init() -> bool:
    if not EE_OK:
        return False
    # Bereits initialisiert?
    try:
        ee.Number(1).getInfo()
        return True
    except Exception:
        pass
    # Init via Streamlit-Secrets (Service Account)
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

# ===== Function Tools (ohne ui_suggest) ======================================
@function_tool
def tool_get_meta() -> str:
    """Lädt layer1_index.yml und liefert den Text (UTF-8)."""
    if not META_INDEX_PATH.exists():
        return _safe_json({"error": f"meta index not found: {META_INDEX_PATH}"})
    return META_INDEX_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_policy() -> str:
    """Lädt policy.json und liefert den Text (UTF-8)."""
    if not POLICY_PATH.exists():
        return _safe_json({"error": f"policy not found: {POLICY_PATH}"})
    return POLICY_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_uc_sections(uc_id: str, sections: List[str]) -> str:
    """
    Lädt gezielte Sektionen eines UC-Packs (YAML → JSON-Auswahl).
    sections: z. B. ["param_spec","invariants","visualize_presets","allowed_patterns","ui_contracts","render_pattern","capabilities_required","capabilities_provided","checks"]
    """
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

# ---- Bundle-Tool zurück (wird vom Agent/Fixer gebraucht) ---------------------
@function_tool
def tool_bundle_components(components: List[str]) -> str:
    """
    Lädt mehrere Komponenten und liefert:
    {
      "bundle": "<concatenated files mit BEGIN/END headers>",
      "manifest": [{"id": path, "sha1": "...", "bytes": N}, ...]
    }
    """
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

# --- Runner-Tool: interne Impl + Tool-Wrapper, mit EE/Path-Prelude ------------
def _tool_run_python_impl(code: str,
                          filename: Optional[str] = None,
                          timeout_sec: int = 600,
                          mode: str = "script",
                          port: int = 8502) -> str:
    """
    Führt Code im runner/sandbox aus.
    - mode="script":  python file.py (stdout/stderr)
    - mode="streamlit": streamlit run file.py --server.headless --server.port {port}
                        (gibt url + pid + bootstrap log zurück)
    Hinweis: Auf Streamlit Cloud ist die zweite Streamlit-Instanz meist nicht erreichbar.
    """
    if not filename:
        filename = "app_run.py"

    # Prelude injizieren: EE-Init im Subprozess (Service Account via ENV)
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
    code_with_prelude = ee_prelude + (code or "")

    target = SANDBOX_DIR / filename
    target.write_text(code_with_prelude, encoding="utf-8")

    # Subprozess-Umgebung: Repo in PYTHONPATH + EE-Secrets weitergeben
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
            return json.dumps({
                "ok": proc.returncode == 0,
                "stdout": proc.stdout[-15000:],
                "stderr": proc.stderr[-15000:],
                "path": str(target),
                "mode": "script"
            }, ensure_ascii=False)
        except subprocess.TimeoutExpired as e:
            return json.dumps({
                "ok": False,
                "stdout": (getattr(e, "stdout", "") or "")[-15000:],
                "stderr": f"TIMEOUT after {timeout_sec}s",
                "path": str(target),
                "mode": "script"
            }, ensure_ascii=False)

    if mode == "streamlit":
        try:
            proc = subprocess.Popen(
                ["streamlit", "run", str(target), "--server.headless", "true", "--server.port", str(port)],
                cwd=SANDBOX_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                bootstrap = proc.stdout.readline().strip() if proc.stdout else ""
            except Exception:
                bootstrap = ""
            url = f"http://localhost:{port}"
            return json.dumps({
                "ok": True,
                "url": url,
                "pid": proc.pid,
                "path": str(target),
                "hint": "Zweite Streamlit-Instanz ist auf Cloud-Hosts i. d. R. nicht erreichbar.",
                "mode": "streamlit",
                "bootstrap_log": bootstrap[-2000:]
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "ok": False,
                "error": f"Failed to start streamlit: {e}",
                "path": str(target),
                "mode": "streamlit"
            }, ensure_ascii=False)

    return json.dumps({"error": f"unknown mode '{mode}'"})

# Für den Agenten als Tool registrieren:
tool_run_python = function_tool(_tool_run_python_impl)

# ===== PLAN_SPEC-Handling (robust) ============================================
PLAN_SPEC_KEY_CANDIDATES = ("use_case", "aoi_spec", "render", "components")

def _looks_like_plan_spec(obj: Any) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in PLAN_SPEC_KEY_CANDIDATES)

def _extract_plan_spec_from_text(answer_text: str) -> Tuple[Optional[dict], str]:
    """
    Sucht nach PLAN_SPEC in der sichtbaren Antwort und entfernt sie.
    Unterstützt:
      - Marker: PLAN_SPEC_BEGIN ... PLAN_SPEC_END mit ```json ... ```
      - Fenced code block ```json ... ```
    """
    text = answer_text or ""
    marker_regex = re.compile(
        r"PLAN_SPEC_BEGIN\s*```json\s*(\{.*?\})\s*```\s*PLAN_SPEC_END",
        re.DOTALL | re.IGNORECASE
    )
    m = marker_regex.search(text)
    if m:
        try:
            spec = json.loads(m.group(1))
            cleaned = marker_regex.sub("", text).strip()
            if _looks_like_plan_spec(spec):
                return spec, cleaned
        except Exception:
            pass

    fence_json_regex = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
    for jm in fence_json_regex.finditer(text):
        try:
            candidate = json.loads(jm.group(1))
            if _looks_like_plan_spec(candidate):
                cleaned = (text[:jm.start()] + text[jm.end():]).strip()
                return candidate, cleaned
        except Exception:
            continue

    return None, text

# ===== Agent Setup + persistente SDK-Session ==================================
if AGENTS_OK:
    # --- ONLY plan_spec as structured output (Pydantic v2 safe) ---------------
    from pydantic import BaseModel, Field

    class PlanSpecOnly(BaseModel):
        plan_spec: Optional[Dict[str, Any]] = Field(default=None)

    # Fix for "class-not-fully-defined" on some environments
    try:
        PlanSpecOnly.model_rebuild()
    except Exception:
class UiPlanCode(BaseModel):
    """Structured output for builder: visible text + plan + code + block index."""
    user_markdown: str = Field(description="Visible user-facing markdown (no code fences).")
    plan_spec: Optional[Dict[str, Any]] = Field(default=None, description="Internal planning spec object.")
    code: Optional[str] = Field(default=None, description="Final Python code (no fences, directly executable).")
    block_index: Optional[List[Dict[str, Any]]] = Field(default=None, description="List of code-block metadata for refactor agent.")

try:
    UiPlanCode.model_rebuild()
except Exception:
    pass


class PatchItem(BaseModel):
    block_id: str
    new_code: str
    old_hash: Optional[str] = None
    notes: Optional[str] = None

class RefactorPatches(BaseModel):
    patches: List[PatchItem]
    user_markdown: Optional[str] = None

try:
    RefactorPatches.model_rebuild()
except Exception:
    pass


        pass



@function_tool(name="request_refactor", description="Signalisiert, dass Patches für den bestehenden, markierten Code erzeugt werden sollen.")
def request_refactor(reason: Optional[str] = None) -> bool:
    st.session_state._want_refactor = True
    st.session_state._refactor_reason = reason or ""
    return True

    openai_client = AsyncOpenAI()  # nutzt OPENAI_API_KEY
    agent = Agent(
        name="EO-Agent",
        instructions=MEGA_PROMPT,
        request_structured_output,
        request_refactor,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
        output_type=AgentOutputSchema(PlanSpecOnly, strict_json_schema=False),  # nur plan_spec
    )


# === Builder-Agent für strukturierte Ausgabe (kein Streaming erforderlich) ===
builder_agent = Agent(
    name="EO-Builder",
    instructions=MEGA_PROMPT + "\n\n" + BUILDER_MARKER_RULES,
    tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python, request_structured_output],
    model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
    output_type=AgentOutputSchema(UiPlanCode, strict_json_schema=True),
)


# === Refactor-Agent (liefert nur Patches im JSON-Schema) ================================
refactor_agent = Agent(
    name="EO-Refactor",
    instructions=MEGA_PROMPT + "\n\n" + REFACTOR_RULES,
    tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python, request_structured_output, request_refactor],
    model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
    output_type=AgentOutputSchema(RefactorPatches, strict_json_schema=True),
)


else:
    agent = None

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

# ============================================================================
# >>>>>>>> SELF-HEALING: Sandbox + Fixer-Agent (Agent 2: Repair-Modus) <<<<<<
# ============================================================================
import sys as _sh_sys
import io as _sh_io

DEFAULT_MAX_TURNS = 12

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

class PythonBlockOutput(BaseModel):
    """Ausgabehülle für Fixer: nur Code."""
    code: str
    """Ausgabehülle für Fixer: nur Code."""

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
            session=sdk_session,  # persistente Session auch für Fixer
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
        ok = True
    except BaseException as e:  # Stop/Rerun werden gefangen, Fixer kann triggern
        out = buf.getvalue() + f"\nERROR({e.__class__.__name__}): {e!r}"
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

# ===== Streamlit UI ===========================================================
st.set_page_config(page_title="talk2earth — EO Agent", layout="wide")
st.title("talk2earth — EO Agent (Agents SDK + Streamlit)")

with st.sidebar:
    st.subheader("Status")
    st.write("Agents SDK:", "✅ bereit" if AGENTS_OK else f"❌ {AGENTS_IMPORT_ERROR}")
    st.write("OPENAI_API_KEY gesetzt:", "✅" if os.environ.get("OPENAI_API_KEY") else "❌")
    st.write(f"Earth Engine: {'✅' if _EE_READY else '❌'}")
    st.divider()
    st.caption("Hinweis: AOI/Zeitraum/Parameter werden im Dialog geklärt; der Agent bündelt Komponenten vor dem Code.")

# Session-States für Chat/Runner
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_code" not in st.session_state:
    st.session_state.last_code = ""
if "last_assistant_text" not in st.session_state:
    st.session_state.last_assistant_text = ""
if "skip_agent_on_next_run" not in st.session_state:
    st.session_state.skip_agent_on_next_run = False
if "queued_input" not in st.session_state:
    st.session_state.queued_input = None
if "_runner_autorun_done" not in st.session_state:
    st.session_state._runner_autorun_done = False

# Guardeter UI-ReRun: UI-Repaint ohne neuen Agent-Call
ui_only_rerun = False
if st.session_state.get("skip_agent_on_next_run"):
    st.session_state["skip_agent_on_next_run"] = False
    ui_only_rerun = True

# Verlauf (UI) rendern
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# Bullets → Buttons (zusätzlich) vor der Eingabe
selected = render_suggestions_from_text(st.session_state.get("last_assistant_text", ""))
if selected:
    st.session_state["queued_input"] = selected  # exakter Bullet-Text
    st.rerun()

# Chat-Eingabe (Queue zuerst, dann regulär)
queued = st.session_state.get("queued_input")
if queued:
    st.session_state["queued_input"] = None
    prompt = queued
else:
    prompt = st.chat_input("Nachricht an den Agenten eingeben und mit Enter senden")

if prompt and not ui_only_rerun:
    # 1) User Nachricht anzeigen/speichern
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2) Agent call mit persistenter SDK-Session
    if not AGENTS_OK or agent is None:
        st.error("Agents SDK nicht verfügbar.")
        st.stop()

    iteration_context = ""
    if st.session_state.last_code:
        iteration_context = "\n\n[HINWEIS] Es liegt bereits ausführbarer Code vor; Iterationen sind möglich."

    ensure_event_loop()

    result = Runner.run_sync(
        agent,
        input=(prompt + iteration_context),
        session=sdk_session,  # persistente Session
        max_turns=60,
    )

    # ===== Sichtbarer Text (ohne Code) & strukturierte Outputs bevorzugen =====
    out = getattr(result, "final_output", None)

    # Sichtbaren Text holen (versch. Felder), dann PLAN_SPEC-JSON am Anfang ggf. entfernen
    visible_text = ""
    for attr in ("text", "final_output_text", "message", "output_text"):
        v = getattr(result, attr, None)
        if isinstance(v, str) and v.strip():
            visible_text = v
            break
    if isinstance(out, str) and not visible_text:
        visible_text = out

    # Erst un-fenced JSON am Anfang entfernen (mögliche PLAN_SPEC-Leak)
    visible_text, leading_obj = _strip_leading_plan_spec(visible_text)
    try:
        if leading_obj is not None and _looks_like_plan_spec(leading_obj):
            st.session_state["last_plan_spec"] = leading_obj
    except Exception:
        pass

    # Danach nochmals: PLAN_SPEC aus fenced-JSON entfernen (Fallback)
    extracted, cleaned_text = _extract_plan_spec_from_text(visible_text)
    if extracted is not None and _looks_like_plan_spec(extracted):
        st.session_state["last_plan_spec"] = extracted
        visible_text = cleaned_text

    # PLAN_SPEC strukturiert lesen (bevor wir rendern)
    plan_spec_obj = None
    if out is not None:
        plan_spec_obj = getattr(out, "plan_spec", None)
    if plan_spec_obj is None and hasattr(result, "outputs") and isinstance(result.outputs, dict):
        plan_spec_obj = result.outputs.get("plan_spec", None)
    try:
        if plan_spec_obj is not None and _looks_like_plan_spec(plan_spec_obj):
            st.session_state["last_plan_spec"] = plan_spec_obj
    except Exception:
        pass

    # 3) Assistant-Antwort rendern (Codefences ausblenden)
    ui_answer = strip_fenced_code_blocks(visible_text or "")
    with st.chat_message("assistant"):
        st.markdown(ui_answer)
    st.session_state.messages.append({"role": "assistant", "content": ui_answer})
    st.session_state["last_assistant_text"] = ui_answer

    # 4) Code → Self-Heal → Auto-Ausführen (silent). Struktur bevorzugt; Fallback: Markdown-Parsing
    code_out = extract_first_python_block(visible_text or "")
    if isinstance(code_out, str) and code_out.strip():
        st.session_state.last_code = code_out
        ok, final_code, heal_log = self_heal_until_runs(code_out, max_rounds=5)
        if ok:
            ns: Dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
            try:
                compiled = compile(final_code, "<visible>", "exec")
                exec(compiled, ns, ns)
                _resp = _tool_run_python_impl(final_code, mode="script")
                try:
                    res = json.loads(_resp) if isinstance(_resp, str) else _resp
                except Exception:
                    res = {"ok": False, "stderr": "Runner response decode failed."}
                if res.get("ok"):
                    st.session_state._runner_autorun_done = True
                    st.sidebar.caption("Code ausgeführt • Runner erfolgreich ausgeführt.")
                else:
                    runner_err = res.get("stderr", "")
                    patched = _sh_fix_code_once(final_code, runner_err) if runner_err else None
                    if patched and patched.strip() != final_code.strip():
                        try:
                            compiled2 = compile(patched, "<visible>", "exec")
                            exec(compiled2, ns, ns)
                            _resp2 = _tool_run_python_impl(patched, mode="script")
                            res2 = json.loads(_resp2) if isinstance(_resp2, str) else _resp2
                            if res2.get("ok"):
                                st.session_state._runner_autorun_done = True
                                st.sidebar.caption("Runner erfolgreich nach Auto-Fix.")
                                st.session_state.last_code = patched
                            else:
                                st.sidebar.warning("Runner-Fehler blieb bestehen (siehe Logs im Backend).")
                        except Exception:
                            st.sidebar.warning("Auto-Fix nach Runner-Fehler schlug fehl.")
            except BaseException as e:
                st.error("Es gab einen Ausführungsfehler. Ich konnte ihn nicht automatisch beheben.")
                st.caption(f"Hinweis: {getattr(e, '__class__', type(e)).__name__} wurde abgefangen; Details sind intern protokolliert.")
        else:
            with st.expander("Fehler beim automatischen Ausführen – Logs", expanded=True):
                st.write(heal_log)

    st.session_state["skip_agent_on_next_run"] = True
    st.rerun()

# ===== Auto-Re-Render nach Re-Run (kein Prompt aktiv) =========================
if (ui_only_rerun or not prompt) and st.session_state.get("last_code"):
    try:
        ns: Dict[str, object] = {"__name__": "__generated__", "st": st, "ee": ee}
        compiled = compile(st.session_state.last_code, "<autorender>", "exec")
        exec(compiled, ns, ns)
        if not st.session_state._runner_autorun_done:
            _ = _tool_run_python_impl(st.session_state.last_code, mode="script")
            st.session_state._runner_autorun_done = True
            st.sidebar.caption("Runner automatisch gestartet.")
    except BaseException:
        st.sidebar.warning("Auto-Render fehlgeschlagen – letzter Code konnte nicht ausgeführt werden.")

# Keine Runner-Buttons/Codeanzeige – vollautomatischer Ablauf


