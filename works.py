"""
Shared code: MusicBrainz work parsing, work lookups and tag building.

Used both by the release processor (release.py) and the right-click
"Find MusicBrainz work..." action (action.py / dialog.py). Nothing here
imports Picard or Qt, so it can be tested on its own.
"""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from functools import partial
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Settings: edit these to taste, commit, then update the plugin in Picard.
# ---------------------------------------------------------------------------
SETTINGS = {
    # Which work becomes the "album":
    #   "top"    -> the top-level work (an opera, rather than one of its acts)
    #   "parent" -> the immediate parent of the track's work (the act)
    "album_level": "top",

    # artist / albumartist (+ sort names and MBIDs) become the composer(s).
    "set_artist": True,
    # Joiner used when a work has several composers.
    "artist_join": " & ",

    # date becomes the composition date. MusicBrainz stores it as the begin /
    # end dates of the composer relationship; "end" = year completed,
    # "begin" = year started. The full range goes to the compositiondate tag.
    "set_date": True,
    "date_from": "end",

    # genre becomes the MusicBrainz work type (Symphony, Sonata, Concerto...).
    "set_genre": True,

    # key becomes the work's "Key" attribute (e.g. "G minor").
    "set_key": True,

    # title becomes the work / movement title rather than the title as
    # printed on the release / in the file.
    "title_from_work": True,

    # tracknumber/totaltracks become the movement number within the work,
    # and discnumber/totaldiscs are reset to 1/1.
    "set_track_numbers": True,

    # Release processor only: copy the original album, artist, date, track
    # numbers... into release_* tags before overwriting them.
    "keep_release_info": True,

    # Prefer work titles in this locale when MusicBrainz has an alias for it,
    # e.g. "fr" or "en". None keeps the canonical (original language) title.
    "title_locale": None,

    # Safety limit on how many levels of "part of" to climb.
    "max_depth": 6,

    # Right-click action: how many search results to show.
    "search_limit": 40,
}

WORK_INC = "aliases+artist-rels+work-rels+series-rels"

# Tags saved before overwriting -> name of the tag that keeps the release value.
RELEASE_BACKUP = {
    "album": "release_album",
    "albumsort": "release_albumsort",
    "albumartist": "release_albumartist",
    "albumartists": "release_albumartists",
    "albumartistsort": "release_albumartistsort",
    "artist": "release_artist",
    "artists": "release_artists",
    "artistsort": "release_artistsort",
    "musicbrainz_artistid": "release_musicbrainz_artistid",
    "musicbrainz_albumartistid": "release_musicbrainz_albumartistid",
    "title": "release_title",
    "date": "releasedate",
    "genre": "release_genre",
    "tracknumber": "release_tracknumber",
    "totaltracks": "release_totaltracks",
    "discnumber": "release_discnumber",
    "totaldiscs": "release_totaldiscs",
}

# Tags that only make sense for a movement; removed when a file is tagged as
# a whole work (so re-tagging a file never leaves stale movement info).
MOVEMENT_TAGS = ("movement", "movementnumber", "movementtotal", "showmovement",
                 "musicbrainz_parentworkid")

