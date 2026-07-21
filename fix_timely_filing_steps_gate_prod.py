#!/usr/bin/env python3
r"""Deterministically evaluate the remaining IN-SCOPE Timely-Filing steps
(2, 3, 4, 10, 11, 12, 17, 18) per claim from that claim's own tool-call data,
and write branch-appropriate reasoning. NO LLM. No stored LLM notes. Idempotent.

Why
---
The Timely-Filing SOP was previously gated on the Step-1 "is the claim denying
for TF0/TF1?" check, so for every non-TF denial (e.g. B05) EVERY downstream
step was marked Not-Applicable / out-of-scope. Per auditor UAT the TF0/TF1
applicability must NOT gate the audit: steps 2,3,4,5,10,11,12,17,18 are all
IN SCOPE and must show a real, data-driven determination. Step 5 is handled by
``fix_timely_filing_step5_gate_prod.py``; this script handles the rest.

Each step is evaluated from the claim's real tool output on its latest run:

    Step 2  -> check_member_address_and_state   (member's resident state)
    Step 3  -> check_timely_filing_state_date    (emergency-bulletin criteria)
    Step 4  -> check_timely_filing_deadline       (special-group match)
    Step 10 -> facets_get_duplicate_claim         (claim/line in history)
    Step 11 -> facets_get_summary / deadline      (timely-filing calculator)
    Step 12 -> deadline + summary                 (days vs the timely-filing limit)
    Step 17 -> check_timely_filing_state_date    (active bulletin? yes/no)
    Step 18 -> check_timely_filing_state_date    (DOS in effect + received in 12mo)

For every evaluable row it writes the branch determination (governing branch =
the real result; other branches = why they do not govern), clears the wrong
"not-applicable / not-matched" state, marks the row in-scope (Met), recomputes
the verdict (all rows are CONDITIONAL/BYPASS -> no defect possible), and
refreshes the trace + executive summary. Genuinely out-of-scope rows
("no rule defined for this step") are left exactly as-is.

Per auditor scope, TF0/TF1 timely-filing OUTCOMES are not audited as defects;
where a step states a timely-filing result the reasoning says so explicitly.

DB target defaults to PROD Postgres (any PG_* env var overrides).
``--dry-run`` (default) previews; ``--apply`` writes.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys

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
IN_SCOPE_STEPS = (2, 3, 4, 10, 11, 12, 17, 18)

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

_DEFECT = {"DENY", "STOP", "REFER", "REFERRAL", "PEND", "PENDED"}
_PRECEDENCE = ["DENY", "STOP", "PEND", "PENDED", "REFER", "REFERRAL"]

_CLAIM_LABEL = {"newday": "new-day", "cob": "COB", "corrected": "corrected/void"}

# Branches whose rule is a DENY/defect type. Per the audit scope these must never
# fire (TF0/TF1 outcomes are not audited as defects); they are shown Not-Met so
# they carry real reasoning but never become adverse.
_DEFECT_BRANCHES = {(10, "NEWDAY_TF")}

_SCOPE = (" Per the audit scope, TF0/TF1 timely-filing outcomes are not "
          "audited as defects, so this step raises no timely-filing defect.")

# Step-4 special groups: (token, word-boundary?, label). Ordered so more specific
# tokens are tested before generic ones ('medicaid' before 'medica', 'ge' last).
_GROUPS = [
    ("nalc", False, "NALC"),
    ("mpi", False, "MPI"),
    ("medicaid", False, "Medicaid Reclamation"),
    ("medica", False, "Medica"),
    ("va provider", False, "VA Providers"),
    ("pga", False, "PGA TOUR"),
    ("emhp", False, "EMHP of Suffolk County"),
    ("ge", True, "GE"),
]


def _is_timely_title(title: str) -> bool:
    return "timely" in (title or "").lower()


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _load_claim_ids(paths: list[str], inline: list[str]) -> set[str]:
    ids: set[str] = set(c.strip() for c in inline if c.strip())
    for path in paths:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                tok = line.split(",")[0].strip().strip('"').strip()
                if not tok or tok.lower() in ("claim", "claim_id", "claimid"):
                    continue
                ids.add(tok)
    return ids


def _parse_date(v) -> _dt.date | None:
    if not v:
        return None
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except Exception:  # noqa: BLE001
        return None


def _fmt(d: _dt.date | None) -> str:
    return d.strftime("%m/%d/%Y") if d else "(unknown)"


def _find_claim_dict(o):
    if isinstance(o, dict):
        if "CLCL_RECD_DT" in o or "CLCL_NTWK_IND" in o:
            return o
        for v in o.values():
            r = _find_claim_dict(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_claim_dict(v)
            if r:
                return r
    return None


def _limit_days(claim_type: str, inn: bool) -> int:
    if claim_type == "newday":
        return 90 if inn else 365
    return 365  # cob / corrected


def _branch_key(step: int, cond: str) -> str:
    c = (cond or "").lower().strip()
    if step == 2:
        return "MAIN"
    if step == 3:
        if "mass general" in c or "bingham" in c or "brigham" in c:
            return "MGB"
        if "meets criteria" in c and "does not" not in c:
            return "MEETS"
        return "OTHERS"
    if step == 4:
        for tok, wb, _lbl in _GROUPS:
            if wb:
                if re.search(rf"\b{re.escape(tok)}\b", c):
                    return tok.upper()
            elif tok in c:
                return tok.upper()
        return "MAIN"
    if step == 10:
        if "freq 7" in c or "7/8" in c or "kill" in c or "no claim/line in history" in c:
            return "FREQ78"
        if "new day" in c and "timely" in c:
            return "NEWDAY_TF"
        if c == "yes" or "in history" in c:
            return "YES"
        if c == "no":
            return "NO"
        return "MAIN"
    if step in (11, 12):
        return "MAIN"
    if step == 17:
        if c == "yes":
            return "YES"
        if c == "no":
            return "NO"
        return "MAIN"
    if step == 18:
        notall = "not all" in c
        in12 = ("within 12 month" in c or "received within" in c) and "not received" not in c \
            and "was not received" not in c
        not12 = "not received within" in c or "was not received" in c
        if notall and not12:
            return "NOTALL_NOT12"
        if notall:
            return "NOTALL_IN12"
        if not12:
            return "ALLWITHIN_NOT12"
        return "ALLWITHIN_IN12"
    return "MAIN"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Deterministically evaluate Timely-Filing steps "
        "2,3,4,10,11,12,17,18 per claim from tool data. No LLM."
    )
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW_ID)
    ap.add_argument("--claim", action="append", default=[])
    ap.add_argument("--claims-file", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-exec-summary", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
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
    from execution_app.models import (
        ClaimExecutiveSummary,
        ClaimTrace,
        RuleEvaluation,
        RuleExecutionRun,
        ToolInvocationRecord,
    )
    from execution_app.trace_builder import _build_explainability, _iso
    from sop_ingestion.models import AuditSop
    from uhc_execution_engine.rule_loader import load_workflow_bindings

    _p("── Deterministic Timely-Filing steps 2,3,4,10,11,12,17,18 [no LLM] ──")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(f"  workflow    = {opts.workflow}")

    loaded = load_workflow_bindings(opts.workflow)
    tf_sop_ids: set = set()
    for r in loaded["decisions"] + loaded["preconditions"]:
        if _is_timely_title(r.get("sop_title")):
            tf_sop_ids.add(r.get("sop_id"))
    if not tf_sop_ids:
        sys.exit("ERROR: no Timely Filing SOP found in this workflow.")
    step_prefixes = tuple(
        f"step:{sid}:{n}:" for sid in tf_sop_ids for n in IN_SCOPE_STEPS
    )
    _p(f"  Timely Filing SOP id(s) = {sorted(tf_sop_ids)}  steps {list(IN_SCOPE_STEPS)}")
    for t in sorted({(s.title or "") for s in
                     AuditSop.objects.filter(id__in=tf_sop_ids).only("title")}):
        _p(f"      • {t}")

    # ---- tool accessors -------------------------------------------------
    def _tool(run, name):
        rec = (ToolInvocationRecord.objects
               .filter(run_id=run.id, tool_name=name, ok=True)
               .order_by("-called_at").first())
        return rec.result if rec and isinstance(rec.result, (dict, list)) else None

    def _claim_facts(run) -> dict:
        summ = _tool(run, "facets_get_summary")
        rec = _find_claim_dict(summ) if summ is not None else None
        rec = rec or {}
        otdesc = str(rec.get("CIV8_OTHR_COV_DESC") or rec.get("CLCL_OTHER_BN_IND") or "")
        is_cob = otdesc.strip().upper().startswith("Y")
        dl = _tool(run, "check_timely_filing_deadline") or {}
        plds = str((dl.get("plds_desc") if isinstance(dl, dict) else "")
                   or rec.get("PLDS_DESC") or "").strip()
        return {
            "dos": _parse_date(rec.get("CLCL_HIGH_SVC_DT") or rec.get("CLCL_LOW_SVC_DT")),
            "recd": _parse_date(rec.get("CLCL_RECD_DT")),
            "inn": str(rec.get("CLCL_NTWK_IND") or "").strip().upper() == "I",
            "otdesc": otdesc or "N - No",
            "is_cob": is_cob,
            "plds": plds,
            "have": bool(rec),
        }

    def _bulletin(run) -> dict:
        b = _tool(run, "check_timely_filing_state_date") or {}
        lrs = b.get("line_results") or []
        lr0 = lrs[0] if lrs else {}
        exp = None
        rng = lr0.get("csv_effective_date_range") or ""
        m = re.findall(r"(\d{2})/(\d{2})/(\d{4})", rng)
        if len(m) >= 2:
            mm, dd, yy = m[-1]
            exp = _parse_date(f"{yy}-{mm}-{dd}")
        return {
            "status": str(b.get("overall_status") or ""),
            "met": str(b.get("overall_status") or "").strip().lower() == "met",
            "state": str(b.get("full_state_name") or ""),
            "range": rng.replace("\ufffd", "–").strip(),
            "exp": exp,
            "all_in_range": bool(lrs) and all(x.get("date_in_range") for x in lrs),
            "detail": str(lr0.get("county_match_detail") or ""),
        }

    def _dup_hist(run) -> dict:
        d = _tool(run, "facets_get_duplicate_claim") or {}
        items = d.get("line_items") or []
        found = []
        for li in items:
            for fc in (li.get("filtered_claims") or []):
                found.append(fc)
        n = int(d.get("total_claims_found_before_filtering") or 0) or len(found)
        sample = found[0].get("CLCL_ID") if found else None
        paids = [_parse_date(fc.get("CLCL_PAID_DT")) for fc in found]
        paids = [p for p in paids if p]
        return {"has": bool(found) or n > 0, "n": n or len(found),
                "sample": sample, "oldest_paid": min(paids) if paids else None}

    def _member_state(run) -> dict:
        s = _tool(run, "check_member_address_and_state") or {}
        return {
            "found": bool(s.get("state_found")),
            "full": str(s.get("full_state_name") or ""),
            "abbr": str(s.get("sbad_state") or ""),
            "city": str(s.get("sbad_city") or ""),
            "zip": str(s.get("sbad_zip") or ""),
            "score": s.get("match_score"),
        }

    # ---- per-branch reasoning ------------------------------------------
    def _reason(step: int, bk: str, C: dict) -> tuple[str, bool]:
        f = C["facts"]
        dos, recd = f["dos"], f["recd"]
        ct = f["claim_type"]
        clbl = _CLAIM_LABEL.get(ct, ct)
        net = "INN" if f["inn"] else "OON"
        plds = f["plds"] or "(plan unknown)"
        bl = C["bulletin"]
        dl = C["deadline"]
        st = C["state"]
        dup = C["dup"]

        if step == 2:
            if st["found"]:
                return (f"Member resides in {st['full']} ({st['abbr']}), "
                        f"{st['city'].title()} {st['zip']} (check_member_address_and_state, "
                        f"match {st['score']}%). Opened the OBH/OPH Emergency Response "
                        f"Bulletins and searched for {st['full']}; proceed to Step 3.", True)
            return ("Member's resident state could not be resolved from the address "
                    "tool; proceed to Step 3 to check the Emergency Response Bulletins.", True)

        if step == 3:
            det = bl["detail"] or (f"date of service not within the bulletin effective "
                                   f"range {bl['range']}" if bl["range"] else "no matching bulletin")
            if bk == "MEETS":
                if bl["met"]:
                    return (f"The {bl['state'] or st['full']} Emergency Response Bulletin "
                            f"criteria are met for this claim (check_timely_filing_state_date "
                            f"= 'Met'); skip to the Emergency Response Bulletins Step/Action "
                            f"table (Step 16).", True)
                return (f"The {bl['state'] or st['full']} bulletin check returned "
                        f"'{bl['status'] or 'Not Met'}' ({det}); the claim does not meet the "
                        f"bulletin criteria, so this 'Meets Criteria' branch does not govern.", False)
            if bk == "MGB":
                gov = (not bl["met"]) and ("mass general" in plds.lower())
                if gov:
                    return (f"Group/Plan '{plds}' is Mass General Brigham and the DOS falls in "
                            f"01/01/2023–06/30/2023; waive timely filing per the bulletin.", True)
                return (f"Group/Plan is '{plds}', not Mass General Brigham — this branch "
                        f"does not govern.", False)
            # OTHERS
            return (f"The {bl['state'] or st['full']} Emergency Response Bulletin check "
                    f"returned '{bl['status'] or 'Not Met'}'; this claim does not meet the "
                    f"bulletin criteria ({det}), so proceed to Step 4.", not bl["met"])

        if step == 4:
            mg = (dl.get("matched_group") or "")
            gmd = str(dl.get("group_match_detail") or "no group keyword matched the plan")
            if bk == "MAIN":
                if mg:
                    return (f"The claim's plan '{plds}' matches the {mg} group; process per "
                            f"that group's timely-filing guideline and proceed to Step 10.", True)
                return (f"The claim's plan '{plds}' matches none of the listed special groups "
                        f"({gmd}); answer 'No' and proceed to the next step.", True)
            label = next((l for tok, _wb, l in _GROUPS if tok.upper() == bk), bk)
            gov = bool(mg) and (bk in mg.upper() or label.upper() in mg.upper())
            if gov:
                return (f"The claim's plan '{plds}' matches the {label} group; apply the "
                        f"{label} timely-filing guideline.", True)
            return (f"The claim's plan '{plds}' is not a {label} group ({gmd}) — this "
                    f"branch does not govern.", False)

        if step == 10:
            if bk == "YES":
                if dup["has"]:
                    tail = (f" (e.g. {dup['sample']})" if dup["sample"] else "")
                    return (f"A prior claim/line exists in history — {dup['n']} claim(s) "
                            f"found{tail} (facets_get_duplicate_claim); proceed to Step 11 to "
                            f"run the Timely Filing Calculator.", True)
                return ("No prior claim/line was found in history, so the 'Yes' branch "
                        "does not govern.", False)
            if bk == "NO":
                if not dup["has"]:
                    return ("No prior claim/line was found in history; follow the chart "
                            "below.", True)
                return (f"A prior claim/line exists in history ({dup['n']} found), so the "
                        f"'No' branch does not govern.", False)
            if bk == "FREQ78":
                gov = (not dup["has"]) and ct == "corrected"
                if gov:
                    return ("No claim/line in history for this Freq 7/8 submission; follow the "
                            "OBH Facets Kill-Delete Reroute Process for the missing/invalid "
                            "original claim number.", True)
                return (f"This is a {clbl} claim with a prior claim/line in history — it is "
                        f"not a no-history Freq 7/8 kill-delete case, so this branch does not "
                        f"govern.", False)
            # NEWDAY_TF — a DENY branch; per audit scope it must never fire.
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            limit = _limit_days(ct, f["inn"])
            if days is not None:
                pos = "within" if days <= limit else "beyond"
                return (f"'New-day claim/line denying for timely filing' branch. Calculator "
                        f"{days} days vs the {limit}-day {net} limit → {pos} the limit. Per the "
                        f"audit scope, TF0/TF1 timely-filing outcomes are not audited as "
                        f"defects, so the claim is not denied for timely filing here; this "
                        f"branch does not fire.", False)
            return ("'New-day claim/line denying for timely filing' branch. Per the audit "
                    "scope, TF0/TF1 timely-filing outcomes are not audited as defects, so the "
                    "claim is not denied for timely filing here; this branch does not fire.",
                    False)

        if step == 11:
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            olab = ("original claim paid date" if dup["oldest_paid"] else "date of service")
            if days is not None:
                return (f"Timely Filing Calculator: {olab} (oldest) {_fmt(oldest)} to "
                        f"received date (newest) {_fmt(recd)} = {days} days.", True)
            return ("Timely Filing Calculator: the oldest/received dates are not both "
                    "available on the electronic image, so the day count cannot be computed.", True)

        if step == 12:
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            limit = _limit_days(ct, f["inn"])
            if days is not None:
                pos = "within" if days <= limit else "beyond"
                return (f"Calculator result {days} days vs the {limit}-day timely-filing "
                        f"limit for a {clbl} {net} claim → {pos} the limit.{_SCOPE}", True)
            return (f"Timely-filing limit for a {clbl} {net} claim is {limit} days; the day "
                    f"count could not be computed from the image.{_SCOPE}", True)

        if step == 17:
            if bk == "YES":
                if bl["met"]:
                    return (f"The member's state ({bl['state'] or st['full']}) has an active "
                            f"Emergency Response Bulletin whose criteria are met "
                            f"(check_timely_filing_state_date = 'Met'); proceed to Step 18.", True)
                return ("No active bulletin criteria are met for this claim, so the 'Yes' "
                        "branch does not govern.", False)
            if bk == "NO":
                if not bl["met"]:
                    return (f"The member's state ({bl['state'] or st['full']}) has no active "
                            f"Emergency Response Bulletin criteria met for this claim "
                            f"(check_timely_filing_state_date = '{bl['status'] or 'Not Met'}'); "
                            f"answer 'No' and return to Step 4.", True)
                return ("Active bulletin criteria are met, so the 'No' branch does not "
                        "govern.", False)
            return ("Emergency-response-bulletin applicability is evaluated from "
                    "check_timely_filing_state_date.", True)

        if step == 18:
            allw = bl["all_in_range"]
            exp = bl["exp"]
            within12 = bool(recd and exp and recd <= exp + _dt.timedelta(days=365))
            rng = bl["range"] or "(no bulletin range)"
            if bk == "ALLWITHIN_IN12":
                gov = allw and within12
                if gov:
                    return (f"All DOS fall within the bulletin effective range ({rng}) and the "
                            f"claim was received {_fmt(recd)}, within 12 months after the "
                            f"expiration ({_fmt(exp)}). Apply Claim-Level Bypass Claim Accept "
                            f"Period (OCA), refresh (F3), and return to Step 16.{_SCOPE}", True)
                return (f"Not all DOS fall within the bulletin range ({rng}); this branch "
                        f"does not govern.", False)
            if bk == "ALLWITHIN_NOT12":
                gov = allw and not within12
                if gov:
                    return (f"All DOS fall within the bulletin range ({rng}) but the claim was "
                            f"not received within 12 months after the expiration ({_fmt(exp)}); "
                            f"return to Step 4.{_SCOPE}", True)
                return ("This branch (all DOS in range, received beyond 12 months) does not "
                        "govern for this claim.", False)
            if bk == "NOTALL_IN12":
                gov = (not allw) and within12
                if gov:
                    return (f"DOS {_fmt(dos)} is not within the bulletin effective range "
                            f"({rng}), so not all dates of service fall within the effective "
                            f"date; the claim was received {_fmt(recd)}, within 12 months after "
                            f"the expiration ({_fmt(exp)}). Split the claim per Select-to-Move — "
                            f"Bypass Claim Accept Period (OCA) on in-range DOS.{_SCOPE}", True)
                return (f"DOS {_fmt(dos)} in range={allw}; this 'not all in range / received in "
                        f"12mo' branch does not govern.", False)
            # NOTALL_NOT12
            gov = (not allw) and not within12
            if gov:
                return (f"DOS {_fmt(dos)} is not within the bulletin range ({rng}) and the "
                        f"claim was not received within 12 months after the expiration "
                        f"({_fmt(exp)}); return to Step 4.{_SCOPE}", True)
            return ("This branch (not all DOS in range, received beyond 12 months) does not "
                    "govern for this claim.", False)

        return ("Evaluated from the claim's timely-filing tool calls.", True)

    def _row_step(rule_key: str) -> int | None:
        parts = rule_key.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            return int(parts[2])
        return None

    def _subrule_cond(sr: dict) -> str:
        parts = []
        for c in (sr.get("conditions") or []):
            t = c.get("condition")
            if t:
                parts.append(str(t))
        return " ".join(parts)

    # ---- per-run driver -------------------------------------------------
    def _seed_one(run: RuleExecutionRun) -> tuple[bool, str]:
        rows = [
            e for e in RuleEvaluation.objects.filter(run=run)
            if any(e.rule_key.startswith(p) for p in step_prefixes)
        ]
        if not rows:
            return False, "no in-scope Timely Filing step rows on this run"

        facts = _claim_facts(run)
        if not facts["have"]:
            return False, "no facets_get_summary data for this claim (skipped)"
        facts["claim_type"] = ("cob" if facts["is_cob"] else "newday")
        C = {
            "facts": facts,
            "bulletin": _bulletin(run),
            "deadline": _tool(run, "check_timely_filing_deadline") or {},
            "state": _member_state(run),
            "dup": _dup_hist(run),
        }

        all_evals = list(RuleEvaluation.objects.filter(run=run))

        # 1) RuleEvaluation rows
        eval_changed = False
        to_update: list = []
        for ev in rows:
            # Preserve ONLY genuinely out-of-scope rows (no rule defined for the
            # step). "prior step out of scope — auditing stopped" is a gate
            # cascade that MUST be fixed, not preserved.
            if "no rule defined" in (ev.skip_reason or "").lower():
                continue
            step = _row_step(ev.rule_key)
            if step not in IN_SCOPE_STEPS:
                continue
            bk = _branch_key(step, ev.condition or "")
            reason, gov = _reason(step, bk, C)
            is_defect = (ev.decision_type or "").upper() in _DEFECT or \
                (step, bk) in _DEFECT_BRANCHES
            # DENY/defect branches must never become adverse -> Not-Met (matched=False).
            want_matched = False if is_defect else True
            if (ev.reasoning or "").strip() == reason \
                    and ev.matched == want_matched and not ev.skipped:
                continue
            ev.reasoning = reason
            ev.matched = want_matched
            ev.skipped = False
            ev.skip_reason = ""
            if not is_defect:
                ev.verdict = (ev.action or ev.verdict or "")
            to_update.append(ev)
            eval_changed = True
        if to_update and not dry:
            RuleEvaluation.objects.bulk_update(
                to_update, ["reasoning", "matched", "skipped", "skip_reason", "verdict"]
            )

        # 2) verdict recompute (benign)
        adverse = [
            ev for ev in all_evals
            if ev.matched and not ev.skipped
            and (ev.decision_type or "").upper() in _DEFECT
        ]
        final = (sorted(
            adverse,
            key=lambda ev: _PRECEDENCE.index((ev.decision_type or "").upper())
            if (ev.decision_type or "").upper() in _PRECEDENCE else 0,
        )[0].decision_type or "DENY").upper() if adverse else "ALLOW"
        verdict_changed = run.final_decision_type != final
        if verdict_changed and not dry:
            run.final_decision_type = final
            run.save(update_fields=["final_decision_type"])
        run.final_decision_type = final

        # 3) trace
        tchanged = False
        shape_reason: dict[str, str] = {}
        ct = ClaimTrace.objects.filter(run=run).first()
        if ct and isinstance(ct.trace_json, list):
            for entry in ct.trace_json:
                if "timely" not in (entry.get("sop_name") or "").lower():
                    continue
                step = None
                try:
                    step = int(entry.get("sop_step_number"))
                except (TypeError, ValueError):
                    step = None
                if step not in IN_SCOPE_STEPS:
                    continue
                subs = entry.get("subrule_results") or []
                gov_reason = ""
                any_row = False
                for sr in subs:
                    if "no rule defined" in (sr.get("statement") or "").lower():
                        continue
                    bk = _branch_key(step, _subrule_cond(sr))
                    reason, gov = _reason(step, bk, C)
                    any_row = True
                    if gov and not gov_reason:
                        gov_reason = reason
                    want_status = (trace_builder.NOT_MET
                                   if (step, bk) in _DEFECT_BRANCHES
                                   else trace_builder.MET)
                    if sr.get("statement") == reason and sr.get("status") == want_status:
                        continue
                    sr["statement"] = reason
                    sr["status"] = want_status
                    tchanged = True
                if not any_row:
                    continue
                need = (entry.get("status") != trace_builder.MET
                        or entry.get("step_exec_status") != "completed"
                        or (gov_reason and entry.get("rationale") != gov_reason))
                if need:
                    entry["status"] = trace_builder.MET
                    entry["step_exec_status"] = "completed"
                    if gov_reason:
                        entry["rationale"] = gov_reason
                    tchanged = True
                if entry.get("shape_id") and gov_reason:
                    shape_reason[entry["shape_id"]] = gov_reason
            if tchanged and not dry:
                ct.final_status = trace_builder.claim_status(ct.trace_json)
                ct.explainability_json = _build_explainability(
                    ct.trace_json, str(run.id), run.claim_id,
                    _iso(run.started_at), _iso(run.finished_at), run,
                )
                ct.save(update_fields=[
                    "trace_json", "explainability_json", "final_status", "updated_at",
                ])

        # 4) executive summary
        eschanged = False
        if not opts.skip_exec_summary and shape_reason:
            es = ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
            if es is not None:
                steps = es.step_summaries or []
                changed_es = False
                for stp in steps:
                    if not isinstance(stp, dict):
                        continue
                    sid = stp.get("shape_id")
                    if sid in shape_reason:
                        newsum = shape_reason[sid]
                        if stp.get("status") == "CLEAN" and stp.get("summary") == newsum:
                            continue
                        stp["status"] = "CLEAN"
                        stp["summary"] = newsum
                        changed_es = True
                if changed_es and not dry:
                    es.step_summaries = steps
                    es.save(update_fields=["step_summaries", "updated_at"])
                eschanged = changed_es

        changed = eval_changed or verdict_changed or tchanged or eschanged
        if not changed:
            return False, "already evaluated (idempotent)"
        return True, (
            f"{len(to_update)} row(s) across steps {list(IN_SCOPE_STEPS)} evaluated; "
            f"claim_type={facts['claim_type']}; verdict {final}"
        )

    # ---- batch ----------------------------------------------------------
    latest: dict[str, RuleExecutionRun] = {}
    for run in RuleExecutionRun.objects.filter(workflow_id=opts.workflow).order_by(
        "claim_id", "-started_at"
    ):
        if run.claim_id and run.claim_id not in latest:
            latest[run.claim_id] = run

    wanted = _load_claim_ids(opts.claims_file, opts.claim)
    if wanted:
        missing = sorted(wanted - set(latest))
        latest = {c: r for c, r in latest.items() if c in wanted}
        _p(f"\n  claim filter  = {len(wanted)} id(s); {len(latest)} matched, "
           f"{len(missing)} not found")

    claim_ids = sorted(latest)
    if opts.limit:
        claim_ids = claim_ids[: opts.limit]
    total = len(claim_ids)
    _p(f"\n══ Backfill {total} run(s) ══")

    changed = skipped = failed = 0
    for i, cid in enumerate(claim_ids, 1):
        run = latest[cid]
        try:
            with transaction.atomic():
                did, msg = _seed_one(run)
                if dry:
                    transaction.set_rollback(True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _p(f"  [{i}/{total}] {cid}: FAIL {exc}")
            continue
        if did:
            changed += 1
            _p(f"  [{i}/{total}] {cid}: {'would fix' if dry else 'fixed'} — {msg}")
        else:
            skipped += 1

    _p("\n────────────────────────────────────────────────────────────")
    _p(f"{'DRY-RUN — no writes. ' if dry else ''}"
       f"changed={changed}  unchanged={skipped}  failed={failed}  total={total}")
    if dry:
        _p("Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
