"""从原始 Trace 按 SHA-256 冻结倒计时回归材料，不改写批跑目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def freeze(batch: Path, output: Path) -> None:
    cases = []
    for sample in sorted(batch.glob("Q*")):
        traces = sorted(sample.glob("attempt_*/trace/source/trace.jsonl"))
        if not traces:
            continue
        context = {}
        sources = []
        for line in traces[-1].read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            for key, descriptor in event.get("artifacts", {}).items():
                if key not in {
                    "card_spec", "task_spec", "accepted_plan", "dsl_extracted_output",
                    "repair_extracted_output",
                }:
                    continue
                digest = descriptor.get("sha256")
                if not isinstance(digest, str):
                    raise ValueError("Missing artifact digest")
                raw = (batch / "trace_blobs" / digest).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError(f"Artifact hash mismatch: {digest}")
                value = raw.decode("utf-8")
                if key in {"dsl_extracted_output", "repair_extracted_output"}:
                    if not sources or sources[-1].get("sourceSha256") != digest:
                        sources.append({"source": value, "sourceSha256": digest,
                                        "plan": context.get("accepted_plan")})
                else:
                    context[key] = json.loads(value)
        card = context.get("card_spec", {})
        countdown = any(
            binding.get("capabilityId") == "GetCountdownDays"
            for binding in card.get("dataBindings") or []
        )
        if countdown:
            blocks_path = traces[-1].parents[2] / "blocks.json"
            delivered = None
            if blocks_path.exists():
                blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
                delivered = blocks.get("designcompactdsl")
            cases.append({"id": sample.name, "card": card,
                          "task": context.get("task_spec"), "sources": sources,
                          "deliveredSource": delivered,
                          "deliveredSourceSha256": (
                              hashlib.sha256(delivered.encode("utf-8")).hexdigest()
                              if isinstance(delivered, str) else None
                          )})
    output.write_text(json.dumps({"batch": batch.name, "cases": cases},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    freeze(args.batch, args.output)
