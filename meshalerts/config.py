"""Load and validate the TOML configuration."""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """The configuration file is not valid."""


@dataclass
class MeshtasticConfig:
    transport: str = "tcp"
    host: str = "192.168.1.50"
    port: int = 4403
    serial_port: str = ""
    serial_port_glob: str = "/dev/cu.wchusbserial*"
    serial_ff_fix: str = "off"
    expected_node_id: str = ""
    heartbeat_interval_s: float = 60
    reconnect_max_backoff_s: float = 60


@dataclass
class AlertsConfig:
    url: str = "https://ubilling.net.ua/aerialalerts/"
    poll_interval_s: float = 15
    request_timeout_s: float = 10
    stale_after_s: float = 120
    min_regions: int = 20
    border_policy: str = "assigned"
    notify_region_change_quiet: bool = False


@dataclass
class GpsConfig:
    geojson_path: str = "data/geoBoundaries-UKR-ADM1.geojson"
    stale_after_s: float = 21600
    drop_oblast_after_s: float = 0
    border_margin_m: float = 1500
    outside_margin_m: float = 3000
    position_history: bool = True
    reject_outside_ukraine: bool = True
    history_days: int = 30


@dataclass
class TxConfig:
    min_interval_s: float = 3
    ack_timeout_s: float = 120
    max_retries: int = 2
    retry_delay_s: float = 60
    channel_index: int = 0
    command_cooldown_s: float = 10


@dataclass
class StorageConfig:
    db_path: str = "data/meshalerts.db"


@dataclass
class LoggingConfig:
    file: str = "logs/meshalerts.log"
    level: str = "INFO"


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""


@dataclass
class Config:
    meshtastic: MeshtasticConfig = field(default_factory=MeshtasticConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    tx: TxConfig = field(default_factory=TxConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    known = {f.name: f for f in dataclasses.fields(cls)}
    default = cls()
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"Unknown option [{section}] {key}.")
        expected = type(getattr(default, key))
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if expected is int and isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, expected) or (
            expected is not bool and isinstance(value, bool)
        ):
            raise ConfigError(
                f"Option [{section}] {key} must be of type {expected.__name__}."
            )
        values[key] = value
    return cls(**values)


def validate(cfg: Config) -> None:
    if cfg.alerts.border_policy not in ("assigned", "any_nearby"):
        raise ConfigError("Option [alerts] border_policy must be assigned or any_nearby.")
    if cfg.alerts.poll_interval_s < 5:
        raise ConfigError("Option [alerts] poll_interval_s must be 5 or more.")
    if cfg.alerts.min_regions < 1:
        raise ConfigError("Option [alerts] min_regions must be 1 or more.")
    mesh = cfg.meshtastic
    if mesh.transport not in ("tcp", "serial"):
        raise ConfigError("Option [meshtastic] transport must be tcp or serial.")
    if mesh.transport == "tcp":
        if not mesh.host.strip():
            raise ConfigError("Option [meshtastic] host is required for transport tcp.")
        if not 1 <= mesh.port <= 65535:
            raise ConfigError("Option [meshtastic] port must be 1 to 65535.")
    elif not mesh.serial_port.strip() and not mesh.serial_port_glob.strip():
        raise ConfigError(
            "Option [meshtastic] serial_port or serial_port_glob is required for transport serial."
        )
    if mesh.serial_ff_fix not in ("auto", "on", "off"):
        raise ConfigError('Option [meshtastic] serial_ff_fix must be "auto", "on" or "off".')
    if mesh.expected_node_id and not re.fullmatch(r"![0-9a-fA-F]{8}", mesh.expected_node_id):
        raise ConfigError("Option [meshtastic] expected_node_id must look like !5f4e7f34.")
    if not 1 <= cfg.dashboard.port <= 65535:
        raise ConfigError("Option [dashboard] port must be 1 to 65535.")
    if cfg.tx.min_interval_s < 0 or cfg.tx.max_retries < 0:
        raise ConfigError("Options [tx] min_interval_s and max_retries must not be negative.")
    if cfg.tx.channel_index < 0:
        raise ConfigError("Option [tx] channel_index must not be negative.")
    if cfg.meshtastic.heartbeat_interval_s <= 0:
        raise ConfigError("Option [meshtastic] heartbeat_interval_s must be more than 0.")
    if cfg.logging.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError("Option [logging] level is not a valid log level.")


def from_dict(data: dict[str, Any]) -> Config:
    kwargs = {}
    for f in dataclasses.fields(Config):
        section = data.get(f.name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"Section [{f.name}] must be a table.")
        kwargs[f.name] = _build(_SECTIONS[f.name], section, f.name)
    extra = set(data) - set(kwargs)
    if extra:
        raise ConfigError(f"Unknown section [{sorted(extra)[0]}].")
    cfg = Config(**kwargs)
    validate(cfg)
    return cfg


_SECTIONS = {
    "meshtastic": MeshtasticConfig,
    "alerts": AlertsConfig,
    "gps": GpsConfig,
    "tx": TxConfig,
    "storage": StorageConfig,
    "logging": LoggingConfig,
    "dashboard": DashboardConfig,
}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}. Copy config.example.toml first.") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid TOML: {exc}") from None
    return from_dict(data)
