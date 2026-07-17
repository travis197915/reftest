"""REST views for the rule execution agent.

POST /api/execute/workflows/<workflow_id>/run-batch/         multipart upload (sync)
POST /api/execute/workflows/<workflow_id>/run-batch-async/   multipart upload (async + SSE)
GET  /api/execute/batches/latest/                            most recent batch (shared DB)
GET  /api/execute/batches/<batch_id>/                        prior batch result
GET  /api/execute/batches/<batch_id>/events/                 SSE stream of live batch events
GET  /api/execute/runs/                                      all processed claims (paginated)
GET  /api/execute/runs/<run_id>/                             single-claim audit trail
GET  /api/execute/runs/<run_id>/nodes/                       per-canvas-node rollup
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any, Iterator

from django.conf import settings
from django.db.models import Avg, DurationField, ExpressionWrapper, F, Q
from django.http import StreamingHttpResponse
from django.utils import timezone as dj_timezone
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import trace_builder
from .models import BatchExecutionRun, RuleExecutionRun
from .reviewer_lookup import resolve_reviewer_names
from .serializers import (BatchExecutionRunSerializer,
                          RuleExecutionRunSerializer, _excel_claim_fields,
                          claim_audit_status,
                          serialize_run_summary)
from .trace_builder import (CLEAN, DEFECT, INCONCLUSIVE, IN_PROGRESS, _CLEAN_DECISIONS,
                            _DEFECT_DECISIONS)

logger = logging.getLogger(__name__)

# Heartbeat cadence for SSE — must stay under the proxy idle timeout
# (nginx default 60s, AWS ALB 60s, gunicorn `--timeout`).
_SSE_HEARTBEAT_SECONDS = 15

# Terminal SSE event kinds — when the bridge sees one of these, it closes
# the pubsub and returns. `summary` is the happy path, `error` is the
# task-level crash path.
_SSE_TERMINAL_KINDS = {"summary", "error"}


def _iso_utc(ts: datetime | None) -> str | None:
    """Return an ISO-8601 UTC timestamp with Z suffix."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_timezone.utc)
    return ts.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_clock(ts: datetime | None) -> str:
    """Return the UI-friendly clock string (UTC), e.g. 06:08:09 AM."""
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_timezone.utc)
    return ts.astimezone(dt_timezone.utc).strftime("%I:%M:%S %p")


