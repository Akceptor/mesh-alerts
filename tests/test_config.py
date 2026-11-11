import pytest

from meshalerts.config import ConfigError, from_dict


def test_serial_needs_no_host():
    cfg = from_dict({"meshtastic": {"transport": "serial", "host": "", "port": 0}})
    assert cfg.meshtastic.transport == "serial"


def test_tcp_requires_host():
    with pytest.raises(ConfigError):
        from_dict({"meshtastic": {"transport": "tcp", "host": ""}})


@pytest.mark.parametrize("bad", [
    {"transport": "bluetooth"},
    {"serial_ff_fix": "maybe"},
    {"expected_node_id": "5f4e7f34"},
    {"transport": "serial", "serial_port": "", "serial_port_glob": ""},
])
def test_invalid_values(bad):
    with pytest.raises(ConfigError):
        from_dict({"meshtastic": bad})
