"""Run the read-only pilot or its isolated local HTTP demonstration."""
import argparse
import threading
from pathlib import Path

from .agents import OllamaReasoner
from .pilot_config import Config
from .pilot_http import make_server
from .pilot_store import PilotStore
from .pilot_worker import Worker


def serve(config, db, port, model=None, ollama_url="http://127.0.0.1:11434", workers=2):
    Path(db).resolve().parent.mkdir(parents=True, exist_ok=True)
    store = PilotStore(db)
    server = make_server(store, config, port)
    stop = threading.Event()
    reasoner = OllamaReasoner(model, ollama_url) if model else None
    threads = [threading.Thread(target=Worker(store, config, reasoner=reasoner).run, args=(stop,), daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    print(f"MTTR Guard pilot: http://127.0.0.1:{server.server_port}", flush=True)
    print("Read-only collectors; no deployment writes. Ctrl+C stops the server. State is retained.", flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve")
    run.add_argument("--config", required=True)
    run.add_argument("--db", default="artifacts/pilot.db")
    run.add_argument("--port", type=int, default=8787)
    run.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    run.add_argument("--model")
    run.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    demo = sub.add_parser("demo")
    demo.add_argument("--directory", default="artifacts/pilot-demo")
    demo.add_argument("--port", type=int, default=8787)
    demo.add_argument("--evidence-port", type=int, default=9191)
    recover = sub.add_parser("demo-recover")
    recover.add_argument("--directory", default="artifacts/pilot-demo")
    recover.add_argument("--evidence-port", type=int, default=9191)
    args = parser.parse_args()
    if args.command == "serve":
        serve(Config.read(args.config), args.db, args.port, args.model, args.ollama_url, args.workers)
    elif args.command == "demo":
        from .pilot_demo import launch
        launch(args)
    else:
        from .pilot_demo import recover
        recover(args)


if __name__ == "__main__":
    main()
