"""Short region names for !<region> commands. One table, keyed by the Ubilling name."""

from __future__ import annotations

from .oblast import NO_API_DATA

CRIMEA = next(iter(NO_API_DATA))

# The first Latin alias of each entry is its canonical short name (shown by !regions).
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "Вінницька область": (
        "vinnytsia", "vinnytsya", "vinnitsa", "vinnitsia", "vinnica", "vinnytska", "vinnitskaya",
        "вінниця", "вінницька",
    ),
    "Волинська область": (
        "volyn", "volin", "volynska", "volynskaya", "lutsk", "волинь", "волинська", "луцьк",
    ),
    "Дніпропетровська область": (
        "dnipro", "dnipropetrovsk", "dnipropetrovska", "dnepropetrovsk", "dnepr", "dnepropetrovskaya",
        "дніпро", "дніпропетровськ", "дніпропетровська",
    ),
    "Донецька область": ("donetsk", "donetska", "donetskaya", "донецьк", "донецька"),
    "Житомирська область": (
        "zhytomyr", "zhitomir", "zhytomyrska", "zhitomirskaya", "житомир", "житомирська",
    ),
    "Закарпатська область": (
        "zakarpattia", "zakarpattya", "zakarpatye", "zakarpatska", "zakarpatskaya", "transcarpathia",
        "uzhhorod", "uzhgorod", "закарпаття", "закарпатська", "ужгород",
    ),
    "Запорізька область": (
        "zaporizhzhia", "zaporizhia", "zaporizhzhya", "zaporizhya", "zaporozhye", "zaporozhe",
        "zaporizka", "zaporozhskaya", "запоріжжя", "запорізька",
    ),
    "Івано-Франківська область": (
        "frankivsk", "ivanofrankivsk", "ivanofrankivska", "ivanofrankovsk", "frankovsk",
        "івано-франківськ", "івано-франківська", "франківськ",
    ),
    "Київська область": (
        "kyivska", "kyivobl", "kyivoblast", "kievska", "kievobl", "kievoblast", "kyivskaya", "kievskaya",
        "київська", "київобл", "київобласть",
    ),
    "Кіровоградська область": (
        "kirovohrad", "kirovograd", "kropyvnytskyi", "kropyvnytskyy", "kropivnitskiy", "kropyvnytsky",
        "kirovohradska", "kirovogradskaya", "кіровоград", "кіровоградська", "кропивницький",
    ),
    "Луганська область": ("luhansk", "lugansk", "luhanska", "luganskaya", "луганськ", "луганська"),
    "Львівська область": ("lviv", "lvov", "lvivska", "lvovskaya", "львів", "львівська"),
    "Миколаївська область": (
        "mykolaiv", "nikolaev", "nikolayev", "mykolaivska", "nikolaevskaya", "миколаїв", "миколаївська",
    ),
    "Одеська область": (
        "odesa", "odessa", "odeska", "odesska", "odesskaya", "одеса", "одеська",
    ),
    "Полтавська область": ("poltava", "poltavska", "poltavskaya", "полтава", "полтавська"),
    "Рівненська область": (
        "rivne", "rovno", "rivnenska", "rovenskaya", "рівне", "рівненська",
    ),
    "Сумська область": ("sumy", "sumska", "sumskaya", "суми", "сумська"),
    "Тернопільська область": (
        "ternopil", "ternopol", "ternopilska", "ternopolskaya", "тернопіль", "тернопільська",
    ),
    "Харківська область": (
        "kharkiv", "kharkov", "kharkivska", "kharkovskaya", "харків", "харківська",
    ),
    "Херсонська область": ("kherson", "khersonska", "khersonskaya", "херсон", "херсонська"),
    "Хмельницька область": (
        "khmelnytskyi", "khmelnytskyy", "khmelnytsky", "khmelnitskiy", "khmelnitsky", "khmelnytska",
        "khmelnitskaya", "хмельницький", "хмельницька",
    ),
    "Черкаська область": (
        "cherkasy", "cherkassy", "cherkaska", "cherkasskaya", "черкаси", "черкаська",
    ),
    "Чернівецька область": (
        "chernivtsi", "chernovtsy", "chernivetska", "chernovitskaya", "чернівці", "чернівецька",
    ),
    "Чернігівська область": (
        "chernihiv", "chernigov", "chernihivska", "chernigovskaya", "чернігів", "чернігівська",
    ),
    "м. Київ": ("kyiv", "kiev", "kyivcity", "kievcity", "mkyiv", "mkiev", "київ", "м. київ"),
    "Севастополь": ("sevastopol", "севастополь"),
}

CRIMEA_ALIASES = ("crimea", "crimean", "krym", "крим", "кримська")

_SUFFIXES = ("oblast", "obl", "область", "обл")


def normalize(text: str) -> str:
    """Lowercase and drop everything except letters and digits (apostrophes, hyphens, spaces, dots)."""
    return "".join(c for c in text.lower() if c.isalnum() and c not in "ʼʻ")


def _build() -> dict[str, str]:
    table: dict[str, str] = {}
    for name, aliases in [*REGION_ALIASES.items(), (CRIMEA, CRIMEA_ALIASES)]:
        for alias in aliases:
            key = normalize(alias)
            if table.setdefault(key, name) != name:
                raise ValueError(f"Region alias {alias!r} belongs to two regions.")
    return table


ALIAS_TO_NAME = _build()
ALIAS_COUNT = sum(len(a) for a in REGION_ALIASES.values()) + len(CRIMEA_ALIASES)
CANONICAL = {name: aliases[0] for name, aliases in REGION_ALIASES.items()}


def lookup(text: str) -> str | None:
    """Return the Ubilling name for a short name, or None. Crimea returns its own name.

    The full text is tried first: "kyivobl" is an alias of the Kyiv oblast, but "obl"
    would otherwise be cut off and leave the city "kyiv".
    """
    key = normalize(text)
    if key in ALIAS_TO_NAME:
        return ALIAS_TO_NAME[key]
    for suffix in _SUFFIXES:
        if key.endswith(suffix) and key != suffix:
            return ALIAS_TO_NAME.get(key[: -len(suffix)])
    return None