# Catalogue numbers as they usually appear in MusicBrainz work titles.
CATALOGUE_RE = re.compile(
    r"""\b(
        op\.?\s*posth\.?
      | op(?:us|\.)?\s*\d+[a-z]?(?:\s*(?:no\.?|nr\.?|n°)\s*\d+[a-z]?)?
      | BWV\s*Anh\.?\s*\d+[a-z]?
      | BWV\s*\d+[a-z]?
      | KV?\.?\s*\d+[a-z]?(?:\s*\(\s*\d+[a-z]?\s*\))?
      | Hob\.?\s*[IVXL]+[:/]?\s*[\w]+
      | HWV\s*\d+[a-z]?
      | RV\s*\d+[a-z]?
      | WoO\s*\d+[a-z]?
      | D\.?\s*\d+[a-z]?
      | S\.?\s*\d+[a-z]?
      | L\.?\s*\d+[a-z]?
      | TH\s*\d+
      | BB\s*\d+
      | Sz\.?\s*\d+
      | FP\s*\d+
      | B\.?\s*\d+
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

# Session-wide cache: work MBID -> parsed work dict.
_work_cache = {}
# Work lookups in flight: work MBID -> callbacks waiting for it.
_pending_works = {}


# ---------------------------------------------------------------------------
# Parsing MusicBrainz work JSON
# ---------------------------------------------------------------------------
def parse_work(node):
    """Reduce a MusicBrainz work JSON node to the fields this plugin uses."""
    work = {
        "id": node.get("id", ""),
        "title": node.get("title") or "",
        "type": node.get("type") or "",
        "disambiguation": node.get("disambiguation") or "",
        "key": "",
        "aliases": node.get("aliases") or [],
        "parent": None,       # MBID of the work this one is a part of
        "parent_title": "",   # its title, when given inline
        "position": None,     # ordering-key within that parent
        "parts": [],          # this work's parts: [{"id", "title", "position"}]
        "parts_count": 0,
        "composers": [],
        "series": [],
        "has_relations": "relations" in node,
    }
    for attribute in node.get("attributes") or []:
        if (attribute.get("type") or "").lower() == "key" and attribute.get("value"):
            work["key"] = attribute["value"]
            break

    for rel in node.get("relations") or []:
        target = rel.get("target-type")
        rel_type = rel.get("type")
        if target == "work" and rel_type == "parts":
            other = rel.get("work") or {}
            if rel.get("direction") == "backward":
                # This work is a part of rel["work"].
                if work["parent"] is None and other.get("id"):
                    work["parent"] = other["id"]
                    work["parent_title"] = other.get("title") or ""
                    work["position"] = rel.get("ordering-key")
            elif other.get("id"):
                work["parts"].append({
                    "id": other["id"],
                    "title": other.get("title") or "",
                    "position": rel.get("ordering-key"),
                    "node": other,
                })
        elif target == "artist" and rel_type == "composer":
            artist = rel.get("artist") or {}
            work["composers"].append({
                "id": artist.get("id", ""),
                "name": rel.get("target-credit") or artist.get("name", ""),
                "sort": artist.get("sort-name") or artist.get("name", ""),
                "begin": rel.get("begin") or "",
                "end": rel.get("end") or "",
            })
        elif target == "series" and rel_type == "part of":
            series = rel.get("series") or {}
            number = (rel.get("attribute-values") or {}).get("number", "")
            name = series.get("name", "")
            if name:
                work["series"].append(f"{name} {number}".strip())

    work["parts"].sort(key=lambda p: (p["position"] is None, p["position"] or 0, p["title"]))
    work["parts_count"] = len(work["parts"])
    return work


def part_as_leaf(work, part):
    """A work dict for one of `work`'s parts, built from the inline data."""
    leaf = parse_work(part.get("node") or {"id": part["id"], "title": part["title"]})
    leaf["parent"] = work["id"]
    leaf["parent_title"] = work["title"]
    leaf["position"] = part["position"]
    return leaf


def performed_works(recording):
    """Work nodes linked to a recording by a 'performance' relationship."""
    return [
        rel["work"]
        for rel in recording.get("relations") or []
        if rel.get("target-type") == "work"
        and rel.get("type") == "performance"
        and rel.get("work")
    ]


def chain_from_cache(start_id, max_depth=None):
    """Climb the part-of chain through the cache.

    Returns (chain, missing_id): chain runs from start_id upwards; missing_id
    is the first work not yet cached, or None if the chain is complete.
    """
    max_depth = max_depth or SETTINGS["max_depth"]
    chain, seen, work_id = [], set(), start_id
    for _ in range(max_depth):
        work = _work_cache.get(work_id)
        if work is None:
            return chain, work_id
        chain.append(work)
        seen.add(work_id)
        if not work["parent"] or work["parent"] in seen:
            return chain, None
        work_id = work["parent"]
    return chain, None


def with_parent_stub(chain):
    """If the top of the chain has a parent that could not be fetched, stand
    in a minimal parent built from the title given inline."""
    if not chain:
        return chain
    top = chain[-1]
    if top["parent"] and top["parent_title"] and top["parent"] not in _work_cache:
        return chain + [parse_work({"id": top["parent"], "title": top["parent_title"]})]
    return chain


# ---------------------------------------------------------------------------
# Building the tags
# ---------------------------------------------------------------------------
def localized_title(work):
    locale = SETTINGS["title_locale"]
    if locale:
        matches = [
            a for a in work["aliases"]
            if (a.get("locale") or "").lower().startswith(locale.lower()) and a.get("name")
        ]
        if matches:
            matches.sort(key=lambda a: not a.get("primary"))
            return matches[0]["name"]
    return work["title"]


def strip_work_prefix(title, prefixes):
    """'Symphony no. 5 ..., op. 67: I. Allegro con brio' -> 'I. Allegro con brio'."""
    for prefix in prefixes:
        if prefix and title.lower().startswith(prefix.lower()) and len(title) > len(prefix):
            rest = title[len(prefix):].lstrip(" :,.;-–—/")
            if rest:
                return rest
    return title


def find_catalogue_number(*titles):
    for title in titles:
        match = CATALOGUE_RE.search(title or "")
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def composition_dates(works):
    """Dates of the first composer relationship that has any, album work first."""
    for work in works:
        for composer in work["composers"]:
            if composer["begin"] or composer["end"]:
                return composer["begin"], composer["end"]
    return "", ""


def backup_release_values(metadata):
    for tag, backup in RELEASE_BACKUP.items():
        values = metadata.getall(tag)
        if values and not metadata.getall(backup):
            metadata[backup] = list(values) if len(values) > 1 else values[0]


def apply_work_metadata(metadata, chain, from_release=True):
    """Rewrite one track's / file's metadata from its work chain (leaf first).

    from_release=True: called while Picard loads a release. Picard's own
    composer tags are trusted and the release values are backed up.
    from_release=False: called from the right-click action on loose files,
    whose existing tags are not trusted: composers come from MusicBrainz.
    """
    leaf = chain[0]
    if SETTINGS["album_level"] == "parent" and len(chain) > 1:
        album_work = chain[1]
    else:
        album_work = chain[-1]
    album_index = chain.index(album_work)
    # Works from the album work down to the leaf: album-level facts first.
    album_down = list(reversed(chain[: album_index + 1]))
    is_movement = album_work is not leaf
    parent = chain[1] if len(chain) > 1 else None

    album_title = localized_title(album_work)
    leaf_title = localized_title(leaf)

    if from_release and SETTINGS["keep_release_info"]:
        backup_release_values(metadata)

    # Album / work / movement
    metadata["album"] = album_title
    metadata["work"] = album_title
    if leaf["id"] and not from_release:
        metadata["musicbrainz_workid"] = leaf["id"]
    if is_movement:
        movement = strip_work_prefix(
            leaf_title,
            [album_title, album_work["title"], localized_title(parent), parent["title"]],
        )
        metadata["movement"] = movement
        metadata["showmovement"] = "1"
        if leaf["position"] is not None and parent is album_work:
            metadata["movementnumber"] = str(leaf["position"])
            if album_work["parts_count"]:
                metadata["movementtotal"] = str(album_work["parts_count"])
            else:
                metadata.delete("movementtotal")
        metadata["musicbrainz_parentworkid"] = album_work["id"]
        if SETTINGS["title_from_work"]:
            metadata["title"] = movement
    else:
        for tag in MOVEMENT_TAGS:
            metadata.delete(tag)
        if SETTINGS["title_from_work"]:
            metadata["title"] = leaf_title

    # Composer as artist. When loading a release, prefer Picard's own composer
    # tags (they honour the user's artist-name translation settings).
    composers = next((w["composers"] for w in album_down if w["composers"]), [])
    if from_release and metadata.getall("composer"):
        names = metadata.getall("composer")
        sorts = metadata.getall("composersort")
        ids = metadata.getall("musicbrainz_composerid")
    else:
        names = [c["name"] for c in composers]
        sorts = [c["sort"] for c in composers]
        ids = [c["id"] for c in composers if c["id"]]
        if names:
            metadata["composer"] = names
            metadata["composersort"] = sorts
            if ids:
                metadata["musicbrainz_composerid"] = ids
    if names and SETTINGS["set_artist"]:
        joined = SETTINGS["artist_join"].join(names)
        joined_sort = SETTINGS["artist_join"].join(sorts) if sorts else joined
        for prefix in ("", "album"):
            metadata[f"{prefix}artist"] = joined
            metadata[f"{prefix}artists"] = names
            metadata[f"{prefix}artistsort"] = joined_sort
            if ids:
                metadata[f"musicbrainz_{prefix}artistid"] = ids

    # Composition date
    begin, end = composition_dates(album_down)
    if begin or end:
        begin_year, end_year = begin[:4], end[:4]
        if begin_year and end_year and begin_year != end_year:
            metadata["compositiondate"] = f"{begin_year}–{end_year}"
        else:
            metadata["compositiondate"] = begin_year or end_year
        if SETTINGS["set_date"]:
            if SETTINGS["date_from"] == "begin":
                metadata["date"] = begin_year or end_year
            else:
                metadata["date"] = end_year or begin_year

    # Genre = work type
    work_type = next((w["type"] for w in album_down if w["type"]), "")
    if work_type:
        metadata["worktype"] = work_type
        if SETTINGS["set_genre"]:
            metadata["genre"] = work_type

    # Key: the movement's own key if it has one, else the work's.
    key = next((w["key"] for w in chain[: album_index + 1] if w["key"]), "")
    if key and SETTINGS["set_key"]:
        metadata["key"] = key

    # Catalogue numbers
    opus = find_catalogue_number(album_work["title"], leaf["title"])
    if opus:
        metadata["opus"] = opus
    if album_work["series"]:
        metadata["work_catalogue"] = album_work["series"]

    # Track numbering within the work. (Only when the movement number is
    # known: for deeper hierarchies such as opera number -> act -> opera, the
    # existing numbering is kept.)
    if SETTINGS["set_track_numbers"]:
        numbered = False
        if not is_movement:
            metadata["tracknumber"] = "1"
            metadata["totaltracks"] = "1"
            numbered = True
        elif leaf["position"] is not None and parent is album_work:
            metadata["tracknumber"] = str(leaf["position"])
            if album_work["parts_count"]:
                metadata["totaltracks"] = str(album_work["parts_count"])
            else:
                metadata.delete("totaltracks")
            numbered = True
        if numbered:
            metadata["discnumber"] = "1"
            metadata["totaldiscs"] = "1"


# ---------------------------------------------------------------------------
# MusicBrainz web service
# ---------------------------------------------------------------------------
def ws_base(api):
    try:
        setting = api.global_config.setting
        host = setting["server_host"] or "musicbrainz.org"
        port = int(setting["server_port"] or 443)
    except Exception:
        host, port = "musicbrainz.org", 443
    scheme = "https" if port == 443 else "http"
    port_part = "" if port in (80, 443) else f":{port}"
    return f"{scheme}://{host}{port_part}/ws/2"


def work_url(api, work_id):
    return f"{ws_base(api)}/work/{work_id}?inc={WORK_INC}&fmt=json"


def get_json(api, url, handler):
    """GET a MusicBrainz URL; handler(data_or_None, error) gets parsed JSON."""
    def on_response(*args):
        # Web service handlers are called as (data, reply, error).
        data = args[0] if args else None
        error = args[-1] if len(args) > 1 else None
        try:
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8", "replace")
            if isinstance(data, str):
                data = json.loads(data) if data.strip() else None
        except ValueError as exc:
            data, error = None, error or exc
        if not isinstance(data, dict):
            data = None
            error = error or "empty response"
        handler(data, error)

    try:
        return api.web_service.get_url(url=url, handler=on_response, parse_response_type="json")
    except TypeError:
        # Signature without parse_response_type: on_response parses it.
        return api.web_service.get_url(url=url, handler=on_response)


def fetch_work(api, work_id, callback):
    """Look a work up (cached); callback(work_or_None, error)."""
    if work_id in _work_cache:
        callback(_work_cache[work_id], None)
        return
    if work_id in _pending_works:
        _pending_works[work_id].append(callback)
        return
    _pending_works[work_id] = [callback]

    def on_data(data, error):
        work = None
        if data is not None and data.get("id"):
            work = parse_work(data)
            _work_cache[work_id] = work
            _work_cache[work["id"]] = work  # merged MBIDs redirect to a new id
        else:
            error = error or "no work in response"
        for waiting in _pending_works.pop(work_id, []):
            try:
                waiting(work, error)
            except Exception as exc:
                api.logger.error(f"Classical Works: work callback failed: {exc!r}")

    try:
        get_json(api, work_url(api, work_id), on_data)
    except Exception as exc:
        on_data(None, exc)


def fetch_chain(api, work_id, callback):
    """Fetch a work and all the works it is part of; callback(chain, error).

    On a failure part-way, the chain fetched so far is passed with the error.
    """
    chain = []

    def got(work, error):
        if work is None:
            callback(chain, error)
            return
        chain.append(work)
        seen = {w["id"] for w in chain}
        if work["parent"] and work["parent"] not in seen and len(chain) < SETTINGS["max_depth"]:
            fetch_work(api, work["parent"], got)
        else:
            callback(chain, None)

    fetch_work(api, work_id, got)


def lucene_escape(text):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)


