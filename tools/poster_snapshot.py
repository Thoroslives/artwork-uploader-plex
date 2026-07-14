#!/usr/bin/env python3
"""Snapshot and restore Plex artwork selections.

A safety net for allow_artist_updates (and for any bulk run): record which poster/background is
selected on every item, plus its lock state and this tool's artwork-ID labels, so a run that
changes something you didn't want can be reverted item by item. Plex keeps every uploaded poster,
so restoring is a matter of re-selecting the one that was live when the snapshot was taken.

Usage (run inside the container so it shares the app config):
    python3 tools/poster_snapshot.py snapshot [out.json]     # record current state (default: config/poster-snapshot-<ts>.json)
    python3 tools/poster_snapshot.py diff <snapshot.json>    # show what changed since a snapshot (read-only)
    python3 tools/poster_snapshot.py restore <snapshot.json> [--apply]   # re-select snapshot posters (dry-run unless --apply)

snapshot and diff are read-only. restore writes to Plex only with --apply.
"""

import json
import sys
import time

from plexapi.server import PlexServer

from core.config import Config


def _connect():
    config = Config()
    config.load()
    if not config.base_url or not config.token:
        sys.exit("Plex base_url/token not configured - check config/config.json")
    plex = PlexServer(config.base_url, config.token)
    return plex, config


def _selected_key(item, kind):
    """Provider key of the currently selected poster (thumb) or background (art). None if the
       call fails or nothing is selected. This is the stable identifier Plex keeps across runs."""
    getter = item.posters if kind == "thumb" else item.arts
    try:
        for image in getter():
            if image.selected:
                return image.ratingKey
    except Exception as error:
        print(f"  ! could not read {kind} for {item.title}: {error}")
    return None


def _field_locked(item, name):
    return any(field.name == name and field.locked for field in item.fields)


def _tool_labels(item):
    prefixes = ("PID:", "CID:", "SID:", "BID:", "SAID:", "EID:")
    return [str(label) for label in item.labels if str(label).startswith(prefixes)]


def _record(item, kind):
    item.reload()
    return {
        "ratingKey": item.ratingKey,
        "type": item.type,
        "title": item.title,
        "year": getattr(item, "year", None),
        "thumb_key": _selected_key(item, "thumb"),
        "art_key": _selected_key(item, "art"),
        "thumb_locked": _field_locked(item, "thumb"),
        "art_locked": _field_locked(item, "art"),
        "labels": _tool_labels(item),
    }


def _iter_items(plex, config):
    for name in config.movie_library:
        for movie in plex.library.section(name).all():
            yield movie, "movie"
    for name in config.tv_library:
        for show in plex.library.section(name).all():
            yield show, "show"
            for season in show.seasons():
                yield season, "season"
    for name in config.movie_library + config.tv_library:
        try:
            for collection in plex.library.section(name).collections():
                yield collection, "collection"
        except Exception:
            pass


def snapshot(out_path):
    plex, config = _connect()
    records = []
    start = time.time()
    for index, (item, _kind) in enumerate(_iter_items(plex, config), 1):
        records.append(_record(item, _kind))
        if index % 100 == 0:
            print(f"  ...{index} items ({int(time.time() - start)}s)")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"created": int(time.time()), "items": records}, handle, indent=2)
    print(f"Snapshot of {len(records)} items written to {out_path} in {int(time.time() - start)}s")


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return {record["ratingKey"]: record for record in json.load(handle)["items"]}


def diff(path):
    plex, config = _connect()
    saved = _load(path)
    changed = 0
    for item, kind in _iter_items(plex, config):
        before = saved.get(item.ratingKey)
        if not before:
            continue
        now = _record(item, kind)
        if now["thumb_key"] != before["thumb_key"] or now["art_key"] != before["art_key"]:
            changed += 1
            print(f"  changed: {before['title']} ({before.get('year')})")
    print(f"{changed} item(s) differ from the snapshot")


def restore(path, apply):
    plex, config = _connect()
    saved = _load(path)
    restored = 0
    for item, kind in _iter_items(plex, config):
        before = saved.get(item.ratingKey)
        if not before:
            continue
        item.reload()
        # thumb selection is locked by Plex when a poster is selected; art the same via lockArt.
        for saved_key, kind_name, lock, unlock, was_locked in (
            (before["thumb_key"], "thumb", item.lockPoster, item.unlockPoster, before["thumb_locked"]),
            (before["art_key"], "art", item.lockArt, item.unlockArt, before["art_locked"]),
        ):
            if not saved_key or _selected_key(item, kind_name) == saved_key:
                continue
            print(f"  {'restoring' if apply else 'would restore'} {kind_name}: {before['title']} ({before.get('year')})")
            if apply:
                images = item.posters() if kind_name == "thumb" else item.arts()
                for image in images:
                    if image.ratingKey == saved_key:
                        image.select()  # selecting also locks the field
                        break
                if not was_locked:      # snapshot had it unlocked, so undo the auto-lock
                    unlock()
            restored += 1
        if apply and before["labels"]:
            existing = {str(label) for label in item.labels}
            for label in before["labels"]:
                if label not in existing:
                    item.addLabel(label)
    print(f"{restored} field(s) {'restored' if apply else 'would be restored'}"
          + ("" if apply else " (dry run - pass --apply to write)"))


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("snapshot", "diff", "restore"):
        print(__doc__)
        sys.exit(1)
    command = args[0]
    if command == "snapshot":
        out = args[1] if len(args) > 1 else f"config/poster-snapshot-{int(time.time())}.json"
        snapshot(out)
    elif command == "diff":
        diff(args[1])
    elif command == "restore":
        restore(args[1], apply="--apply" in args)


if __name__ == "__main__":
    main()
