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
    from openai.types.responses import ResponseTextDeltaEvent
    openai_client = AsyncOpenAI() if AGENTS_OK else None
except Exception as e:
    AGENTS_OK = False
    AGENTS_IMPORT_ERROR = str(e)

# --- Defaults -----------------------------------------------------------------
agent0 = None            # type: ignore
builder_agent = None     # type: ignore
refactor_agent = None    # type: ignore

# ===== Prompt laden ===========================================================
def load_text_file(path: pathlib.Path, fallback: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return fallback

MEGA_PROMPT = load_text_file(
    PROMPTS_DIR / "mega_prompt.md",
    fallback="SYSTEM: Du bist ein Gesprächs-Agent für Mini-Apps (GEE-first, UI optional)."
)

# === Role Addenda =============================================================
AGENT0_ADDENDUM = """
[Rollen-Zusatz · Agent 0]
- Du bist nur für die sichtbare Gesprächsführung zuständig.
- Du gibst niemals Code oder JSON aus, sondern nur Markdown.
- Sobald die Stop-Kriterien erfüllt sind, leitest du an den Builder (Agent 1) weiter.
- Nach dem ersten erfolgreichen Build: alle Folge-Turns sind Refactor-Turns → Agent 2.
"""

BUILDER_ADDENDUM = """
[Rollen-Zusatz · Agent 1 (Builder)]
- Liefere ausschließlich UiPlanCode (Structured Output).
- plan_spec (JSON) → nur intern, niemals sichtbar.
- code (vollständige Python-Datei) → mit Block-Marker und Entry-Point.
- user_markdown → einziger sichtbarer Text.
"""

REFACTOR_ADDENDUM = """
[Rollen-Zusatz · Agent 2 (Refactor)]
- Führe Gespräch in Markdown sichtbar weiter.
- Liefere zusätzlich ausschließlich RefactorPatches (Structured Output).
- Patches ändern nur Block-Bodies; kein Vollcode.
- Nutzer sieht niemals Code oder JSON.
"""

# ===== Hilfsfunktionen ========================================================
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

# ===== Session-State Setup ====================================================
def _ensure_state():
    if "build_completed" not in st.session_state:
        st.session_state.build_completed = False
    if "refactor_mode" not in st.session_state:
        st.session_state.refactor_mode = False
    if "builder_context" not in st.session_state:
        st.session_state.builder_context = {
            "plan_spec": None,
            "block_index": None,
            "code": None,
            "components_manifest": None,
        }
    if "last_code" not in st.session_state:
        st.session_state.last_code = None
    if "_runner_autorun_done" not in st.session_state:
        st.session_state._runner_autorun_done = False

# ===== Agents Setup ===========================================================
if AGENTS_OK:
    _ensure_state()
    try:
        if "agent_session_id" not in st.session_state:
            st.session_state.agent_session_id = uuid.uuid4().hex
        SESSIONS_DB = str((RUNNER_DIR / "sessions.db").resolve())
        sdk_session = SQLiteSession(st.session_state.agent_session_id, SESSIONS_DB)
    except Exception:
        sdk_session = SQLiteSession(st.session_state.agent_session_id)
else:
    sdk_session = None  # type: ignore

if AGENTS_OK:
    _runner = Runner(session=sdk_session)
    _resp_model = OpenAIResponsesModel(model=os.environ.get("OPENAI_MODEL", "gpt-4o"), openai_client=openai_client)

    agent0 = Agent(
        name="Agent0-Conversational",
        instructions=MEGA_PROMPT + "\n\n" + AGENT0_ADDENDUM,
        model=_resp_model,
        client=openai_client,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
    )

    builder_agent = Agent(
        name="Agent1-Builder",
        instructions=MEGA_PROMPT + "\n\n" + BUILDER_ADDENDUM,
        model=_resp_model,
        client=openai_client,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        output_type=AgentOutputSchema("UiPlanCode", strict_json_schema=False),
    )

    refactor_agent = Agent(
        name="Agent2-Refactor",
        instructions=MEGA_PROMPT + "\n\n" + REFACTOR_ADDENDUM,
        model=_resp_model,
        client=openai_client,
        tools=[tool_get_meta, tool_get_policy, tool_get_uc_sections, tool_bundle_components],
        output_type=AgentOutputSchema("RefactorPatches", strict_json_schema=False),
    )

# ===== Preflight + Switch =====================================================
def preflight_and_switch(code_text: str) -> bool:
    ok, final_code, _ = self_heal_until_runs(code_text, max_rounds=5)
    if not ok:
        return False
    st.session_state.last_code = final_code
    st.session_state.builder_context["code"] = final_code
    st.session_state.build_completed = True
    st.session_state.refactor_mode = True
    st.session_state._runner_autorun_done = False
    return True

# ===== Chat-UI & Event-Loop ===================================================
_ensure_state()

user_text = st.session_state.get("queued_input") or st.chat_input("Was sollen wir bauen oder ändern?")

if user_text:
    st.session_state.queued_input = None

    # Agent 0: Gespräch (sichtbar)
    with st.chat_message("user"):
        st.markdown(user_text)
    streamed = ""
    resp_stream = _runner.run_streamed(agent0, user_text)
    with st.chat_message("assistant"):
        placeholder = st.empty()
        for ev in resp_stream.events():
            if getattr(ev, "type", "") == "ResponseTextDeltaEvent":
                streamed += ev.delta
                placeholder.markdown(streamed)

    # Erst-Build oder Refactor?
    if not st.session_state.build_completed:
        builder_out = _runner.run_sync(builder_agent, input=user_text, require_output_type="UiPlanCode")
        plan_spec = builder_out.get("plan_spec")
        code_out = builder_out.get("code")
        block_index = builder_out.get("block_index")
        components_manifest = builder_out.get("components_manifest")
        user_md = builder_out.get("user_markdown", "")

        st.session_state.builder_context = {
            "plan_spec": plan_spec,
            "block_index": block_index,
            "code": code_out,
            "components_manifest": components_manifest,
        }

        if user_md:
            with st.chat_message("assistant"):
                st.markdown(user_md)

        ok = preflight_and_switch(code_out)
        if not ok:
            with st.chat_message("assistant"):
                st.markdown("Ich behebe Laufzeitfehler im Hintergrund …")
    else:
        ctx = st.session_state.builder_context
        ref_in = {
            "user_change_request": user_text,
            "source_of_truth_code": ctx["code"],
            "block_index": ctx["block_index"],
            "plan_spec": ctx["plan_spec"],
            "components_manifest": ctx["components_manifest"],
        }
        patches_payload = _runner.run_sync(refactor_agent, input=json.dumps(ref_in), require_output_type="RefactorPatches")
        from blocks.components.util.block_marker_utils import apply_patches
        patched_code = apply_patches(ctx["code"], patches_payload.get("patches", []), strategy="body_only")
        st.session_state.builder_context["code"] = patched_code
        ok = preflight_and_switch(patched_code)
        if not ok:
            with st.chat_message("assistant"):
                st.markdown("Die Änderung führte zu Laufzeitfehlern — ich korrigiere das intern.")

# ===== Autorun ================================================================
if st.session_state.last_code and not st.session_state._runner_autorun_done:
    try:
        ns = {}
        compiled = compile(st.session_state.last_code, "<autorender>", "exec")
        exec(compiled, ns, ns)
        entry = None
        for cand in ("t2e_app", "render", "main"):
            if cand in ns and callable(ns[cand]):
                entry = ns[cand]; break
        if entry is None:
            raise RuntimeError("Kein Entry-Point gefunden.")
        import inspect
        if len(inspect.signature(entry).parameters) == 1:
            entry(st)
        else:
            entry()
        st.session_state._runner_autorun_done = True
        st.sidebar.success("App gestartet.")
    except Exception as e:
        st.sidebar.error(f"Autorender-Fehler: {e}")
