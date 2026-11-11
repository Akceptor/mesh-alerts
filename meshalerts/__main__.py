"""Entry point: python -m meshalerts --config config.toml"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .oblast import OblastDataError


def setup_logging(file: str, level: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        )
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meshalerts", description="Meshtastic air alert service.")
    parser.add_argument("--config", default="config.toml", help="Path to the TOML config file.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.logging.file, config.logging.level)

    from .service import Service

    try:
        service = Service(config)
    except OblastDataError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2
    service.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
