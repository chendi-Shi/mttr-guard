"""Supervised API/worker and operational commands for the single-host hardened pilot."""
import argparse
import json
import logging
import signal
import threading
from pathlib import Path

from .operations import backup, inspect_database, read_key, restore
from .pilot_store import PilotStore
from .security import ConfigProvider
from . import checkpoints


def open_store(database, audit_cache_mib=16, audit_checkpoint=None):
    Path(database).resolve().parent.mkdir(parents=True, exist_ok=True)
    return PilotStore(database, audit_key=read_key(), audit_cache_bytes=audit_cache_mib * 1024 * 1024,
                      audit_checkpoint=audit_checkpoint)


def cache_budget(value):
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Audit cache budget must be an integer MiB value")
    if not 0 <= size <= 256:
        raise argparse.ArgumentTypeError("Audit cache budget must be between 0 and 256 MiB")
    return size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("api", "worker"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--db", required=True)
        command.add_argument("--audit-checkpoint", help="Required external checkpoint file, reloaded every transaction")
        command.add_argument("--audit-cache-mib", type=cache_budget, default=16,
                             help="Per-process verified-row cache budget (default 16); 0 repeats all cryptographic checks")
        if name == "api":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8787)
        else:
            command.add_argument("--queue", choices=["all", "diagnose", "observe"], default="all")
            command.add_argument("--model")
            command.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    for name in ("backup", "restore", "audit-verify", "audit-checkpoint"):
        command = commands.add_parser(name)
        command.add_argument("--db", required=True)
        if name in {"backup", "restore", "audit-checkpoint"}:
            command.add_argument("--output", required=True)
        command.add_argument("--checkpoint", help="Independently retained trusted audit checkpoint")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command in {"api", "worker"}:
        provider = ConfigProvider(args.config)
        provider.get()
        store = open_store(args.db, args.audit_cache_mib, args.audit_checkpoint)
        if args.command == "api":
            import uvicorn
            from .asgi import create_app
            uvicorn.run(create_app(store, provider), host=args.host, port=args.port, proxy_headers=False,
                        access_log=False, limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=15)
        else:
            from .job_runner import IsolatedRunner
            from .pilot_worker import Worker
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            Worker(store, provider, queue=args.queue, runner=IsolatedRunner(model=args.model, ollama_url=args.ollama_url)).run(stop)
    elif args.command == "backup":
        print(json.dumps(backup(args.db, args.output, read_key(), checkpoints.read(args.checkpoint) if args.checkpoint else None), indent=2))
    elif args.command == "restore":
        print(json.dumps(restore(args.db, args.output, read_key(), checkpoints.read(args.checkpoint) if args.checkpoint else None), indent=2))
    elif args.command == "audit-checkpoint":
        print(json.dumps(checkpoints.export(args.db, args.output, read_key(), args.checkpoint), indent=2))
    else:
        checkpoint = checkpoints.read(args.checkpoint) if args.checkpoint else None
        print(json.dumps(inspect_database(args.db, read_key(), checkpoint), indent=2))


if __name__ == "__main__":
    main()
