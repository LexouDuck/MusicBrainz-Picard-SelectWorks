# Classical Works — Picard 3 plugin

Tags classical music around the **MusicBrainz work** rather than the release
it was recorded on.

## Tagging files from a work (right-click)

1. Drag your files into Picard. Leave them unmatched (in *Unclustered Files*
   or a cluster).
2. Select the files of one work (or the cluster), right-click →
   **Find MusicBrainz work…**
3. The search is pre-filled from the files' tags, or from their folders
   (`<Composer>/<Work>/file` or `<Composer>/file`). Edit it and press
   **Search** if needed. You can also paste a MusicBrainz work link.
4. Click the right work in the list. The plugin loads its movements and
   guesses which movement each file is (from roman/arabic numbers, movement
   names, track numbers, or file order). Correct any guess in the **Tag as**
   column. *Whole work* is for a file containing the complete piece.
5. **Apply tags**, check the result in Picard's metadata panel, then **Save**.

Nothing is written to disk until you save, so you can review the changes
first.

Tips:
- MusicBrainz work titles follow the original language and its conventions:
  search "Symphony 5" or "Ballade 1" with the composer, rather than a full
  title.
- If a search shows both a work and its movements, pick the work; picking a
  single movement tags the file as that movement.
- For operas, search for the act or the number directly when you want
  numbered tracks within an act.

## Automatic retagging of releases

When files are matched to a MusicBrainz release (via **Scan** or **Lookup**)
and the release's recordings are linked to works, each track is retagged the
same way when the release loads. The original release values are kept in
`release_album`, `release_artist`, `releasedate`, `release_tracknumber`, etc.

This needs **Options → Metadata → "Use track relationships"**.

## Tags written

| Tag | Value | Example |
|---|---|---|
| `album`, `work` | work | Symphony no. 5 in C minor, op. 67 |
| `title`, `movement` | movement (work prefix stripped) | II. Andante con moto |
| `artist`, `albumartist`, `composer` (+ sort names, MBIDs) | composer(s) | Ludwig van Beethoven |
| `date` | composition year (completed) | 1808 |
| `compositiondate` | composition years | 1804–1808 |
| `genre`, `worktype` | work type | Symphony |
| `key` | key | C minor |
| `opus` | catalogue number from the work title | op. 67 |
| `work_catalogue` | catalogue series | Beethoven: Opus numbers Op. 67 |
| `tracknumber` / `totaltracks` | movement number / count | 2 / 4 |
| `movementnumber` / `movementtotal` / `showmovement` | same, for players that use them | 2 / 4 / 1 |
| `musicbrainz_workid`, `musicbrainz_parentworkid` | work MBIDs | |

Values MusicBrainz doesn't have (often the composition date or key) are left
as they were.

## Requirements and install

- Picard **3.0.3 or newer**.
- Install: Options → Plugins → Install Plugin… → local tab → path to this
  folder (it is a git repository, as Picard 3 requires).

## Settings

Edit the `SETTINGS` dict in `works.py`, then `git commit -am "settings"` in
this folder and update the plugin in Picard (Picard installs the committed
version, not the files on disk):

- `album_level`: `"top"` (the opera) or `"parent"` (the act).
- `date_from`: `"end"` (year completed) or `"begin"` (year started).
- `title_locale`: e.g. `"fr"` to use French work titles where MusicBrainz has
  them.
- `set_artist`, `set_date`, `set_genre`, `set_key`, `title_from_work`,
  `set_track_numbers`, `keep_release_info`: turn individual changes off.

## Troubleshooting

Help → View Error/Debug Log; the plugin's lines mention "Classical Works".

## Files

- `works.py`: settings, MusicBrainz parsing and lookups, tag building.
- `action.py`, `dialog.py`: the right-click action and its window.
- `release.py`: automatic retagging when a release loads.
