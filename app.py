from __future__ import annotations

import os
import re
import json
import uuid
import hashlib
import pathlib
import subprocess
from typing import Optional, List, Dict, Any, Tuple, TypedDict  # <-- NEU: TypedDict
from pydantic import BaseModel  # <-- NEU: strukturiertes Output-Schema

import streamlit as st
import asyncio  # Event-Loop-Fix für Streamlit-Thread
# ... existing imports ...
import os
import ee


import geemap
from geemap.foliumap import Map  # häufig genutzt in Render-Komponenten

# === EE-Init (zentral & idempotent) ==========================================
import json

def _ee_try_ready() -> bool:
    try:
        _ = ee.Image(1).getInfo()
        return True
    except Exception:
        return False

def _ee_parse_key_from_secrets() -> dict:
    key_val = st.secrets.get("EE_PRIVATE_KEY")
    if key_val is None:
        raise RuntimeError("EE_PRIVATE_KEY fehlt in streamlit secrets.")
    if isinstance(key_val, dict):
        return key_val
    if isinstance(key_val, str):
        return json.loads(key_val.strip())
    raise RuntimeError("EE_PRIVATE_KEY Format unbekannt (weder str noch dict).")

@st.cache_data(show_spinner=False)
def ee_maybe_init() -> bool:
    # 0) Bereits initialisiert?
    if _ee_try_ready():
        return True

    # 1) Streamlit-Secrets (Service Account)
    project = st.secrets.get("EE_PROJECT")
    sa_email = st.secrets.get("EE_SERVICE_ACCOUNT")
    if project and sa_email and st.secrets.get("EE_PRIVATE_KEY") is not None:
        try:
            key_dict = _ee_parse_key_from_secrets()
            creds = ee.ServiceAccountCredentials(email=sa_email, key_data=json.dumps(key_dict))
            ee.Initialize(credentials=creds, project=project)
            if _ee_try_ready():
                return True
        except Exception:
            pass  # weiter zu (2)

    # 2) Application Default / Host (falls vorhanden)
    try:
        ee.Initialize()  # nutzt Host-/ADC-Umgebung, wenn vorhanden
        if _ee_try_ready():
            return True
    except Exception:
        pass

    return False

# einmalig aufrufen (vor dem Rest der UI)
_EE_READY = ee_maybe_init()

# --- Agent run limits (configurable via env var) ---
DEFAULT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "100"))  # raise from SDK default (~12)

BASE_DIR = pathlib.Path(__file__).parent.resolve()

# ===== Agents SDK korrekt importieren =========================================
AGENTS_OK = True
try:
    from agents import Agent, Runner, function_tool, SQLiteSession
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI
except Exception as e:
    AGENTS_OK = False
    AGENTS_IMPORT_ERROR = str(e)

# ===== Pfade / Repo-Layout ====================================================
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
PROMPTS_DIR = KNOWLEDGE_DIR / "prompts"
META_INDEX_PATH = KNOWLEDGE_DIR / "meta" / "layer1_index.yml"   # L1.1 (Alltagssprache)
POLICY_PATH = KNOWLEDGE_DIR / "policy.json"                     # L2 (strict JSON)
USECASES_DIR = KNOWLEDGE_DIR / "usecases"                       # L1.2 (pro UC)

BLOCKS_DIR = BASE_DIR / "blocks"
COMPONENTS_DIR = BLOCKS_DIR / "components"
LEGACY_DIR = COMPONENTS_DIR / "legacy"                          # blockieren beim Bundling

RUNNER_DIR = BASE_DIR / "runner"
SANDBOX_DIR = RUNNER_DIR / "sandbox"
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

# ===== Prompt laden (NEU: mega_prompt.md) ====================================
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

# ===== Hilfsfunktionen ========================================================
def _sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)

def _is_legacy(p: pathlib.Path) -> bool:
    return (LEGACY_DIR in p.parents) or p.name.startswith("fs_")

