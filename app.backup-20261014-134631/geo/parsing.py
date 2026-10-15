"""
app/geo/parsing.py
===================
Deterministic repair of what the LLM extracted, so the pipeline does not depend on a 32B model getting
every detail of a sentence right (revision round 2). Pure functions -- no database, no LLM -- so
every rule below is unit-tested in tests/test_parsing.py.

What it fixes (revision item numbers in brackets):
  * house numbers: "Marcel Thiry 52", "Rue Stuyvenbergh, 29", "29 Rue X", "Rue X 29 - Jette" are
    split into street / number / municipality (6, 14, 22, 23) -- the old pipeline passed the whole
    string to the street-name matcher and never looked at the number;
  * "between number 48 and 82" on a street becomes two addresses of that street (14, 22);
  * a municipality written right after an address ("... 134 - Jette") is part of the address, not a
    waypoint (23);
  * roles from the sentence frame instead of from the LLM: "X between Y and Z" -> main X, start Y,
    end Z (9, 20); "from Y to Z via V" -> start/end/via (24);
  * entities the LLM mangled ("Ouderkerken" for "oudergem") are snapped back onto the words that are
    really in the prompt (12);
  * generic words in front of a name ("zone Meiser", "quartier Mutsaard") are stripped and turned into
    a ranking hint (7, 13);
  * "railway between station A and B" -> a RAILWAY main entity the LLM did not emit (21);
  * a bare "tram" (no line number) is a MODIFIER on the street ("avenue de Jette with a tram"), not a
    transit line (16);
  * a bare place name with no relation becomes SELF (the object itself), not a 50 m buffer (7, 11,
    13, 15, 17, 19, 25, 26, 27); "buffer of 500 m around X" is NEAR 500 (10);
  * an explicit distance in the prompt ("20 m", "1,5 km") is used even if the LLM left it null.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Optional

AREA_TYPE_NAMES = {"MUNICIPALITY", "STATISTICAL_SECTOR", "MONITORING_DISTRICT", "REGION", "NEIGHBOUR_MUNICIPALITY"}
STREET_LIKE = {"STREET", "OUTSIDE_STREET", "RAILWAY", "TRANSIT_LINE"}

# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def fold(text: str) -> str:
    """lower-case, accent-free, punctuation -> single spaces."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


_ARTICLES = re.compile(r"^(?:le|la|les|l|het|de|den|der|du|des|the)\s+")

# words in front of a name that describe the KIND of place rather than being part of its name
_HINT_PREFIXES = [
    ("AREA", r"(?:quartier|wijk|buurt|barri[eè]re|barrier|neighbou?rhood|district|sector|secteur)"),
    ("PLACE", r"(?:zone|zona|area|gebied|omgeving|environs)"),
]


def strip_generic(text: str) -> tuple[str, Optional[str]]:
    """'zone meiser' -> ('meiser', 'PLACE'); 'quartier Mutsaard' -> ('Mutsaard', 'AREA')."""
    t = (text or "").strip()
    for hint, words in _HINT_PREFIXES:
        m = re.match(rf"^{words}\s+(?:de\s+la\s+|de\s+l'|du\s+|des\s+|de\s+|van\s+de\s+|van\s+|d')?(?P<rest>.+)$", t, flags=re.I)
        if m and len(m.group("rest")) >= 3:
            return m.group("rest").strip(), hint
    return t, None


def _hint_from_prompt(text: str, prompt: str) -> Optional[str]:
    """'... du quartier Murtsaard' -> 'AREA' even when the LLM kept only 'Murtsaard' as the entity text."""
    low = prompt.lower()
    i = low.find((text or "").lower())
    if i <= 0:
        return None
    before = re.sub(r"\s+", " ", low[max(0, i - 24):i]).strip()
    for hint, words in _HINT_PREFIXES:
        if re.search(rf"\b{words}(?:\s+(?:de\s+la|de\s+l'|du|des|de|van\s+de|van|d'))?$", before):
            return hint
    return None


def strip_article(text: str) -> str:
    t = fold(text)
    return _ARTICLES.sub("", t).strip() or t


# --------------------------------------------------------------------------- #
# addresses
# --------------------------------------------------------------------------- #

