"""Exercise valid, incomplete and altered-input studies through the real CLI."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mttr_guard.validation import read_json, write_report
from scripts.make_study_fixture import fixture


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan, registry, outcomes = fixture()
        for name, data in (("plan", plan), ("registry", registry), ("outcomes", outcomes)):
            write_report(root / (name + ".json"), data)
        def run(outcomes_name, output_name):
            return subprocess.run([sys.executable, "-m", "mttr_guard.validation", "evaluate",
                "--plan", str(root / "plan.json"), "--registry", str(root / "registry.json"),
                "--outcomes", str(root / outcomes_name), "--output", str(root / output_name)],
                capture_output=True, text=True, timeout=20)
        passed = run("outcomes.json", "report.json")
        assert passed.returncode == 0, "Valid synthetic CLI fixture failed"
        result = read_json(root / "report.json")
        assert result["domain"] == "SYNTHETIC"
        assert result["claims"]["causal_mttr_reduction"] == "not_established"
        outcomes["incidents"].pop()
        write_report(root / "incomplete.json", outcomes)
        assert run("incomplete.json", "incomplete-report.json").returncode == 2
        incomplete = read_json(root / "incomplete-report.json")
        assert incomplete["numeric_status"] == "insufficient_evidence" and incomplete["registered"] == 16
        outcomes["registry_sha256"] = "0" * 64
        write_report(root / "altered.json", outcomes)
        assert run("altered.json", "altered-report.json").returncode == 1
        assert not (root / "altered-report.json").exists()
        output = Path("artifacts") / f"evaluation-drill-{time.time_ns()}.json"
        write_report(output, {"domain": "SYNTHETIC", "complete_study": result,
            "incomplete_study": incomplete, "altered_input_rejected": True,
            "disclaimer": "CLI validation only; no real incidents or actual MTTR reduction"})
        print(json.dumps({"output": str(output), "synthetic_cli_checks": "passed"}))


if __name__ == "__main__":
    main()
