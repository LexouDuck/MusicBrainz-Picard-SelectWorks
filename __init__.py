"""
Classical Works — a MusicBrainz Picard 3 plugin.

Picard tags files from a MusicBrainz *release* (the CD/album that was sold).
For classical music it is usually more useful to tag around the *work*:

    artist      -> composer
    album       -> work (e.g. "Symphony no. 5 in C minor, op. 67")
    title       -> movement (e.g. "I. Allegro con brio")
    date        -> composition date
    genre       -> work type (Symphony, Sonata, Concerto, ...)
    key         -> musical key
    tracknumber -> movement number within the work

How it works
------------
With "Use track relationships" enabled, the release data Picard downloads
already contains the work each recording performs, plus that work's own
relationships (composer, and the parent work if it is a movement). The plugin
then fetches the parent work(s) from the MusicBrainz API, walking up the
"part of" chain until it reaches the top-level work. Work lookups are cached
for the session, so the four movements of a symphony cost one request, and
all requests for an album go out one after another inside a single album task
(Picard rate-limits MusicBrainz to about one request per second).

The original release values are copied into release_* tags before being
replaced, so nothing is lost.
"""

import json
import re
from functools import partial


# ---------------------------------------------------------------------------
# Settings: edit these to taste, then restart Picard.
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
    # printed on the release.
    "title_from_work": True,

    # tracknumber/totaltracks become the movement number within the work,
    # and discnumber/totaldiscs are reset to 1/1.
    "set_track_numbers": True,

    # Copy the original album, artist, date, track numbers... into release_*
    # tags before overwriting them.
    "keep_release_info": True,

    # Prefer work titles in this locale when MusicBrainz has an alias for it,
    # e.g. "fr" or "en". None keeps the canonical (original language) title.
    # Setting this costs one extra request per track.
    "title_locale": None,

    # Safety limit on how many levels of "part of" to climb.
    "max_depth": 6,
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

# Session-wide cache: work MBID -> parsed work dict.
_work_cache = {}
# In-flight album fetches: id(album) -> _AlbumFetch.
_album_fetches = {}
_warned = set()