_NUM_WORD = r"(?:n[°o]\.?|nr\.?|no\.?|nummer|num[eé]ro|number|numero|#)"
_NUM = r"\d{1,4}\s?[A-Za-z]?(?![\d])"
_ZIP_MUNI = r"(?:,\s*)?(?:\b\d{4}\b\s+)?"

_ADDR_TRAILING = re.compile(
    rf"^(?P<street>.*?[^\W\d_].*?)[,\s]+(?:{_NUM_WORD}\s*)?(?P<num>\d{{1,4}}[A-Za-z]?)"
    rf"(?:\s*(?:/|bte|bus|box|b)\s*(?P<box>\w+))?"
    rf"(?:\s*[,\-–]\s*(?:\d{{4}}\s+)?(?P<muni>[^\d,]+?))?\s*$",
    flags=re.I,
)
_ADDR_LEADING = re.compile(
    rf"^(?:{_NUM_WORD}\s*)?(?P<num>\d{{1,4}}[A-Za-z]?)[,\s]+(?P<street>[^\d].+?)"
    rf"(?:\s*[,\-–]\s*(?:\d{{4}}\s+)?(?P<muni>[^\d,]+?))?\s*$",
    flags=re.I,
)


_TRANSIT_WORDS = re.compile(r"^(?:ligne\s+de\s+|ligne\s+|lijn\s+|line\s+)?(?:tram|trams|tramway|bus|metro|train|ligne|lijn|line|tramlijn|buslijn|metrolijn)$", re.I)


def parse_address(text: str) -> Optional[dict]:
    """
    'Marcel Thiry 52' -> {'street': 'Marcel Thiry', 'number': '52', 'box': None, 'municipality': None}
    'Rue Stuyvenbergh, 29 - Jette' -> street 'Rue Stuyvenbergh', number '29', municipality 'Jette'
    '12 Rue de la Loi' -> street 'Rue de la Loi', number '12'
    Returns None when there is no house number (so a street such as 'Rue du 11 Novembre' stays a street).
    """
    t = (text or "").strip()
    if not t:
        return None
    for rx in (_ADDR_TRAILING, _ADDR_LEADING):
        m = rx.match(t)
        if not m:
            continue
        street = m.group("street").strip(" ,-–")
        if len(fold(street)) < 3 or _TRANSIT_WORDS.match(fold(street)):
            continue   # "tram 8" / "bus 71" are lines, not house numbers
        muni = m.groupdict().get("muni")
        return {
            "street": street,
            "number": m.group("num").replace(" ", "").upper(),
            "box": (m.groupdict().get("box") or None),
            "municipality": muni.strip(" ,-–") if muni and muni.strip(" ,-–") else None,
        }
    return None


# --------------------------------------------------------------------------- #
# distances
# --------------------------------------------------------------------------- #

_DIST = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(km|kilom[eè]tres?|kilometers?|m|meters?|metres?|mètres?)\b(?!\w)", re.I)


def extract_distance(prompt: str) -> Optional[float]:
    """First explicit distance in the prompt, in metres ('20 m', '1,5 km', '500m')."""
    m = _DIST.search(prompt or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2).lower().startswith("k") else value


# --------------------------------------------------------------------------- #
# snapping entity text onto the prompt
# --------------------------------------------------------------------------- #


def snap_to_prompt(text: str, prompt: str) -> str:
    """If `text` is not literally in the prompt (the LLM re-spelled it: 'Ouderkerken'), return the
    prompt words that look most like it ('oudergem'); otherwise return it unchanged."""
    ft, fp = fold(text), fold(prompt)
    if not ft or ft in fp:
        return text
    words = re.findall(r"[^\s,;:()]+", prompt)
    n = max(1, len(ft.split()))
    best, best_ratio = None, 0.0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, len(words) - size + 1):
            cand = " ".join(words[i:i + size])
            fc = fold(cand)
            if not fc or fc[0] != ft[0]:
                continue
            r = difflib.SequenceMatcher(None, ft, fc).ratio()
            if r > best_ratio:
                best, best_ratio = cand, r
    return best.strip(".,;:") if best and best_ratio >= 0.55 else text


def _pos(text: str, prompt: str) -> Optional[int]:
    """Character position of an entity's text in the prompt (folded comparison, same length mapping)."""
    low = prompt.lower()
    i = low.find((text or "").lower())
    if i >= 0:
        return i
    ft = fold(text)
    for m in re.finditer(r"\S+", prompt):
        if fold(m.group(0)) == (ft.split()[0] if ft else ""):
            return m.start()
    return None