def build_search_query(composer, title):
    terms = " ".join(lucene_escape(t) for t in title.split())
    parts = []
    if terms:
        parts.append(f"(work:({terms}) OR alias:({terms}))")
    composer_terms = " ".join(lucene_escape(t) for t in composer.split())
    if composer_terms:
        parts.append(f"artist:({composer_terms})")
    return " AND ".join(parts)


def search_url(api, composer, title):
    query = build_search_query(composer, title)
    return f"{ws_base(api)}/work/?query={quote(query)}&limit={SETTINGS['search_limit']}&fmt=json"


def parse_search_results(data):
    """MusicBrainz work search JSON -> list of simple result dicts."""
    results = []
    for node in (data or {}).get("works") or []:
        composers = [
            rel.get("artist", {}).get("name", "")
            for rel in node.get("relations") or []
            if rel.get("type") == "composer" and rel.get("artist")
        ]
        key = next(
            (a.get("value", "") for a in node.get("attributes") or []
             if (a.get("type") or "").lower() == "key"),
            "",
        )
        results.append({
            "id": node.get("id", ""),
            "title": node.get("title") or "",
            "type": node.get("type") or "",
            "disambiguation": node.get("disambiguation") or "",
            "composers": ", ".join(dict.fromkeys(c for c in composers if c)),
            "key": key,
            "score": node.get("score") or 0,
        })
    return results


