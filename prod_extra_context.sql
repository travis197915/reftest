-- Business context (Workbench.config->>'extra_context') for prod.
-- Matches on config->>'sop_title' (stable across DBs; workbench UUID / sop_id are NOT).
-- Safe to re-run (idempotent: overwrites extra_context for the matched SOP).

BEGIN;

-- Timely Filing  (local sop_id=7, len=2988 chars)
UPDATE builder_workbench
SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                       '{extra_context}',
                       to_jsonb($ctx$TIMELY FILING — business rules for this SOP (interpretation guidance only; never dictates a verdict):

- This P&P applies ONLY when the claim/line is actually denying for TIMELY FILING (a TF0 or TF1 edit). Step 1 ("Is your claim/line denying for TF1 or TF0?") is answered "Yes" only when the claim/line's actual FACETS denial reason is a timely-filing edit (TF0/TF1, or the EOB/EX code that maps to timely filing).

- Denial/disallow codes that are NOT timely filing include, for example: B05, B14 ("Please forward to correct carrier" — a wrong-carrier / coordination-of-benefits reason), B43 (a plan-specific medical-service denial), and any other non-timely-filing reason. When the claim/line is denying for one of these NON-timely-filing reasons (and not for TF0/TF1), the Step 1 answer is "No" -> "This P&P does not apply": set applicable=false and do NOT match the timely-filing DENY rows (including Step 10 "New Day claim/line denying for timely filing"). A $0 paid amount or claim status "02 Accepted" alone is NOT evidence of a timely-filing denial.

- THIS IS DECISIVE, NOT AMBIGUOUS. If the claim/line's actual denial/disallow code is NOT a TF0/TF1 timely-filing code, the Step 1 answer is "No" and every downstream timely-filing DENY row (including Step 10) is matched=false / applicable=false. Do NOT answer "Yes" or match a timely-filing deny row "for completeness" or "to be safe" when the real denial reason (e.g. B14 wrong-carrier/COB, B05, B43) is not timely filing. A DENY row is matched only when timely filing is affirmatively the denial reason.

- STEP 1 "YES" IS A NON-SCORING ROUTING GATE (return applicable=false, not matched=true). Step 1 row RULE-001-001 (condition "Yes", action "Proceed to next step. Confirm INN status via provider lookup") exists ONLY to route the audit forward — matching it is NOT a finding. For THIS specific row, when the claim/line is denying for TF1/TF0 and you are simply proceeding to verify, you MUST return "applicable": false (the engine then records it as a non-adverse pass-through). Do NOT return matched=true on this row: because the row carries a DENY disposition, matched=true here registers a FALSE timely-filing defect even though the row is pure routing. Setting applicable=false does not stop the audit — evaluation continues to the downstream verification steps.

- THE TIMELY-FILING DEFECT IS DECIDED DOWNSTREAM, NOT AT STEP 1. A genuine timely-filing DEFECT exists ONLY when a downstream verification step (e.g. Steps 7, 10, 13, 14, 15) affirmatively finds the denial was applied INCORRECTLY (the claim was actually received within the filing limit, or a valid INN / contract / PAR / POTF filing-limit exception applies and was ignored). When the claim was CORRECTLY denied for timely filing (genuinely submitted after the provider's filing limit with a valid TF1/TF0 edit and no exception applies), no downstream deny row matches and the audit result is CLEAN — the system's denial is correct, which is NOT a defect.$ctx$::text),
                       true),
    updated_at = now()
WHERE config->>'sop_title' = '37cb2d10fdbf4cffbe6908e44901b781_OBH_Facets_Timely_Filing';

