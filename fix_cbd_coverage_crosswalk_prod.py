#!/usr/bin/env python3
r"""STANDALONE fix for the Coverage/Benefit (Process 8) coverage narrative.

Resolves per-code coverage CORRECTLY from the real CBD grid via an embedded
CPT->benefit-category crosswalk. Everything is self-contained in THIS file:
the crosswalk, the coverage resolver, and the CBD API "tool call" all live here.
The only external dependency is Django (for reading/writing the same Postgres the
dashboard uses) — exactly like the other ``*_prod.py`` scripts.

Why this is needed
------------------
The CBD API returns a benefit-CATEGORY grid (rows keyed by ``descCode`` /
``descName`` with a per-plan ``covered`` Yes/No flag) — it carries NO CPT codes.
The old tool matched CPTs against ``descCode`` (a category id), so every code came
back "not found" and a prior backfill blanket-flipped them all to "Covered" —
wrongly marking genuinely not-covered codes (e.g. ``G2211``) as covered.

Correct determination (this script):
    CPT --(crosswalk)--> benefit category --(this claim's plan grid)--> covered?
      * category present, covered=Yes -> Covered
      * category present, covered=No  -> Not covered
      * no BH category / category not in the plan grid (E/M, G2211) -> "Not in CBD"
        (out of the behavioral-health CBD's scope; flagged for auditor review)

Coverage grids were never persisted, so this script RE-FETCHES each claim's grid
through the SAME MCP server the engine uses at runtime — it calls the
``cbd_coverage`` tool via ``uhc_execution_engine.mcp_client.mcp_invoke``. Auth
(the ``x-api-key`` + base URL) is read from the active ``McpServerConfig`` DB row
(or the ``MCP_SERVER_*`` env vars) exactly like production; there is NO token or
API URL to configure in this file. It then deterministically rewrites the
persisted narrative. NO LLM.

VERDICT-SAFE: rule matched/decision_type + the run verdict are untouched;
``final_status`` is recomputed and must not change or the claim is rolled back.
Claims that now contain a "Not covered" / "Not in CBD" code are flagged in the
output for auditor verdict review.

Idempotent via the v2 marker. ``--dry-run`` (default) previews; ``--apply`` writes.

Usage:
    python scripts/fix_cbd_coverage_crosswalk_prod.py --dry-run
    python scripts/fix_cbd_coverage_crosswalk_prod.py --apply
    python scripts/fix_cbd_coverage_crosswalk_prod.py --claim 25XJ87029800 --apply

Offline (no MCP network — supply grids yourself):
    python scripts/fix_cbd_coverage_crosswalk_prod.py --grid-file grids.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Tool served by the MCP server that returns the plan's raw CBD benefit grid.
CBD_MCP_TOOL = os.environ.get("CBD_MCP_TOOL", "cbd_coverage")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root(start: str) -> str:
    d = start
    for _ in range(6):
        if os.path.exists(os.path.join(d, "manage.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


REPO_ROOT = _find_repo_root(_HERE)
DEFAULT_WORKFLOW_ID = "7c476f09-5196-438f-b25e-9cc3c96eac97"

_PROD_ENV = {
    "APP_ENV": "prod",
    "DJANGO_SETTINGS_MODULE": "sop_backend.settings",
    "LLM_BACKEND": "none",
    "NO_LLM": "1",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

COVERAGE_SOP_TITLE = "Access Covered Benefit SOP"
TARGET_STEPS = {"3", "4", "5"}
_FIX_MARKER = "auditor-fix: cbd crosswalk-resolved (v3)"

COVERED = "Covered"
NOT_COVERED = "Not covered"
NOT_IN_CBD = "Not in CBD"

# ═══════════════════════════════════════════════════════════════════════════
#  EMBEDDED CPT/HCPCS -> CBD benefit-category crosswalk + coverage resolver
#  (self-contained copy of agent_tools/tools/cbd_crosswalk.py)
#  Handles MCP ``{"raw": "<json>"}`` (incl. truncated 32k) + fine/coarse grids.
# ═══════════════════════════════════════════════════════════════════════════
CAT_PSYCH_DIAG = "Psychiatric Diagnostic Evaluation"
CAT_PSYTX_30 = "Psychotherapy W/Patient 30 Minutes"
CAT_PSYTX_45 = "Psychotherapy W/Patient 45 Minutes"
CAT_PSYTX_60 = "Psychotherapy W/Patient 60 Minutes"
CAT_NEURO_1ST = "Neuropsychological Testing - First Hour"
CAT_NEURO_ADDL = "Neuropsychological Testing - Each Add'L Hour"
CAT_PSYTEST_1ST = "Psychological Test Admin - First 30 Min"
CAT_PSYTEST_ADDL = "Psychological Test Admin - Each Add'L 30 Min"
CAT_ABA_PROTOCOL = "Adaptive Behavior Treatment With Protocol Mod"
CAT_CASE_MGMT = "Case Management, Each 15 Minutes"

CPT_TO_CATEGORY: dict[str, str] = {
    "90791": CAT_PSYCH_DIAG, "90792": CAT_PSYCH_DIAG,
    "90832": CAT_PSYTX_30, "90833": CAT_PSYTX_30,
    "90834": CAT_PSYTX_45, "90836": CAT_PSYTX_45,
    "90837": CAT_PSYTX_60, "90838": CAT_PSYTX_60,
    "90846": CAT_PSYTX_45, "90847": CAT_PSYTX_45,
    "90853": CAT_PSYTX_60, "90863": CAT_PSYTX_30,
    "90785": CAT_PSYCH_DIAG, "96127": CAT_PSYTEST_1ST,
    "96132": CAT_NEURO_1ST, "96133": CAT_NEURO_ADDL,
    "96136": CAT_PSYTEST_1ST, "96137": CAT_PSYTEST_ADDL,
    "97155": CAT_ABA_PROTOCOL, "T1016": CAT_CASE_MGMT,
    "H0031": CAT_PSYCH_DIAG, "H0032": CAT_PSYCH_DIAG, "H0038": CAT_PSYTX_30,
    "H2011": CAT_PSYTX_30, "H2016": CAT_PSYTX_60, "S9480": CAT_PSYTX_60,
    "99492": CAT_PSYTX_60, "99494": CAT_PSYTX_30,
}

EXPLICIT_NOT_COVERED: dict[str, str] = {
    "G2211": "E/M visit-complexity add-on — Not Covered per CBD",
}
EXPLICIT_NOT_IN_CBD: dict[str, str] = {
    "G8431": "Quality measure / screening HCPCS — not a Covered Benefit Document benefit",
}
EM_OFFICE_CODES: set[str] = {
    "99201", "99202", "99203", "99204", "99205",
    "99211", "99212", "99213", "99214", "99215",
}
NON_BH_CODES: dict[str, str] = {
    **EXPLICIT_NOT_COVERED,
    **EXPLICIT_NOT_IN_CBD,
    **{c: "E/M office visit" for c in EM_OFFICE_CODES},
}
_CATEGORY_II_RE = re.compile(r"^\d{4}F$")
_FINE_CAT_FRAGMENTS = (
    "psychotherapywpatient", "psychiatricdiagnosticevaluation",
    "neuropsychologicaltesting", "psychologicaltestadmin", "adaptivebehavior",
)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def normalize_cpt(code: str | None) -> str:
    c = re.sub(r"[^A-Za-z0-9]", "", (code or "")).upper()
    if not c:
        return ""
    if c[0].isalpha():
        return c[:5]
    m = re.match(r"(\d{5})", c)
    return m.group(1) if m else c


def cpt_category(code: str | None) -> str | None:
    return CPT_TO_CATEGORY.get(normalize_cpt(code))


def is_category_ii(code: str | None) -> bool:
    return bool(_CATEGORY_II_RE.match(normalize_cpt(code)))


def _salvage_json_objects(raw: str) -> list[dict]:
    rows: list[dict] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start: i + 1])
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    rows.append(obj)
                start = None
    return rows


def _as_row_list(value: object) -> list[dict]:
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return _salvage_json_objects(value)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            return _grid_rows(parsed)
        return []
    return []


def _grid_rows(cbd_response: object) -> list[dict]:
    if isinstance(cbd_response, list):
        return [r for r in cbd_response if isinstance(r, dict)]
    if not isinstance(cbd_response, dict):
        return []
    if "response" in cbd_response and not cbd_response.get("data") and not cbd_response.get("raw"):
        inner = cbd_response.get("response")
        if isinstance(inner, dict) and "body" in inner:
            return _grid_rows(inner.get("body"))
        return _grid_rows(inner)
    if isinstance(cbd_response.get("data"), list):
        return [r for r in cbd_response["data"] if isinstance(r, dict)]
    if "raw" in cbd_response:
        rows = _as_row_list(cbd_response.get("raw"))
        if rows:
            return rows
    val = cbd_response.get("value")
    if isinstance(val, dict) and isinstance(val.get("data"), list):
        return [r for r in val["data"] if isinstance(r, dict)]
    if isinstance(val, (list, str)):
        rows = _as_row_list(val)
        if rows:
            return rows
    return []


def _row_covered(row: dict) -> bool | None:
    v = row.get("covered")
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"yes", "y", "true", "1", "covered"}:
        return True
    if s in {"no", "n", "false", "0", "not covered", "notcovered"}:
        return False
    return None


def _has_fine_taxonomy(rows: list[dict]) -> bool:
    for row in rows:
        n = _norm(row.get("descName"))
        if any(frag in n for frag in _FINE_CAT_FRAGMENTS):
            return True
        if str(row.get("descCode") or "").strip() in {
            "100", "101", "102", "103", "104", "105", "106", "107"}:
            return True
    return False


def _psych_facility_covered(rows: list[dict]) -> bool | None:
    flags: list[bool] = []
    for row in rows:
        n = _norm(row.get("descName"))
        if n.startswith("psychiatric") or "psychiatricclinic" in n:
            cov = _row_covered(row)
            if cov is not None:
                flags.append(cov)
    return any(flags) if flags else None


def _clinic_visit_covered(rows: list[dict]) -> bool | None:
    flags: list[bool] = []
    for row in rows:
        n = _norm(row.get("descName"))
        if n in {"clinic", "visitchargegeneral"} or n.startswith("clinicvisit") \
                or "psychiatricclinic" in n:
            cov = _row_covered(row)
            if cov is not None:
                flags.append(cov)
    return any(flags) if flags else None


def resolve_coverage(cbd_response: object, cpt_codes: list[str]) -> dict:
    """Per-CPT coverage from a raw CBD grid (fine or coarse / MCP raw)."""
    if isinstance(cbd_response, dict):
        pre = cbd_response.get("coverage_details")
        if isinstance(pre, list) and pre and any(
                isinstance(d, dict) and d.get("cpt_code") for d in pre):
            determ: dict[str, str] = {}
            details: list[dict] = []
            not_found: list[str] = []
            found: set[str] = set()
            by_code = {
                normalize_cpt(d.get("cpt_code")): d
                for d in pre if isinstance(d, dict) and d.get("cpt_code")
            }
            for raw in cpt_codes:
                base = normalize_cpt(raw)
                if base in EXPLICIT_NOT_COVERED:
                    determ[raw] = NOT_COVERED
                    details.append({"cpt_code": raw, "covered": "No",
                                    "disposition": NOT_COVERED, "desc_name": None,
                                    "service_type": None,
                                    "note": EXPLICIT_NOT_COVERED[base]})
                    continue
                if base in EXPLICIT_NOT_IN_CBD or is_category_ii(base):
                    note = EXPLICIT_NOT_IN_CBD.get(
                        base, "CPT Category II quality-measure code — not a CBD benefit")
                    determ[raw] = NOT_IN_CBD
                    not_found.append(raw)
                    details.append({"cpt_code": raw, "covered": "Unknown",
                                    "disposition": NOT_IN_CBD, "desc_name": None,
                                    "service_type": None, "note": note})
                    continue
                d = by_code.get(base)
                if not d:
                    determ[raw] = NOT_IN_CBD
                    not_found.append(raw)
                    details.append({"cpt_code": raw, "covered": "Unknown",
                                    "disposition": NOT_IN_CBD, "desc_name": None,
                                    "service_type": None})
                    continue
                is_cov = str(d.get("covered") or "").strip().lower() in {
                    "yes", "y", "true", "1", "covered"}
                determ[raw] = COVERED if is_cov else NOT_COVERED
                found.add(base)
                details.append({"cpt_code": raw,
                                "covered": "Yes" if is_cov else "No",
                                "disposition": determ[raw],
                                "authorization": str(d.get("authorization") or "Unknown"),
                                "desc_name": d.get("desc_name") or d.get("descName"),
                                "service_type": d.get("service_type") or d.get("serviceType")})
            return {"coverage_details": details, "not_found_codes": not_found,
                    "codes_found": len(found), "determinations": determ}

    rows = _grid_rows(cbd_response)
    idx: dict[str, dict] = {}
    for row in rows:
        key = _norm(row.get("descName"))
        if not key:
            continue
        prev = idx.get(key)
        if prev is None:
            idx[key] = row
        elif _row_covered(prev) is not True and _row_covered(row) is True:
            idx[key] = row
    fine = _has_fine_taxonomy(rows)
    psych_cov = _psych_facility_covered(rows)
    clinic_cov = _clinic_visit_covered(rows)

    details = []
    not_found: list[str] = []
    determ: dict[str, str] = {}
    for raw in cpt_codes:
        base = normalize_cpt(raw)
        if base in EXPLICIT_NOT_COVERED:
            determ[raw] = NOT_COVERED
            details.append({"cpt_code": raw, "covered": "No", "disposition": NOT_COVERED,
                            "desc_name": None, "service_type": None,
                            "note": EXPLICIT_NOT_COVERED[base]})
            continue
        if base in EXPLICIT_NOT_IN_CBD or is_category_ii(base):
            note = EXPLICIT_NOT_IN_CBD.get(
                base, "CPT Category II quality-measure code — not a CBD benefit")
            determ[raw] = NOT_IN_CBD
            not_found.append(raw)
            details.append({"cpt_code": raw, "covered": "Unknown",
                            "disposition": NOT_IN_CBD, "desc_name": None,
                            "service_type": None, "note": note})
            continue
        cat = CPT_TO_CATEGORY.get(base)
        if cat and fine:
            row = idx.get(_norm(cat))
            if row is not None:
                cov = _row_covered(row)
                is_cov = True if cov is None else cov
                determ[raw] = COVERED if is_cov else NOT_COVERED
                details.append({"cpt_code": raw, "covered": "Yes" if is_cov else "No",
                                "disposition": determ[raw],
                                "authorization": str(row.get("authorization") or "Unknown"),
                                "desc_name": row.get("descName") or cat,
                                "service_type": row.get("serviceType")})
                continue
        if cat and psych_cov is not None:
            determ[raw] = COVERED if psych_cov else NOT_COVERED
            details.append({"cpt_code": raw, "covered": "Yes" if psych_cov else "No",
                            "disposition": determ[raw], "authorization": "Unknown",
                            "desc_name": "Psychiatric clinic" if psych_cov else cat,
                            "service_type": None,
                            "note": "resolved via coarse Psychiatric* facility categories"})
            continue
        if base in EM_OFFICE_CODES:
            flag = clinic_cov if clinic_cov is not None else psych_cov
            if flag is not None:
                determ[raw] = COVERED if flag else NOT_COVERED
                details.append({"cpt_code": raw, "covered": "Yes" if flag else "No",
                                "disposition": determ[raw], "authorization": "Unknown",
                                "desc_name": "Clinic / Visit charge", "service_type": None,
                                "note": "E/M office visit resolved via clinic/visit benefits"})
                continue
        if cat:
            determ[raw] = NOT_IN_CBD
            not_found.append(raw)
            details.append({"cpt_code": raw, "covered": "Unknown", "disposition": NOT_IN_CBD,
                            "desc_name": cat, "service_type": None,
                            "note": f"benefit category '{cat}' not present in this plan's CBD grid"})
            continue
        determ[raw] = NOT_IN_CBD
        not_found.append(raw)
        details.append({"cpt_code": raw, "covered": "Unknown", "disposition": NOT_IN_CBD,
                        "desc_name": None, "service_type": None,
                        "note": "no behavioral-health benefit category maps to this code"})
    return {"coverage_details": details, "not_found_codes": not_found,
            "codes_found": len([d for d in details if d.get("disposition") in (COVERED, NOT_COVERED)]),
            "determinations": determ}


# ═══════════════════════════════════════════════════════════════════════════
#  CBD "TOOL CALL" — routed through the MCP server (no auth handled here)
#  Uses the engine's mcp_client, so base_url + x-api-key come from the active
#  McpServerConfig DB row / MCP_SERVER_* env — identical to the runtime path.
# ═══════════════════════════════════════════════════════════════════════════
def _fetch_cbd_grid(claim_id: str) -> object:
    """THE TOOL CALL: fetch the plan's raw CBD benefit grid from the MCP server."""
    # Prefer the claims-mock MCP when CLAIMS_MOCK_BASE_URL is set (local/dev).
    # Env wins over the McpServerConfig DB row inside mcp_client.
    mock = os.environ.get("CLAIMS_MOCK_BASE_URL", "").strip()
    if mock and not os.environ.get("MCP_SERVER_BASE_URL", "").strip():
        os.environ["MCP_SERVER_BASE_URL"] = mock
        os.environ.setdefault("MCP_SERVER_AUTH_HEADER", "x-api-key")

    from uhc_execution_engine.mcp_client import (
        active_config_source, mcp_invoke, reset_cache,
    )
    reset_cache()

    out = mcp_invoke(CBD_MCP_TOOL, {"claim_number": claim_id})
    if out is None:
        raise RuntimeError(
            f"MCP not configured for '{CBD_MCP_TOOL}'. Ensure an active "
            "McpServerConfig row exists (or set MCP_SERVER_BASE_URL / "
            f"MCP_SERVER_API_KEY) and the tool has an mcp_path. "
            f"(config source={active_config_source()})")
    if not out.get("ok"):
        raise RuntimeError(f"MCP {CBD_MCP_TOOL} failed: {out.get('error')}")
    return out.get("result")