def search_works(api, composer, title, callback):
    """callback(results, error)."""
    def on_data(data, error):
        callback(parse_search_results(data) if data is not None else [], error)
    get_json(api, search_url(api, composer, title), on_data)


# ---------------------------------------------------------------------------
# Guessing which movement each file is
# ---------------------------------------------------------------------------
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
         "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}
NOISE_RE = re.compile(r"\[[^\]]*\]")          # [performer], [youtube id]
LEADING_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s*[-.)_ ]")
ROMAN_RE = re.compile(r"(?:^|[\s\-:(_,])(xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)\s*[.)]", re.IGNORECASE)


def plain(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def number_in(text):
    """Movement number written in a title: '1. Allegro', 'II. Andante'."""
    match = LEADING_NUMBER_RE.match(text)
    if match:
        return int(match.group(1))
    match = ROMAN_RE.search(" " + text)
    if match:
        return ROMAN.get(match.group(1).lower())
    return None


def file_number(info):
    """Movement number of a file: from its name/title, else its track number."""
    name = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", info.get("name", ""))  # "x.mp3" is not movement X
    for text in (info.get("title", ""), name):
        number = number_in(NOISE_RE.sub(" ", text))
        if number:
            return number
    match = re.match(r"\s*(\d+)", info.get("tracknumber") or "")
    return int(match.group(1)) if match else None


def text_match(part_title, text):
    """Share of the movement title's words found in the file text (fuzzy)."""
    words = [w for w in plain(part_title).split() if len(w) >= 3 and not w.isdigit() and w not in ROMAN]
    if not words:
        return 0.0
    text_words = plain(text).split()
    hits = sum(
        1 for w in words
        if any(t == w or (len(t) >= 4 and SequenceMatcher(None, w, t).ratio() >= 0.85) for t in text_words)
    )
    return hits / len(words)


def guess_assignments(files, work):
    """For each file, the index into work['parts'] it most likely is, or None
    for the whole work.

    files: [{"name", "title", "tracknumber"}] in display order.
    """
    parts = work["parts"]
    if not parts:
        return [None] * len(files)
    labels = [strip_work_prefix(p["title"], [work["title"]]) for p in parts]
    numbers = [p["position"] if isinstance(p["position"], int) else number_in(label)
               for p, label in zip(parts, labels)]

    guesses = []
    for info in files:
        text = NOISE_RE.sub(" ", f"{info.get('title', '')} {info.get('name', '')}")
        number = file_number(info)
        best, best_score = None, 0.0
        for index, label in enumerate(labels):
            score = text_match(label, text)
            if number is not None and numbers[index] == number:
                score += 0.5
            if score > best_score:
                best, best_score = index, score
        guesses.append(best if best_score >= 0.5 else None)

    # As many files as movements but the clues are inconclusive: assume the
    # files are the movements in order.
    if len(files) == len(parts) and (None in guesses or len(set(guesses)) < len(guesses)):
        order = sorted(range(len(files)), key=lambda i: (file_number(files[i]) or 0, files[i].get("name", "")))
        guesses = [None] * len(files)
        for position, file_index in enumerate(order):
            guesses[file_index] = position
    return guesses


# ---------------------------------------------------------------------------
# Pre-filling the search from the selected files
# ---------------------------------------------------------------------------
def clean_name(text):
    """File or folder name -> plausible search text."""
    text = NOISE_RE.sub(" ", text)
    text = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", text)          # extension
    text = re.sub(r"^\s*\d{1,3}\s*[-.)_ ]\s*", "", text)        # track number
    text = re.sub(r"[_]+", " ", text)
    return " ".join(text.split())


def initial_search(files):
    """(composer, work) guesses from the selected files' tags and folders.

    files: [{"name", "folder", "parent_folder", "title", "album", "composer", "artist"}]
    """
    def common(key):
        values = {f.get(key, "").strip() for f in files}
        values.discard("")
        return values.pop() if len(values) == 1 else ""

    # The artist tag of a loose file is usually the performer or uploader, so
    # it is only a last resort for the composer.
    composer = common("composer") or common("albumartist")
    folder, parent_folder = common("folder"), common("parent_folder")
    # MUSIC/<Composer>/<file> or MUSIC/<Composer>/<Work>/<file>
    folder_is_work = looks_like_work(folder)
    work = common("album")
    if len(files) == 1:
        work = work or files[0].get("title", "") or clean_name(files[0].get("name", ""))
    if not work and folder_is_work:
        work = clean_name(folder)
    if not composer:
        composer = parent_folder if folder_is_work else folder
    if not composer:
        composer = common("artist")
    if work.lower() == composer.lower():
        work = ""
    return composer, work


WORK_WORDS_RE = re.compile(
    r"symphon|sinfon|sonat|concert|konzert|nocturn|etud|study|studies|prelud|ballad|waltz|valse|walzer|"
    r"mazurk|polonais|impromptu|scherzo|suite|partita|quartet|quatuor|quintet|trio|fugue|fuga|toccata|"
    r"variation|requiem|mass|messe|missa|opera|oper\b|ballet|overture|ouverture|serenad|rhapsod|"
    r"fantas|gnossienne|gymnop|cantata|kantate|oratori|lieder|songs|melodies|pieces|stücke|\bop\b|\bop\.|\bno\b|\bno\.",
    re.IGNORECASE,
)


def looks_like_work(name):
    """Is this folder name a work ('Symphony 5 [Kleiber]') rather than a person?"""
    name = NOISE_RE.sub(" ", name or "")
    return bool(re.search(r"\d", name) or WORK_WORDS_RE.search(name))
