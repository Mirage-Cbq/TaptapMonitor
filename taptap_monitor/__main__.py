import argparse
import asyncio
import json
import logging
import os

from .client import TapTapClient
from .collector import Collector
from .config import Settings
from .store import Store
from .lock import ProcessLock


def main():
    parser = argparse.ArgumentParser(description="TapTap cloud feedback monitor")
    parser.add_argument("command", choices=["serve", "collect", "status", "backup"], nargs="?", default="serve")
    parser.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", "config.toml"))
    parser.add_argument("--output", help="Backup destination (backup command)")
    parser.add_argument("--full", action="store_true", help="完整采集所有分页与回复（collect 命令）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP request URLs can contain query credentials for integrations.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.load(args.config)
    if args.command == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.environ.get("MONITOR_PORT", "8080")), workers=1)
        return
    store = Store(settings.database)
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    elif args.command == "backup":
        import sqlite3
        if not args.output:
            parser.error("backup requires --output")
        from pathlib import Path
        if Path(args.output).resolve() == Path(settings.database).resolve() or Path(args.output).exists():
            parser.error("Backup destination must be a new file different from the live database")
        with store.connect() as source, sqlite3.connect(args.output) as target:
            source.backup(target)
        print(args.output)
    else:
        for developer in settings.developers:
            store.add_developer(developer["id"], developer.get("name", ""))

        async def run():
            client = TapTapClient(settings)
            try:
                result = await Collector(store, client, settings).collect(mode="full" if args.full else "incremental")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return bool(result["errors"])
            finally:
                await client.close()

        with ProcessLock(settings.database):
            store.recover_interrupted()
            raise SystemExit(1 if asyncio.run(run()) else 0)


if __name__ == "__main__":
    main()