# ═══════════════════════════════════════════════════════════════════════════
#  Narrative rewrite helpers
# ═══════════════════════════════════════════════════════════════════════════
_CPT_DET_RE = re.compile(
    r"(CPT\s+)([A-Z0-9]+)(\s*[\u2014\-]\s*)"
    r"(Not covered|Not found|Not in CBD|Covered)",
    re.IGNORECASE,
)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _phrase(det: dict[str, str]) -> str:
    return "; ".join(f"CPT {c} \u2014 {v}" for c, v in det.items())


def _disposition_note(det: dict[str, str]) -> str:
    cov = [c for c, v in det.items() if v == COVERED]
    ncov = [c for c, v in det.items() if v == NOT_COVERED]
    ncbd = [c for c, v in det.items() if v == NOT_IN_CBD]
    parts = []
    if cov:
        parts.append(f"{len(cov)} covered")
    if ncov:
        parts.append(f"{len(ncov)} not covered")
    if ncbd:
        parts.append(f"{len(ncbd)} out of behavioral-health CBD scope (review)")
    return ", ".join(parts) or "no procedure codes"


def _reason_for(step: str, det: dict[str, str]) -> str:
    phrase = _phrase(det)
    summary = _disposition_note(det)
    if step == "3":
        body = (
            "The Covered Benefit Document (CBD) coverage tool completed "
            "successfully and returned the plan's benefit grid. Each procedure "
            "code was mapped to its Covered Benefit Document benefit category and "
            "resolved against this plan's coverage grid. Per-code determinations: "
            f"{phrase}. Summary: {summary}. Codes with no behavioral-health benefit "
            "category (e.g. E/M office visits, the G2211 add-on) are out of the "
            "behavioral-health CBD's scope and are reported for review rather than "
            "covered.")
    elif step == "4":
        body = (
            "Procedure code(s) and plan description are available from the FACETS "
            "tools, and Covered Benefit Document (CBD) coverage results are "
            "available for all procedure code(s). Coverage determinations resolved "
            f"from the plan's CBD benefit grid: {phrase}. Summary: {summary}.")
    else:  # "5"
        body = (
            "Claim identifiers and plan context are available, and categorized "
            "procedure code(s) with coverage outcomes resolved from the Covered "
            f"Benefit Document (CBD) benefit grid: {phrase}. Summary: {summary}. "
            "The audit summary inputs are prepared.")
    return f"{body} [{_FIX_MARKER}]"


