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


# ===== Patch-/Marker-Helpers ===================================================
def _normalize_patches_list(patches_in) -> List[Dict[str, Any]]:
    """Akzeptiert Pydantic-Objekte (PatchItem) ODER dicts und liefert List[dict]."""
    out: List[Dict[str, Any]] = []
    for p in patches_in or []:
        if isinstance(p, dict):
            out.append(p)
        else:
            out.append({
                "block_id": getattr(p, "block_id", None),
                "new_code": getattr(p, "new_code", None),
                "old_hash": getattr(p, "old_hash", None),
                "notes": getattr(p, "notes", None),
            })
    # nur valide Einträge
    return [d for d in out if d.get("block_id") and isinstance(d.get("new_code"), str)]

_REGION_RE = re.compile(r"^\s*#\s*region\s+BLOCK\s+id\s*=", re.MULTILINE)
def _has_region_markers(code_text: str) -> bool:
    return bool(_REGION_RE.search(code_text or ""))

def _looks_like_full_program(snippet: str) -> bool:
    """Ein Patch kann fälschlich Vollcode enthalten -> heuristisch erkennen."""
    if not isinstance(snippet, str):
        return False
    s = snippet.strip()
    # typische Artefakte für Vollcode
    return (
        s.startswith("from __future__ import annotations")
        or s.startswith("import ")
        or "def main(" in s
        or "# ==== BUNDLED COMPONENTS BEGIN ====" in s
    )



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
    # FIX: prüfe korrekt den Policy-Pfad (vorher: META_INDEX_PATH)
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

# ---------- Addendum: Haupt-Agent (4-Felder-Protokoll) ----------
APP_AGENT_ADDENDUM = """
[OUTPUT PROTOKOLL — HAUPT-AGENT · STRICT]

Du gibst deinen Output IMMER als strukturiertes Objekt mit bis zu vier Feldern zurück:

1) user_markdown (string)
   - Sichtbarer, natürlicher Text für die Person.
   - Keine Code-Fences, kein JSON, keine Marker.
   - Kurz und menschlich: Orientierung, Rückfragen oder Bestätigung.
   - Wenn alles klar ist (Stop-Kriterien erfüllt), ein kurzer Satz: „Ich baue dir …“

2) suggestions (array of string, max 4)
   - Bis zu vier ergänzende, klickbare Vorschläge.
   - Kurz, handlungsorientiert, konsistent mit user_markdown.
   - Beispiele: „Nimm Juli 2023“, „Erweitere Umkreis auf 20 km“, „Zeige Vergleich links/rechts“.

3) json (object)
   - Nur intern, niemals anzeigen.
   - Container für strukturierte Daten wie:
     - plan_spec (gemäß Abschnitt 15.2)
     - block_index (Liste der Code-Blöcke mit Metadaten)
     - components_manifest (Liste der verwendeten Komponenten inkl. Hash/Bytes)
     - policy_notes (interne Korrekturen/Begründungen)
   - Strikt valides JSON. Keine Kommentare, keine Erklärsätze.

4) code (string)
   - Vollständiger, lauffähiger Python-Quelltext, ohne Markdown-Fences.
   - Alle ausführbaren Teile liegen in markierten Regionen:
     # region BLOCK id=... kind=... phase=... name="..." plan_ref="..." hash=... modifiable=<yes|no>
     ... Body ...
     # endregion BLOCK id=...
   - Ein einziger Entry-Point (def main(): …), keine EE-Init/Auth im Code.
   - UI optional, GEE-first, Render nur in main(), nichts beim Import.
   - Nur ausgeben, wenn die Stop-Kriterien erfüllt sind (siehe Abschnitt 12).

SICHTBARKEIT:
- user_markdown und suggestions → sichtbar.
- json und code → niemals direkt im Chat anzeigen.

PLAN_SPEC:
- Falls Richtung/Parameter klar (Stop-Kriterien erfüllt): lege PLAN_SPEC streng nach Abschnitt 15.2 in json.plan_spec ab.
- Fehlen Pflichtangaben: stelle Rückfragen in user_markdown und gib KEIN json.plan_spec aus.
- Niemals PLAN_SPEC als Text ausgeben.
"""

