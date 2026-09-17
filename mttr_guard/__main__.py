"""Local demonstration. Explicitly simulated identities, telemetry, time and writes."""
import argparse
import asyncio
import json
import uuid
from pathlib import Path

from .agents import OllamaReasoner, RuleReasoner
from .core import Engine, Principal, Scope
from .deployment_simulator import FaultInjectingAdapter, SQLiteDeploymentAdapter


class DemoClock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


async def demo(args, db_path):
    clock = DemoClock()
    engine = Engine(db_path, clock)
    scope = Scope("commerce", "staging", "cn-east-1", "trade", "checkout")
    engine.seed_simulator(scope, args.scenario)
    iid = engine.ingest(scope, "checkout-error-rate", clock())
    reasoner = OllamaReasoner(args.model, args.ollama_url) if args.model else RuleReasoner()
    print(f"MODE: SIMULATED | reasoning: {reasoner.name} | incident: {iid}")
    inc = await engine.diagnose(iid, scope, reasoner)
    for finding in inc["findings"]:
        print(f"  {finding['agent']:8} -> {finding['verdict']}")
    print("DIAGNOSIS:", inc["state"])
    if inc["state"] == "AWAITING_APPROVAL":
        print("PLAN:", json.dumps(inc["plan"], ensure_ascii=False))
        if args.simulate_approval:
            engine.approve(iid, scope, Principal("demo:oncall", "approver", scope), inc["plan"]["hash"])
            actor = Principal("demo:executor", "executor", scope)
            adapter = None
            if args.execution_mode == "adapter":
                adapter = SQLiteDeploymentAdapter(str(db_path) + ".deployment.db", clock)
                adapter.seed(scope)
                inc = engine.dispatch(iid, scope, actor, inc["plan"]["hash"], FaultInjectingAdapter(adapter, args.fault))
                print("DISPATCH:", inc["state"])
                if inc["state"] == "EXECUTION_UNKNOWN":
                    # Recreate the engine to demonstrate restart without a second write.
                    engine = Engine(db_path, clock)
                    inc = engine.reconcile(iid, scope, actor, adapter)
                    print("RECONCILIATION AFTER RESTART:", inc["state"])
                print("REMOTE SIMULATED SIDE EFFECTS:", adapter.inspect(scope)["executions"])
            else:
                engine.execute(iid, scope, actor, inc["plan"]["hash"])
                inc = engine.get(iid, scope)
            if inc["state"] == "VERIFYING":
                print("EXECUTION:", inc["receipt"]["mode"], inc["receipt"]["revision"])
                for _ in range(3):
                    clock.advance(30)
                    observed_revision = adapter.inspect(scope)["revision"] if adapter else None
                    engine.simulate_observation(scope, healthy=True, revision=observed_revision)
                    inc = engine.verify(iid, scope)
                print("VERIFICATION:", inc["state"])
                print("Synthetic elapsed seconds:", inc["recovery_seconds"], "(virtual time; NOT an MTTR benchmark)")
            else:
                print("HUMAN HANDOFF: outcome unresolved; service lock retained, no automatic resubmission.")
        else:
            print("Approval pending. Use --simulate-approval for an explicit synthetic full-cycle demo.")
    else:
        print("ESCALATION:", inc.get("escalation_reason"))
    report = {"mode": "SIMULATED", "execution_mode": args.execution_mode, "fault": args.fault,
              "reasoner": reasoner.name, "model": args.model,
              "incident": inc, "events": engine.events(iid, scope)}
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("REPORT:", path.resolve())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["release", "downstream", "migration", "missing"], default="release")
    parser.add_argument("--simulate-approval", action="store_true")
    parser.add_argument("--execution-mode", choices=["adapter", "atomic"], default="adapter")
    parser.add_argument("--fault", choices=["none", "before_submit", "after_submit"], default="none")
    parser.add_argument("--model", help="An already installed Ollama model name; optional")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--db", help="A fresh SQLite path to retain the simulation; default is temporary")
    parser.add_argument("--output", help="Write incident and audit events to JSON")
    args = parser.parse_args()
    if args.execution_mode == "atomic" and args.fault != "none":
        parser.error("--fault requires --execution-mode adapter")
    if args.db:
        asyncio.run(demo(args, args.db))
    else:
        temp_root = (Path.cwd() / "artifacts" / "tmp").resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        db_path = temp_root / (uuid.uuid4().hex + ".db")
        try:
            asyncio.run(demo(args, str(db_path)))
        finally:
            assert db_path.resolve().is_relative_to(temp_root)
            db_path.unlink(missing_ok=True)
            Path(str(db_path) + ".deployment.db").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
