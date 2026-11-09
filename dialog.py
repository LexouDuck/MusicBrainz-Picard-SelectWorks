"""
The "Find MusicBrainz work" dialog (PyQt6).

    Composer [Chopin            ]  Work [Ballade 1            ]  [Search]
    +---------------------------------------------------------------+
    | Work                               | Composer | Type    | Key |   search results
    +---------------------------------------------------------------+
    Selected: Ballade no. 1 in G minor, op. 23 - no movements
    +---------------------------------------------------------------+
    | 01 Ballade No 1 [Zimerman].mp3     | [Whole work         v] |    one row per file
    +---------------------------------------------------------------+
                                                  [Apply tags] [Cancel]
"""

import os
from html import escape

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from . import works
from .works import (MBID_RE, apply_work_metadata, fetch_chain, guess_assignments,
                    initial_search, part_as_leaf, search_works, strip_work_prefix,
                    with_parent_stub)

# Open dialogs, kept referenced so Python does not garbage-collect them.
_open_dialogs = set()


def open_dialog(api, files):
    parent = getattr(getattr(api, "tagger", None), "window", None)
    dialog = WorkLookupDialog(api, files, parent)
    _open_dialogs.add(dialog)
    dialog.finished.connect(lambda _result: _open_dialogs.discard(dialog))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    dialog.start()


def tag_value(metadata, tag):
    try:
        values = metadata.getall(tag)
        return values[0] if values else ""
    except Exception:
        return ""


def file_info(file):
    path = getattr(file, "filename", "") or ""
    folder_path = os.path.dirname(path)
    md = file.metadata
    return {
        "name": os.path.basename(path),
        "folder": os.path.basename(folder_path),
        "parent_folder": os.path.basename(os.path.dirname(folder_path)),
        "title": tag_value(md, "title"),
        "album": tag_value(md, "album"),
        "composer": tag_value(md, "composer"),
        "albumartist": tag_value(md, "albumartist"),
        "artist": tag_value(md, "artist"),
        "tracknumber": tag_value(md, "tracknumber"),
    }