# ---------- Addendum: Refactor-Agent (Patch-Only) ----------
REFACTOR_ADDENDUM = """
[REFACTOR-MODUS · PATCH-ONLY]

Kontextquellen (vom Host übergeben):
- CURRENT_CODE (vollständiger, aktuell laufender Code mit Block-Markern).
- json.block_index (Struktur und Metadaten der vorhandenen Blöcke).
- json.plan_spec (Plan-Spezifikation des aktuellen Builds).
- json.components_manifest (verwendete Komponenten/Hashes).

Dein Output ist strikt ein strukturiertes Objekt:
{
  "user_markdown": "<optional sichtbarer Text>",
  "suggestions": ["<max 4 kurze Vorschläge>"],
  "patches": [
    { "block_id": "<id>", "new_code": "<GANZER Body ohne Marker>", "old_hash": "<optional>", "notes": "<optional>" }
  ]
}

Regeln:
- Nur Block-BODIES patchen, keine Marker mitsenden.
- Minimal-invasiv: nur angefragte Blöcke oder solche mit modifiable=yes ändern.
- Öffentliche Signaturen erhalten, keine neuen Abhängigkeiten, keine EE-Init/Auth.
- Keine neuen Blöcke anlegen, außer die Person bestätigt dies explizit.
- Wenn eine Änderung neue Blöcke erfordert: zuerst um Zustimmung bitten (in user_markdown), dann Patch liefern.
- Niemals Vollcode ausgeben. Keine JSON/Code-Fences im sichtbaren Text.
"""

# ---------- Output-Schemas ----------
class UiResponse(BaseModel):
    user_markdown: str = Field(description="Sichtbarer Text für den Chat. Kein Code-Fence.")
    suggestions: Optional[List[str]] = Field(default=None, description="Bis zu 4 Vorschläge (Buttons).")
    json: Optional[Dict[str, Any]] = Field(default=None, description="Interner JSON-Block (z. B. plan_spec, block_index, components_manifest). Niemals anzeigen.")
    code: Optional[str] = Field(default=None, description="Vollständiger Python-Code (ohne Markdown-Fences).")

try:
    UiResponse.model_rebuild()
except Exception:
    pass

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

# ---------- Agent-Instanzen ----------
APP_AGENT = None
REFACTOR_AGENT = None

if AGENTS_OK:
    # Haupt-Agent: Mega-Prompt + 4-Felder-Addendum
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

    # Refactor-Agent: Mega-Prompt + Patch-Only-Addendum
    REFACTOR_AGENT = Agent(
        name="EO-Refactor",
        instructions=MEGA_PROMPT + "\n\n" + REFACTOR_ADDENDUM,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        model=OpenAIResponsesModel(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client
        ),
        output_type=AgentOutputSchema(RefactorResponse, strict_json_schema=True),
    )

# ===== Self-Heal / Fixer (separat, nur intern) ================================
import sys as _sh_sys
import io as _sh_io

# --- Import-sicherer Shim: verhindert NameError, falls vorzeitig aufgerufen ---
if "self_heal_until_runs" not in globals():
    def self_heal_until_runs(code_text: str, max_rounds: int = 5) -> Tuple[bool, str, List[str]]:
        """
        Import-sicherer Minimal-Stub:
        Wird NUR benötigt, falls an anderer Stelle (vor nachstehender echter Definition)
        bereits auf self_heal_until_runs zugegriffen wird.
        Führt KEINE Heilung aus, verhindert aber NameError beim Import.
        """
        return True, code_text, []

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
    _stdout, _stderr = _sh_sys.stdout, _sh_sys.stderr
    ok_out: bool = False
    text_out: str = ""
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

        ok_out = True
        text_out = buf.getvalue()
    except BaseException as e:
        text_out = buf.getvalue() + f"\nERROR({e.__class__.__name__}): {e!r}"
        ok_out = False
    finally:
        _sh_sys.stdout = _stdout
        _sh_sys.stderr = _stderr
    return ok_out, text_out


