"""
Right-click "Find MusicBrainz work..." action for files and clusters.

Opens a dialog to search MusicBrainz for a work, pick the movement each
selected file is, and tag the files from that work, without any release.
"""

from picard.plugin3.api import BaseAction


def collect_files(objs):
    """Files inside the selected objects (files, clusters, tracks, albums)."""
    files, seen = [], set()

    def add(file):
        if id(file) not in seen:
            seen.add(id(file))
            files.append(file)

    for obj in objs:
        if hasattr(obj, "filename") and hasattr(obj, "metadata"):
            add(obj)  # a File
        elif hasattr(obj, "iterfiles"):
            for file in obj.iterfiles():
                add(file)
        elif hasattr(obj, "files"):
            for file in obj.files:
                add(file)
    return files


class FindWorkAction(BaseAction):
    TITLE = "Find MusicBrainz work…"

    def callback(self, objs):
        files = collect_files(objs)
        if not files:
            self.api.logger.info("Classical Works: no files in the selection")
            return
        try:
            from .dialog import open_dialog
            open_dialog(self.api, files)
        except Exception as exc:
            self.api.logger.exception(f"Classical Works: could not open the work dialog: {exc!r}")


class FindWorkClusterAction(FindWorkAction):
    """Same action, registered separately for the cluster context menu."""