# --------------------------------------------------------------------------- #
# the repair pass
# --------------------------------------------------------------------------- #

_BETWEEN_KW = re.compile(r"\b(tussen|entre|between|zwischen)\b", re.I)
_VIA_KW = re.compile(r"\b(via|en passant par|langs)\b", re.I)
_TO_KW = re.compile(r"\b(tot|naar|à|au|jusqu['e ]|jusque|to|until|toward|vers)\b", re.I)
_HOUSE_PAIR = re.compile(
    rf"(?:{_NUM_WORD}\s*)(?P<a>\d{{1,4}}[A-Za-z]?)\s*(?:en|et|and|&|tot|à|a|to|-|jusqu['e ]\s*(?:au\s*)?)\s*(?:{_NUM_WORD}\s*)?(?P<b>\d{{1,4}}[A-Za-z]?)\b",
    flags=re.I,
)
_RAIL_WORDS = re.compile(r"\b(spoorweg|spoorlijn|spoorwegen|chemin de fer|voie ferr[eé]e|voies ferr[eé]es|railway|railroad|rail|ligne ferroviaire)\b", re.I)
_GENERIC_TRANSIT = re.compile(r"^(?:la\s+|le\s+|de\s+|het\s+|a\s+|een\s+)?(?:ligne\s+de\s+|lijn\s+)?(tram|tramway|trams|tramlijn)s?$", re.I)
_BUFFER_WORD = re.compile(r"\b(buffer|tampon|zone tampon|bufferzone)\b", re.I)


def _new(text: str, etype: str, role: str, **extra) -> dict:
    return {"text": text, "type": etype, "role": role, **extra}