def _repo_rel(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except Exception:
        return str(path)

def _ls_usecase_ids() -> List[str]:
    if not USECASES_DIR.exists():
        return []
    return sorted([p.stem for p in USECASES_DIR.glob("*.yml")])

def extract_first_python_block(text: str) -> Optional[str]:
    m = re.search(r"```(?:python)?\s*(.+?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None

# ---- NEU: Helper: Vorschläge rendern (Layer 1) -------------------------------
def render_l1_suggestions() -> Optional[Dict[str, Any]]:
    """Zeigt Layer-1-Vorschläge als Buttons; Rückgabe = gewählter Vorschlag oder None."""
    suggs = st.session_state.get("l1_suggestions") or []
    if not suggs:
        return None
    st.subheader("Vorschläge")
    cols = st.columns(min(3, len(suggs)))
    chosen = None
    for i, s in enumerate(suggs):
        label = s.get("label", f"Option {i+1}")
        with cols[i % len(cols)]:
            if st.button(label, key=f"sugg_{s.get('id', i)}"):
                chosen = s
    return chosen

# ---- NEU: PLAN_SPEC abfangen & aus UI entfernen ------------------------------
PLAN_SPEC_KEY_CANDIDATES = ("use_case", "aoi_spec", "render", "components")

def _looks_like_plan_spec(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return all(k in obj for k in PLAN_SPEC_KEY_CANDIDATES)

def _extract_plan_spec_from_text(answer_text: str) -> Tuple[Optional[dict], str]:
    """
    Sucht nach PLAN_SPEC in der sichtbaren Antwort und entfernt sie.
    Unterstützt:
      - Marker: PLAN_SPEC_BEGIN ... PLAN_SPEC_END (mit JSON dazwischen)
      - Fenced code block ```json ... ``` mit Schlüsseln 'use_case', 'aoi_spec', ...
    Gibt (plan_spec_dict|None, bereinigter_antwort_text) zurück.
    """
    text = answer_text or ""

    # 1) Marker-Variante
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
            pass  # weiter unten andere Varianten testen

    # 2) Fenced JSON-Block ohne Marker, der wie PLAN_SPEC aussieht
    fence_json_regex = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
    for jm in fence_json_regex.finditer(text):
        try:
            candidate = json.loads(jm.group(1))
            if _looks_like_plan_spec(candidate):
                cleaned = (text[:jm.start()] + text[jm.end():]).strip()
                return candidate, cleaned
        except Exception:
            continue

    # 3) Inline-JSON mit PLAN_SPEC-Hinweisen
    brace_regex = re.compile(r"(\{[^{}]+\})", re.DOTALL)
    for bm in brace_regex.finditer(text):
        try:
            candidate = json.loads(bm.group(1))
            if _looks_like_plan_spec(candidate):
                cleaned = (text[:bm.start()] + text[bm.end():]).strip()
                return candidate, cleaned
        except Exception:
            continue

    # Nichts gefunden
    return None, text

# ===== Neue Function Tools (Layer 1.1 → 1.2 → 2 → 3) =========================
@function_tool
def tool_get_meta() -> str:
    """
    Load global Layer1.1 meta index (single YAML as text).
    Returns the full knowledge/meta/layer1_index.yml content (UTF-8).
    """
    if not META_INDEX_PATH.exists():
        return _safe_json({"error": f"meta index not found: {META_INDEX_PATH}"})
    return META_INDEX_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_policy() -> str:
    """
    Load global Layer2 policy (strict JSON as text).
    Returns the content of knowledge/policy.json (UTF-8).
    """
    if not POLICY_PATH.exists():
        return _safe_json({"error": f"policy not found: {POLICY_PATH}"})
    return POLICY_PATH.read_text(encoding="utf-8")

@function_tool
def tool_get_uc_sections(uc_id: str, sections: List[str]) -> str:
    """
    Load specific sections from a UC YAML.
    Allowed sections include:
      param_spec, invariants, visualize_presets, allowed_patterns,
      ui_contracts, render_pattern, capabilities_required,
      capabilities_provided, checks.
    Returns JSON with only the requested sections, or {"error": "..."}.
    """
    try:
        import yaml  # lazy import (verhindert Start-Crash, falls PyYAML fehlt)
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
    """
    Load multiple component files and return a single concatenated string plus a manifest.
    Input: components -> relative repo paths, e.g. 'blocks/components/gee/aoi_from_spec.py'
    Rejects legacy/fs_*.
    Returns JSON:
      {
        "bundle": "<concatenated files with BEGIN/END headers>",
        "manifest": [{"id": path, "sha1": "...", "bytes": N}, ...]
      }
    """
    bundle_parts: List[str] = []
    manifest: List[Dict[str, Any]] = []

    for rel in components or []:
        p = (BASE_DIR / rel).resolve()
        # scope guard
        if not str(p).startswith(str(BASE_DIR)):
            return _safe_json({"error": f"component outside repo scope: {rel}"})
        if _is_legacy(p):
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

@function_tool
def tool_run_python(code: str,
                    filename: Optional[str] = None,
                    timeout_sec: int = 600,
                    mode: str = "script",
                    port: int = 8502) -> str:
    """
    Execute code inside runner/sandbox.
    - mode="script": python file.py (returns stdout/stderr)
    - mode="streamlit": streamlit run file.py --server.headless --server.port {port}
                        (returns url + pid + short bootstrap log)
    Hinweis: Auf Streamlit Cloud ist die zweite Streamlit-Instanz i. d. R. NICHT sichtbar.
    """
    if not filename:
        filename = "app_run.py"
    target = SANDBOX_DIR / filename
    target.write_text(code, encoding="utf-8")

    if mode == "script":
        try:
            proc = subprocess.run(
                ["python", str(target)],
                cwd=SANDBOX_DIR,
                capture_output=True,
                text=True,
                timeout=timeout_sec
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
                text=True
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
                "hint": "Auf Streamlit Cloud meist nicht sichtbar (zweite Instanz).",
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


# ===== NEU: UI-Vorschläge (Layer 1) — strict schema ===========================
class UISuggestion(TypedDict):
    id: str            # stabiler Key (snake_case)
    label: str         # Button-Text (max ~50 Zeichen)
    payload_json: str  # JSON-kodierter Payload (wir parsen ihn serverseitig)

# ---- NEU (LLM-first): strukturiertes Output-Schema nur für Python-Code ----
class PythonBlockOutput(BaseModel):
    code: str  # kompletter, lauffähiger Python-Quelltext (ohne Erklärtext)

@function_tool
def ui_suggest(suggestions: List[UISuggestion], replace: bool = False) -> str:
    """
    Der Agent ruft dies früh in Layer 1 auf.

    Hinweise:
    - 'suggestions' ist eine Liste von Objekten {id, label, payload_json}.
    - 'payload_json' ist ein JSON-String; wird hier geparst und als 'payload' (dict) gespeichert.
    - Wenn 'replace' True ist, werden vorhandene Vorschläge ersetzt.
    """
    # Ersetzen oder anfügen
    if replace or "l1_suggestions" not in st.session_state:
        st.session_state["l1_suggestions"] = []

    normalized: List[Dict[str, Any]] = []
    for s in suggestions or []:
        try:
            sid = str(s["id"]).strip()
            label = str(s["label"]).strip()
            payload_json = str(s["payload_json"])
            if not sid or not label:
                continue
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                continue
            # Duplikate per id vermeiden
            if any(x.get("id") == sid for x in st.session_state["l1_suggestions"]):
                continue
            normalized.append({"id": sid, "label": label, "payload": payload})
        except Exception:
            continue

    st.session_state["l1_suggestions"].extend(normalized)
    return json.dumps({"received": len(normalized)}, ensure_ascii=False)

# ===== Async-Loop-Sicherung (Fix für Runner.run_sync in Streamlit-Thread) =====
def ensure_event_loop() -> None:
    """
    Stellt sicher, dass im aktuellen Thread ein asyncio-Event-Loop vorhanden ist.
    Notwendig, weil Streamlit Code in einem ScriptRunner-Thread ohne Default-Loop ausführt.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

# ===== Agent Setup + echte SDK-Session ========================================
if AGENTS_OK:
    openai_client = AsyncOpenAI()  # liest OPENAI_API_KEY
    agent = Agent(
        name="EO-Agent",
        instructions=MEGA_PROMPT,
        tools=[ui_suggest,  # <--- NEU
               tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components, tool_run_python],
        model=OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client),
        # Hinweis: Structured Outputs (plan_spec/python_code) werden unten robust abgegriffen,
        # selbst wenn das Modell sie in den Text schreibt.
    )

# ===== Streamlit: echtes Chat-Interface ======================================
st.set_page_config(page_title="talk2earth — EO Agent", layout="wide")
st.title("talk2earth — EO Agent (Agents SDK + Streamlit)")

# >>> NEU: Sichtbarer App-Ausgabebereich (ersetzt sich selbst)
if "app_ph" not in st.session_state:
    st.session_state["app_ph"] = st.empty()

with st.sidebar:
    st.subheader("Status")
    st.write("Agents SDK:", "✅ bereit" if AGENTS_OK else f"❌ {AGENTS_IMPORT_ERROR}")
    st.write("OPENAI_API_KEY gesetzt:", "✅" if os.environ.get("OPENAI_API_KEY") else "❌")
    st.divider()
    st.subheader("Knowledge (debug)")
    try:
        ucs = _ls_usecase_ids()
        st.caption("Use-Cases: " + (", ".join(ucs) if ucs else "—"))
        st.caption("Meta-Index: " + (_repo_rel(META_INDEX_PATH) if META_INDEX_PATH.exists() else "not found"))
        st.caption("Policy: " + (_repo_rel(POLICY_PATH) if POLICY_PATH.exists() else "not found"))
    except Exception as e:
        st.warning(f"Debug-Auflistung fehlgeschlagen: {e}")
    st.divider()
    # ---- NEU: PLAN_SPEC Capture-Status
    plan_present = bool(st.session_state.get("last_plan_spec"))
    st.caption(f"PLAN_SPEC captured: {'✅' if plan_present else '—'}")
    if plan_present and st.checkbox("PlanSpec (debug) anzeigen"):
        st.json(st.session_state.get("last_plan_spec"))

    # >>> NEU: Code nur auf Wunsch sichtbar machen
    if st.button("Code anzeigen", type="secondary", help="Zeigt den aktuell erzeugten (ggf. reparierten) Code im Chat."):
        code_to_show = st.session_state.get("healed_code") or st.session_state.get("last_code")
        if code_to_show:
            st.session_state["show_code"] = True
            st.session_state.messages.append({"role": "assistant", "content": f"```python\n{code_to_show}\n```"})
            st.rerun()

    st.caption("Hinweis: AOI/Zeitraum/Parameter werden im Dialog geklärt; der Agent bündelt Komponenten vor dem Code.")

# Chat-Speicher (UI) — unabhängig von der SDK-Session
if "messages" not in st.session_state:
    st.session_state.messages = []  # [{"role":"user"/"assistant","content":str}]
if "last_code" not in st.session_state:
    st.session_state.last_code = ""
if "agent_session_id" not in st.session_state:
    # stabile ID pro Browser-Session
    st.session_state.agent_session_id = uuid.uuid4().hex
# Neu: PlanSpec-Speicher
if "last_plan_spec" not in st.session_state:
    st.session_state.last_plan_spec = None
if "last_plan_spec_raw" not in st.session_state:
    st.session_state.last_plan_spec_raw = ""
# --- NEU: Vorschlags-UI (Layer 1) ---
if "l1_suggestions" not in st.session_state:
    st.session_state["l1_suggestions"] = []          # wird vom Tool gefüllt
if "queued_input" not in st.session_state:
    st.session_state["queued_input"] = None          # nächste "synthetische" User-Eingabe
if "queued_label" not in st.session_state:
    st.session_state["queued_label"] = None          # Anzeige-Label für die Auswahl

# SDK-Session herstellen (persistentes Gedächtnis via SQLite)
if AGENTS_OK:
    try:
        SESSIONS_DB = str((RUNNER_DIR / "sessions.db").resolve())
        sdk_session = SQLiteSession(st.session_state.agent_session_id, SESSIONS_DB)
        with st.sidebar:
            st.caption(f"Session: {st.session_state.agent_session_id[:8]}… (SQLite @ {SESSIONS_DB})")
    except Exception as e:
        sdk_session = SQLiteSession(st.session_state.agent_session_id)  # in-memory fallback
        with st.sidebar:
            st.warning(f"SDK-Session init: Fallback in-memory ({e})")
else:
    sdk_session = None  # type: ignore

# Verlauf (UI) rendern
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# ---- NEU: Vorschläge-Block (immer sichtbar, vor der Chat-Eingabe) -----------
chosen = render_l1_suggestions()
if chosen:
    # Klick wird zur nächsten User-Eingabe umgewandelt
    st.session_state["queued_input"] = "USE_SUGGESTION " + json.dumps(chosen.get("payload", {}), ensure_ascii=False)
    st.session_state["queued_label"] = chosen.get("label")
    st.rerun()

# ---- NEU: Chat-Eingabe (Queue zuerst, dann normales Eingabefeld) ------------
queued = st.session_state.pop("queued_input", None)
queued_label = st.session_state.pop("queued_label", None)

prompt = None
if queued:
    prompt = queued
else:
    prompt = st.chat_input("Nachricht an den Agenten eingeben und mit Enter senden…")

if prompt:
    # 1) User Nachricht anzeigen/speichern
    display_text = prompt
    if prompt.startswith("USE_SUGGESTION ") and queued_label:
        display_text = f"[Auswahl] {queued_label}"
    st.session_state.messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    # 2) Agent call mit Iterations-Injektion + echter SDK-Session
    if not AGENTS_OK:
        answer = "**Fehler:** Agents SDK nicht verfügbar. Bitte SDK installieren/konfigurieren und Server neu starten."
        raw_answer = answer
        result = None
    else:
        iteration_context = ""
        if st.session_state.last_code:
            iteration_context = f"\n\n[EXISTING_CODE_BEGIN]\n{st.session_state.last_code}\n[EXISTING_CODE_END]\n"
        history_note = ""
        if st.session_state.messages[:-1]:
            history_note = "\n\n[HISTORY NOTE] Continue this session; respond naturally and follow iteration rules.\n"

        # Event-Loop sicherstellen
        ensure_event_loop()

        # Alte Vorschläge ausblenden – neue wird das Tool setzen
        st.session_state["l1_suggestions"] = []

        # >>> Persistente SDK-Session übergeben (SQLiteSession)
        result = Runner.run_sync(
            agent,
            input=(prompt + history_note + iteration_context),
            session=sdk_session,  # type: ignore
            max_turns=DEFAULT_MAX_TURNS,  # <-- allow enough tool/LLM steps per run
        )

        raw_answer = result.final_output or ""
        answer = raw_answer

        # ---- NEU: Versuche, PLAN_SPEC zuerst direkt aus result-Objekt zu lesen
        plan_spec_obj = None
        try:
            # gängige Pfade in der Agents SDK (robust gegen Varianten)
            if hasattr(result, "outputs") and isinstance(result.outputs, dict) and result.outputs.get("plan_spec"):
                plan_spec_obj = result.outputs.get("plan_spec")
            elif hasattr(result, "named_outputs") and isinstance(result.named_outputs, dict) and result.named_outputs.get("plan_spec"):
                plan_spec_obj = result.named_outputs.get("plan_spec")
            elif hasattr(result, "get_output") and callable(result.get_output):
                plan_spec_obj = result.get_output("plan_spec")  # type: ignore
        except Exception:
            plan_spec_obj = None

        # Falls nicht im Result-Kanal: aus sichtbarem Text extrahieren & entfernen
        if plan_spec_obj is None:
            extracted, cleaned_text = _extract_plan_spec_from_text(raw_answer)
            if extracted:
                plan_spec_obj = extracted
                answer = cleaned_text  # UI-bereinigte Antwort
        # persistieren (debugbar, aber nicht in UI angezeigt)
        if plan_spec_obj is not None and _looks_like_plan_spec(plan_spec_obj):
            st.session_state.last_plan_spec = plan_spec_obj
            st.session_state.last_plan_spec_raw = json.dumps(plan_spec_obj, ensure_ascii=False, indent=2)

    # 3) Assistant-Antwort rendern — PLAN_SPEC ist ggf. schon entfernt
    with st.chat_message("assistant"):
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})

    # 4) Hidden execution pipeline: auto-heal, dann rendern (keine Code-Anzeige)
    code_block = extract_first_python_block(answer)
    if code_block:
        st.session_state.last_code = code_block  # nur für "Code anzeigen"
        ok, final_code, heal_log = self_heal_until_runs(code_block, max_rounds=3)
        if ok:
            st.session_state["healed_code"] = final_code
            # alten App-Output leeren und neue App rendern
            outlet = st.session_state.get("app_ph")
            if outlet:
                outlet.empty()
            ns: dict[str, object] = {
                "__name__": "__generated__", "st": st, "ee": ee, "geemap": geemap, "Map": Map
            }
            run_generated_code_visible(final_code, ns, outlet)
            with st.sidebar:
                st.caption("✅ Code automatisch repariert & ausgeführt.")
        else:
            # Keine Code-Anzeige – nur Logs, damit Fixer weiter iterieren kann
            with st.expander("Fehler beim automatischen Ausführen – Logs", expanded=True):
                st.write(heal_log)
            with st.sidebar:
                st.warning("Automatische Reparatur noch nicht erfolgreich.")

# ============================================================================
# >>>>>>>> SELF-HEALING: Prä-Exec-Sandbox, EE-Init-Detektor, Fixer-Agent <<<<<<
# ============================================================================
import sys as _sh_sys
import io as _sh_io
import tokenize as _sh_tokenize
import traceback as _sh_traceback
import contextlib as _sh_ctx
from typing import Set as _sh_Set, Optional as _sh_Optional, List as _sh_List, Tuple as _sh_Tuple

from geemap.foliumap import Map as _sh_Map
import geemap as _sh_geemap
import ee as _sh_ee


# --- Fixer-Code-Extraktion (unterstützt fenced ```python ... ``` und Rohtext) ---
_SH_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

def _sh_extract_code_block_or_raw(text: str) -> str:
    if not isinstance(text, str):
        return ""
    m = _SH_CODE_BLOCK_RE.search(text)
    return (m.group(1) if m else text).strip()


# 1) Tokenbasierter EE-Init-Detektor
_SH_FORBIDDEN_CALLS: _sh_Set[str] = {"Initialize", "Authenticate"}

def _sh_detect_forbidden_ee_usage(code_text: str) -> _sh_List[_sh_Tuple[int, str]]:
    findings: _sh_List[_sh_Tuple[int, str]] = []
    if not isinstance(code_text, str) or not code_text.strip():
        return [(0, "Leerer oder ungültiger Code-Text.")]

    aliases: _sh_Set[str] = {"ee"}

    try:
        tokens = list(_sh_tokenize.generate_tokens(_sh_io.StringIO(code_text).readline))
    except _sh_tokenize.TokenError as te:
        findings.append((0, f"Tokenisierungsfehler: {te}"))
        return findings

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == _sh_tokenize.NAME and tok.string == "import":
            j = i + 1
            while j < len(tokens) and tokens[j].type != _sh_tokenize.NEWLINE:
                if tokens[j].type == _sh_tokenize.NAME and tokens[j].string == "ee":
                    k = j + 1
                    alias = "ee"
                    if k < len(tokens) and tokens[k].type == _sh_tokenize.NAME and tokens[k].string == "as":
                        if k + 1 < len(tokens) and tokens[k + 1].type == _sh_tokenize.NAME:
                            alias = tokens[k + 1].string
                            j = k + 1
                    aliases.add(alias)
                j += 1
            i = j
            continue

        if tok.type == _sh_tokenize.NAME and tok.string == "from":
            j = i + 1
            if j < len(tokens) and tokens[j].type == _sh_tokenize.NAME and tokens[j].string == "ee":
                j += 1
                if j < len(tokens) and tokens[j].type == _sh_tokenize.NAME and tokens[j].string == "import":
                    j += 1
                    while j < len(tokens) and tokens[j].type != _sh_tokenize.NEWLINE:
                        if tokens[j].type == _sh_tokenize.NAME and tokens[j].string in _SH_FORBIDDEN_CALLS:
                            findings.append((tokens[j].start[0], f"Verbotener Direktimport aus ee: {tokens[j].string}"))
                        j += 1
            while j < len(tokens) and tokens[j].type != _sh_tokenize.NEWLINE:
                j += 1
            i = j
            continue

        i += 1

    i = 0
    while i < len(tokens) - 3:
        t0, t1, t2, t3 = tokens[i], tokens[i + 1], tokens[i + 2], tokens[i + 3]
        if (
            t0.type == _sh_tokenize.NAME and t0.string in aliases and
            t1.type == _sh_tokenize.OP and t1.string == "." and
            t2.type == _sh_tokenize.NAME and t2.string in _SH_FORBIDDEN_CALLS and
            t3.type == _sh_tokenize.OP and t3.string == "("
        ):
            findings.append((t2.start[0], f"Verbotener Aufruf: {t0.string}.{t2.string}(...)"))
        i += 1

    return findings

# 2) Null-Streamlit-Stub für unsichtbaren Testlauf
class _SH_NullStreamlit:
    def __init__(self):
        self.session_state = {}
    def __getattr__(self, name):
        if name == "cache_data":
            def _identity_decorator(*dargs, **dkwargs):
                def _wrap(func):
                    return func
                return _wrap
            return _identity_decorator
        def _noop(*args, **kwargs):
            return None
        return _noop

class _SH_TempStreamlitModule:
    def __init__(self, stub):
        self.stub = stub
        self._orig = None
        self._had = False
    def __enter__(self):
        self._had = 'streamlit' in _sh_sys.modules
        if self._had:
            self._orig = _sh_sys.modules['streamlit']
        _sh_sys.modules['streamlit'] = self.stub
        return self
    def __exit__(self, exc_type, exc, tb):
        if self._had:
            _sh_sys.modules['streamlit'] = self._orig
        else:
            try:
                del _sh_sys.modules['streamlit']
            except KeyError:
                pass

# 3) Unsichtbarer Testlauf (Sandbox)
def _sh_dry_run_code(code_text: str) -> _sh_Tuple[bool, str]:
    forbidden = _sh_detect_forbidden_ee_usage(code_text)
    if forbidden:
        lines = [f"Zeile {ln}: {reason}" for ln, reason in forbidden]
        return False, "Policy-Verstoß (EE-Init im Snippet erkannt):\n" + "\n".join(lines)

    null_st = _SH_NullStreamlit()
    ns = {
        "__name__": "__main__",
        "ee": _sh_ee,
        "geemap": _sh_geemap,
        "Map": _sh_Map,
        "st": null_st,
    }
    stdout_buf = _sh_io.StringIO()
    stderr_buf = _sh_io.StringIO()
    try:
        compiled = compile(code_text, "<dry-run>", "exec")
        with _SH_TempStreamlitModule(null_st), \
             _sh_ctx.redirect_stdout(stdout_buf), \
             _sh_ctx.redirect_stderr(stderr_buf):
            exec(compiled, ns, ns)
        logs = stdout_buf.getvalue()
        errs = stderr_buf.getvalue()
        combined = (logs + ("\n" + errs if errs else "")).strip()
        return True, combined
    except Exception:
        tb = _sh_traceback.format_exc()
        errs = stderr_buf.getvalue()
        logs = stdout_buf.getvalue()
        combined = (logs + ("\n" + errs if errs else "") + ("\n" + tb)).strip()
        return False, combined

# 4) Fixer-Agent (zweite Instanz) — Agents SDK an AGENTS_OK koppeln
if AGENTS_OK:
    from agents import Agent as _sh_Agent
    from agents.models.openai_responses import OpenAIResponsesModel as _sh_Model
else:
    _sh_Agent = None  # type: ignore
    _sh_Model = None  # type: ignore

_SH_FIXER_PROMPT = (
    "Du bist ein Korrektur-Agent. Deine einzige Aufgabe ist es, übergebenen Python-Code "
    "auf Basis des Fehlerprotokolls zu REPARIEREN, sodass er fehlerfrei läuft.\n"
    "Rahmenbedingungen:\n"
    "- Gib AUSSCHLIESSLICH den korrigierten, vollständigen Python-Code zurück – ohne Erklärungen.\n"
    "- Verwende nur die bereits vorhandenen Komponenten/Imports, nichts Erfinden.\n"
    "- Niemals ee.Initialize() oder ee.Authenticate() aufrufen. EE ist host-seitig initialisiert.\n"
    "- Behalte Funktionalität und Parameter bei, repariere nur Fehlerursachen.\n"
    "- Keine Platzhalter, keine Ellipsen, vollständiger, sofort lauffähiger Code."
)

def _sh_get_fixer_agent():
    if not AGENTS_OK:
        return None
    if "_fixer_agent" in st.session_state and st.session_state["_fixer_agent"] is not None:
        return st.session_state["_fixer_agent"]
    fixer_agent = _sh_Agent(  # type: ignore
        name="Fixer",
        model=_sh_Model(  # type: ignore
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_client=openai_client,  # reuse same OpenAI client as main agent
        ),
        instructions=_SH_FIXER_PROMPT,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],  # <-- NEU: gleiche Tools
        output_type=PythonBlockOutput,  # <-- NEU: strukturiert, nur Code
    )
    st.session_state["_fixer_agent"] = fixer_agent
    return fixer_agent

def _sh_fix_code_once(code_text: str, error_log: str) -> _sh_Optional[str]:
    # Streamlit-Thread hat oft keinen Default-Event-Loop → sicherstellen
    ensure_event_loop()

    fixer_agent = _sh_get_fixer_agent()
    if fixer_agent is None:
        return None

    user_payload = (
        "Repariere den folgenden Python-Code auf Basis dieses Fehlerlogs.\n"
        "Gib NUR den vollständigen, korrigierten Code zurück.\n\n"
        "=== FEHLERLOG ===\n"
        f"{error_log}\n\n"
        "=== CODE ===\n"
        f"{code_text}\n"
        "=== ENDE ==="
    )

    try:
        # Korrekte Agents-SDK-Nutzung: kein Runner-Konstruktor, sondern Runner.run_sync(agent, ...)
        res = Runner.run_sync(
            fixer_agent,
            input=user_payload,
            session=sdk_session,
            max_turns=DEFAULT_MAX_TURNS,
        )
        out = getattr(res, "final_output", None)
        # Structured Output bevorzugt
        if hasattr(out, "code") and isinstance(out.code, str) and out.code.strip():
            return out.code.strip()
        # Fallback (sollte selten vorkommen)
        if isinstance(out, str) and out.strip():
            return out.strip()
        return None
    except Exception:
        return None


# 5) Orchestrierung: iteriere bis lauffähig
def self_heal_until_runs(code_text: str, max_rounds: int = 3) -> _sh_Tuple[bool, str, str]:
    current = code_text
    attempts: _sh_List[str] = []
    for round_idx in range(1, max_rounds + 1):
        ok, logs = _sh_dry_run_code(current)
        attempts.append(f"[Runde {round_idx}] OK={ok}\n{logs}")
        if ok:
            return True, current, logs
        fixed = _sh_fix_code_once(current, logs)
        if not fixed:
            return False, current, "\n\n".join(attempts)
        current = fixed
    ok, logs = _sh_dry_run_code(current)
    attempts.append(f"[Runde {max_rounds+1}] OK={ok}\n{logs}")
    return (True, current, "\n\n".join(attempts)) if ok else (False, current, "\n\n".join(attempts))

# 6) Sichtbare Ausführung (nach bestandenem Dry-Run)
def run_generated_code_visible(code_text: str, ns: dict, outlet=None) -> None:
    compiled = compile(code_text, "<generated>", "exec")
    # Versuche, in einen bereitgestellten Outlet zu rendern; fallback auf globales st
    try:
        if outlet is not None and hasattr(outlet, "container"):
            with outlet.container():
                exec(compiled, ns, ns)
                return
    except Exception:
        pass
    exec(compiled, ns, ns)

# === Debug: Code nur anzeigen, wenn explizit gewünscht ========================
if st.session_state.get("show_code", False):
    shown = st.session_state.get("healed_code") or st.session_state.get("last_code")
    if shown:
        st.write("---")
        st.subheader("Code (Debug)")
        st.code(shown, language="python")
