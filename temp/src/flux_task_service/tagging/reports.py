"""Small, explicit result parsers. Counts are reported facts, not defect truth."""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace

from .adapters import object_value, stable_id
from .models import Evidence, TestRun

COUNT_KEYS = ("passed", "failed", "blocked", "skipped", "not_run")


def counts(**values) -> dict[str, int | None]:
    return {key: values.get(key) for key in COUNT_KEYS}


def code_guard_report(text: str, ref: Evidence, bound_to_task: bool) -> TestRun | None:
    """Read the capability's explicit JSONL schema, not generic report text."""
    try:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return None
    records = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("schema_version") == "0.0.0-bits-code-guard"
        and record.get("source") == "bits-code-guard"
    ]
    summaries = [
        record
        for record in records
        if record.get("record_type") == "capability_summary"
    ]
    if len(summaries) != 1:
        return None
    payload = object_value(summaries[0].get("payload"))
    finding_counts = [
        _number(payload.get(key)) for key in ("finding_p0", "finding_p1", "finding_p2")
    ]
    total = (
        sum(finding_counts)
        if all(value is not None for value in finding_counts)
        else None
    )
    return TestRun(
        run_id="code-guard:" + stable_id(records),
        test_type="other",
        origin="bits-code-guard-report.v1",
        association="matched" if bound_to_task else "unknown",
        execution_state="reported",
        counts=counts(),
        reported_issue_count=total,
        evidence=[replace(ref, rule="bits-code-guard-jsonl.v1")],
    )


def _number(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def association(run: dict, task_id: str, sessions: set[str]) -> str:
    tasks = [str(run[k]) for k in ("flux_task_id", "task_id") if run.get(k)]
    session_ids = [
        str(run[k])
        for k in ("exec_session_id", "worker_session_id", "session_id")
        if run.get(k)
    ]
    if any(value != task_id for value in tasks):
        return "mismatch"
    if sessions and any(value not in sessions for value in session_ids):
        return "mismatch"
    if task_id in tasks or any(value in sessions for value in session_ids):
        return "matched"
    return "unknown"


def result_state(values: dict) -> str:
    if (values.get("passed") or 0) + (values.get("failed") or 0) > 0:
        return "executed"
    if any((values.get(key) or 0) > 0 for key in ("blocked", "not_run", "skipped")):
        return "not_executed"
    return "unknown"


def unit_report(
    data: dict, ref: Evidence, task_id: str, sessions: set[str]
) -> TestRun | None:
    if data.get("schema") != "unit_test/report@2":
        return None
    run, summary = object_value(data.get("run")), object_value(data.get("summary"))
    values = counts(**{key: _number(summary.get(key)) for key in COUNT_KEYS})
    return TestRun(
        run_id=str(run.get("run_id") or "report:" + stable_id(data)),
        test_type="unit",
        origin="unit_test/report@2",
        association=association(run, task_id, sessions),
        execution_state=result_state(values),
        counts=values,
        reported_issue_count=_number(object_value(summary.get("issues")).get("total")),
        evidence=[replace(ref, rule="unit_test/report@2")],
        versions={key: run[key] for key in ("repo", "head") if key in run},
    )


def gui_runs(
    data: dict, ref: Evidence, task_id: str, sessions: set[str]
) -> list[TestRun]:
    """Support explicit case arrays only; other upstream shapes remain unknown."""
    result = []
    status_map = {
        "PASS": "passed",
        "PASSED": "passed",
        "FAIL": "failed",
        "FAILED": "failed",
        "BLOCKED": "blocked",
        "SKIP": "skipped",
        "SKIPPED": "skipped",
        "NOT_RUN": "not_run",
    }
    for index, item in enumerate(data.get("items", [])):
        if not isinstance(item, dict):
            continue
        raw = item.get("case_result")
        cases = raw if isinstance(raw, list) else object_value(raw).get("cases")
        if not isinstance(cases, list):
            continue
        values = counts(**dict.fromkeys(COUNT_KEYS, 0))
        for case in cases:
            if not isinstance(case, dict):
                break
            status = case.get("status", case.get("result"))
            key = status_map.get(status.upper()) if isinstance(status, str) else None
            if key is None:
                break
            values[key] += 1
        else:
            result.append(
                TestRun(
                    run_id=str(item.get("run_id") or "gui:" + stable_id(item)),
                    test_type="gui",
                    origin="gui-case-array.v1",
                    association=association(item, task_id, sessions),
                    execution_state=result_state(values),
                    counts=values,
                    reported_issue_count=None,
                    evidence=[
                        replace(
                            ref,
                            pointer=f"{ref.pointer}/items/{index}/case_result",
                            rule="gui-case-array.v1",
                        )
                    ],
                )
            )
    return result


def junit_report(text: str, ref: Evidence) -> TestRun | None:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag not in {"testsuites", "testsuite"}:
        return None
    cases = list(root.iter("testcase"))
    values = counts(passed=0, failed=0, skipped=0)
    for case in cases:
        if case.find("failure") is not None or case.find("error") is not None:
            values["failed"] += 1
        elif case.find("skipped") is not None:
            values["skipped"] += 1
        else:
            values["passed"] += 1
    if not cases:
        return None
    # JUnit does not normally carry Flux task/session identity.
    return TestRun(
        "junit:" + stable_id(text),
        "unit",
        "junit",
        "unknown",
        result_state(values),
        values,
        None,
        [replace(ref, rule="junit-testcase.v1")],
    )


def output_counts(output, test_type: str) -> dict | None:
    """Only called on recognized test invocations, never general shell output."""
    obj = object_value(output)
    if obj:
        output = obj.get("stdout", obj.get("aggregated_output", obj.get("output", "")))
    if not isinstance(output, str):
        return None
    # Pytest terminal summaries (use the last run, retaining unknown absent fields).
    lines = [
        line
        for line in output.splitlines()
        if re.search(r"\b\d+ (?:passed|failed|skipped)\b", line)
        and (re.search(r"\bin \d+(?:\.\d+)?s\b", line) or line.strip().startswith("="))
    ]
    if lines:
        pairs = re.findall(r"\b(\d+) (passed|failed|skipped)\b", lines[-1])
        return counts(**{key: int(value) for value, key in pairs})
    # Jest/Vitest Tests summary; Test Suites counts are not test case counts.
    lines = [
        line
        for line in output.splitlines()
        if re.match(r"\s*Tests:?\s+", line) and "total" in line
    ]
    if lines:
        pairs = re.findall(r"\b(\d+) (passed|failed|skipped)\b", lines[-1])
        if pairs:
            return counts(**{key: int(value) for value, key in pairs})
    return None
