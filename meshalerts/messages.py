"""All user-facing text. Alert texts are Ukrainian. Replies are English (STE)."""

from __future__ import annotations

MAX_TEXT_BYTES = 200


def truncate_utf8(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def fit(text: str) -> str:
    return truncate_utf8(text, MAX_TEXT_BYTES)


def alert_start(oblast: str) -> str:
    return fit(f"ПОВІТРЯНА ТРИВОГА! {oblast}")


def alert_end(oblast: str) -> str:
    return fit(f"ВІДБІЙ ТРИВОГИ. {oblast}")


def region_in_alert(oblast: str) -> str:
    return fit(f"Ви в зоні тривоги: {oblast}. Тривога триває.")


def region_left_alert(new_oblast: str) -> str:
    return fit(f"Ви вийшли із зони тривоги. {new_oblast}: тривоги немає.")


def region_new_quiet(oblast: str) -> str:
    return fit(f"Новий регіон: {oblast}. Тривоги немає.")


def test_alert(oblast: str | None) -> str:
    return fit(f"ТЕСТ. Перевірка сповіщень. {oblast or 'регіон невідомий'}")


def state_label(active: bool | None) -> str:
    if active is None:
        return "no data"
    return "Active" if active else "No alert"


def alerts_enabled(
    oblast: str | None, active: bool | None, has_position: bool, fixed: bool = False
) -> str:
    if oblast is None:
        if not has_position:
            return (
                "Alerts enabled. Region: unknown (no GPS position). "
                "Send a position to get alerts."
            )
        return "Alerts enabled. Region: outside Ukraine."
    tag = " (fixed)" if fixed else ""
    return fit(f"Alerts enabled. Region: {oblast}{tag}. Status: {state_label(active)}.")


def gps_region_reply(oblast: str | None, active: bool | None) -> str:
    if oblast is None:
        return "Region from GPS: unknown (no GPS position)."
    return fit(f"Region from GPS: {oblast}. Status: {state_label(active)}.")


UNKNOWN_REGION = "Unknown region. Send !regions for the list."

CRIMEA_NO_DATA = "The alert API has no data for Crimea. Region not changed."

REGIONS_NOTE = "!kyiv = Kyiv city, !kyivska = Kyiv oblast. !gps = region by GPS."


def regions_lists(names: list[str]) -> list[str]:
    """Pack the short names into messages of at most MAX_TEXT_BYTES bytes."""
    out: list[str] = []
    current = "Regions: "
    for name in [*names, REGIONS_NOTE]:
        piece = name if current.endswith(": ") else " " + name
        if len((current + piece).encode("utf-8")) > MAX_TEXT_BYTES:
            out.append(current)
            current, piece = "More: ", name
        current += piece
    out.append(current)
    return out


REJECTED_NOTE = "Last GPS fix rejected (outside Ukraine)."

ALERTS_DISABLED = "Alerts disabled. Send !start to enable."

HELP = "Commands: !start !stop !status !location !<region> (e.g. !lviv) !gps !regions"


def status_reply(
    oblast: str | None,
    active: bool | None,
    has_position: bool,
    position_age_min: int | None,
    data_age_min: int | None,
    stale_position: bool,
    stale_data: bool,
    fixed: bool = False,
) -> str:
    tag = " (fixed)" if fixed else ""
    if oblast is None:
        head = "Region: unknown (no GPS position)." if not has_position else "Region: outside Ukraine."
    elif active is None:
        head = f"Region: {oblast}{tag}. Status: no data."
    elif active:
        head = f"Region: {oblast}{tag}. Status: ПОВІТРЯНА ТРИВОГА."
    else:
        head = f"Region: {oblast}{tag}. Status: no alert."
    parts = [head]
    if stale_position and position_age_min is not None:
        parts.append(f"Position is old ({position_age_min} min).")
    if stale_data:
        parts.append(
            f"Data is old ({data_age_min} min)." if data_age_min is not None else "Data is old."
        )
    return fit(" ".join(parts))


def location_reply(
    lat: float | None,
    lon: float | None,
    hhmm: str | None,
    age_min: int | None,
    region: str,
) -> str:
    if lat is None or lon is None:
        return "No position known."
    return fit(
        f"Position: {lat:.5f}, {lon:.5f}. Time: {hhmm} UTC ({age_min} min ago). Region: {region}."
    )
