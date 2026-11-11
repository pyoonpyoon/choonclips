"""`clipforge` / `python -m clipforge`: start the local studio."""
from __future__ import annotations

import argparse
import logging


def main() -> None:
    from .config import settings

    ap = argparse.ArgumentParser(prog="clipforge", description="Local short-form clipping studio")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    print(f"\n  ClipForge running at http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}\n")
    uvicorn.run("clipforge.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