# --- SANITIZER für typische Agent-Ausgaben (kein KI-Fix, nur mechanisch) -----
_SANITIZE_IMPORT_RE = re.compile(r'^\s*from\s+blocks\.components\.[^\n]+\n', re.MULTILINE)
def _sanitize_agent_code(code_text: str) -> str:
    s = code_text or ""
    # 1) Korrigiere future-Import
    s = s.replace("from future import annotations", "from __future__ import annotations")
    # 2) Korrigiere main-Guard
    s = re.sub(r'if\s+name\s*==\s*[\'"]main[\'"]\s*:', 'if __name__ == "__main__":', s)
    # 3) Entferne In-Repo-Imports, da Komponenten bereits inline gebundelt sind
    s = _SANITIZE_IMPORT_RE.sub("", s)
    return s


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
    # Modusgesteuert: Aus / Nur Preflight / Voll
    mode = st.session_state.get("self_heal_mode", "full")  # "full" | "preflight" | "off"
    sanitized = _sanitize_agent_code(code_text)

    def _switch(final_code: str) -> bool:
        final_code = _sanitize_agent_code(final_code) 
        st.session_state.last_code = final_code
        st.session_state._runner_autorun_done = False
        st.session_state.build_completed = True
        st.session_state.refactor_mode = True
        try:
            autorender_now()
            return True
        except Exception:
            return False

    if mode == "off":
        # Direkt übernehmen, keine Heilung
        return _switch(sanitized)

    if mode == "preflight":
        ok, _out = _sh_sandbox_exec(sanitized)  # nur Testlauf (keine Reparatur)
        if not ok:
            return False
        return _switch(sanitized)

    # Default: full (Preflight + Fixer)
    ok, final_code, _heal = self_heal_until_runs(sanitized, max_rounds=5)
    if not ok:
        return False
    return _switch(final_code)


# ===== Session / SDK-Session ==================================================
if AGENTS_OK:
    try:
        if "agent_session_id" not in st.session_state:
            st.session_state.agent_session_id = uuid.uuid4().hex
        if "sdk_session" not in st.session_state:
            SESSIONS_DB = str((RUNNER_DIR / "sessions.db").resolve())
            st.session_state.sdk_session = SQLiteSession(st.session_state.agent_session_id, SESSIONS_DB)
    except Exception:
        # Fallback *persistiert* ebenfalls im Session State (verhindert Neuaufbau pro Run)
        if "sdk_session" not in st.session_state:
            st.session_state.sdk_session = SQLiteSession(st.session_state.agent_session_id)  # in-memory fallback
    sdk_session = st.session_state.sdk_session
else:
    sdk_session = None  # type: ignore

# ===== UI =====================================================================


def autorender_now() -> None:
    """Führt den aktuellen last_code *im render_slot* aus (ersetzt alte Anzeige)."""
    slot = st.session_state.render_slot
    slot.empty()  # alte Mini-App entfernen
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





st.set_page_config(page_title="talk2earth — EO Agent", layout="wide")
st.title("talk2earth — EO Agent (Agents SDK + Streamlit)")