-- Provider Selection Guidelines  (local sop_id=12, len=2235 chars)
UPDATE builder_workbench
SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                       '{extra_context}',
                       to_jsonb($ctx$PROVIDER NETWORK (INN vs OON) DETERMINATION — business rules for this SOP:

- INN vs OON must be DERIVED from the provider's FACETS network participation.
  Do NOT read it literally from CLCL_NTWK_IND (the claim-level network indicator)
  or from an assumed group_model value.

- The PRIMARY in-network signal is the FACETS claim-summary provider
  network-relationship records. Treat the presence of any of these as evidence
  the billed provider PARTICIPATES IN-NETWORK:
    * CIV8_NWPR_* — "Network - Provider Relationship"
    * CIV8_NWPE_* — "Capitation Network Provider Entity Relationship"
    * CIV8_NWCR_* — "Global Capitation Relationship"

- When one or more of these network/capitation relationship records is present
  for the billed provider, determine the provider as IN-NETWORK (INN) EVEN IF
  CLCL_NTWK_IND = "O". In the 3B group model an INN determination selects the
  1st/2nd choice and the claim is processed per the INN benefit — this is CLEAN,
  NOT a provider-selection defect.

- A provider is OUT-OF-NETWORK (OON) only when a single provider record matches
  on all 3 points (Tax ID/EIN + NPI + name/address) AND no network-relationship
  records exist. Do NOT raise a provider-selection denial based solely on the
  literal CLCL_NTWK_IND value.

- TIN-MATCH EXCEPTION. The "TIN on DOC360 does not match the provider TIN in FACETS" exception applies ONLY when the two TINs genuinely DIFFER. If the DOC360 TIN equals the FACETS provider TIN (e.g. it matches MCTN_ID / the PRPR Tax ID), the TINs MATCH and this exception does NOT apply — set matched=false. Never conclude the "does not match" exception holds after finding the TINs are equal.

- DO NOT SELECT A DENY CHOICE FOR AN INN PROVIDER. Before matching a 3rd/4th/5th "choice" deny row, apply the INN determination above: if ANY CIV8_NWPR_* / CIV8_NWPE_* / CIV8_NWCR_* network-relationship record exists for the billed provider, the provider is IN-NETWORK and the 1st/2nd choice applies — the claim is CLEAN. Only match a deny "choice" row when the provider is genuinely OON (a single 3-point match with NO network-relationship records) AND the earlier choices are truly not satisfied. Do not match a deny "choice" row "for completeness" or "to be safe".$ctx$::text),
                       true),
    updated_at = now()
WHERE config->>'sop_title' = 'OBH Facets Provider Selection Guidelines';