def _norm_code(code: str) -> str:
    return normalize_cpt(code)


def _retrue_str(s: str, det: dict[str, str], det_norm: dict[str, str]) -> str:
    if not s:
        return s

    def repl(m: re.Match) -> str:
        code = m.group(2)
        true = det.get(code) or det.get(code.upper()) or det_norm.get(_norm_code(code))
        return m.group(1) + m.group(2) + m.group(3) + (true or m.group(4))

    return _CPT_DET_RE.sub(repl, s)


def _deep_retrue(obj, det, det_norm):
    if isinstance(obj, str):
        return _retrue_str(obj, det, det_norm)
    if isinstance(obj, list):
        return [_deep_retrue(v, det, det_norm) for v in obj]
    if isinstance(obj, dict):
        return {k: _deep_retrue(v, det, det_norm) for k, v in obj.items()}
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Standalone: correctly resolve Coverage/Benefit per-code "
        "coverage from the CBD grid via the CPT->category crosswalk. Verdict-safe.")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[],
                    help="Only these claim id(s) (repeatable).")
    ap.add_argument("--limit", type=int, default=0, help="Cap claims (0 = all).")
    ap.add_argument("--grid-file", default="",
                    help="JSON {claim_id|plan_name: raw CBD grid} for OFFLINE runs "
                         "(no CBD network call).")
    ap.add_argument("--force", action="store_true",
                    help="Re-process runs already carrying the v2 marker.")
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    ap.add_argument("--apply", action="store_true", help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)
    os.environ["NO_LLM"] = "1"
    os.environ["LLM_BACKEND"] = "none"
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    import django
    django.setup()

    from django.db import transaction
    from execution_app import trace_builder
    from execution_app.models import (ClaimTrace, RuleEvaluation,
                                       RuleExecutionRun, ToolInvocationRecord)
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditSop

    grids: dict = {}
    if opts.grid_file:
        with open(opts.grid_file) as fh:
            grids = json.load(fh)

    _p("── Coverage/Benefit: crosswalk-resolved per-code coverage (v3, standalone) ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")
    if grids:
        _p("  coverage    = grid-file (offline)")
    else:
        from uhc_execution_engine.mcp_client import active_config_source
        _p(f"  coverage    = MCP re-fetch via '{CBD_MCP_TOOL}' "
           f"(config source={active_config_source()})")

    cov_sop_ids = {
        s.id for s in AuditSop.objects.filter(title=COVERAGE_SOP_TITLE).only("id")
    }
    _p(f"  Coverage SOP id(s) = {sorted(cov_sop_ids)}")
    cov_key_re = re.compile(
        r"^step:(" + "|".join(str(i) for i in sorted(cov_sop_ids)) + r"):"
    ) if cov_sop_ids else None

    def _is_cov_entry(entry: dict) -> bool:
        return entry.get("sop_name") == COVERAGE_SOP_TITLE

    def _queried_codes_and_plan(run) -> tuple[list[str], str, str]:
        rows = ToolInvocationRecord.objects.filter(
            run=run, tool_name="check_medicare_coverage")
        codes: list[str] = []
        group = plan = ""
        for r in rows:
            res = r.result if isinstance(r.result, dict) else {}
            for c in (res.get("not_found_codes") or []):
                if c not in codes:
                    codes.append(c)
            for d in (res.get("coverage_details") or []):
                cc = d.get("cpt_code")
                if cc and cc not in codes:
                    codes.append(cc)
            group = group or (res.get("group_name") or "")
            plan = plan or (res.get("plan_name") or "")
        return codes, group, plan

    def _determinations(run) -> dict[str, str] | None:
        codes, group, plan = _queried_codes_and_plan(run)
        if not codes:
            return None
        if grids:
            grid = grids.get(run.claim_id) or grids.get(plan) or grids.get(group)
            if grid is None:
                return None
        else:
            grid = _fetch_cbd_grid(run.claim_id)
        return dict(resolve_coverage(grid, codes)["determinations"])

    def _apply_one(run) -> str:
        det = _determinations(run)
        if not det:
            return "SKIP: could not resolve coverage (no CBD invocation / grid)"
        det_norm = {_norm_code(c): v for c, v in det.items()}

        ct = ClaimTrace.objects.filter(run=run).first()
        prev_status = ct.final_status if ct else None

        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if not _is_cov_entry(entry):
                    continue
                step = str(entry.get("sop_step_number"))
                if step in TARGET_STEPS:
                    entry["rationale"] = _reason_for(step, det)
                else:
                    entry["rationale"] = _retrue_str(
                        entry.get("rationale") or "", det, det_norm)
                if entry.get("evidence_refs") is not None:
                    entry["evidence_refs"] = _deep_retrue(
                        entry["evidence_refs"], det, det_norm)
                if entry.get("subrule_results") is not None:
                    for sr in entry["subrule_results"]:
                        if isinstance(sr, dict) and sr.get("statement"):
                            sr["statement"] = _retrue_str(sr["statement"], det, det_norm)
            if not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                if ct.final_status != prev_status:
                    raise RuntimeError(
                        f"refusing: final_status changed {prev_status} -> {ct.final_status}")
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run)
                ct.save(update_fields=[
                    "trace_json", "explainability_json", "final_status", "updated_at"])

        for ev in RuleEvaluation.objects.filter(run=run):
            if not (cov_key_re and cov_key_re.match(ev.rule_key or "")):
                continue
            step = ev.rule_key.split(":")[2]
            new_reason = (_reason_for(step, det) if step in TARGET_STEPS
                          else _retrue_str(ev.reasoning or "", det, det_norm))
            if new_reason != (ev.reasoning or "") and not dry:
                ev.reasoning = new_reason
                ev.save(update_fields=["reasoning"])

        flags = [f"{c}={v}" for c, v in det.items() if v != COVERED]
        tag = f"  [REVIEW: {', '.join(flags)}]" if flags else ""
        return f"{_phrase(det)}{tag}"

    def _needs(run) -> bool:
        if opts.force:
            return True
        for ev in RuleEvaluation.objects.filter(run=run, rule_key__startswith="step:"):
            if cov_key_re and cov_key_re.match(ev.rule_key or "") and \
                    _FIX_MARKER in (ev.reasoning or ""):
                return False
        return RuleEvaluation.objects.filter(
            run=run, rule_key__regex=cov_key_re.pattern).exists() if cov_key_re else False

    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
            "claim_id", "-started_at"):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    wanted = {c.strip() for c in opts.claim if c.strip()}
    if wanted:
        latest = {c: r for c, r in latest.items() if c in wanted}

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Scan {total} claim(s) ══")

    fixed = skipped = already = failed = review = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            if not _needs(run):
                already += 1
                continue
            if dry:
                det = _determinations(run)
                if not det:
                    skipped += 1
                    _p(f"[{i}/{total}] claim={cid} SKIP (no coverage resolvable)")
                    continue
                flags = [f"{c}={v}" for c, v in det.items() if v != COVERED]
                if flags:
                    review += 1
                _p(f"[{i}/{total}] claim={cid} [WOULD FIX] {_phrase(det)}"
                   + (f"  [REVIEW: {', '.join(flags)}]" if flags else ""))
                fixed += 1
            else:
                with transaction.atomic():
                    msg = _apply_one(run)
                if msg.startswith("SKIP"):
                    skipped += 1
                    _p(f"[{i}/{total}] claim={cid} {msg}")
                else:
                    fixed += 1
                    if "[REVIEW" in msg:
                        review += 1
                    _p(f"[{i}/{total}] claim={cid} [FIXED] {msg}")
        except Exception as exc:  # pragma: no cover
            failed += 1
            _p(f"[{i}/{total}] claim={cid} run={run.id} FAILED: {exc}")
        if i % 25 == 0 or i == total:
            _p(f"PROGRESS {i}/{total}  fixed={fixed} already={already} "
               f"skipped={skipped} review={review} failed={failed}")

    _p("────────────────────────────────────────────────────────────")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  scanned                       = {total}")
    _p(f"  corrected (verdict unchanged) = {fixed}")
    _p(f"    of which flagged for review = {review}")
    _p(f"  already v2 / no coverage SOP  = {already}")
    _p(f"  unresolved (skipped)          = {skipped}")
    _p(f"  failed                        = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