with st.sidebar:
    st.subheader("Status")
    st.write("Agents SDK:", "✅ bereit" if AGENTS_OK else f"❌ {AGENTS_IMPORT_ERROR}")
    st.write("OPENAI_API_KEY gesetzt:", "✅" if os.environ.get("OPENAI_API_KEY") else "❌")
    st.write(f"Earth Engine: {'✅' if _EE_READY else '❌'}")
    st.divider()
    st.caption("Antwortformat: Markdown + Suggestions + JSON (intern) + Code (unsichtbarer Preflight).")
    st.divider()
    # >>> Self-Heal Umschalter
    _mode_label_to_key = {
        "Voll (Preflight+Fixer)": "full",
        "Nur Preflight": "preflight",
        "Aus": "off",
    }
    _current_mode = st.session_state.get("self_heal_mode", "full")
    _mode_label_init = {v: k for k, v in _mode_label_to_key.items()}[_current_mode]
    _mode_label = st.selectbox(
        "Self-Heal-Modus",
        ["Voll (Preflight+Fixer)", "Nur Preflight", "Aus"],
        index=["Voll (Preflight+Fixer)", "Nur Preflight", "Aus"].index(_mode_label_init),
        help="Steuert Preflight-Runner und automatischen Fixer."
    )
    st.session_state["self_heal_mode"] = _mode_label_to_key[_mode_label]

# Zentraler Render-Platzhalter, in den die Mini-App *immer* gerendert wird
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
if "last_suggestions" not in st.session_state:
    st.session_state.last_suggestions = None
if "sdk_session" not in st.session_state:
    st.session_state.sdk_session = None
if "self_heal_mode" not in st.session_state:
    st.session_state.self_heal_mode = "full"

# --- UI-only Rerun Handling (einmalige Entkopplung des Autorender-Reruns) ---
ui_only_rerun = False
if st.session_state.get("skip_agent_on_next_run"):
    # Dies ist der Turn nach dem Autorender-Rerun: Agent NICHT anrufen,
    # Flag zurücksetzen, UI/Autorender laufen lassen.
    st.session_state["skip_agent_on_next_run"] = False
    ui_only_rerun = True


# Verlauf anzeigen
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

def render_suggestions(suggestions: Optional[List[str]]) -> Optional[str]:
    if not suggestions:
        return None
    s = [x for x in suggestions if isinstance(x, str) and x.strip()][:4]
    if not s:
        return None

    selected: Optional[str] = None
    with st.chat_message("assistant"):
        st.subheader("Vorschläge")
        cols = st.columns(2)
        for i, label in enumerate(s):
            with cols[i % 2]:
                with st.container(border=True):
                    st.markdown(label)
                    if st.button("Auswählen", key=f"sugg_{i}", use_container_width=True):
                        selected = label
    return selected


def render_persistent_suggestions() -> None:
    """Zeigt ggf. zuletzt empfangene Vorschläge (persistiert) und setzt bei Klick queued_input."""
    sugg = st.session_state.get("last_suggestions") or []
    if not isinstance(sugg, list) or not sugg:
        return

    with st.chat_message("assistant"):
        st.subheader("Vorschläge")
        cols = st.columns(2)
        for i, label in enumerate(sugg[:4]):
            with cols[i % 2]:
                if st.button(label, key=f"sugg_btn_{i}", use_container_width=True):
                    st.session_state["queued_input"] = label
                    st.session_state["last_suggestions"] = None
                    st.session_state["skip_agent_on_next_run"] = False
                    st.rerun()

# Persistente Vorschläge immer anzeigen (falls vorhanden)
render_persistent_suggestions()

# Eingabe — chat_input IMMER rendern, queued hat nur Priorität
_new_input = st.chat_input("Nachricht eingeben…")
_queued = st.session_state.get("queued_input")
if _queued:
    prompt = _queued
    st.session_state["queued_input"] = None
else:
    prompt = _new_input

# Verliere keine Eingabe im UI-only Rerun: Puffer + sofort rerun
if ui_only_rerun and prompt:
    st.session_state["queued_input"] = prompt
    st.session_state["skip_agent_on_next_run"] = False
    st.rerun()

