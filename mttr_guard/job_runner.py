"""Bounded subprocess for external collection/model work; never performs deployment writes."""
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict

from .agents import OllamaReasoner, RuleReasoner, investigate
from .collectors import Collector, candidate_blocker
from .core import Scope
from .pilot_config import Service


class JobDeadlineExceeded(TimeoutError):
    pass


def bounded_process(command, payload, timeout, env=None):
    try:
        return subprocess.run(command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise JobDeadlineExceeded("External evidence work exceeded its total deadline") from exc


class IsolatedRunner:
    def __init__(self, timeout=70, model=None, ollama_url="http://127.0.0.1:11434"):
        self.timeout, self.model, self.ollama_url = timeout, model, ollama_url

    def __call__(self, service, incident, now, kind):
        payload = json.dumps({"service": asdict(service), "detected_at": incident["detected_at"],
            "now": now, "kind": kind, "model": self.model, "ollama_url": self.ollama_url}).encode()
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONUTF8"}
        allowed.update(name for name in (service.prometheus_token_env, service.evidence_token_env) if name)
        environment = {name: value for name, value in os.environ.items() if name.upper() in {key.upper() for key in allowed}}
        result = bounded_process([sys.executable, "-m", "mttr_guard.job_runner"], payload, self.timeout, environment)
        if result.returncode or len(result.stdout) > 262144:
            raise ValueError("Isolated evidence worker failed")
        output = json.loads(result.stdout)
        if output.get("error"):
            raise ValueError("Isolated evidence collection failed")
        return output


def main():
    try:
        data = json.loads(sys.stdin.buffer.read(262145))
        service = Service(**{**data["service"], "scope": Scope(**data["service"]["scope"])})
        snapshot = Collector(service).collect(time.time(), data["detected_at"] if data["kind"] == "diagnose" else None)
        output = {"snapshot": snapshot, "findings": [], "blocker": None}
        if data["kind"] == "diagnose":
            reasoner = OllamaReasoner(data["model"], data["ollama_url"]) if data["model"] else RuleReasoner()
            findings = asyncio.run(investigate(reasoner, snapshot["evidence"]))
            expected = {"metrics": "regression", "traces": "local_failure", "changes": "recent_change", "critic": "candidate"}
            blocker = candidate_blocker(snapshot, service)
            if any(f.verdict != expected[f.agent] for f in findings):
                blocker = blocker or "分析结论不一致或模型不可用，请根据证据和 Runbook 人工判断。"
            output.update(findings=[asdict(f) for f in findings], blocker=blocker)
        sys.stdout.write(json.dumps(output))
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    main()