def _format_duration(duration_ms: int | None) -> str:
    """Return human-friendly duration: 12s, 1m 04s."""
    total_seconds = max(0, int((duration_ms or 0) / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _relay_error(
    message: str,
    *,
    status_code: int,
    details: dict[str, Any] | None = None,
    source: str = "django",
) -> Response:
    payload: dict[str, Any] = {"error": message, "source": source}
    if details:
        payload["details"] = details
    return Response(payload, status=status_code)


def _build_node_rollup(run: RuleExecutionRun) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build per-node and outer-tool rollups for one execution run."""
    # Insertion-ordered dict keyed by shape_id, so the response preserves
    # the order in which the engine first touched each node (driven by
    # RuleEvaluation.order_index, which is set in canvas order).
    nodes: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    outer_tools: list[dict[str, Any]] = []

    def _node_slot(shape_id: str, shape_label: str) -> dict[str, Any]:
        slot = nodes.get(shape_id)
        if slot is None:
            slot = {
                "shape_id": shape_id,
                "shape_label": shape_label,
                "evaluations": [],
                "tool_invocations": [],
                "rules_evaluated": 0,
                "rules_matched": 0,
                "matched_decision_types": [],
                "terminated_here": False,
            }
            nodes[shape_id] = slot
        elif shape_label and not slot["shape_label"]:
            slot["shape_label"] = shape_label
        return slot

    # Iterate evaluations in their persisted order. The shape_id on the
    # binding wins; if the binding was deleted (FK SET_NULL), we still
    # have a stable key via the captured shape_label or rule_key prefix.
    for ev in run.evaluations.all().order_by("order_index"):
        rb = ev.rule_binding  # may be None if the binding was deleted
        if rb is not None:
            shape_id = str(rb.shape_id)
            shape_label = (rb.shape.label or "") if rb.shape else ""
        else:
            shape_id = ""
            shape_label = ""
        # Fall back to whatever the engine captured at run time. We don't
        # store ev.shape_id on the model today, so use a synthetic key
        # built from rule_key when neither source is available.
        if not shape_id:
            shape_id = f"orphaned:{ev.rule_key}"
        slot = _node_slot(shape_id, shape_label)
        slot["evaluations"].append({
            "order_index": ev.order_index,
            "rule_key": ev.rule_key,
            "rule_source": ev.rule_source,
            "condition": ev.condition,
            "action": ev.action,
            "matched": ev.matched,
            "skipped": getattr(ev, "skipped", False),
            "skip_reason": getattr(ev, "skip_reason", ""),
            "confidence": ev.confidence,
            "reasoning": ev.reasoning,
            "decision_type": ev.decision_type,
            "codes": list(ev.codes or []),
            "llm_provider": ev.llm_provider,
            "llm_ms": ev.llm_ms,
        })
        if not getattr(ev, "skipped", False):
            slot["rules_evaluated"] += 1
        if ev.matched and not getattr(ev, "skipped", False):
            slot["rules_matched"] += 1
            if ev.decision_type and ev.decision_type not in slot["matched_decision_types"]:
                slot["matched_decision_types"].append(ev.decision_type)

    # Tool invocations: bucket the shape-scoped ones onto their node,
    # surface the outer FETCH/PARSE calls (no tool_binding) separately.
    for inv in run.tool_invocations.all().order_by("called_at"):
        tb = inv.tool_binding
        payload = {
            "tool_name": inv.tool_name,
            "phase": inv.phase,
            "ok": inv.ok,
            "duration_ms": inv.duration_ms,
            "error": inv.error,
            "called_at": inv.called_at,
        }
        if tb is None or inv.phase in ("FETCH", "PARSE"):
            outer_tools.append(payload)
            continue
        shape_id = str(tb.shape_id)
        shape_label = (tb.shape.label or "") if tb.shape else ""
        slot = _node_slot(shape_id, shape_label)
        slot["tool_invocations"].append(payload)

    # Flag the node that triggered an early halt: walk the rollup we
    # just built and mark the first node whose matched-rule list
    # contains a DENY/STOP outcome.
    if run.status == "TERMINATED_EARLY":
        for slot in nodes.values():
            if any(e["matched"] and e["decision_type"] in {"DENY", "STOP"}
                   for e in slot["evaluations"]):
                slot["terminated_here"] = True
                break

    return list(nodes.values()), outer_tools


def _trace_status_by_shape(trace) -> dict[str, str]:
    """shape_id -> aggregated audit status from the stored trace steps.

    The trace is the most faithful per-step audit signal (it carries the LLM's
    Met/Not-Met verdicts), so the agent chip should agree with the
    Explainability view. Returns an empty map when no trace exists.
    """
    out: dict[str, list[str]] = {}
    if trace is None:
        return {}
    for step in (trace.trace_json or []):
        sid = str(step.get("shape_id") or "")
        if not sid:
            continue
        out.setdefault(sid, []).append(str(step.get("status") or ""))
    return {sid: trace_builder.aggregate_status(sts) for sid, sts in out.items()}


def _agent_status(node: dict[str, Any], trace_by_shape: dict[str, str] | None = None) -> str:
    """SOP/agent status for one node: CLEAN / DEFECT / OUT_OF_SCOPE / NOT_APPLICABLE.

    Classified from the persisted rule rows (see ``node_audit_status``). The
    trace is consulted only to escalate to DEFECT (so the agent chip never
    misses a Not-Met the trace caught, e.g. a failed coverage tool), never to
    re-introduce the old catch-all INCONCLUSIVE for a SOP that simply did not
    apply / was out of scope.
    """
    if node.get("terminated_here"):
        return DEFECT
    base = trace_builder.node_audit_status(node["evaluations"])
    if base == DEFECT:
        return DEFECT
    if trace_by_shape and trace_by_shape.get(node["shape_id"]) == DEFECT:
        return DEFECT
    return base


def _claim_status(
    run: RuleExecutionRun,
    nodes: list[dict[str, Any]],
    trace=None,
) -> str:
    """3-state claim audit status (CLEAN / DEFECT / INCONCLUSIVE).

    A system/fetch failure is *inconclusive* (the claim could not be audited),
    not a claim defect. When a trace exists we trust its aggregate so the header
    agrees with the per-agent / Explainability views.
    """
    if run.status == "RUNNING":
        return IN_PROGRESS
    if run.status in {"FAILED", "FETCH_FAILED"}:
        return INCONCLUSIVE
    if run.status == "TERMINATED_EARLY":
        return DEFECT
    # The engine's aggregated verdict is authoritative (ALLOW → CLEAN,
    # DENY/REFER/PEND/STOP → DEFECT). Only fall back to the trace / node rollup
    # when there is no decision at all.
    decision = trace_builder.normalize_decision(run.final_decision_type)
    if decision:
        return decision
    if trace is not None and trace.trace_json:
        return trace_builder.claim_status(trace.trace_json)
    statuses = [_agent_status(node) for node in nodes]
    return trace_builder.aggregate_status(statuses)


def _processing_time_min(run: RuleExecutionRun) -> float | None:
    if run.finished_at is None:
        return None
    return round((run.finished_at - run.started_at).total_seconds() / 60.0, 2)


def _serialize_agent(
    node: dict[str, Any],
    run: RuleExecutionRun,
    trace_by_shape: dict[str, str] | None = None,
) -> dict[str, Any]:
    invocations = node["tool_invocations"]
    begin_ts = invocations[0]["called_at"] if invocations else run.started_at
    end_ts = invocations[-1]["called_at"] if invocations else (run.finished_at or run.started_at)
    duration_sec = max(0, int((end_ts - begin_ts).total_seconds()))
    steps = []
    for idx, inv in enumerate(invocations, start=1):
        details = f"Called {inv['tool_name']} for claim {run.claim_id}"
        if inv.get("error"):
            details = f"{details}. Error: {inv['error']}"
        steps.append({
            "id": f"s{idx}",
            "name": inv["tool_name"],
            "status": "completed" if inv["ok"] else "failed",
            "duration": _format_duration(inv["duration_ms"]),
            "details": details,
        })
    process_summary = []
    for evaluation in node["evaluations"]:
        reasoning = (evaluation.get("reasoning") or "").strip()
        if reasoning:
            process_summary.append(reasoning)
    return {
        "id": node["shape_id"],
        "agentName": node["shape_label"] or node["shape_id"],
        "status": _agent_status(node, trace_by_shape),
        "beginTime": _format_clock(begin_ts),
        "endTime": _format_clock(end_ts),
        "durationSec": duration_sec,
        "processSummary": process_summary[:10],
        "steps": steps,
    }


def _serialize_evaluation(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "orderIndex": ev["order_index"],
        "ruleKey": ev["rule_key"],
        "ruleSource": ev["rule_source"],
        "condition": ev["condition"],
        "action": ev["action"],
        "matched": ev["matched"],
        "decisionType": ev["decision_type"],
        "confidence": ev["confidence"],
        "reasoning": ev["reasoning"],
        "codes": list(ev.get("codes") or []),
        "llmProvider": ev.get("llm_provider") or "",
        "llmMs": ev.get("llm_ms") or 0,
    }


def _serialize_agent_detail(
    node: dict[str, Any],
    run: RuleExecutionRun,
    trace_by_shape: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = _serialize_agent(node, run, trace_by_shape)
    payload["evaluations"] = [_serialize_evaluation(ev) for ev in node["evaluations"]]
    return payload


def _serialize_agent_light(
    node: dict[str, Any],
    run: RuleExecutionRun,
) -> dict[str, Any]:
    """Agent card payload without loading trace_json for per-shape status."""
    invocations = node["tool_invocations"]
    begin_ts = invocations[0]["called_at"] if invocations else run.started_at
    end_ts = invocations[-1]["called_at"] if invocations else (run.finished_at or run.started_at)
    duration_sec = max(0, int((end_ts - begin_ts).total_seconds()))
    steps = []
    for idx, inv in enumerate(invocations, start=1):
        details = f"Called {inv['tool_name']} for claim {run.claim_id}"
        if inv.get("error"):
            details = f"{details}. Error: {inv['error']}"
        steps.append({
            "id": f"s{idx}",
            "name": inv["tool_name"],
            "status": "completed" if inv["ok"] else "failed",
            "duration": _format_duration(inv["duration_ms"]),
            "details": details,
        })
    process_summary = []
    for evaluation in node["evaluations"]:
        reasoning = (evaluation.get("reasoning") or "").strip()
        if reasoning:
            process_summary.append(reasoning)
    status = _agent_status_light(node) if "matched_decisions" in node else _agent_status(node)
    return {
        "id": node["shape_id"],
        "agentName": node["shape_label"] or node["shape_id"],
        "status": status,
        "beginTime": _format_clock(begin_ts),
        "endTime": _format_clock(end_ts),
        "durationSec": duration_sec,
        "processSummary": process_summary[:10],
        "steps": steps,
    }


def _serialize_agent_detail_light(
    node: dict[str, Any],
    run: RuleExecutionRun,
) -> dict[str, Any]:
    payload = _serialize_agent_light(node, run)
    payload["evaluations"] = [_serialize_evaluation(ev) for ev in node["evaluations"]]
    return payload


def _serialize_agent_summary(
    node: dict[str, Any],
    run: RuleExecutionRun,
    trace_by_shape: dict[str, str] | None = None,
) -> dict[str, Any]:
    process_summary = []
    for evaluation in node["evaluations"]:
        reasoning = (evaluation.get("reasoning") or "").strip()
        if reasoning:
            process_summary.append(reasoning)
    return {
        "id": node["shape_id"],
        "agentName": node["shape_label"] or node["shape_id"],
        "status": _agent_status(node, trace_by_shape),
        "processSummary": process_summary[:10],
    }


def _parse_run_lookup_uuids(
    request: Request,
) -> tuple[uuid.UUID | None, uuid.UUID | None, Response | None]:
    run_id_param = (request.query_params.get("run_id") or "").strip()
    batch_id_param = (request.query_params.get("batch_id") or "").strip()
    run_uuid: uuid.UUID | None = None
    batch_uuid: uuid.UUID | None = None
    if run_id_param:
        try:
            run_uuid = uuid.UUID(run_id_param)
        except ValueError:
            return None, None, _relay_error(
                "Malformed run_id query parameter",
                status_code=status.HTTP_400_BAD_REQUEST,
                details={"run_id": run_id_param},
            )
    if batch_id_param:
        try:
            batch_uuid = uuid.UUID(batch_id_param)
        except ValueError:
            return None, None, _relay_error(
                "Malformed batch_id query parameter",
                status_code=status.HTTP_400_BAD_REQUEST,
                details={"batch_id": batch_id_param},
            )
    return run_uuid, batch_uuid, None


def _load_run_for_claim(
    claim_id: str,
    run_uuid: uuid.UUID | None,
    batch_uuid: uuid.UUID | None,
    *,
    lightweight: bool = False,
) -> tuple[RuleExecutionRun | None, Response | None]:
    try:
        base = RuleExecutionRun.objects.select_related("workflow", "batch")
        if lightweight:
            # Summary/agents endpoints do not need heavyweight blobs like
            # raw_fetch/cost_breakdown; avoid transferring/de-serializing them.
            base = base.only(
                "id",
                "batch_id",
                "workflow_id",
                "claim_id",
                "claim_payload",
                "started_at",
                "finished_at",
                "status",
                "final_decision_type",
                "applied_codes",
                "narrative",
                "error_message",
                "review_status",
                "review_feedback",
                "auditor_status",
                "review_started_at",
                "reviewed_at",
                "htl_reviewer",
                "original_auditor",
                "claim_lob",
            )
        if not lightweight:
            base = base.prefetch_related(
                "evaluations__rule_binding__shape",
                "tool_invocations__tool_binding__shape",
            )
        if run_uuid is not None:
            run = base.get(id=run_uuid)
        else:
            queryset = base.filter(claim_id=claim_id)
            if batch_uuid is not None:
                queryset = queryset.filter(batch_id=batch_uuid)
            run = queryset.order_by("-started_at").first()
            if run is None:
                return None, _relay_error(
                    f"No run found for claim {claim_id}",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
    except RuleExecutionRun.DoesNotExist:
        return None, _relay_error(
            f"No run found for claim {claim_id}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except Exception as exc:
        logger.exception("claim run lookup failed claim_id=%s", claim_id)
        return None, _relay_error(
            "Unexpected error while loading claim run",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"message": str(exc)},
        )
    return run, None


def _claim_run_context(
    run: RuleExecutionRun,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any, dict[str, str]]:
    nodes, outer_tools = _build_node_rollup(run)
    from .models import ClaimTrace
    trace = ClaimTrace.objects.filter(run=run).first()
    trace_by_shape = _trace_status_by_shape(trace)
    return nodes, outer_tools, trace, trace_by_shape


def _claim_status_light(run: RuleExecutionRun, trace=None) -> str:
    """Fast claim status for the summary tab — no trace_json walk or node rollup."""
    if run.status == "RUNNING":
        return IN_PROGRESS
    if run.status in {"FAILED", "FETCH_FAILED"}:
        return INCONCLUSIVE
    if run.status == "TERMINATED_EARLY":
        return DEFECT
    decision = trace_builder.normalize_decision(run.final_decision_type)
    if decision:
        return decision
    if trace is not None and trace.final_status:
        return trace_builder.normalize_status(trace.final_status)
    return INCONCLUSIVE


def _build_summary_rollup(
    run: RuleExecutionRun,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lightweight rollup for GET /claims/<id>/summary/ — reasoning + status only."""
    nodes: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    eval_rows = (
        run.evaluations
        .values(
            "order_index",
            "reasoning",
            "matched",
            "decision_type",
            "skipped",
            "skip_reason",
            "codes",
            "rule_key",
            "rule_binding__shape_id",
            "rule_binding__shape__label",
        )
        .order_by("order_index")
    )
    for ev in eval_rows:
        shape_fk = ev.get("rule_binding__shape_id")
        if shape_fk:
            shape_id = str(shape_fk)
            shape_label = (ev.get("rule_binding__shape__label") or "")
        else:
            shape_id = f"orphaned:{ev.get('rule_key') or ''}"
            shape_label = ""
        slot = nodes.get(shape_id)
        if slot is None:
            slot = {
                "shape_id": shape_id,
                "shape_label": shape_label,
                "reasonings": [],
                "matched_decisions": [],
                "evals": [],
                "terminated_here": False,
            }
            nodes[shape_id] = slot
        reasoning = (ev.get("reasoning") or "").strip()
        if reasoning:
            slot["reasonings"].append(reasoning)
        slot["evals"].append({
            "matched": bool(ev.get("matched")),
            "skipped": bool(ev.get("skipped")),
            "skip_reason": ev.get("skip_reason") or "",
            "decision_type": ev.get("decision_type") or "",
            "codes": list(ev.get("codes") or []),
        })
        if ev.get("matched") and not ev.get("skipped"):
            dt = str(ev.get("decision_type") or "").upper()
            if dt and dt not in slot["matched_decisions"]:
                slot["matched_decisions"].append(dt)

    if run.status == "TERMINATED_EARLY":
        for slot in nodes.values():
            if any(d in _DEFECT_DECISIONS for d in slot["matched_decisions"]):
                slot["terminated_here"] = True
                break

    outer_tools = [
        {
            "tool_name": inv.get("tool_name") or "",
            "phase": inv.get("phase") or "",
            "ok": bool(inv.get("ok")),
            "duration_ms": int(inv.get("duration_ms") or 0),
        }
        for inv in (
            run.tool_invocations
            .filter(Q(tool_binding__isnull=True) | Q(phase__in=("FETCH", "PARSE")))
            .values("tool_name", "phase", "ok", "duration_ms")
            .order_by("called_at")
        )
    ]
    return list(nodes.values()), outer_tools


def _agent_status_light(node: dict[str, Any]) -> str:
    """SOP/agent status for the summary tab: CLEAN / DEFECT / OUT_OF_SCOPE /
    NOT_APPLICABLE (Inconclusive is no longer emitted here — a non-executed SOP
    is a scope state, not an unknown)."""
    if node.get("terminated_here") or any(
        d in _DEFECT_DECISIONS for d in node.get("matched_decisions", [])
    ):
        return DEFECT
    # Summary-rollup nodes carry rows under "evals"; agents-rollup nodes under
    # "evaluations". Accept either so both tabs classify identically.
    evals = node.get("evals")
    if evals is None:
        evals = node.get("evaluations", [])
    return trace_builder.node_audit_status(evals)


def _build_agents_rollup(
    run: RuleExecutionRun,
) -> list[dict[str, Any]]:
    """Per-node rollup for the agents tab — skips outer tools and trace_json."""
    nodes: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def _node_slot(shape_id: str, shape_label: str) -> dict[str, Any]:
        slot = nodes.get(shape_id)
        if slot is None:
            slot = {
                "shape_id": shape_id,
                "shape_label": shape_label,
                "evaluations": [],
                "tool_invocations": [],
                "matched_decisions": [],
                "terminated_here": False,
            }
            nodes[shape_id] = slot
        elif shape_label and not slot["shape_label"]:
            slot["shape_label"] = shape_label
        return slot

    for ev in (
        run.evaluations
        .select_related("rule_binding__shape")
        .order_by("order_index")
    ):
        rb = ev.rule_binding
        if rb is not None:
            shape_id = str(rb.shape_id)
            shape_label = (rb.shape.label or "") if rb.shape else ""
        else:
            shape_id = f"orphaned:{ev.rule_key}"
            shape_label = ""
        slot = _node_slot(shape_id, shape_label)
        slot["evaluations"].append({
            "order_index": ev.order_index,
            "rule_key": ev.rule_key,
            "rule_source": ev.rule_source,
            "condition": ev.condition,
            "action": ev.action,
            "matched": ev.matched,
            "skipped": getattr(ev, "skipped", False),
            "skip_reason": getattr(ev, "skip_reason", ""),
            "confidence": ev.confidence,
            "reasoning": ev.reasoning,
            "decision_type": ev.decision_type,
            "codes": list(ev.codes or []),
            "llm_provider": ev.llm_provider,
            "llm_ms": ev.llm_ms,
        })
        if ev.matched and not getattr(ev, "skipped", False):
            dt = (ev.decision_type or "").upper()
            if dt and dt not in slot["matched_decisions"]:
                slot["matched_decisions"].append(dt)

    for inv in (
        run.tool_invocations
        .select_related("tool_binding__shape")
        .exclude(Q(tool_binding__isnull=True) | Q(phase__in=("FETCH", "PARSE")))
        .order_by("called_at")
    ):
        tb = inv.tool_binding
        if tb is None:
            continue
        shape_id = str(tb.shape_id)
        shape_label = (tb.shape.label or "") if tb.shape else ""
        slot = _node_slot(shape_id, shape_label)
        slot["tool_invocations"].append({
            "tool_name": inv.tool_name,
            "phase": inv.phase,
            "ok": inv.ok,
            "duration_ms": inv.duration_ms,
            "error": inv.error,
            "called_at": inv.called_at,
        })

    if run.status == "TERMINATED_EARLY":
        for slot in nodes.values():
            if any(d in _DEFECT_DECISIONS for d in slot["matched_decisions"]):
                slot["terminated_here"] = True
                break

    return list(nodes.values())


def _serialize_agent_summary_light(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["shape_id"],
        "agentName": node["shape_label"] or node["shape_id"],
        "status": _agent_status_light(node),
        "processSummary": node["reasonings"][:10],
    }


def _run_header_payload_light(run: RuleExecutionRun, trace=None) -> dict[str, Any]:
    return {
        "claimId": run.claim_id,
        "runId": str(run.id),
        "batchId": str(run.batch_id) if run.batch_id else None,
        "workflowId": str(run.workflow_id),
        "claimStatus": _claim_status_light(run, trace),
        "runStatus": run.status,
        "errorMessage": run.error_message or "",
        "finalDecisionType": run.final_decision_type or "",
        "appliedCodes": list(run.applied_codes or []),
        "narrative": run.narrative or "",
        "claimLob": run.claim_lob or {},
        "lobLabel": (run.claim_lob or {}).get("label", ""),
        "processingTimeMin": _processing_time_min(run),
        "startedAt": _iso_utc(run.started_at),
        "finishedAt": _iso_utc(run.finished_at),
        "reviewStatus": run.review_status or None,
        "auditorStatus": run.auditor_status or None,
        "feedback": run.review_feedback or None,
        **_review_date_fields(run),
        **_reviewer_fields(run),
        **_excel_claim_fields(run.claim_payload),
    }


def _run_header_payload(
    run: RuleExecutionRun,
    nodes: list[dict[str, Any]],
    trace,
) -> dict[str, Any]:
    return {
        "claimId": run.claim_id,
        "runId": str(run.id),
        "batchId": str(run.batch_id) if run.batch_id else None,
        "workflowId": str(run.workflow_id),
        "claimStatus": _claim_status(run, nodes, trace),
        "runStatus": run.status,
        "errorMessage": run.error_message or "",
        "finalDecisionType": run.final_decision_type or "",
        "appliedCodes": list(run.applied_codes or []),
        "narrative": run.narrative or "",
        "claimLob": run.claim_lob or {},
        "lobLabel": (run.claim_lob or {}).get("label", ""),
        "processingTimeMin": _processing_time_min(run),
        "startedAt": _iso_utc(run.started_at),
        "finishedAt": _iso_utc(run.finished_at),
        "reviewStatus": run.review_status or None,
        "auditorStatus": run.auditor_status or None,
        "feedback": run.review_feedback or None,
        **_review_date_fields(run),
        **_reviewer_fields(run),
        **_excel_claim_fields(run.claim_payload),
    }


_TERMINAL_BATCH_STATUSES = {"COMPLETED", "PARTIAL", "FAILED"}


def _dispatch_batch(
    *,
    request: Request,
    workflow_id: str,
) -> tuple[Response, str | None]:
    """Shared kickoff: validate upload → stash xlsx → reserve BatchExecutionRun
    → dispatch the Celery master task → return (error_response_or_None, batch_id).

    On success returns ``(None, batch_id)``. On validation failure returns
    ``(Response(4xx/5xx), None)`` — callers should just forward that response.
    """
    upload = request.FILES.get("file")
    if upload is None:
        return Response({"detail": "file (multipart) is required"},
                        status=status.HTTP_400_BAD_REQUEST), None
    if not upload.name.lower().endswith(".xlsx"):
        return Response({"detail": "only .xlsx is supported"},
                        status=status.HTTP_400_BAD_REQUEST), None

    batch_id = str(uuid.uuid4())

    xlsx_path = _execution_upload_dir() / f"{batch_id}.xlsx"
    try:
        with xlsx_path.open("wb") as fh:
            for chunk in upload.chunks():
                fh.write(chunk)
    except OSError as exc:
        logger.exception("run-batch: could not stash upload")
        return Response(
            {"detail": f"failed to stash upload: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ), None

    BatchExecutionRun.objects.create(
        id=batch_id,
        workflow_id=str(workflow_id),
        source_filename=upload.name,
        claim_id_column=str(request.data.get("claim_id_column") or "claim_id"),
        total_claims=0,
        status="RUNNING",
    )

    from .tasks import run_batch_async
    run_batch_async.delay(
        batch_id=batch_id,
        xlsx_path=str(xlsx_path),
        workflow_id=str(workflow_id),
        filename=upload.name,
        claim_id_column=request.data.get("claim_id_column") or None,
        sheet_name=request.data.get("sheet_name") or None,
    )
    logger.info("run-batch dispatched batch=%s workflow=%s file=%s",
                batch_id, workflow_id, upload.name)
    return None, batch_id


class RunBatchView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch/

    Multipart form: ``file`` (.xlsx, required), ``claim_id_column`` (optional),
    ``sheet_name`` (optional). Dispatches the batch through Celery to a
    dedicated OS subprocess (same path as ``/run-batch-async/``), then
    polls the ``BatchExecutionRun`` row until it reaches a terminal state
    and returns the aggregated batch dict from the DB.

    HTTP response is still synchronous — caller blocks until the batch
    finishes — but the work no longer runs inside the gunicorn worker.
    """
    parser_classes = [MultiPartParser]
    # Bound on how long the sync HTTP request will wait. Overridable via env
    # for CI/large batches; align with the gunicorn / proxy timeout in prod.
    _DEFAULT_TIMEOUT_SEC = 600
    _POLL_INTERVAL_SEC = 0.5

    def post(self, request: Request, workflow_id: str) -> Response:
        err, batch_id = _dispatch_batch(request=request, workflow_id=workflow_id)
        if err is not None:
            return err

        timeout = float(os.environ.get(
            "RUN_BATCH_SYNC_TIMEOUT_SEC", str(self._DEFAULT_TIMEOUT_SEC)))
        deadline = _monotonic() + timeout

        # Poll the BatchExecutionRun row until terminal.
        while True:
            try:
                batch = BatchExecutionRun.objects.prefetch_related("runs").get(id=batch_id)
            except BatchExecutionRun.DoesNotExist:
                # Shouldn't happen — _dispatch_batch just created it.
                logger.error("run-batch: batch row %s disappeared mid-poll", batch_id)
                return Response(
                    {"detail": "batch row disappeared during run"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            if batch.status in _TERMINAL_BATCH_STATUSES:
                payload = BatchExecutionRunSerializer(batch).data
                # Preserve the response shape the legacy in-process runner
                # returned: top-level batch fields + a `results` list of
                # per-claim dicts.
                results = []
                for run in batch.runs.all().order_by("started_at"):
                    results.append({
                        "run_id":              str(run.id),
                        "claim_id":            run.claim_id,
                        "status":              run.status,
                        "final_decision_type": run.final_decision_type,
                        "applied_codes":       list(run.applied_codes or []),
                        "narrative":           run.narrative,
                        "error_message":       run.error_message,
                    })
                response_body = {
                    "batch_id":      str(batch.id),
                    "status":        batch.status,
                    "total_claims":  batch.total_claims,
                    "completed":     batch.completed,
                    "failed":        batch.failed,
                    "error_message": batch.error_message,
                    "results":       results,
                }
                http_status = (status.HTTP_400_BAD_REQUEST
                               if batch.status == "FAILED"
                               else status.HTTP_200_OK)
                return Response(response_body, status=http_status)

            if _monotonic() >= deadline:
                logger.warning(
                    "run-batch: timed out waiting on batch=%s after %ss "
                    "(status=%s); returning 504 — work continues in the background",
                    batch_id, timeout, batch.status,
                )
                return Response(
                    {
                        "detail":     "batch is still running; subscribe to the stream "
                                       "URL or poll GET /api/execute/batches/<id>/",
                        "batch_id":   batch_id,
                        "status":     batch.status,
                        "stream_url": f"/api/execute/batches/{batch_id}/events/",
                    },
                    status=status.HTTP_504_GATEWAY_TIMEOUT,
                )

            time.sleep(self._POLL_INTERVAL_SEC)


class BatchLatestView(APIView):
    """GET /api/execute/batches/latest/ — most recent batch from shared DB."""

    def get(self, _request: Request) -> Response:
        batch = (
            BatchExecutionRun.objects
            .prefetch_related("runs")
            .order_by("-started_at")
            .first()
        )
        if batch is None:
            return Response({"detail": "no batches"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BatchExecutionRunSerializer(batch).data)


class BatchDetailView(APIView):
    def get(self, _request: Request, batch_id: str) -> Response:
        try:
            batch = BatchExecutionRun.objects.prefetch_related("runs").get(id=batch_id)
        except BatchExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BatchExecutionRunSerializer(batch).data)


def _parse_yyyy_mm_dd(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=dt_timezone.utc)
    except ValueError:
        return None


def _filter_run_list_queryset(request: Request, qs):
    """Apply list-view filters from query params (claim id, status, date range)."""
    claim_id = (request.query_params.get("claim_id") or "").strip()
    if claim_id:
        qs = qs.filter(claim_id__icontains=claim_id)

    claim_status = (request.query_params.get("claim_status") or "").strip().upper()
    if claim_status == CLEAN:
        qs = qs.filter(final_decision_type__in=list(_CLEAN_DECISIONS))
    elif claim_status == DEFECT:
        qs = qs.filter(
            Q(status="TERMINATED_EARLY")
            | Q(final_decision_type__in=list(_DEFECT_DECISIONS))
        )
    elif claim_status == INCONCLUSIVE:
        qs = qs.filter(
            Q(status__in=["FAILED", "FETCH_FAILED"])
            | Q(final_decision_type__iexact="INCONCLUSIVE")
            | Q(final_decision_type="")
        )
    elif claim_status == IN_PROGRESS:
        qs = qs.filter(status="RUNNING")

    from_date = _parse_yyyy_mm_dd(request.query_params.get("from_date", ""))
    if from_date is not None:
        qs = qs.filter(finished_at__gte=from_date)

    to_date = _parse_yyyy_mm_dd(request.query_params.get("to_date", ""))
    if to_date is not None:
        # inclusive end-of-day
        end = to_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        qs = qs.filter(finished_at__lte=end)

    return qs


def _avg_processing_time_min(qs) -> float:
    agg = (
        qs.filter(finished_at__isnull=False, started_at__isnull=False)
        .aggregate(
            avg_duration=Avg(
                ExpressionWrapper(
                    F("finished_at") - F("started_at"),
                    output_field=DurationField(),
                )
            )
        )
    )
    avg = agg.get("avg_duration")
    if avg is None:
        return 0.0
    return round(avg.total_seconds() / 60.0, 1)


class RunListView(APIView):
    """GET /api/execute/runs/ — all processed claims across every batch."""

    _DEFAULT_LIMIT = 25
    _MAX_LIMIT = 200

    def get(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", self._DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = self._DEFAULT_LIMIT
        try:
            offset = int(request.query_params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0

        limit = max(1, min(limit, self._MAX_LIMIT))
        offset = max(0, offset)

        qs = (
            RuleExecutionRun.objects
            .select_related("trace")
            .order_by("-started_at")
        )
        qs = _filter_run_list_queryset(request, qs)
        total = qs.count()
        runs = list(qs[offset: offset + limit])
        reviewer_names = resolve_reviewer_names(
            [r.htl_reviewer for r in runs] + [r.original_auditor for r in runs]
        )
        return Response({
            "count": total,
            "limit": limit,
            "offset": offset,
            "avg_processing_time_min": _avg_processing_time_min(qs),
            "results": [
                serialize_run_summary(r, reviewer_names=reviewer_names)
                for r in runs
            ],
        })


class RunDetailView(APIView):
    def get(self, _request: Request, run_id: str) -> Response:
        try:
            run = (RuleExecutionRun.objects
                   .prefetch_related("evaluations", "tool_invocations")
                   .get(id=run_id))
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(RuleExecutionRunSerializer(run).data)


_VALID_REVIEW_STATUSES = frozenset({
    "", "pending", "in_progress", "approved", "rejected", "completed",
})

_REVIEW_TO_AUDITOR_STATUS = {
    "": "",
    "pending": "PENDING",
    "in_progress": "IN_PROGRESS",
    "approved": "APPROVED",
    "rejected": "REJECTED",
    "completed": "COMPLETED",
}


def _auditor_status_for_review(review_status: str) -> str:
    return _REVIEW_TO_AUDITOR_STATUS.get(review_status, "")


def _parse_review_status_body(request: Request) -> tuple[str | None, Response | None]:
    if "reviewStatus" not in request.data and "review_status" not in request.data:
        return None, Response(
            {"detail": "reviewStatus is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    raw = request.data.get("reviewStatus", request.data.get("review_status"))
    value = str(raw or "").strip().lower().replace("-", "_")
    if value not in _VALID_REVIEW_STATUSES:
        return None, Response(
            {
                "detail": f"invalid reviewStatus: {raw!r}",
                "allowed": sorted(_VALID_REVIEW_STATUSES - {""}),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return value, None


def _review_date_fields(run: RuleExecutionRun) -> dict[str, Any]:
    return {
        "reviewStartedAt": _iso_utc(run.review_started_at),
        "reviewedAt": _iso_utc(run.reviewed_at),
    }


def _reviewer_fields(run: RuleExecutionRun) -> dict[str, Any]:
    names = resolve_reviewer_names([run.htl_reviewer, run.original_auditor])
    return {
        "htlReviewer": names.get(run.htl_reviewer) or (run.htl_reviewer or None),
        "originalAuditor": names.get(run.original_auditor) or (run.original_auditor or None),
    }


def _reviewer_from_request(request: Request) -> str:
    """userID (JWT ``sub``) of the authenticated reviewer.

    Empty when unauthenticated. Resolved to a display name at response time
    via ``resolve_reviewer_names`` — this stores the raw corebackend userID.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    uid = str(getattr(user, "id", "") or "").strip()
    if uid:
        return uid
    auth = getattr(request, "auth", None)
    if isinstance(auth, dict):
        sub = str(auth.get("sub") or "").strip()
        if sub:
            return sub
    return ""


def _current_uid_or_401(request: Request) -> tuple[str | None, Response | None]:
    """Resolve the caller's userID, or a 401 if somehow unauthenticated.

    ``IsAuthenticated`` is the global DRF default, so this should never
    actually trip in practice — it's a defensive backstop for the lock/
    release checks below, which need a concrete identity to compare against.
    """
    uid = _reviewer_from_request(request)
    if not uid:
        return None, Response(
            {"detail": "authentication required to review a claim"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return uid, None


def _holder_display_name(run: RuleExecutionRun) -> str:
    names = resolve_reviewer_names([run.htl_reviewer])
    return names.get(run.htl_reviewer) or run.htl_reviewer


def _locked_by_other_response(
    run: RuleExecutionRun, current_uid: str,
) -> Response | None:
    """409 when another reviewer currently holds this claim ``in_progress``.

    Only the holder can start/approve/reject/release while locked; anyone
    (including the holder) may act once it's unlocked (never started, or
    already released back to pending).
    """
    if run.review_status == "in_progress" and run.htl_reviewer and run.htl_reviewer != current_uid:
        holder = _holder_display_name(run)
        return Response(
            {"detail": f"already being reviewed by {holder}", "heldBy": holder},
            status=status.HTTP_409_CONFLICT,
        )
    return None


def _apply_review_release(run: RuleExecutionRun) -> RuleExecutionRun:
    run.review_status = "pending"
    run.auditor_status = _auditor_status_for_review("pending")
    run.htl_reviewer = ""
    run.review_started_at = None
    run.save(update_fields=[
        "review_status", "auditor_status", "htl_reviewer", "review_started_at",
    ])
    return run


def _serialize_review_status_update(run: RuleExecutionRun) -> dict[str, Any]:
    return {
        "runId": str(run.id),
        "claimId": run.claim_id,
        "batchId": str(run.batch_id) if run.batch_id else None,
        "runStatus": run.status,
        "claimStatus": claim_audit_status(run),
        "reviewStatus": run.review_status or None,
        "auditorStatus": run.auditor_status or None,
        "feedback": run.review_feedback or None,
        **_review_date_fields(run),
        **_reviewer_fields(run),
    }


def _parse_feedback_body(
    request: Request, *, required: bool,
) -> tuple[str | None, Response | None]:
    raw = request.data.get("feedback", request.data.get("reviewFeedback"))
    if raw is None:
        raw = ""
    value = str(raw).strip()
    if required and not value:
        return None, Response(
            {"detail": "feedback is required when rejecting a review"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return value, None


def _apply_review_decision(
    run: RuleExecutionRun,
    *,
    review_status: str,
    feedback: str,
    htl_reviewer: str = "",
) -> RuleExecutionRun:
    now = dj_timezone.now()
    run.review_status = review_status
    run.auditor_status = _auditor_status_for_review(review_status)
    run.review_feedback = feedback
    if review_status in {"approved", "rejected"}:
        run.reviewed_at = now
    fields = ["review_status", "auditor_status", "review_feedback", "reviewed_at"]
    if htl_reviewer:
        run.htl_reviewer = htl_reviewer
        fields.append("htl_reviewer")
    run.save(update_fields=fields)
    return run


def _apply_review_status(
    run: RuleExecutionRun,
    review_status: str,
    *,
    htl_reviewer: str = "",
) -> RuleExecutionRun:
    now = dj_timezone.now()
    run.review_status = review_status
    run.auditor_status = _auditor_status_for_review(review_status)
    fields = ["review_status", "auditor_status"]
    if review_status == "in_progress":
        run.review_started_at = now
        fields.append("review_started_at")
    if htl_reviewer:
        run.htl_reviewer = htl_reviewer
        fields.append("htl_reviewer")
    run.save(update_fields=fields)
    return run

class RunReviewApproveView(APIView):
    """POST /api/execute/runs/<run_id>/review/approve/"""

    def post(self, request: Request, run_id: str) -> Response:
        try:
            run = RuleExecutionRun.objects.get(id=run_id)
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        feedback, err = _parse_feedback_body(request, required=False)
        if err is not None:
            return err
        assert feedback is not None

        _apply_review_decision(
            run, review_status="approved", feedback=feedback,
            htl_reviewer=current_uid,
        )
        return Response(_serialize_review_status_update(run))


class RunReviewRejectView(APIView):
    """POST /api/execute/runs/<run_id>/review/reject/"""

    def post(self, request: Request, run_id: str) -> Response:
        try:
            run = RuleExecutionRun.objects.get(id=run_id)
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        feedback, err = _parse_feedback_body(request, required=True)
        if err is not None:
            return err
        assert feedback is not None

        _apply_review_decision(
            run, review_status="rejected", feedback=feedback,
            htl_reviewer=current_uid,
        )
        return Response(_serialize_review_status_update(run))


class ClaimReviewApproveView(APIView):
    """POST /api/claims/<claim_id>/review/approve/"""

    def post(self, request: Request, claim_id: str) -> Response:
        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        feedback, err = _parse_feedback_body(request, required=False)
        if err is not None:
            return err
        assert feedback is not None

        _apply_review_decision(
            run, review_status="approved", feedback=feedback,
            htl_reviewer=current_uid,
        )
        return Response(_serialize_review_status_update(run))


class ClaimReviewRejectView(APIView):
    """POST /api/claims/<claim_id>/review/reject/"""

    def post(self, request: Request, claim_id: str) -> Response:
        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        feedback, err = _parse_feedback_body(request, required=True)
        if err is not None:
            return err
        assert feedback is not None

        _apply_review_decision(
            run, review_status="rejected", feedback=feedback,
            htl_reviewer=current_uid,
        )
        return Response(_serialize_review_status_update(run))


class RunReviewStatusView(APIView):
    """PATCH /api/execute/runs/<run_id>/review-status/

    Mark the human audit / review workflow for one claim run (e.g.
    ``{"reviewStatus": "in_progress"}``). Blocked with 409 while another
    reviewer already holds the claim ``in_progress`` — see
    ``RunReviewReleaseView``.
    """

    def patch(self, request: Request, run_id: str) -> Response:
        try:
            run = RuleExecutionRun.objects.get(id=run_id)
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)

        review_status, err = _parse_review_status_body(request)
        if err is not None:
            return err
        assert review_status is not None

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        _apply_review_status(run, review_status, htl_reviewer=current_uid)
        return Response(_serialize_review_status_update(run))


class ClaimReviewStatusView(APIView):
    """PATCH /api/claims/<claim_id>/review-status/

    Same as ``RunReviewStatusView`` but resolves the run via ``claim_id`` and
    optional ``?run_id=`` / ``?batch_id=`` query params.
    """

    def patch(self, request: Request, claim_id: str) -> Response:
        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        review_status, err = _parse_review_status_body(request)
        if err is not None:
            return err
        assert review_status is not None

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err
        lock_err = _locked_by_other_response(run, current_uid)
        if lock_err is not None:
            return lock_err

        _apply_review_status(run, review_status, htl_reviewer=current_uid)
        return Response(_serialize_review_status_update(run))


class RunReviewReleaseView(APIView):
    """POST /api/execute/runs/<run_id>/review/release/

    Only the reviewer currently holding a claim (``review_status=in_progress``,
    ``htl_reviewer`` = them) can release it — sets it back to ``pending`` with
    no holder, so another auditor can start it.
    """

    def post(self, request: Request, run_id: str) -> Response:
        try:
            run = RuleExecutionRun.objects.get(id=run_id)
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err

        if run.review_status != "in_progress" or not run.htl_reviewer:
            return Response(
                {"detail": "claim is not currently in progress"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if run.htl_reviewer != current_uid:
            holder = _holder_display_name(run)
            return Response(
                {"detail": f"only {holder} can release this claim"},
                status=status.HTTP_403_FORBIDDEN,
            )

        _apply_review_release(run)
        return Response(_serialize_review_status_update(run))


class ClaimReviewReleaseView(APIView):
    """POST /api/claims/<claim_id>/review/release/"""

    def post(self, request: Request, claim_id: str) -> Response:
        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        current_uid, err = _current_uid_or_401(request)
        if err is not None:
            return err

        if run.review_status != "in_progress" or not run.htl_reviewer:
            return Response(
                {"detail": "claim is not currently in progress"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if run.htl_reviewer != current_uid:
            holder = _holder_display_name(run)
            return Response(
                {"detail": f"only {holder} can release this claim"},
                status=status.HTTP_403_FORBIDDEN,
            )

        _apply_review_release(run)
        return Response(_serialize_review_status_update(run))


class RunNodesView(APIView):
    """GET /api/execute/runs/<run_id>/nodes/

    Per-canvas-node rollup for one claim's run. Walks the already-persisted
    ``RuleEvaluation`` + ``ToolInvocationRecord`` rows, groups them by the
    Shape that owned each binding, and returns one entry per node visited
    during the run — in the order the engine evaluated them.

    No new tables; this is purely a derived view.
    """
    def get(self, _request: Request, run_id: str) -> Response:
        try:
            run = (RuleExecutionRun.objects
                   .select_related("workflow")
                   .prefetch_related(
                       "evaluations__rule_binding__shape",
                       "tool_invocations__tool_binding__shape",
                   )
                   .get(id=run_id))
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        nodes, outer_tools = _build_node_rollup(run)

        return Response({
            "run_id":              str(run.id),
            "workflow_id":         str(run.workflow_id),
            "claim_id":            run.claim_id,
            "status":              run.status,
            "final_decision_type": run.final_decision_type,
            "applied_codes":       list(run.applied_codes or []),
            "narrative":           run.narrative,
            "nodes":               nodes,
            "outer_tool_invocations": outer_tools,
        })


def _serialize_executive_summary(row) -> dict[str, Any] | None:
    """Shape the ClaimExecutiveSummary row for the summary payload."""
    if row is None:
        return None
    return {
        "headline": row.headline or "",
        "overallSummary": row.overall_summary or "",
        "verdict": row.verdict or "",
        "auditStatus": row.audit_status or "",
        "keyFindings": list(row.key_findings or []),
        "steps": [
            {
                "shapeId": s.get("shape_id", ""),
                "agentName": s.get("agent_name", ""),
                "status": s.get("status", ""),
                "summary": s.get("summary", ""),
            }
            for s in (row.step_summaries or [])
            if isinstance(s, dict)
        ],
        "generatedBy": row.generated_by or "",
        "updatedAt": _iso_utc(row.updated_at),
    }


class ClaimSummaryView(APIView):
    """GET /api/claims/<claim_id>/summary/ — header + outer tools + summaries."""

    def get(self, request: Request, claim_id: str) -> Response:
        from .models import ClaimExecutiveSummary, ClaimTrace

        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        trace = (
            ClaimTrace.objects
            .filter(run_id=run.id)
            .only("final_status")
            .first()
        )
        nodes, outer_tools = _build_summary_rollup(run)
        exec_summary = (
            ClaimExecutiveSummary.objects.filter(run_id=run.id).first()
        )
        payload = {
            **_run_header_payload_light(run, trace),
            "executiveSummary": _serialize_executive_summary(exec_summary),
            "agents": [_serialize_agent_summary_light(node) for node in nodes],
            "outerToolInvocations": [
                {
                    "phase": inv["phase"],
                    "tool": inv["tool_name"],
                    "status": "completed" if inv["ok"] else "failed",
                    "durationMs": inv["duration_ms"],
                }
                for inv in outer_tools
            ],
            "reviewStatus": run.review_status or None,
            "auditorStatus": run.auditor_status or None,
            "feedback": run.review_feedback or None,
        }
        return Response(payload, status=status.HTTP_200_OK)


class ClaimAgentsView(APIView):
    """GET /api/claims/<claim_id>/agents/ — per-node execution + evaluations."""

    def get(self, request: Request, claim_id: str) -> Response:
        from .models import ClaimTrace

        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(
            claim_id, run_uuid, batch_uuid, lightweight=True,
        )
        if err is not None:
            return err
        assert run is not None

        trace = (
            ClaimTrace.objects
            .filter(run_id=run.id)
            .only("final_status")
            .first()
        )
        nodes = _build_agents_rollup(run)
        payload = {
            **_run_header_payload_light(run, trace),
            "agents": [
                _serialize_agent_detail_light(node, run) for node in nodes
            ],
        }
        return Response(payload, status=status.HTTP_200_OK)


class ClaimProcessingView(APIView):
    """GET /api/claims/<claim_id>/processing/ — legacy full snapshot (all tabs)."""

    def get(self, request: Request, claim_id: str) -> Response:
        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err
        run, err = _load_run_for_claim(claim_id, run_uuid, batch_uuid)
        if err is not None:
            return err
        assert run is not None

        nodes, outer_tools, trace, trace_by_shape = _claim_run_context(run)

        from sop_ingestion.models import LLMCallLog
        llm_calls = list(
            LLMCallLog.objects.filter(execution_run_id=run.id).order_by("id")
        )

        payload = {
            **_run_header_payload(run, nodes, trace),
            "agents": [_serialize_agent(node, run, trace_by_shape) for node in nodes],
            "outerToolInvocations": [
                {
                    "phase": inv["phase"],
                    "tool": inv["tool_name"],
                    "status": "completed" if inv["ok"] else "failed",
                    "durationMs": inv["duration_ms"],
                }
                for inv in outer_tools
            ],
            "llmCalls": [
                {
                    "stage":            log.stage,
                    "agentName":        log.agent_name,
                    "provider":         log.llm_provider,
                    "model":            log.llm_model,
                    "promptTokens":     log.prompt_tokens,
                    "completionTokens": log.completion_tokens,
                    "totalTokens":      log.total_tokens,
                    "durationMs":       log.duration_ms,
                    "success":          log.success,
                    "error":            log.error_message,
                    "calledAt":         _iso_utc(log.called_at),
                }
                for log in llm_calls
            ],
            "reviewStatus": run.review_status or None,
            "auditorStatus": run.auditor_status or None,
            "feedback": run.review_feedback or None,
        }
        return Response(payload, status=status.HTTP_200_OK)


import re as _re

_SOP_HASH_PREFIX_RE = _re.compile(r"^[0-9a-fA-F]{16,}_")


def _clean_sop_name(name: str) -> str:
    """Human-readable SOP name from a raw trace ``sop_name``.

    Ingested PDF/DOCX uploads carry a storage key title like
    ``37cb2d10..._OBH_Facets_Timely_Filing``; strip the hash prefix and turn
    underscores into spaces. Already-clean titles pass through unchanged.
    """
    raw = (name or "").strip()
    cleaned = _SOP_HASH_PREFIX_RE.sub("", raw).replace("_", " ").strip()
    return cleaned or raw


def _sop_link_index() -> dict[str, tuple[int, str]]:
    """Map ``AuditSop.title`` -> ``(sop_id, source_url)`` for current SOPs.

    The execution trace stores ``sop_name`` = the SOP's title, so this lets a
    step resolve its SOP. Cleaned names are also indexed as a fallback for
    minor title drift.
    """
    from sop_ingestion.models import AuditSop

    idx: dict[str, tuple[int, str]] = {}
    clean_idx: dict[str, tuple[int, str]] = {}
    rows = (
        AuditSop.objects
        .filter(is_current=True)
        .order_by("id")
        .values_list("id", "title", "url")
    )
    for sop_id, title, url in rows:
        if not title:
            continue
        val = (sop_id, url or "")
        idx.setdefault(title, val)
        clean_idx.setdefault(_clean_sop_name(title), val)
    # Merge cleaned fallbacks without clobbering exact-title hits.
    for key, val in clean_idx.items():
        idx.setdefault(key, val)
    return idx


def _enrich_trace_sop_links(data: Any) -> Any:
    """Attach SOP identity + source availability to each trace step.

    Purely additive read-side enrichment so the claim-detail UI can show the
    real SOP name and, for HTML SOPs, a click-through to the raw crawled SOP
    HTML (cached in Mongo). PDF uploads and node/YAML SOPs have no HTML source,
    so they are flagged ``sop_available=False`` with a ``sop_kind`` and get no
    view URL. Nothing rewrites stored ``trace_json``.
    """
    from sop_ingestion.sop_html_crawler import classify_sop_source, stored_sop_ids

    if not isinstance(data, list) or not data:
        return data
    idx = _sop_link_index()
    # SOPs whose HTML template is already cached in Mongo — viewable even when
    # the source URL itself is not crawlable (e.g. a file:// upload whose HTML
    # was loaded via scripts/load_sop_html_to_mongo.py). Fetched once per call.
    stored = stored_sop_ids()
    for step in data:
        if not isinstance(step, dict):
            continue
        raw = step.get("sop_name") or ""
        display = _clean_sop_name(raw)
        step["sop_display_name"] = display
        hit = idx.get(raw) or idx.get(display)
        if hit:
            sop_id, url = hit
            step["sop_id"] = sop_id
            kind = classify_sop_source(url)
            if kind == "html" or sop_id in stored:
                # HTML source (crawled) or a manually stored template — the raw
                # original SOP template (with rules) can be opened.
                step["sop_kind"] = "html"
                step["sop_available"] = True
                step["sop_view_url"] = f"/api/ingest/sops/{sop_id}/stored-html/"
            else:
                # PDF upload or node/workflow (YAML) SOP — no HTML to open.
                step["sop_kind"] = kind
                step["sop_available"] = False
    return data


def _load_claim_trace_record(
    claim_id: str,
    run_uuid: uuid.UUID | None,
    batch_uuid: uuid.UUID | None,
    *,
    fields: tuple[str, ...],
):
    from .models import ClaimTrace

    if run_uuid is not None:
        return (
            ClaimTrace.objects
            .filter(run_id=run_uuid)
            .only(*fields)
            .first()
        )
    qs = ClaimTrace.objects.filter(claim_id=claim_id).only(*fields)
    if batch_uuid is not None:
        qs = qs.filter(run__batch_id=batch_uuid)
    return qs.order_by("-created_at").first()


class ClaimTraceView(APIView):
    """GET /api/claims/<claim_id>/trace/ and /explainability/.

    Additive endpoints serving the denormalized ``ClaimTrace`` arrays in the
    ``trace.json`` / ``explainability.json`` shapes. ``?run_id=`` overrides the
    claim lookup; ``?batch_id=`` scopes it; ``?download=1`` returns the JSON as
    a file attachment. ``kind`` is set per URL route ("trace" | "explainability").
    """
    kind = "trace"

    def get(self, request: Request, claim_id: str) -> Response:
        from django.http import JsonResponse

        run_uuid, batch_uuid, err = _parse_run_lookup_uuids(request)
        if err is not None:
            return err

        download = (request.query_params.get("download") or "").strip() in {"1", "true", "yes"}
        json_field = "explainability_json" if self.kind == "explainability" else "trace_json"
        trace = _load_claim_trace_record(
            claim_id,
            run_uuid,
            batch_uuid,
            fields=("claim_id", json_field),
        )

        if trace is None:
            return _relay_error(
                f"No trace found for claim {claim_id}",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        data = getattr(trace, json_field) or []

        if self.kind == "trace":
            data = _enrich_trace_sop_links(data)

        if download:
            resp = JsonResponse(data, safe=False, json_dumps_params={"indent": 2})
            fname = f"{self.kind}_{trace.claim_id or claim_id}.json"
            resp["Content-Disposition"] = f'attachment; filename="{fname}"'
            return resp
        return Response(data, status=status.HTTP_200_OK)


# ── Streaming endpoints ──────────────────────────────────────────────────────


def _execution_upload_dir() -> Path:
    """Where the kickoff view stashes the .xlsx for the Celery task to read.

    Sits under MEDIA_ROOT when configured, tempfile.gettempdir() under the system temp
    directory (matches Django's default upload behaviour).
    """
    media = getattr(settings, "MEDIA_ROOT", "") or ""
    base = Path(media) if media else Path(tempfile.gettempdir())
    target = base / "execution_uploads"
    target.mkdir(parents=True, exist_ok=True)
    return target


class RunBatchAsyncView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch-async/

    Multipart upload — same form fields as the sync ``RunBatchView``.
    Returns 202 immediately with::

        {
            "batch_id":   "<uuid>",
            "status":     "RUNNING",
            "stream_url": "/api/execute/batches/<id>/events/",
        }

    The actual per-claim work runs in the ``execution_app.run_batch_async``
    Celery task. The SPA subscribes to ``stream_url`` via EventSource to
    receive per-Shape / per-rule / per-claim events as they happen.
    """
    parser_classes = [MultiPartParser]
    def post(self, request: Request, workflow_id: str) -> Response:
        err, batch_id = _dispatch_batch(request=request, workflow_id=workflow_id)
        if err is not None:
            return err
        return Response(
            {
                "batch_id":   batch_id,
                "status":     "RUNNING",
                "stream_url": f"/api/execute/batches/{batch_id}/events/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


def _sse_format(event_kind: str, data: dict) -> bytes:
    """Format one SSE event. Always terminates with a blank line."""
    return (
        f"event: {event_kind}\n"
        f"data: {json.dumps(data, default=str)}\n\n"
    ).encode("utf-8")


def _claim_payload_from_run(run: RuleExecutionRun) -> dict:
    """Project a RuleExecutionRun row into a `claim` SSE payload."""
    from uhc_execution_engine.duplicate_claim import skip_metadata

    return {
        "claim_id":               run.claim_id,
        "run_id":                 str(run.id),
        "status":                 run.status,
        "final_decision_type":    run.final_decision_type,
        "applied_codes":          list(run.applied_codes or []),
        "narrative":              run.narrative,
        "error_message":          run.error_message,
        **_excel_claim_fields(run.claim_payload),
        **skip_metadata(run.claim_payload),
    }


class _EventStreamRenderer(BaseRenderer):
    """No-op renderer that advertises ``text/event-stream``.

    Exists solely to satisfy DRF's content negotiation for SSE endpoints.
    ``EventSource`` always sends ``Accept: text/event-stream``; with only
    ``JSONRenderer`` registered project-wide, the negotiator would 406 the
    request before our ``get()`` could return a ``StreamingHttpResponse``.

    ``render()`` is never invoked because the view returns a
    ``StreamingHttpResponse`` directly — DRF only renders ``Response``
    objects.
    """
    media_type = "text/event-stream"
    format = "txt"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):  # pragma: no cover
        return data


class BatchEventsView(APIView):
    """GET /api/execute/batches/<batch_id>/events/

    Server-Sent Events stream. Subscribes to the Redis pub/sub channel
    ``batch:<batch_id>`` and pipes each published event to the SPA. On
    connect, replays any already-finished claims from the DB so reconnects
    pick up cleanly without the publisher having to retain events.

    Terminates when a ``summary`` or ``error`` event arrives (the task's
    final publish) or when the client disconnects.
    """
    renderer_classes = [_EventStreamRenderer]

    def get(self, _request: Request, batch_id: str) -> StreamingHttpResponse:
        # Existence check before we commit to streaming. 404 is meaningful
        # only here; once we're inside the SSE body, all errors are
        # delivered as `event: error`.
        try:
            batch = BatchExecutionRun.objects.get(id=batch_id)
        except BatchExecutionRun.DoesNotExist:
            return StreamingHttpResponse(
                iter([_sse_format("error", {
                    "batch_id": str(batch_id),
                    "message": "batch not found",
                })]),
                content_type="text/event-stream",
                status=status.HTTP_404_NOT_FOUND,
            )

        response = StreamingHttpResponse(
            self._iter_sse(batch),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-store"
        response["X-Accel-Buffering"] = "no"  # nginx: don't buffer
        # NOTE: `Connection: keep-alive` is a hop-by-hop header (RFC 7230 §6.1)
        # — WSGI applications must not emit it; the server manages it. Setting
        # it here crashes wsgiref/runserver with AssertionError and is a no-op
        # under gunicorn (which already keeps HTTP/1.1 connections alive).
        return response

    def _iter_sse(self, batch: BatchExecutionRun) -> Iterator[bytes]:
        """Generator that yields SSE-framed bytes until the batch is done."""
        # Lazy imports — Redis isn't needed for the model-existence check.
        try:
            from uhc_execution_engine.llm import _get_redis
            pubsub = _get_redis().pubsub(ignore_subscribe_messages=True)
        except Exception as exc:
            logger.exception("batch-events: could not init pubsub")
            yield _sse_format("error", {
                "batch_id": str(batch.id),
                "message": f"redis unavailable: {exc}",
            })
            return

        channel = f"batch:{batch.id}"
        emitted_run_ids: set[str] = set()
        try:
            # Subscribe FIRST so events published during the DB catch-up
            # window aren't lost. Pubsub buffers between subscribe and the
            # first get_message call.
            pubsub.subscribe(channel)

            # batch_start envelope from the DB (the Celery task will also
            # publish its own batch_start once it parses the workbook; the
            # SPA can dedupe on batch_id if it cares).
            yield _sse_format("batch_start", {
                "batch_id":        str(batch.id),
                "workflow_id":     str(batch.workflow_id),
                "total_claims":    batch.total_claims,
                "claim_id_column": batch.claim_id_column,
                "source_filename": batch.source_filename,
                "status":          batch.status,
            })

            # Catch-up: replay finished claim rows so a late subscriber
            # sees them as `claim` events.
            already_finished = RuleExecutionRun.objects.filter(
                batch_id=batch.id, finished_at__isnull=False,
            ).order_by("started_at")
            for run in already_finished:
                emitted_run_ids.add(str(run.id))
                yield _sse_format("claim", _claim_payload_from_run(run))

            # If the batch is already terminal before we connected, emit
            # a synthetic summary and bail — no point waiting for events
            # that will never come.
            if batch.status in {"COMPLETED", "PARTIAL", "FAILED"}:
                yield _sse_format("summary", {
                    "id":            str(batch.id),
                    "status":        batch.status,
                    "total_claims":  batch.total_claims,
                    "completed":     batch.completed,
                    "failed":        batch.failed,
                    "error_message": batch.error_message,
                })
                return

            # Live loop. Heartbeat every _SSE_HEARTBEAT_SECONDS to defeat
            # proxy idle timeouts.
            while True:
                msg = pubsub.get_message(timeout=_SSE_HEARTBEAT_SECONDS)
                if msg is None:
                    yield b": keepalive\n\n"
                    continue
                if msg.get("type") != "message":
                    continue
                try:
                    raw = msg.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    event = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    logger.warning("batch-events: bad payload (%s): %r",
                                   exc, msg.get("data"))
                    continue
                kind = event.get("kind") or ""

                # Dedupe `claim` events against the catch-up set so a row
                # that just finished isn't emitted twice.
                if kind == "claim":
                    result = event.get("result") or {}
                    run_id = str(result.get("run_id") or "")
                    if run_id and run_id in emitted_run_ids:
                        continue
                    if run_id:
                        emitted_run_ids.add(run_id)
                    yield _sse_format("claim", result)
                    continue

                # batch_start from the task — skip; we already sent our
                # DB-derived one above.
                if kind == "batch_start":
                    continue

                # summary / error — terminal; emit and break.
                if kind == "summary":
                    yield _sse_format("summary", event.get("batch") or {})
                    break
                if kind == "error":
                    yield _sse_format("error", {
                        "batch_id": event.get("batch_id") or str(batch.id),
                        "message":  event.get("message") or "",
                    })
                    break

                # Pass-through for the engine's per-Shape / per-rule
                # events. Strip the envelope wrapper — SPA reads from
                # event.data directly.
                payload = {k: v for k, v in event.items() if k != "kind"}
                yield _sse_format(kind, payload)
        except GeneratorExit:
            # Client disconnected mid-stream. The Celery task is
            # unaffected; a reconnect will catch up via the DB read.
            logger.info("batch-events: client disconnected batch=%s", batch.id)
            raise
        except Exception as exc:
            logger.exception("batch-events: bridge crashed batch=%s", batch.id)
            yield _sse_format("error", {
                "batch_id": str(batch.id),
                "message":  f"bridge crashed: {exc}",
            })
        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("batch-events: pubsub teardown failed (%s)", exc)
