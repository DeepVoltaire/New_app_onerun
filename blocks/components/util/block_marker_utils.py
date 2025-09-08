"""
Utilities for code block annotations:
- spec_hash(plan_spec): deterministic hash for planning spec objects
- build_block_index(code_str): scan "# region BLOCK ..."/"# endregion BLOCK ..." markers -> block metadata with line spans
- apply_patches(code_str, patches, strategy="body_only"): replace block bodies or whole blocks by block_id; update hash in region header
"""
from __future__ import annotations
import re, json, hashlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

REGION_START_RE = re.compile(r'^#\s*region\s+BLOCK\s+(?P<attrs>.+)$')
REGION_END_RE   = re.compile(r'^#\s*endregion\s+BLOCK\s+id=(?P<id>[\w\.\-]+)\s*$')

def spec_hash(plan_spec: Any) -> str:
    try:
        s = json.dumps(plan_spec, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = str(plan_spec)
    return hashlib.sha1((s + ":SCHEMA_V1").encode("utf-8")).hexdigest()[:8]

@dataclass
class BlockInfo:
    id: str
    start_line: int
    end_line: int
    header_line: int
    kind: Optional[str] = None
    phase: Optional[str] = None
    plan_ref: Optional[str] = None
    name: Optional[str] = None
    hash: Optional[str] = None
    modifiable: Optional[str] = None
    attrs: Dict[str, str] = None

def _parse_attrs(s: str) -> Dict[str, str]:
    """Parse key=value pairs; values may be quoted."""
    out: Dict[str, str] = {}
    tok_re = re.compile(r'(\w+)=(".*?"|\S+)')
    for m in tok_re.finditer(s.strip()):
        k = m.group(1)
        v = m.group(2)
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k] = v
    return out

def build_block_index(code_str: str) -> List[BlockInfo]:
    lines = code_str.splitlines()
    stack: List[Tuple[int, Dict[str, str]]] = []
    blocks: List[BlockInfo] = []
    for i, line in enumerate(lines, start=1):
        m1 = REGION_START_RE.match(line.rstrip())
        if m1:
            attrs = _parse_attrs(m1.group('attrs'))
            stack.append((i, attrs))
            continue
        m2 = REGION_END_RE.match(line.rstrip())
        if m2 and stack:
            start_line, attrs = stack.pop()
            bid = attrs.get("id") or m2.group("id")
            info = BlockInfo(
                id=bid,
                start_line=start_line,
                end_line=i,
                header_line=start_line,
                kind=attrs.get("kind"),
                phase=attrs.get("phase"),
                plan_ref=attrs.get("plan_ref"),
                name=attrs.get("name"),
                hash=attrs.get("hash"),
                modifiable=attrs.get("modifiable"),
                attrs=attrs
            )
            blocks.append(info)
    return blocks

def _compute_body_hash(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]

def apply_patches(code_str: str, patches: List[Dict[str, Any]], strategy: str = "body_only") -> Tuple[str, List[Dict[str, Any]]]:
    """Apply patches to code_str by block_id; update hash in region header."""
    lines = code_str.splitlines()
    blocks = build_block_index(code_str)
    by_id = {b.id: b for b in blocks}
    report: List[Dict[str, Any]] = []

    for p in patches:
        bid = p.get("block_id")
        new_code = p.get("new_code", "")
        old_hash = p.get("old_hash")

        info = by_id.get(bid)
        if not info:
            report.append({"block_id": bid, "status": "missing"})
            continue

        # Body lines (exclude header and footer)
        body_s = info.header_line     # 1-based header -> body starts next line => index = header_line
        body_e = info.end_line - 1    # 1-based endregion -> body ends line before
        body_text = "\n".join(lines[body_s:body_e])

        if old_hash and (info.hash and old_hash != info.hash):
            report.append({"block_id": bid, "status": "hash_mismatch", "expected": info.hash, "got": old_hash})

        new_hash = _compute_body_hash(new_code)

        if strategy == "body_only":
            new_lines = lines[:body_s] + new_code.splitlines() + lines[body_e:]
            lines = new_lines
            header = lines[info.header_line-1]
            header = re.sub(r'\bhash=[0-9a-fA-F]{8}\b', f'hash={new_hash}', header)
            lines[info.header_line-1] = header
        else:
            header = lines[info.header_line-1]
            footer = lines[info.end_line-1]
            header = re.sub(r'\bhash=[0-9a-fA-F]{8}\b', f'hash={new_hash}', header)
            new_region = [header] + new_code.splitlines() + [footer]
            lines = lines[:info.header_line-1] + new_region + lines[info.end_line:]

        report.append({"block_id": bid, "status": "patched", "new_hash": new_hash})

        # Rebuild index for subsequent patches
        code_str = "\n".join(lines)
        blocks = build_block_index(code_str)
        by_id = {b.id: b for b in blocks}

    return "\n".join(lines), report
