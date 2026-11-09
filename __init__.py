"""
Classical Works — a MusicBrainz Picard 3 plugin.

Tags classical music around the MusicBrainz *work* rather than the release:

    artist      -> composer
    album       -> work (e.g. "Symphony no. 5 in C minor, op. 67")
    title       -> movement (e.g. "I. Allegro con brio")
    date        -> composition date
    genre       -> work type (Symphony, Sonata, Concerto, ...)
    key         -> musical key
    tracknumber -> movement number within the work

Two ways in:

- Right-click files or a cluster > "Find MusicBrainz work...": search for
  the work, pick which movement each file is, apply. No release needed.
  (action.py, dialog.py)
- Automatically, when files are matched to a release whose recordings are
  linked to works (needs "Use track relationships"). (release.py)

Settings live in works.py (SETTINGS).
"""

from .release import process_track


def enable(api):
    api.register_track_metadata_processor(process_track)
    try:
        from .action import FindWorkAction, FindWorkClusterAction
        api.register_file_action(FindWorkAction)
        api.register_cluster_action(FindWorkClusterAction)
    except Exception as exc:
        # Keep the release processor working even if the UI part fails.
        api.logger.exception(f"Classical Works: could not add the right-click action: {exc!r}")
