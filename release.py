"""
Release processor: when Picard loads a release, retag each track whose
recording is linked to a MusicBrainz work around that work.

Needs Options > Metadata > "Use track relationships". All work lookups for an
album go out one after another inside a single blocking album task, so Picard
waits for them before it finishes loading the album.
"""

from functools import partial

from . import works
from .works import SETTINGS, _work_cache, apply_work_metadata, parse_work, performed_works

# In-flight album fetches: id(album) -> _AlbumFetch.
_album_fetches = {}
_warned = set()


def full_chain(leaf, start_id):
    chain, missing = works.chain_from_cache(start_id)
    if start_id != leaf["id"]:
        chain = [leaf] + chain
    return chain, missing


def chain_for_tagging(leaf, start_id):
    chain, _missing = full_chain(leaf, start_id)
    return works.with_parent_stub(chain)


def start_id_for(leaf):
    """Where to start fetching. The track's own work comes inline with the
    release, so fetch from its parent when we already know it."""
    if SETTINGS["title_locale"] or not leaf["has_relations"] or not leaf["parent"]:
        return leaf["id"]
    return leaf["parent"]


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


def request_next(api, fetch):
    """Send the next queued work lookup; finish the album task when done."""
    while fetch.queue and fetch.queue[0] in _work_cache:
        fetch.queue.pop(0)
    if not fetch.queue:
        finish(api, fetch)
        return None
    work_id = fetch.queue.pop(0)
    return works.get_json(api, works.work_url(api, work_id), partial(on_work_response, api, fetch, work_id))


def on_work_response(api, fetch, work_id, data, error):
    try:
        if error or data is None or "id" not in data:
            fetch.failed.add(work_id)
            api.logger.warning(f"Classical Works: could not fetch work {work_id}: {error}")
        else:
            work = parse_work(data)
            _work_cache[work_id] = work
            _work_cache[work["id"]] = work
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
            try:
                apply_work_metadata(metadata, chain_for_tagging(leaf, start_id), from_release=True)
            except Exception as exc:
                api.logger.error(f"Classical Works: could not tag work {leaf['id']}: {exc!r}")
    finally:
        api.complete_album_task(fetch.album, fetch.task_id)


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

    performed = performed_works(recording)
    if not performed:
        return  # recording not linked to a work: leave the track alone
    leaf = parse_work(performed[0])
    if len(performed) > 1:
        api.logger.debug(
            f"Classical Works: recording {recording.get('id')} performs {len(performed)} works, using the first"
        )

    start_id = start_id_for(leaf)
    chain, missing = full_chain(leaf, start_id)
    if missing is None:
        apply_work_metadata(metadata, chain, from_release=True)
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