class WorkLookupDialog(QtWidgets.QDialog):
    def __init__(self, api, files, parent=None):
        super().__init__(parent)
        self.api = api
        self.files = files
        self.infos = [file_info(f) for f in files]
        self.chain = []          # loaded work first, then the works it is part of
        self._serial = 0         # ignores answers to superseded requests
        self._alive = True

        self.setWindowTitle("Find MusicBrainz work")
        self.resize(980, 680)
        layout = QtWidgets.QVBoxLayout(self)

        # Search row
        row = QtWidgets.QHBoxLayout()
        self.composer_edit = QtWidgets.QLineEdit()
        self.composer_edit.setPlaceholderText("Composer")
        self.work_edit = QtWidgets.QLineEdit()
        self.work_edit.setPlaceholderText("Work title, or a MusicBrainz work link")
        self.search_button = QtWidgets.QPushButton("Search")
        row.addWidget(QtWidgets.QLabel("Composer"))
        row.addWidget(self.composer_edit, 2)
        row.addWidget(QtWidgets.QLabel("Work"))
        row.addWidget(self.work_edit, 3)
        row.addWidget(self.search_button)
        layout.addLayout(row)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # Search results
        self.results = QtWidgets.QTreeWidget()
        self.results.setHeaderLabels(["Work", "Composer", "Type", "Key"])
        self.results.setRootIsDecorated(False)
        self.results.setAlternatingRowColors(True)
        self.results.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        header = self.results.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        splitter.addWidget(self.results)

        # File -> movement assignments
        lower = QtWidgets.QWidget()
        lower_layout = QtWidgets.QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self.work_label = QtWidgets.QLabel("Pick a work in the list above.")
        self.work_label.setWordWrap(True)
        lower_layout.addWidget(self.work_label)
        self.mapping = QtWidgets.QTableWidget(len(files), 2)
        self.mapping.setHorizontalHeaderLabels(["File", "Tag as"])
        self.mapping.verticalHeader().setVisible(False)
        self.mapping.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        mapping_header = self.mapping.horizontalHeader()
        mapping_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        mapping_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.combos = []
        for index, info in enumerate(self.infos):
            item = QtWidgets.QTableWidgetItem(info["name"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(getattr(files[index], "filename", ""))
            self.mapping.setItem(index, 0, item)
            combo = QtWidgets.QComboBox()
            combo.setEnabled(False)
            self.mapping.setCellWidget(index, 1, combo)
            self.combos.append(combo)
        lower_layout.addWidget(self.mapping)
        splitter.addWidget(lower)
        splitter.setSizes([320, 300])

        # Buttons
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Apply tags")
        self.apply_button.setEnabled(False)
        layout.addWidget(self.buttons)

        # Signals
        self.search_button.clicked.connect(self.search)
        self.composer_edit.returnPressed.connect(self.search)
        self.work_edit.returnPressed.connect(self.search)
        self.results.itemSelectionChanged.connect(self.on_result_selected)
        self.buttons.accepted.connect(self.apply)
        self.buttons.rejected.connect(self.reject)
        self.finished.connect(self._on_finished)

        composer, work = initial_search(self.infos)
        self.composer_edit.setText(composer)
        self.work_edit.setText(work)

    # -- lifecycle --------------------------------------------------------
    def start(self):
        if self.work_edit.text().strip() or self.composer_edit.text().strip():
            self.search()
        else:
            self.work_edit.setFocus()

    def _on_finished(self, _result):
        self._alive = False

    def _current(self, serial):
        """Should an answer to request `serial` still be shown?"""
        return self._alive and serial == self._serial

    def set_status(self, text):
        self.status.setText(text)

    # -- search -----------------------------------------------------------
    def search(self):
        composer = self.composer_edit.text().strip()
        title = self.work_edit.text().strip()
        mbid = MBID_RE.search(title)
        if mbid:
            self.load_work(mbid.group(0).lower())
            return
        if not composer and not title:
            self.set_status("Type a composer and/or a work title.")
            return
        self._serial += 1
        serial = self._serial
        self.results.clear()
        self.clear_work()
        self.set_status("Searching MusicBrainz…")
        self.search_button.setEnabled(False)
        search_works(self.api, composer, title, lambda results, error: self.on_results(serial, results, error))

    def on_results(self, serial, results, error):
        if not self._current(serial):
            return
        self.search_button.setEnabled(True)
        if error and not results:
            self.set_status(f"Search failed: {error}")
            return
        if not results:
            self.set_status("No works found. Try fewer words, or the work's original-language title.")
            return
        for result in results:
            title = result["title"]
            if result["disambiguation"]:
                title += f" ({result['disambiguation']})"
            item = QtWidgets.QTreeWidgetItem([title, result["composers"], result["type"], result["key"]])
            item.setData(0, Qt.ItemDataRole.UserRole, result["id"])
            item.setToolTip(0, title)
            self.results.addTopLevelItem(item)
        self.set_status(f"{len(results)} works found. Pick the one these files are a recording of.")

    def on_result_selected(self):
        items = self.results.selectedItems()
        if items:
            self.load_work(items[0].data(0, Qt.ItemDataRole.UserRole))

    # -- loading a work ---------------------------------------------------
    def clear_work(self):
        self.chain = []
        self.apply_button.setEnabled(False)
        self.work_label.setText("Pick a work in the list above.")
        for combo in self.combos:
            combo.clear()
            combo.setEnabled(False)

    def load_work(self, work_id):
        self._serial += 1
        serial = self._serial
        self.clear_work()
        self.set_status("Loading the work and its movements…")
        fetch_chain(self.api, work_id, lambda chain, error: self.on_chain(serial, chain, error))

    def on_chain(self, serial, chain, error):
        if not self._current(serial):
            return
        if not chain:
            self.set_status(f"Could not load the work: {error}")
            return
        if error:
            self.set_status(f"Loaded the work, but not everything it is part of ({error}).")
        else:
            self.set_status("")
        self.chain = with_parent_stub(chain)
        work = self.chain[0]

        description = f"<b>{escape(work['title'])}</b>"
        composers = ", ".join(c["name"] for c in work["composers"])
        if composers:
            description += f" — {escape(composers)}"
        if len(self.chain) > 1:
            description += f"<br>Part of: {escape(self.chain[-1]['title'])}"
        description += f"<br>{work['parts_count']} movements/parts" if work["parts"] else "<br>No movements listed in MusicBrainz."
        self.work_label.setText(description)

        whole_label = "This movement" if len(self.chain) > 1 else "Whole work"
        labels = [strip_work_prefix(p["title"], [work["title"]]) for p in work["parts"]]
        guesses = guess_assignments(self.infos, work)
        for combo, guess in zip(self.combos, guesses):
            combo.clear()
            combo.addItem(f"{whole_label}: {work['title']}", -1)
            for index, label in enumerate(labels):
                combo.addItem(label, index)
            combo.setCurrentIndex(0 if guess is None else guess + 1)
            combo.setEnabled(True)
        self.apply_button.setEnabled(True)

    # -- applying ---------------------------------------------------------
    def apply(self):
        if not self.chain:
            return
        work = self.chain[0]
        tagged = 0
        for file, combo in zip(self.files, self.combos):
            choice = combo.currentData()
            if choice is None or choice < 0:
                chain = self.chain
            else:
                chain = [part_as_leaf(work, work["parts"][choice])] + self.chain
            try:
                apply_work_metadata(file.metadata, chain, from_release=False)
                tagged += 1
            except Exception as exc:
                self.api.logger.exception(f"Classical Works: could not tag {getattr(file, 'filename', file)}: {exc!r}")
                continue
            try:
                file.update()
            except Exception as exc:
                self.api.logger.warning(f"Classical Works: could not refresh {getattr(file, 'filename', file)}: {exc!r}")
        self.api.logger.info(f"Classical Works: tagged {tagged} file(s) from work {work['id']}")
        self.accept()
