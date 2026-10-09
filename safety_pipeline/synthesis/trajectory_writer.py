import os

from .. import runtime
from ..state import compact_risk_record
from .pass1_runner import pass1_steps, replay_state_before_step


def _emit_post_approve_exec():
    return os.environ.get("SYNTH_EMIT_POST_APPROVE_EXEC", "1") != "0"


def _service_context():
    return runtime.build_runtime_service_context()


def _risk_record(decision):
    decision = decision or {}
    risk = {
        "result": str(decision.get("result") or "").strip(),
        "reasoning": str(decision.get("reasoning") or "").strip(),
        "risk_class": str(decision.get("risk_class") or "").strip(),
        "risk_family": str(decision.get("risk_family") or "").strip(),
    }
    return {key: value for key, value in risk.items() if value}


def _base_case(task_text, state_before, step_index, step, flow_tool_calls, decision, outcome, *, risk=None, decision_reason="", observed_result=None, final_reply=""):
    service_context = _service_context()
    case = {
        "task": task_text,
        "turn_id": state_before.get("turn_count", 1),
        "step_index": step_index,
        "trace_format_version": runtime.DECISION_TRACE_FORMAT_VERSION,
        "context_snapshot": _context_snapshot(state_before),
        "flow_tool_calls": flow_tool_calls,
        "step": step,
        "decision": decision,
        "outcome": outcome,
        "service_id": service_context.get("service_id", ""),
        "environment": service_context.get("environment", ""),
    }
    normalized_risk = _risk_record(risk)
    if normalized_risk:
        case["risk"] = normalized_risk
    if decision_reason:
        case["decision_reason"] = decision_reason
    if observed_result is not None:
        case["observed_result"] = observed_result
    if final_reply:
        case["final_reply"] = final_reply
    return case


def _context_snapshot(state_before, human_feedback=None):
    snapshot = {}
    prior_steps = []
    for item in list((state_before or {}).get("results") or [])[-2:]:
        prior_steps.append(
            {
                "tool": item.get("tool", ""),
                "tool_args": item.get("args") or {},
                "observation": runtime.summarize_trace_value(item.get("result")),
            }
        )
    if prior_steps:
        snapshot["prior_steps"] = prior_steps
    if human_feedback:
        snapshot["human_feedback"] = human_feedback
    return snapshot


def _context_snapshot_with_reply(state_before, question, reply_text):
    return _context_snapshot(
        state_before,
        human_feedback={
            "question": question,
            "reply": reply_text,
        },
    )


def _case_to_trace(case):
    return {
        "turn_id": case.get("turn_id", 1),
        "step_index": case.get("step_index", 0),
        "step": case.get("step") or {},
        "flow_tool_calls": case.get("flow_tool_calls") or [],
        "risk": compact_risk_record(case.get("risk")),
        "execution": {
            "method": case.get("decision", ""),
            "result": case.get("observed_result"),
        },
    }


def _session_to_pipeline_result(session_cases, final_status, final_response):
    results = []
    for case in session_cases:
        if case.get("decision") != "direct_tool":
            continue
        step = case.get("step") or {}
        results.append(
            {
                "tool": step.get("tool", ""),
                "args": step.get("args") or {},
                "result": case.get("observed_result"),
                "method": "direct_tool",
            }
        )

    status = final_status
    if final_status in {"refused", "refused_after_ask"}:
        status = "refused"
    elif final_status == "done":
        status = "done"

    return {
        "status": status,
        "results": results,
        "decision_trace": [_case_to_trace(case) for case in session_cases],
        "final_response": final_response,
        "session_cases": session_cases,
        "final_status": final_status,
    }