-- Duplicate Claim Handling  (local sop_id=6, len=3631 chars)
UPDATE builder_workbench
SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                       '{extra_context}',
                       to_jsonb($ctx$DUPLICATE CLAIM HANDLING — business rules for this SOP (interpretation guidance only; never dictates a verdict):

- PREMISE GATE (Steps 7 & 8). Every duplicate-DENY row in Step 7 and Step 8 starts from the situation that the claim/claim line IS ALREADY denying as a duplicate in FACETS — e.g. the ultra-blue edit "CDD - Definite Duplicate Claim", or a duplicate EOB/EX code (E51/F51/003) already present on the CURRENT claim/line's disallow code (CDML_DISALL_EXCD) or EOB. Only walk these DENY rows when the CURRENT claim/line is actually being flagged/denied as a duplicate by FACETS. The `facets_get_duplicate_claim` tool merely LISTS other claims that share member + date-of-service + provider; a tool hit BY ITSELF does NOT mean the current claim is a duplicate. If the current claim carries no duplicate edit/alert (no CDD, no duplicate EOB/EX code) and is not otherwise denying as a duplicate, none of the Step 7/8 duplicate-deny rows apply — set applicable=false / matched=false.

- IGNORE SELF-MATCHES. Disregard any entry returned by facets_get_duplicate_claim whose CLCL_ID equals the current claim's own CLCL_ID. A claim is never its own duplicate.

- DIFFERENT PROVIDER + DIFFERENT TAX ID = NOT A DUPLICATE. When the historical claim is from a genuinely different provider — different PRPR_NAME AND a different Tax ID/EIN — the providers are NOT affiliated. Per this SOP that is the BYPASS row (Bypass Duplicate Edit, EX 020/001): the line/claim is NOT a duplicate and is CLEAN. Only providers that are AFFILIATED (different names but the SAME Tax ID) are treated as a duplicate. Do not raise a duplicate denial when the two claims are from different providers with different Tax IDs.

- ORIGINAL MUST HAVE BEEN PAID. A duplicate/adjustment denial presupposes a prior claim that was actually PAID. If the matched historical claim's CLCL_TOT_PAYABLE (or paid amount) is 0 — i.e. it was never paid — there is no prior payment to duplicate or adjust; do not deny the current claim as a duplicate on the basis of that history record.

- "ADDITIONAL/CHANGED BILLED OR ALLOWED AMOUNT, WITH NO OTHER CHANGES" (Step 7 changed-amount row; Step 8). This applies only to a TRUE duplicate of the SAME service where the ONLY difference is the billed/allowed amount. A large billed-amount difference that reflects a different procedure/service, different units, or different line items means there ARE other changes — the "With No other Changes to the claim" clause is NOT satisfied, so this row does not apply.

- STEP 8 IS MEDICAID-ONLY. Per Step 6 ("Is the claim a Medicaid Claim?"), a Medicaid claim skips to Step 8 while a non-Medicaid claim proceeds through Step 7. For a Commercial or Medicare claim, the Step 8 rows are out of scope — set applicable=false rather than matching them.

- DO NOT MATCH A DENY ROW UNDER AMBIGUITY. The Step 7/8 duplicate rows are adverse (DENY) rows. Match one ONLY when the duplicate premise is AFFIRMATIVELY established by the facts above. If the premise is absent, ambiguous, unverified, or you are matching "for completeness" or "to be safe" — set matched=false. Never deny a claim as a duplicate just to be thorough; the default for a duplicate-DENY row whose premise is not clearly proven is matched=false.

- "ADDITIONAL/CHANGED UNITS", "NEW/CHANGED MODIFIER", "CASE MANAGEMENT SERVICES" = NOT A DUPLICATE. Per this SOP these are explicit BYPASS situations (Bypass Duplicate Edit, EX 020/001): the line/claim is NOT a duplicate and is CLEAN. Do not treat "additional or changed units" or a changed modifier as a reason to DENY as a duplicate — that row's own action is to bypass, not deny.$ctx$::text),
                       true),
    updated_at = now()
WHERE config->>'sop_title' = '9c6e450f25f04a438afa6fa756a24d9d_OBH_Facets_Duplicate_Claim_Handling';

-- Provider Opt-Out Look-Up Audit Guidelines  (local sop_id=16, len=933 chars)
UPDATE builder_workbench
SET config = jsonb_set(COALESCE(config, '{}'::jsonb),
                       '{extra_context}',
                       to_jsonb($ctx$MEDICARE PROVIDER OPT-OUT — business rules for this SOP (interpretation guidance only; never dictates a verdict):

- This SOP validates a MEDICARE opt-out scenario and applies only to Medicare line-of-business claims. For a Commercial or Medicaid claim it is out of scope — set applicable=false.

- Conclude "provider is opted-out and the claim's denial is correct" ONLY when BOTH hold: (1) the billed provider is CONFIDENTLY matched on the Medicare opt-out list (medicare_optout_tool returns an opt-out record matching this provider's NPI/name), AND (2) the claim was actually DENIED FOR the Medicare opt-out reason. A claim merely showing $0 paid, $0 payable, or status "02 Accepted" is NOT, by itself, evidence of an opt-out denial. If the provider is not confidently matched as opted-out, or the claim's denial is for an unrelated reason, this validation does not apply — set applicable=false and do not affirm an opt-out denial.$ctx$::text),
                       true),
    updated_at = now()
WHERE config->>'sop_title' = 'Provider Opt-Out Look-Up Audit Guidelines';

-- Verify before committing:
--   SELECT config->>'sop_title' AS sop, length(config->>'extra_context') AS ctx_len
--   FROM builder_workbench WHERE config ? 'extra_context' ORDER BY sop;

COMMIT;