if prompt and not ui_only_rerun:
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

        # 2) suggestions → persistieren + **sofort** rendern
        if isinstance(suggestions, list) and suggestions:
            st.session_state["last_suggestions"] = suggestions
            render_persistent_suggestions()  # <-- sofortige Anzeige im selben Run

        # 3) JSON → nur persistieren (kann plan_spec, block_index, components_manifest enthalten)
        if isinstance(json_obj, dict):
            st.session_state["last_json"] = json_obj

        # 4) code → Preflight/Fixer/Autorender + Moduswechsel
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
                mode = st.session_state.get("self_heal_mode", "full")
                with st.chat_message("assistant"):
                    if mode == "off":
                        st.markdown("Code kam mit Fehlern — **Self-Heal ist deaktiviert**. Aktiviere Self-Heal oder prüfe den letzten Log.")
                    elif mode == "preflight":
                        st.markdown("Preflight hat Fehler gefunden — **keine automatische Reparatur aktiv**.")
                    else:
                        st.markdown("Ich hatte gerade ein Problem mit dem Code – ich versuche, es automatisch zu reparieren.")

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

        # Vorschläge → persistieren + **sofort** rendern
        if isinstance(suggestions, list) and suggestions:
            st.session_state["last_suggestions"] = suggestions
            render_persistent_suggestions()  # <-- sofortige Anzeige im selben Run

        # Patches anwenden → Preflight → Autorender
        handoff_done = False
        if patches:
            # 1) Patch-Objekte → dicts
            patches_dicts = _normalize_patches_list(patches)

            # 2) Pfad A: Marker-basiertes Patchen (empfohlen)
            try_marker = _has_region_markers(st.session_state.last_code)

            try:
                if try_marker:
                    patched = apply_patches(st.session_state.last_code, patches_dicts, strategy="body_only")
                    ok = preflight_and_switch(patched)
                    if ok:
                        # OPTIONAL: zukünftige Zusatzdaten übernehmen
                        if isinstance(patches_out, dict):
                            new_block_index = patches_out.get("block_index")
                            if new_block_index:
                                if not st.session_state.get("last_json"):
                                    st.session_state["last_json"] = {}
                                st.session_state["last_json"]["block_index"] = new_block_index
                        handoff_done = True
                    else:
                        mode = st.session_state.get("self_heal_mode", "full")
                        with st.chat_message("assistant"):
                            if mode == "off":
                                st.markdown("Patch führte zu Fehlern — **Self-Heal ist deaktiviert**.")
                            elif mode == "preflight":
                                st.markdown("Preflight fand Fehler im Patch — **keine Reparatur aktiv**.")
                            else:
                                st.markdown("Die Änderung führte zu Laufzeitfehlern — ich korrigiere das intern und starte neu, sobald stabil.")
                else:
                    # 3) Pfad B (Fallback): Wenn keine Marker AND genau 1 Patch und der liefert vollen Code → direkt übernehmen
                    if len(patches_dicts) == 1 and _looks_like_full_program(patches_dicts[0]["new_code"]):
                        full_code = patches_dicts[0]["new_code"]
                        ok = preflight_and_switch(full_code)
                        if ok:
                            handoff_done = True
                        else:
                            mode = st.session_state.get("self_heal_mode", "full")
                            with st.chat_message("assistant"):
                                if mode == "off":
                                    st.markdown("Vollcode-Patch hat Fehler — **Self-Heal ist deaktiviert**.")
                                elif mode == "preflight":
                                    st.markdown("Preflight fand Fehler — **keine Reparatur aktiv**.")
                                else:
                                    st.markdown("Die Änderung führte zu Laufzeitfehlern — ich korrigiere das intern und starte neu, sobald stabil.")
                    else:
                        raise RuntimeError("Keine Block-Marker gefunden und Patch ist kein Vollcode – kann nicht anwenden.")
            except Exception as e:
                with st.chat_message("assistant"):
                    st.markdown(f"Patch-Anwendung fehlgeschlagen: {e!r}. Bitte Wünsche etwas konkreter formulieren.")

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

