"""Report generation.

After a run finishes (or on demand), snapshot the whole registry into a Markdown
report and a machine-readable JSON sidecar: chain state, per-worker activity, and
every finding ranked by severity.  The report is the deliverable of a 1-hour run.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .state import StateRegistry

_SEV_ORDER = {"critical": 0, "high": 1, "med": 2, "low": 3, "info": 4}


def write_report(registry: StateRegistry, report_dir: str) -> tuple[str, str]:
    os.makedirs(report_dir, exist_ok=True)
    snap = registry.snapshot()
    findings = registry.all_findings()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = f"eeztest-{registry.instance_name}-{stamp}"
    md_path = os.path.join(report_dir, base + ".md")
    json_path = os.path.join(report_dir, base + ".json")

    with open(json_path, "w") as fh:
        json.dump({"snapshot": snap, "findings": findings}, fh, indent=2, default=str)

    with open(md_path, "w") as fh:
        fh.write(_render_markdown(snap, findings))

    return md_path, json_path


def _render_markdown(snap: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"# EEZtest report — {snap['instance']}")
    lines.append("")
    lines.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append(f"- Run duration: {_dur(snap.get('run_duration'))}")
    lines.append(f"- Elapsed: {_dur(snap.get('elapsed'))}")
    lines.append(f"- Workers: {len(snap.get('workers', []))}")
    lines.append(f"- Findings: {len(findings)}")
    lines.append("")

    # ── verdict ─────────────────────────────────────────────────────────────
    by_sev = _count_by_severity(findings)
    verdict = _verdict(by_sev)
    lines.append(f"## Verdict: {verdict}")
    lines.append("")
    lines.append(
        "| critical | high | med | low | info |\n|---:|---:|---:|---:|---:|\n"
        f"| {by_sev.get('critical',0)} | {by_sev.get('high',0)} | {by_sev.get('med',0)} "
        f"| {by_sev.get('low',0)} | {by_sev.get('info',0)} |"
    )
    lines.append("")

    # ── chain state ─────────────────────────────────────────────────────────
    lines.append("## Chain state")
    lines.append("")
    for side in ("l1", "l2"):
        c = snap.get("chain", {}).get(side)
        if not c:
            continue
        lines.append(f"### {side.upper()}")
        for k, v in c.items():
            lines.append(f"- **{k}**: `{v}`")
        lines.append("")

    # ── findings ────────────────────────────────────────────────────────────
    lines.append("## Findings")
    lines.append("")
    if not findings:
        lines.append("_No findings recorded — no anomalies observed during the run._")
        lines.append("")
    else:
        ordered = sorted(findings, key=lambda f: (_SEV_ORDER.get(f["severity"], 9), f["ts"]))
        for f in ordered:
            ts = time.strftime("%H:%M:%S", time.gmtime(f["ts"]))
            lines.append(f"### [{f['severity'].upper()}] {f['title']}")
            lines.append(f"- worker: `{f.get('worker','?')}`  ·  at {ts} UTC")
            lines.append(f"- {f['detail']}")
            extras = {k: v for k, v in f.items() if k not in ("ts", "worker", "title", "severity", "detail")}
            for k, v in extras.items():
                lines.append(f"  - {k}: `{v}`")
            lines.append("")

    # ── per-worker activity ─────────────────────────────────────────────────
    lines.append("## Worker activity")
    lines.append("")
    for w in sorted(snap.get("workers", []), key=lambda x: x["name"]):
        lines.append(f"### {w['name']} — {w['status']}")
        if w.get("description"):
            lines.append(f"_{w['description']}_")
        counters = w.get("counters", {})
        metrics = w.get("metrics", {})
        if counters:
            lines.append("")
            lines.append("Counters:")
            for k, v in sorted(counters.items()):
                lines.append(f"- {k}: {v}")
        if metrics:
            lines.append("")
            lines.append("Metrics:")
            for k, v in sorted(metrics.items()):
                lines.append(f"- {k}: `{v}`")
        lines.append("")

    return "\n".join(lines)


def _count_by_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return out


def _verdict(by_sev: dict[str, int]) -> str:
    if by_sev.get("critical"):
        return "❌ CRITICAL issues found"
    if by_sev.get("high"):
        return "⚠️ HIGH-severity issues found"
    if by_sev.get("med") or by_sev.get("low"):
        return "🟡 Minor issues found"
    return "✅ Healthy — no significant issues"


def _dur(seconds: Any) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"
