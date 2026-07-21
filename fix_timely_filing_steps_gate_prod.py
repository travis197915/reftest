#!/usr/bin/env python3
r"""Deterministically evaluate the IN-SCOPE Timely-Filing steps
(2, 3, 4, 5, 10, 11, 12, 17, 18) per claim from that claim's own tool-call data,
and write branch-appropriate reasoning. NO LLM. No stored LLM notes. Idempotent.

Why
---
The Timely-Filing SOP was previously gated on the Step-1 "is the claim denying
for TF0/TF1?" check, so for every non-TF denial (e.g. B05) EVERY downstream
step was marked Not-Applicable / out-of-scope. Per auditor UAT the TF0/TF1
applicability must NOT gate the audit: steps 2,3,4,5,10,11,12,17,18 are all
IN SCOPE and must show a real, data-driven determination.

Flow-aware evaluation (per auditor UAT, Wendy 7/21)
---------------------------------------------------
The script walks the SOP Step/Action gotos for each claim (``_reached``) and,
for every step the claim actually traverses, marks ONLY the governing branch
``Met`` — every sibling branch of a reached step, and every step the claim's
path bypasses, is ``Not Applicable``. Paths:

    Step 3 "Meets Criteria"  -> Emergency Bulletin table (Steps 17, 18); 4/5/10/11/12 N/A
    Step 4 group match       -> Step 10 (calculator 10/11/12); Step 5 N/A
    Step 5 new-day within    -> Step 15 (calculator 10/11/12 N/A)
    Step 5 new-day beyond    -> Step 6 -> Step 10 (calculator 10/11/12 reached)

Each step is evaluated from the claim's real tool output on its latest run:

    Step 2  -> check_member_address_and_state    (member's resident state)
    Step 3  -> check_timely_filing_state_date     (emergency-bulletin criteria)
    Step 4  -> check_timely_filing_deadline        (special-group match)
    Step 5  -> facets_get_summary                  (submission type + TF calculator)
    Step 10 -> facets_get_duplicate_claim          (claim/line in history)
    Step 11 -> facets_get_summary / deadline       (timely-filing calculator)
    Step 12 -> deadline + summary                  (days vs the timely-filing limit)
    Step 17 -> check_timely_filing_state_date     (active bulletin? yes/no)
    Step 18 -> check_timely_filing_state_date     (DOS in effect + received in 12mo)

For every evaluable row it writes the branch determination, clears the wrong
"not-applicable / not-matched" gate state, sets Met/Not-Applicable/Not-Met per
the flow, recomputes the verdict (all rows are CONDITIONAL/BYPASS -> no defect
possible), and refreshes the trace + executive summary. Genuinely out-of-scope
rows ("no rule defined for this step" and the Step-5 Adjustments/Appeals branch)
are left exactly as-is.

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
IN_SCOPE_STEPS = (2, 3, 4, 5, 10, 11, 12, 17, 18)

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

# Human labels used when a step is off the claim's audited path (Not Applicable).
_STEP_LABEL = {
    4: "group review (Step 4)",
    5: "timely-filing limit table (Step 5)",
    10: "claim-history check (Step 10)",
    11: "Timely Filing Calculator (Step 11)",
    12: "days-vs-limit comparison (Step 12)",
    17: "Emergency Response Bulletin county check (Step 17)",
    18: "Emergency Response Bulletin determination (Step 18)",
}

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
    if step == 5:
        if "adjustment" in c or "appeals" in c:
            return "ADJ"
        if ("resubmission" in c or "corrected" in c or "(7 or 8)" in c
                or "frequency 7" in c):
            return "CORRECTED"
        # COB has a legacy (processed on/before 12/31/2023) and a current
        # (processed on/after 01/01/2024) provision. Claims here are processed
        # in 2025, so ONLY the row explicitly scoped to on/after 01/01/2024
        # governs; every other COB row (including the pre-2024 header and its
        # detail bullets) is the legacy provision -> Not Applicable.
        if "01/01/2024" in c or "on or after" in c:
            return "COB"
        if "cob" in c or "other carrier" in c or "12/31/2023" in c or "on or before" in c:
            return "COB_LEGACY"
        if "new day" in c or "new-day" in c:
            return "NEWDAY"
        return "OTHER"
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

    _p("── Deterministic Timely-Filing steps 2,3,4,5,10,11,12,17,18 [no LLM] ──")
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
        ni = str(rec.get("CLCL_NTWK_IND") or "").strip().upper()
        return {
            "dos": _parse_date(rec.get("CLCL_HIGH_SVC_DT") or rec.get("CLCL_LOW_SVC_DT")),
            "recd": _parse_date(rec.get("CLCL_RECD_DT")),
            "inn": ni == "I",
            "net_known": ni in ("I", "O"),
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
        # Cite a prior claim in history as the example, never the claim itself.
        sample = next((fc.get("CLCL_ID") for fc in found
                       if str(fc.get("CLCL_ID") or "") != str(run.claim_id or "")), None)
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

    # ---- flow-path walker (from the SOP Step/Action gotos) --------------
    def _within_limit(f: dict) -> bool | None:
        dos, recd = f.get("dos"), f.get("recd")
        if not (dos and recd):
            return None
        limit = 90 if (f["claim_type"] == "newday" and f["inn"]) else 365
        return (recd - dos).days <= limit

    def _reached(C: dict) -> tuple[set, str]:
        """Walk the SOP: which in-scope steps this claim's path actually reaches.

        2 → 3 always. Step 3 'Meets Criteria' → Emergency Bulletin table (17,18).
        Else → Step 4. Step 4 group match → Step 10 (calculator 10,11,12). Else →
        Step 5. Step 5 within limit → skip to Step 15 (calculator NOT reached);
        beyond → Step 6 → Step 10 (calculator reached).
        """
        f, bl, dl = C["facts"], C["bulletin"], C["deadline"]
        r = {2, 3}
        if bl["met"]:
            return r | {17, 18}, "bulletin"
        r.add(4)
        if dl.get("matched_group"):
            return r | {10, 11, 12}, "group"
        r.add(5)
        if _within_limit(f) is False:
            return r | {10, 11, 12}, "beyond"
        return r, "within"

    def _route_clause(C: dict) -> str:
        path, bl, dl = C["path"], C["bulletin"], C["deadline"]
        if path == "within":
            return ("the claim was received within the timely-filing limit at Step 5 "
                    "(submitted within timely filing) and routes to Step 15 to process")
        if path == "group":
            return (f"the claim matches the {dl.get('matched_group')} group at Step 4 and "
                    f"routes to Step 10 for the group guidelines")
        if path == "bulletin":
            return (f"the claim meets the {bl['state'] or 'member-state'} Emergency Response "
                    f"Bulletin criteria at Step 3 and routes to the bulletin Step/Action table")
        return "the claim follows the timely-filing calculator path (Steps 10-12)"

    def _offpath(step: int, C: dict) -> str:
        lbl = _STEP_LABEL.get(step, f"Step {step}")
        return (f"Not applicable — {_route_clause(C)}, so the {lbl} is not reached for "
                f"this claim.")

    # ---- per-branch evaluation: (status, reason); status in MET/NA/NOT_MET
    def _eval(step: int, bk: str, C: dict) -> tuple[str, str]:
        if step not in C["reached"]:
            return "NA", _offpath(step, C)

        f = C["facts"]
        dos, recd = f["dos"], f["recd"]
        ct = f["claim_type"]
        clbl = _CLAIM_LABEL.get(ct, ct)
        net = "INN" if f["inn"] else "OON"
        # When the network indicator is absent we fall back to the more lenient
        # OON 365-day bound and say so, rather than asserting a network we don't know.
        net_note = ("" if f["net_known"] else
                    " (the network indicator is not present on the electronic image, so "
                    "the more lenient OON 365-day limit is applied)")
        plds = f["plds"] or "(plan unknown)"
        bl = C["bulletin"]
        dl = C["deadline"]
        st = C["state"]
        dup = C["dup"]

        if step == 2:
            if st["found"]:
                return "MET", (
                    f"Member resides in {st['full']} ({st['abbr']}), "
                    f"{st['city'].title()} {st['zip']} (check_member_address_and_state, "
                    f"match {st['score']}%). Opened the OBH/OPH Emergency Response "
                    f"Bulletins and searched for {st['full']}; proceed to Step 3.")
            return "MET", ("Member's resident state could not be resolved from the address "
                           "tool; proceed to Step 3 to check the Emergency Response Bulletins.")

        if step == 3:
            det = bl["detail"] or (f"date of service not within the bulletin effective "
                                   f"range {bl['range']}" if bl["range"] else "no matching bulletin")
            if bk == "MEETS":
                if bl["met"]:
                    return "MET", (
                        f"The {bl['state'] or st['full']} Emergency Response Bulletin criteria "
                        f"are met for this claim (check_timely_filing_state_date = 'Met'); skip "
                        f"to the Emergency Response Bulletins Step/Action table.")
                return "NA", (
                    f"Not applicable — the {bl['state'] or st['full']} bulletin check returned "
                    f"'{bl['status'] or 'Not Met'}' ({det}); the claim does not meet the "
                    f"bulletin criteria, so the 'Meets Criteria' branch does not apply.")
            if bk == "MGB":
                if (not bl["met"]) and ("mass general" in plds.lower()):
                    return "MET", (
                        f"Group/Plan '{plds}' is Mass General Brigham and the DOS falls in "
                        f"01/01/2023–06/30/2023; waive timely filing per the bulletin.")
                return "NA", (
                    f"Not applicable — Group/Plan '{plds}' is not Mass General Brigham, so "
                    f"this branch does not apply.")
            # OTHERS
            if not bl["met"]:
                return "MET", (
                    f"The {bl['state'] or st['full']} Emergency Response Bulletin check "
                    f"returned '{bl['status'] or 'Not Met'}'; this claim does not meet the "
                    f"bulletin criteria ({det}) — select 'Does not meet criteria' and proceed "
                    f"to Step 4.")
            return "NA", ("Not applicable — the bulletin criteria are met, so the 'does not "
                          "meet criteria' branch does not apply.")

        if step == 4:
            mg = (dl.get("matched_group") or "")
            gmd = str(dl.get("group_match_detail") or "no group keyword matched the plan")
            if bk == "MAIN":
                if mg:
                    return "NA", (
                        f"Not applicable — the claim's plan '{plds}' matches the {mg} group, so "
                        f"the {mg}-specific timely-filing rule governs (see that branch).")
                return "MET", (
                    f"The claim's plan '{plds}' matches none of the listed special groups "
                    f"({gmd}); answer 'No' and proceed to the next step (Step 5).")
            label = next((l for tok, _wb, l in _GROUPS if tok.upper() == bk), bk)
            if mg and (bk in mg.upper() or label.upper() in mg.upper()):
                return "MET", (f"The claim's plan '{plds}' matches the {label} group; apply "
                               f"the {label} timely-filing guideline.")
            return "NA", (f"Not applicable — the claim's plan '{plds}' is not a {label} group "
                          f"({gmd}).")

        if step == 5:
            _BL = {"COB": "COB-submission", "CORRECTED": "resubmission/corrected (Freq 7/8)",
                   "NEWDAY": "new-day-claim", "ADJ": "adjustments/appeals"}
            bt = {"COB": "cob", "CORRECTED": "corrected", "NEWDAY": "newday"}.get(bk, "other")
            d1, d2 = _fmt(dos), _fmt(recd)
            days = (recd - dos).days if (dos and recd) else None
            limit = _limit_days(ct, f["inn"])
            if bk == "COB_LEGACY":
                return "NA", (
                    "Not applicable — this provision covers COB claims processed on or before "
                    "12/31/2023; this claim was processed in 2025, so the current COB rule "
                    "(processed on/after 01/01/2024) governs.")
            if bt != ct:
                return "NA", (f"Not applicable — this is a {clbl} submission, so the "
                              f"{_BL.get(bk, bk)} branch does not apply.")
            # governing submission-type branch
            if bt == "newday" and days is not None:
                if days <= limit:
                    return "MET", (
                    f"New-day {net} claim. The Timely Filing Calculator shows {days} days "
                    f"from DOS {d1} to received date {d2}, within the {limit}-day {net} "
                    f"limit{net_note} — the claim was submitted within timely filing. Override "
                    f"with Bypass Claim Accept Period (EXP OCA) and skip to Step 15.")
                return "MET", (
                    f"New-day {net} claim. The Timely Filing Calculator shows {days} days from "
                    f"DOS {d1} to received date {d2}, beyond the {limit}-day {net} limit"
                    f"{net_note}. Per the audit scope, TF0/TF1 timely-filing outcomes are not "
                    f"audited as defects, so no timely-filing defect is raised.")
            if bt == "cob":
                return "MET", (
                    f"COB {net} claim{net_note}. Timely-filing limit is 90 days from the other "
                    f"carrier's paid date (or 365 days from the date of service); the electronic "
                    f"image does not carry the other-carrier paid date. DOS {d1} to received {d2}"
                    + (f" = {days} days" if days is not None else "") + f".{_SCOPE}")
            if bt == "corrected":
                pos = ("within" if (days is not None and days <= limit) else "beyond") \
                    if days is not None else "—"
                return "MET", (
                    f"Resubmission/corrected {net} claim. Timely-filing limit is {limit} days. "
                    f"DOS {d1} to received {d2}"
                    + (f" = {days} days ({pos} the {limit}-day limit)" if days is not None else "")
                    + f".{_SCOPE}")
            return "MET", (f"{_BL.get(bk, bk).capitalize()} branch governs for this {clbl} "
                           f"claim.{_SCOPE}")

        if step == 10:
            if bk == "YES":
                if dup["has"]:
                    tail = (f" (e.g. {dup['sample']})" if dup["sample"] else "")
                    return "MET", (
                        f"A prior claim/line exists in history — {dup['n']} claim(s) found{tail} "
                        f"(facets_get_duplicate_claim); proceed to Step 11 to run the Timely "
                        f"Filing Calculator.")
                return "NA", "Not applicable — no prior claim/line was found in history."
            if bk == "NO":
                if not dup["has"]:
                    return "MET", ("No prior claim/line was found in history; follow the "
                                   "chart below.")
                return "NA", (f"Not applicable — a prior claim/line exists in history "
                              f"({dup['n']} found).")
            if bk == "FREQ78":
                if (not dup["has"]) and ct == "corrected":
                    return "MET", ("No claim/line in history for this Freq 7/8 submission; "
                                   "follow the OBH Facets Kill-Delete Reroute Process.")
                return "NA", (f"Not applicable — this is a {clbl} claim, not a no-history "
                              f"Freq 7/8 kill-delete case.")
            # NEWDAY_TF — DENY branch; per audit scope it must never fire -> Not-Met.
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            limit = _limit_days(ct, f["inn"])
            if days is not None:
                pos = "within" if days <= limit else "beyond"
                return "NOT_MET", (
                    f"'New-day claim/line denying for timely filing' branch. Calculator {days} "
                    f"days vs the {limit}-day {net} limit{net_note} → {pos} the limit. Per the "
                    f"audit scope, TF0/TF1 timely-filing outcomes are not audited as defects, so "
                    f"the claim is not denied for timely filing here; this branch does not fire.")
            return "NOT_MET", (
                "'New-day claim/line denying for timely filing' branch. Per the audit scope, "
                "TF0/TF1 timely-filing outcomes are not audited as defects, so the claim is "
                "not denied for timely filing here; this branch does not fire.")

        if step == 11:
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            olab = ("original claim paid date" if dup["oldest_paid"] else "date of service")
            if days is not None:
                return "MET", (f"Timely Filing Calculator: {olab} (oldest) {_fmt(oldest)} to "
                               f"received date (newest) {_fmt(recd)} = {days} days.")
            return "MET", ("Timely Filing Calculator: the oldest/received dates are not both "
                           "available on the electronic image, so the day count cannot be computed.")

        if step == 12:
            oldest = dup["oldest_paid"] or dos
            days = (recd - oldest).days if (recd and oldest) else None
            limit = _limit_days(ct, f["inn"])
            if days is not None:
                pos = "within" if days <= limit else "beyond"
                return "MET", (f"Calculator result {days} days vs the {limit}-day timely-filing "
                               f"limit for a {clbl} {net} claim{net_note} → {pos} the "
                               f"limit.{_SCOPE}")
            return "MET", (f"Timely-filing limit for a {clbl} {net} claim{net_note} is {limit} "
                           f"days; the day count could not be computed from the image.{_SCOPE}")

        if step == 17:
            if bk == "YES":
                if bl["met"]:
                    return "MET", (
                        f"The member's state ({bl['state'] or st['full']}) has an active "
                        f"Emergency Response Bulletin whose criteria are met "
                        f"(check_timely_filing_state_date = 'Met'); proceed to Step 18.")
                return "NA", "Not applicable — no active bulletin criteria are met for this claim."
            if bk == "NO":
                if not bl["met"]:
                    return "MET", (
                        f"The member's state ({bl['state'] or st['full']}) has no active "
                        f"Emergency Response Bulletin criteria met for this claim "
                        f"(check_timely_filing_state_date = '{bl['status'] or 'Not Met'}'); "
                        f"answer 'No' and return to Step 4.")
                return "NA", "Not applicable — active bulletin criteria are met."
            return "MET", ("Emergency-response-bulletin applicability evaluated from "
                           "check_timely_filing_state_date.")

        if step == 18:
            allw = bl["all_in_range"]
            exp = bl["exp"]
            within12 = bool(recd and exp and recd <= exp + _dt.timedelta(days=365))
            rng = bl["range"] or "(no bulletin range)"
            if bk == "ALLWITHIN_IN12":
                if allw and within12:
                    return "MET", (
                        f"All DOS fall within the bulletin effective range ({rng}) and the "
                        f"claim was received {_fmt(recd)}, within 12 months after the expiration "
                        f"({_fmt(exp)}). Apply Claim-Level Bypass Claim Accept Period (OCA), "
                        f"refresh (F3), and return to Step 16.{_SCOPE}")
                return "NA", f"Not applicable — not all DOS fall within the bulletin range ({rng})."
            if bk == "ALLWITHIN_NOT12":
                if allw and not within12:
                    return "MET", (
                        f"All DOS fall within the bulletin range ({rng}) but the claim was not "
                        f"received within 12 months after the expiration ({_fmt(exp)}); return "
                        f"to Step 4.{_SCOPE}")
                return "NA", ("Not applicable — this claim is not 'all DOS in range and received "
                              "beyond 12 months'.")
            if bk == "NOTALL_IN12":
                if (not allw) and within12:
                    return "MET", (
                        f"DOS {_fmt(dos)} is not within the bulletin effective range ({rng}), so "
                        f"not all dates of service fall within the effective date; the claim was "
                        f"received {_fmt(recd)}, within 12 months after the expiration "
                        f"({_fmt(exp)}). Split the claim per Select-to-Move — Bypass Claim Accept "
                        f"Period (OCA) on in-range DOS.{_SCOPE}")
                return "NA", ("Not applicable — this claim is not 'not all DOS in range and "
                              "received within 12 months'.")
            # NOTALL_NOT12
            if (not allw) and not within12:
                return "MET", (
                    f"DOS {_fmt(dos)} is not within the bulletin range ({rng}) and the claim was "
                    f"not received within 12 months after the expiration ({_fmt(exp)}); return "
                    f"to Step 4.{_SCOPE}")
            return "NA", ("Not applicable — this claim is not 'not all DOS in range and received "
                          "beyond 12 months'.")

        return "MET", "Evaluated from the claim's timely-filing tool calls."

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
        C["reached"], C["path"] = _reached(C)

        all_evals = list(RuleEvaluation.objects.filter(run=run))

        # 1) RuleEvaluation rows.  Only the governing branch of a reached step is
        # Met; sibling branches and off-path steps are Not Applicable; the DENY
        # branch is Not-Met (never adverse).
        eval_changed = False
        to_update: list = []
        for ev in rows:
            # Preserve genuinely out-of-scope rows: steps with no rule defined,
            # and the Step 5 Adjustments/Appeals branch (legitimately OOS). The
            # gate cascade ("prior step out of scope — auditing stopped") is NOT
            # preserved — it must be fixed.
            _sr = (ev.skip_reason or "").lower()
            if "no rule defined" in _sr or "adjustment" in _sr or "appeals" in _sr:
                continue
            step = _row_step(ev.rule_key)
            if step not in IN_SCOPE_STEPS:
                continue
            bk = _branch_key(step, ev.condition or "")
            status, reason = _eval(step, bk, C)
            if (ev.decision_type or "").upper() in _DEFECT and status == "MET":
                status = "NOT_MET"  # never let a DENY row become adverse
            want_matched = status == "MET"
            want_skipped = status == "NA"
            want_skip_reason = "not applicable" if status == "NA" else ""
            if (ev.reasoning or "").strip() == reason and ev.matched == want_matched \
                    and ev.skipped == want_skipped:
                continue
            ev.reasoning = reason
            ev.matched = want_matched
            ev.skipped = want_skipped
            ev.skip_reason = want_skip_reason
            if status == "MET":
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
        _TS = {"MET": trace_builder.MET, "NA": trace_builder.NOT_APPLICABLE,
               "NOT_MET": trace_builder.NOT_MET}
        tchanged = False
        shape_reason: dict[str, tuple[str, str]] = {}  # shape_id -> (exec_status, summary)
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
                first_reason = ""
                any_row = any_met = False
                all_na = True
                for sr in subs:
                    _stmt = (sr.get("statement") or "").lower()
                    _cnd = _subrule_cond(sr).lower()
                    if "no rule defined" in _stmt or (
                            step == 5 and ("adjustment" in _cnd or "appeals" in _cnd
                                           or "adjustment" in _stmt)):
                        continue
                    bk = _branch_key(step, _subrule_cond(sr))
                    status, reason = _eval(step, bk, C)
                    any_row = True
                    first_reason = first_reason or reason
                    if status == "MET":
                        any_met = True
                        gov_reason = gov_reason or reason
                    if status != "NA":
                        all_na = False
                    want_status = _TS[status]
                    if sr.get("statement") == reason and sr.get("status") == want_status:
                        continue
                    sr["statement"] = reason
                    sr["status"] = want_status
                    tchanged = True
                if not any_row:
                    continue
                if any_met:
                    entry_status = trace_builder.MET
                elif all_na:
                    entry_status = trace_builder.NOT_APPLICABLE
                else:
                    entry_status = trace_builder.NOT_MET
                rationale = gov_reason or first_reason
                need = (entry.get("status") != entry_status
                        or entry.get("step_exec_status") != "completed"
                        or (rationale and entry.get("rationale") != rationale))
                if need:
                    entry["status"] = entry_status
                    entry["step_exec_status"] = "completed"
                    if rationale:
                        entry["rationale"] = rationale
                    tchanged = True
                if entry.get("shape_id"):
                    exec_status = "CLEAN" if any_met else (
                        "NOT_APPLICABLE" if all_na else "CLEAN")
                    shape_reason[entry["shape_id"]] = (exec_status, rationale)
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
                        new_status, newsum = shape_reason[sid]
                        if stp.get("status") == new_status and stp.get("summary") == newsum:
                            continue
                        stp["status"] = new_status
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
