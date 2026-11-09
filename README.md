# Classical Works — Picard 3 plugin

Tags classical tracks around the **MusicBrainz work** instead of the release:

| Tag | Becomes | Example |
|---|---|---|
| `album`, `work` | top-level work | Symphony no. 5 in C minor, op. 67 |
| `title`, `movement` | movement, work prefix stripped | II. Andante con moto |
| `artist`, `albumartist` (+ sort, `artists`, MBIDs) | composer(s) | Ludwig van Beethoven |
| `date` | composition year (completed) | 1808 |
| `compositiondate` | full composition range | 1804–1808 |
| `genre`, `worktype` | MusicBrainz work type | Symphony |
| `key` | work "Key" attribute | C minor |
| `opus` | catalogue number found in the work title | op. 67 |
| `work_catalogue` | catalogue series the work belongs to | Beethoven: Opus numbers Op. 67 |
| `tracknumber` / `totaltracks` | movement number / count | 2 / 4 |
| `movementnumber` / `movementtotal` / `showmovement` | same, for players that use them | 2 / 4 / 1 |
| `musicbrainz_parentworkid` | MBID of the album-level work | |

Before overwriting, the release values are copied to `release_album`,
`release_artist`, `release_albumartist`, `releasedate`, `release_tracknumber`,
`release_discnumber`, `release_title`, `release_genre`, etc. Performer,
conductor and orchestra tags written by Picard are left alone.

Tracks whose recording is not linked to a work in MusicBrainz are not changed.

## Requirements

- Picard **3.0.3 or newer** (earlier 3.0.x releases could hang an album forever
  when a plugin used a blocking album task — PICARD-3513).
- **Options → Metadata → "Use track relationships"** must be on, otherwise the
  release data contains no work links (the plugin logs a warning).

## Install

Picard 3 installs plugins from a git repository (a local one is fine):

```bash
cd classical_works
git init && git add . && git commit -m "Classical Works 0.1.0"
picard-cli plugins install "$(pwd)" --yes
```

Then restart Picard and enable it under Options → Plugins.

## Settings

There is no options page yet: edit the `SETTINGS` dict at the top of
`__init__.py`, commit, and run `picard-cli plugins update classical_works`
(or reinstall). The main ones:

- `album_level`: `"top"` (the opera) or `"parent"` (the act).
- `date_from`: `"end"` (year completed) or `"begin"` (year started).
- `title_locale`: e.g. `"fr"` to use French work titles when MusicBrainz has a
  French alias (costs one extra request per track).
- `set_artist`, `set_date`, `set_genre`, `set_key`, `title_from_work`,
  `set_track_numbers`, `keep_release_info`: turn individual changes off.

## Things to know

- **Speed.** Picard limits MusicBrainz to about one request per second. The
  plugin needs one request per distinct parent work (four movements of a
  symphony = one request) or one per track for standalone pieces. Results are
  cached until Picard restarts.
- **Coverage depends on MusicBrainz data.** Composition dates live on the
  composer relationship and are often missing; keys are often missing on
  less-catalogued works. Missing fields are simply left as Picard set them.
- **File naming.** If your naming script uses `%album%`/`%albumartist%`, files
  will now be organised by composer/work. Use `%release_album%` etc. to keep
  release-based folders.
- **`musicbrainz_albumid` is not changed** (Picard needs it to reload the
  release). Players that group albums by that ID rather than by the album tag
  will still show the whole release as one album.
- **Multi-level works** (opera → act → number): `album` is the opera, `title`
  keeps the act prefix ("acte I: no. 2 …"), and the release track numbering is
  kept since there is no single movement number.
- Recordings linked to several works (medleys, pot-pourris) use the first work.