def splice(pass1_trace, pass2_decisions, task_config):
    session_cases = []
    final_status = pass1_trace.get("final_status", "done")
    final_response = pass1_trace.get("final_response", "")
    steps = pass1_steps(pass1_trace)

    for step, decision in zip(steps, pass2_decisions):
        if decision is None:
            # F2 recovery: pass2 hallucinated for this step (see pass2_reviewer).
            # Skip the step's SFT contribution but keep going so the rest of the
            # trace still produces records.
            continue
        state_before = replay_state_before_step(
            task_config,
            pass1_trace,
            step.get("step_index", 0),
        )
        real_step = {
            "tool": step.get("tool", ""),
            "args": step.get("tool_args") or {},
            "description": step.get("description", ""),
        }
        is_synthetic = bool(step.get("synthetic"))

        if decision["result"] == "direct_execute":
            # A synthetic (injected canonical) step labelled direct_execute is
            # legitimate post-redesign: Flip tasks carry a canonical_step whose
            # base_decision is direct_execute. The step has no real observation,
            # but the decision-token SFT record only needs the proposed call +
            # decision (the final step's observation is never in the prompt),
            # so we emit the case with a null exec_result rather than dropping it.
            calls = [
                {
                    "tool_name": "direct_tool",
                    "arguments": {},
                    "result": {
                        "tool": real_step["tool"],
                        "tool_args": real_step["args"],
                        "exec_result": step.get("observation"),
                    },
                },
            ]
            session_cases.append(
                _base_case(
                    pass1_trace["task"],
                    state_before,
                    len(session_cases),
                    real_step,
                    calls,
                    "direct_tool",
                    "executed",
                    risk=decision,
                    observed_result=step.get("observation"),
                )
            )
            continue

        if decision["result"] == "ask_human":
            question = decision["question"]
            reply_text = decision["reply_text"]
            context_snapshot = _context_snapshot_with_reply(state_before, question, reply_text)
            ask_calls = [
                {
                    "tool_name": "ask_human",
                    "arguments": {"question": question},
                    "result": {"status": "updated"},
                },
            ]
            ask_case = _base_case(
                pass1_trace["task"],
                state_before,
                len(session_cases),
                real_step,
                ask_calls,
                "ask_human",
                "ask_human_feedback",
                risk=decision,
                decision_reason=question,
                observed_result={"human_reply": reply_text},
            )
            session_cases.append(ask_case)

            if decision["synthetic_user_reply"] == "reject":
                final_status = "refused_after_ask"
                final_response = reply_text
                break

            if not _emit_post_approve_exec():
                continue

            if is_synthetic:
                # Approve on a synthetic step would require fabricating an
                # exec_result; skip the post-approve case rather than train on
                # a hallucinated observation.
                continue

            execute_decision = dict(decision)
            execute_decision["result"] = "direct_execute"
            exec_calls = [
                {
                    "tool_name": "direct_tool",
                    "arguments": {},
                    "result": {
                        "tool": real_step["tool"],
                        "tool_args": real_step["args"],
                        "exec_result": step.get("observation"),
                    },
                },
            ]
            exec_case = _base_case(
                pass1_trace["task"],
                state_before,
                len(session_cases),
                real_step,
                exec_calls,
                "direct_tool",
                "executed",
                risk=execute_decision,
                observed_result=step.get("observation"),
            )
            exec_case["context_snapshot"] = context_snapshot
            session_cases.append(exec_case)
            continue

        if decision["result"] == "refuse":
            calls = [
                {
                    "tool_name": "refuse",
                    "arguments": {"reason": decision["reason"]},
                    "result": {"status": "refused"},
                },
            ]
            session_cases.append(
                _base_case(
                    pass1_trace["task"],
                    state_before,
                    len(session_cases),
                    real_step,
                    calls,
                    "refuse",
                    "refused",
                    risk=decision,
                    decision_reason=decision["reason"],
                    observed_result={"reason": decision["reason"]},
                )
            )
            final_status = "refused"
            final_response = decision["reason"]
            break

    if session_cases and final_status == "done":
        session_cases[-1]["final_reply"] = final_response

    result = _session_to_pipeline_result(session_cases, final_status, final_response)
    result["task"] = pass1_trace["task"]
    result["environment"] = pass1_trace.get("environment", "")
    return result
