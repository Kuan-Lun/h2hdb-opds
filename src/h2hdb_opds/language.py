__all__ = ["normalize_bcp47"]

import re

_ALNUM = re.compile(r"^[A-Za-z0-9]+$")
_GRANDFATHERED = {
    value.casefold(): value
    for value in (
        "en-GB-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "sgn-BE-FR",
        "sgn-BE-NL",
        "sgn-CH-DE",
        "art-lojban",
        "cel-gaulish",
        "no-bok",
        "no-nyn",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    )
}


def normalize_bcp47(value: str) -> str | None:
    """Return a consistently cased, well-formed BCP 47 tag, or ``None``."""
    candidate = value.strip().replace("_", "-")
    if not candidate:
        return None
    grandfathered = _GRANDFATHERED.get(candidate.casefold())
    if grandfathered is not None:
        return grandfathered

    subtags = candidate.split("-")
    if any(not subtag or not _ALNUM.fullmatch(subtag) for subtag in subtags):
        return None

    if subtags[0].casefold() == "x":
        if len(subtags) < 2 or any(not 1 <= len(part) <= 8 for part in subtags[1:]):
            return None
        return "-".join(part.casefold() for part in subtags)

    language = subtags[0]
    if not language.isalpha() or len(language) not in {2, 3, 4, 5, 6, 7, 8}:
        return None
    result = [language.casefold()]
    index = 1

    if len(language) in {2, 3}:
        extlang_count = 0
        while (
            index < len(subtags)
            and extlang_count < 3
            and len(subtags[index]) == 3
            and subtags[index].isalpha()
        ):
            result.append(subtags[index].casefold())
            index += 1
            extlang_count += 1

    if index < len(subtags) and len(subtags[index]) == 4 and subtags[index].isalpha():
        result.append(subtags[index].title())
        index += 1

    if index < len(subtags) and (
        (len(subtags[index]) == 2 and subtags[index].isalpha())
        or (len(subtags[index]) == 3 and subtags[index].isdigit())
    ):
        region = subtags[index]
        result.append(region.upper() if region.isalpha() else region)
        index += 1

    variants: set[str] = set()
    while index < len(subtags):
        subtag = subtags[index]
        is_variant = (5 <= len(subtag) <= 8) or (
            len(subtag) == 4 and subtag[0].isdigit()
        )
        if not is_variant:
            break
        normalized = subtag.casefold()
        if normalized in variants:
            return None
        variants.add(normalized)
        result.append(normalized)
        index += 1

    extensions: set[str] = set()
    while index < len(subtags) and len(subtags[index]) == 1:
        singleton = subtags[index].casefold()
        if singleton == "x":
            break
        if singleton in extensions or singleton == "x":
            return None
        extensions.add(singleton)
        index += 1
        start = index
        while index < len(subtags) and 2 <= len(subtags[index]) <= 8:
            result.append(subtags[index].casefold())
            index += 1
        if index == start:
            return None
        result.insert(start - 1, singleton)

    if index < len(subtags) and subtags[index].casefold() == "x":
        result.append("x")
        index += 1
        if index == len(subtags):
            return None
        while index < len(subtags):
            if not 1 <= len(subtags[index]) <= 8:
                return None
            result.append(subtags[index].casefold())
            index += 1

    if index != len(subtags):
        return None
    return "-".join(result)