def repair_extraction(prompt: str, extraction: dict) -> dict:
    """Return a repaired copy of the LLM extraction, with a human-readable `notes` list."""
    notes: list[str] = []
    out = dict(extraction or {})
    ents = [dict(e) for e in (out.get("entities") or []) if (e.get("text") or "").strip()]
    relationship = (out.get("relationship") or "NONE").upper()
    modifier = None

    # 1. snap mangled text back onto the prompt, strip generic prefixes -> hint
    for e in ents:
        snapped = snap_to_prompt(e["text"], prompt)
        if snapped != e["text"]:
            notes.append(f"entity '{e['text']}' is not in the prompt; used '{snapped}'")
            e["text"] = snapped
        clean, hint = strip_generic(e["text"])
        if not hint:
            hint = _hint_from_prompt(e["text"], prompt)
        if hint:
            e["text"], e["hint"] = clean, hint
            notes.append(f"'{clean}': leading word of kind '{hint}' stripped (ranking hint)")

    # 2. generic "tram" = modifier of the street, not a line
    kept = []
    for e in ents:
        if e.get("type") == "TRANSIT_LINE" and _GENERIC_TRANSIT.match(e["text"].strip()):
            modifier = "TRAM"
            notes.append("generic 'tram' (no line number) -> TRAM modifier on the street: only axes with tram tracks")
            continue
        kept.append(e)
    ents = kept

    if modifier is None and re.search(r"\b(?:avec|met|with|having|ayant|waar)\b[^.?!]{0,15}\btram(?:s|way|lijn)?\b", prompt, re.I) \
            and not any(e.get("type") == "TRANSIT_LINE" for e in ents) and any(e.get("type") in ("STREET", "ANY", "POI") for e in ents):
        modifier = "TRAM"
        notes.append("'... with a tram' in the prompt -> TRAM modifier on the street")

    # 3. addresses: split street / number / municipality
    for e in ents:
        if e.get("type") == "ADDRESS" or (e.get("type") in ("STREET", "POI", "ANY") and parse_address(e["text"]) and re.search(r"\d", e["text"])
                                            and not re.search(r"\d\s*(m|km)\b", e["text"], re.I)):
            addr = parse_address(e["text"])
            if addr:
                e["type"], e["address"] = "ADDRESS", addr
            elif e.get("type") == "ADDRESS":
                e["type"] = "STREET"
                notes.append(f"'{e['text']}' has no house number: treated as a street")

    # 4. "between number 48 and 82" on a street -> two addresses of that street
    pair = _HOUSE_PAIR.search(prompt)
    if pair:
        street_e = next((e for e in ents if e.get("type") in ("STREET", "ADDRESS") and not e.get("address")), None)
        if street_e is None:
            street_e = next((e for e in ents if e.get("type") == "ADDRESS"), None)
        if street_e is not None:
            street_text = street_e["address"]["street"] if street_e.get("address") else street_e["text"]
            if not any(e.get("address", {}).get("number", "").upper() == pair.group("a").upper() for e in ents):
                ents = [e for e in ents if e is not street_e and not (e.get("type") == "ADDRESS" and fold(street_text) in fold(e["text"]))]
                ents.append(_new(f"{street_text} {pair.group('a')}", "ADDRESS", "start",
                                 address={"street": street_text, "number": pair.group("a").upper(), "box": None, "municipality": None}))
                ents.append(_new(f"{street_text} {pair.group('b')}", "ADDRESS", "end",
                                 address={"street": street_text, "number": pair.group("b").upper(), "box": None, "municipality": None}))
                notes.append(f"house numbers {pair.group('a')} and {pair.group('b')} on '{street_text}' -> two addresses of that street")

    # 4b. one "number 52" in the prompt, a street entity in front of it, no address yet (#6:
    #     "on Marcel thiry between number 52 and Theodere Decuyper")
    if not any(e.get("address") for e in ents):
        nums = list(re.finditer(rf"{_NUM_WORD}\s*(\d{{1,4}}[A-Za-z]?)\b", prompt, flags=re.I))
        if len(nums) == 1:
            n = nums[0]
            cands = [e for e in ents if e.get("type") in ("STREET", "ANY") and _pos(e["text"], prompt) is not None
                     and _pos(e["text"], prompt) < n.start()]
            if cands:
                tgt = max(cands, key=lambda e: _pos(e["text"], prompt))
                tgt["type"] = "ADDRESS"
                tgt["address"] = {"street": tgt["text"], "number": n.group(1).upper(), "box": None, "municipality": None}
                tgt["text"] = f"{tgt['text']} {n.group(1)}"
                notes.append(f"'number {n.group(1)}' belongs to '{tgt['address']['street']}'")

    # 5. a municipality written right after an address belongs to the address
    addresses = [e for e in ents if e.get("address")]
    if addresses:
        for e in list(ents):
            if e.get("type") != "MUNICIPALITY":
                continue
            p = _pos(e["text"], prompt)
            if p is None:
                continue
            before = prompt[max(0, p - 8):p]
            if re.search(r"\d\s*[,\-–]\s*(?:\d{4}\s*)?$", before) or re.search(r"\d\s+(?:à|a|in|te)\s+$", before, re.I):
                for a in addresses:
                    if not a["address"].get("municipality"):
                        a["address"]["municipality"] = e["text"]
                ents.remove(e)
                notes.append(f"'{e['text']}' follows an address -> part of the address, not a waypoint")

    # 6. railway as the MAIN object of "railway between station A and B"
    if _BETWEEN_KW.search(prompt) and not any(e.get("type") == "RAILWAY" for e in ents):
        m = _RAIL_WORDS.search(prompt)
        kw = _BETWEEN_KW.search(prompt)
        if m and kw and m.start() < kw.start():
            ents.insert(0, _new(m.group(0), "RAILWAY", "main"))
            notes.append("railway word before 'between' -> RAILWAY main entity")

    # 7. roles from the sentence frame
    ents, relationship = _assign_roles(prompt, ents, relationship, notes)

    # 8. relationship / distance
    distance = out.get("distance_meters")
    if distance in (None, "", 0):
        distance = extract_distance(prompt)
        if distance is not None:
            notes.append(f"distance {distance:g} m taken from the prompt text")
    has_start_end = any(e.get("role") == "start" for e in ents) and any(e.get("role") == "end" for e in ents)
    if has_start_end and relationship not in ("BETWEEN", "FROM_TO"):
        relationship = "BETWEEN"
    if relationship in ("NONE", "", None):
        relationship = "NEAR" if (_BUFFER_WORD.search(prompt) or distance) and ents else ("SELF" if ents else "NONE")
        if relationship != "NONE":
            notes.append(f"no relation given: {relationship}")

    # 9. drop exact duplicates / a bare street that is only the street of an address in the same role set
    seen, final = set(), []
    for e in ents:
        key = (fold(e["text"]), e.get("role"))
        if key in seen:
            continue
        seen.add(key)
        final.append(e)
    addr_streets = {fold(a["address"]["street"]) for a in final if a.get("address")}
    final = [e for e in final if e.get("address") or e.get("role") == "main" or fold(e["text"]) not in addr_streets]
    for e in final:
        e.pop("_pos", None)

    out.update(entities=final, relationship=relationship, distance_meters=distance, modifier=modifier, notes=notes)
    return out