# ---------------------------------------------------------------------------
# Parsing MusicBrainz work JSON
# ---------------------------------------------------------------------------
def parse_work(node):
    """Reduce a MusicBrainz work JSON node to the fields this plugin uses."""
    work = {
        "id": node.get("id", ""),
        "title": node.get("title") or "",
        "type": node.get("type") or "",
        "key": "",
        "aliases": node.get("aliases") or [],
        "parent": None,       # MBID of the work this one is a part of
        "parent_title": "",   # its title, when given inline
        "position": None,     # ordering-key within that parent
        "parts_count": 0,     # number of parts this work has (when fetched)
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
            if rel.get("direction") == "backward":
                # This work is a part of rel["work"].
                if work["parent"] is None and rel.get("work"):
                    work["parent"] = rel["work"].get("id")
                    work["parent_title"] = rel["work"].get("title") or ""
                    work["position"] = rel.get("ordering-key")
            else:
                work["parts_count"] += 1
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
    return work


def performed_works(recording):
    """Work nodes linked to a recording by a 'performance' relationship."""
    return [
        rel["work"]
        for rel in recording.get("relations") or []
        if rel.get("target-type") == "work"
        and rel.get("type") == "performance"
        and rel.get("work")
    ]


def chain_from_cache(start_id, max_depth):
    """Climb the part-of chain through the cache.

    Returns (chain, missing_id): chain runs from start_id upwards; missing_id
    is the first work not yet cached, or None if the chain is complete.
    """
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


def full_chain(leaf, start_id):
    chain, missing = chain_from_cache(start_id, SETTINGS["max_depth"])
    if start_id != leaf["id"]:
        chain = [leaf] + chain
    return chain, missing


def chain_for_tagging(leaf, start_id):
    """Like full_chain, but if a parent could not be fetched, stand in a
    minimal parent built from the title given inline in the release data."""
    chain, _missing = full_chain(leaf, start_id)
    top = chain[-1]
    if top["parent"] and top["parent_title"] and top["parent"] not in _work_cache:
        stub = parse_work({"id": top["parent"], "title": top["parent_title"]})
        chain = chain + [stub]
    return chain


def start_id_for(leaf):
    """Where to start fetching. The track's own work comes inline with the
    release, so fetch from its parent when we already know it."""
    if SETTINGS["title_locale"] or not leaf["has_relations"] or not leaf["parent"]:
        return leaf["id"]
    return leaf["parent"]


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


def apply_work_metadata(metadata, chain):
    """Rewrite one track's metadata from its work chain (leaf first)."""
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

    if SETTINGS["keep_release_info"]:
        backup_release_values(metadata)

    # Album / work / movement
    metadata["album"] = album_title
    metadata["work"] = album_title
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
        metadata["musicbrainz_parentworkid"] = album_work["id"]
        if SETTINGS["title_from_work"]:
            metadata["title"] = movement
    elif SETTINGS["title_from_work"]:
        metadata["title"] = leaf_title

    # Composer as artist. Prefer Picard's own composer tags (they honour the
    # user's artist-name translation settings), fall back to the work chain.
    if SETTINGS["set_artist"]:
        names = metadata.getall("composer")
        sorts = metadata.getall("composersort")
        ids = metadata.getall("musicbrainz_composerid")
        if not names:
            composers = next((w["composers"] for w in album_down if w["composers"]), [])
            names = [c["name"] for c in composers]
            sorts = [c["sort"] for c in composers]
            ids = [c["id"] for c in composers if c["id"]]
        if names:
            joined = SETTINGS["artist_join"].join(names)
            joined_sort = SETTINGS["artist_join"].join(sorts) if sorts else joined
            for prefix in ("", "album"):
                metadata[f"{prefix}artist"] = joined
                metadata[f"{prefix}artists"] = names
                metadata[f"{prefix}artistsort"] = joined_sort
                if ids:
                    metadata[f"musicbrainz_{prefix}artistid"] = ids
            if not metadata.getall("composer"):
                metadata["composer"] = names
                metadata["composersort"] = sorts

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

    # Track numbering within the work
    # (Only when the movement number is known: for deeper hierarchies such as
    # opera number -> act -> opera, the release numbering is kept.)
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
# Fetching works for an album (one blocking album task per album)
# ---------------------------------------------------------------------------
class _AlbumFetch:
    def __init__(self, album):
        self.album = album
        self.task_id = f"classical_works_{id(album)}"
        self.tracks = []      # (metadata, leaf, start_id)
        self.queue = []       # work MBIDs still to request
        self.failed = set()

    def enqueue(self, work_id):
        if work_id and work_id not in self.queue and work_id not in self.failed:
            self.queue.append(work_id)


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


def request_next(api, fetch):
    """Send the next queued work lookup; finish the album task when done."""
    while fetch.queue and fetch.queue[0] in _work_cache:
        fetch.queue.pop(0)
    if not fetch.queue:
        finish(api, fetch)
        return None
    work_id = fetch.queue.pop(0)
    url = f"{ws_base(api)}/work/{work_id}?inc={WORK_INC}&fmt=json"
    handler = partial(on_work_response, api, fetch, work_id)
    try:
        return api.web_service.get_url(url=url, handler=handler, parse_response_type="json")
    except TypeError:
        # Older/newer signature without parse_response_type: parse it ourselves.
        return api.web_service.get_url(url=url, handler=handler)


def on_work_response(api, fetch, work_id, *args):
    # Web service handlers are called as (data, reply, error).
    data = args[0] if args else None
    error = args[-1] if len(args) > 1 else None
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        if isinstance(data, str):
            data = json.loads(data) if data.strip() else None
        if error or not isinstance(data, dict) or "id" not in data:
            fetch.failed.add(work_id)
            api.logger.warning(f"Classical Works: could not fetch work {work_id}: {error}")
        else:
            _work_cache[work_id] = parse_work(data)
        # The new work may point to a parent we have not fetched yet.
        for _metadata, leaf, start_id in fetch.tracks:
            _chain, missing = full_chain(leaf, start_id)
            fetch.enqueue(missing)
    except Exception as exc:
        fetch.failed.add(work_id)
        api.logger.error(f"Classical Works: error handling work {work_id}: {exc!r}")
    try:
        request_next(api, fetch)
    except Exception as exc:
        api.logger.error(f"Classical Works: request failed: {exc!r}")
        finish(api, fetch)


def finish(api, fetch):
    if _album_fetches.get(id(fetch.album)) is not fetch:
        return  # already finished
    del _album_fetches[id(fetch.album)]
    try:
        for metadata, leaf, start_id in fetch.tracks:
            chain = chain_for_tagging(leaf, start_id)
            try:
                apply_work_metadata(metadata, chain)
            except Exception as exc:
                api.logger.error(f"Classical Works: could not tag work {leaf['id']}: {exc!r}")
    finally:
        api.complete_album_task(fetch.album, fetch.task_id)


# ---------------------------------------------------------------------------
# Picard hook
# ---------------------------------------------------------------------------
def process_track(api, track, metadata, track_node, release_node=None):
    recording = track_node.get("recording") or {}
    if "relations" not in recording:
        if "no_track_rels" not in _warned:
            _warned.add("no_track_rels")
            api.logger.warning(
                "Classical Works: no recording relationships in the release data. "
                "Enable Options > Metadata > 'Use track relationships'."
            )
        return

    works = performed_works(recording)
    if not works:
        return  # recording not linked to a work: leave the track alone
    leaf = parse_work(works[0])
    if len(works) > 1:
        api.logger.debug(
            f"Classical Works: recording {recording.get('id')} performs {len(works)} works, using the first"
        )

    start_id = start_id_for(leaf)
    chain, missing = full_chain(leaf, start_id)
    if missing is None:
        apply_work_metadata(metadata, chain)
        return

    album = track.album
    fetch = _album_fetches.get(id(album))
    new_fetch = fetch is None
    if new_fetch:
        fetch = _AlbumFetch(album)
        _album_fetches[id(album)] = fetch
    fetch.tracks.append((metadata, leaf, start_id))
    fetch.enqueue(missing)
    if new_fetch:
        # Queue is filled before the task starts: the factory may run at once.
        # Later tracks of the same album just add to the queue while it runs.
        api.add_album_task(
            album,
            fetch.task_id,
            "Fetching classical work details",
            request_factory=partial(request_next, api, fetch),
            blocking=True,
        )


def enable(api):
    api.register_track_metadata_processor(process_track)