def _assign_roles(prompt: str, ents: list[dict], relationship: str, notes: list[str]) -> tuple[list[dict], str]:
    """Set start / end / via / main from keywords and entity positions in the prompt."""
    if not ents:
        return ents, relationship
    for e in ents:
        e["_pos"] = _pos(e["text"], prompt) if e.get("_pos") is None else e["_pos"]
    positioned = [e for e in ents if e["_pos"] is not None]
    unpositioned = [e for e in ents if e["_pos"] is None]
    positioned.sort(key=lambda e: e["_pos"])
    ents = positioned + unpositioned
    non_area = [e for e in ents if e.get("type") not in AREA_TYPE_NAMES]

    kw = _BETWEEN_KW.search(prompt)
    via = _VIA_KW.search(prompt)

    if kw and len(non_area) >= 2:
        before = [e for e in non_area if e["_pos"] is not None and e["_pos"] < kw.start()]
        after = [e for e in non_area if e["_pos"] is None or e["_pos"] >= kw.start()]
        if via:
            after_via = [e for e in after if e["_pos"] is not None and e["_pos"] >= via.start()]
            after = [e for e in after if e not in after_via]
        else:
            after_via = []
        # "between Y and Z ON X" (the main street named last): X follows a word like sur / op / on / along
        if not before and len(after) == 3 and via is None:
            lead = prompt[:after[2]["_pos"]].lower() if after[2]["_pos"] is not None else ""
            if re.search(r"\b(sur|op|on|langs|along|le long de)\s+(?:la |le |l'|de |den |het |the )?$", lead):
                after[2]["role"], after[0]["role"], after[1]["role"] = "main", "start", "end"
                notes.append(f"frame 'between Y and Z on X': main='{after[2]['text']}', start='{after[0]['text']}', end='{after[1]['text']}'")
                return ents, "BETWEEN"
        main = before[-1] if before else None
        # "X between Y and Z": X is the street the stretch lies on, not an endpoint
        if main is not None and len(after) >= 2:
            main["role"] = "main"
            for e in before[:-1]:
                e["role"] = "anchor"
            after[0]["role"], after[-1]["role"] = "start", "end"
            for e in after[1:-1]:
                e["role"] = "via"
            for e in after_via:
                e["role"] = "via"
            notes.append(f"frame 'X between Y and Z': main='{main['text']}', start='{after[0]['text']}', end='{after[-1]['text']}'")
            return ents, "BETWEEN"
        if len(after) >= 2 and not before:
            after[0]["role"], after[-1]["role"] = "start", "end"
            for e in after_via:
                e["role"] = "via"
            for e in after[1:-1]:
                e["role"] = "via"
            notes.append("frame 'between Y and Z'")
            return ents, "BETWEEN"
        if main is not None and len(after) == 1 and any(e.get("role") == "main" for e in ents) is False:
            # "railway between station A and B" where one endpoint is missing: leave the LLM roles alone
            pass

    if via and len(non_area) >= 3:
        after_via = [e for e in non_area if e["_pos"] is not None and e["_pos"] >= via.start()]
        rest = [e for e in non_area if e not in after_via]
        if after_via and len(rest) >= 2:
            rest[0]["role"], rest[-1]["role"] = "start", "end"
            for e in after_via:
                e["role"] = "via"
            notes.append(f"'via' frame: via='{after_via[0]['text']}'")
            return ents, "FROM_TO" if relationship in ("FROM_TO", "NONE", "") else relationship

    # keep LLM roles; make sure an unknown role is 'anchor'
    for e in ents:
        if e.get("role") not in ("anchor", "start", "end", "via", "main"):
            e["role"] = "anchor"
    # "main" is only valid together with start and end
    if not (any(e["role"] == "start" for e in ents) and any(e["role"] == "end" for e in ents)):
        for e in ents:
            if e["role"] == "main":
                e["role"] = "anchor"
    return ents, relationship
