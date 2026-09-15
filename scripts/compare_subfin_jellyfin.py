#!/usr/bin/env python3
"""
Compare a Jellyfin music catalog with the same catalog exposed through Subfin.

Outputs:
  jellyfin_tracks.csv
  subfin_search_tracks.csv
  subfin_album_tracks.csv
  subfin_albums.csv
  missing_from_subfin_search.csv
  missing_from_subfin_album_traversal.csv
  search_only_not_album_traversal.csv
  missing_analysis.csv
  album_diagnostics.csv
  possible_signature_matches.csv
  summary.json

Only Python's standard library is required.

Typical usage:

  export JELLYFIN_URL='https://example.com:4433/prefix'
  export JELLYFIN_TOKEN='...'
  export SUBFIN_USER='username'
  export SUBFIN_PASSWORD='...'

  python3 compare_subfin_jellyfin.py --out-dir catalog-diff

If Subfin token auth is unavailable for the linked account:
  python3 compare_subfin_jellyfin.py --subfin-auth enc --out-dir catalog-diff

Note: --subfin-auth enc sends p=enc:<hex>. This is only obfuscation, not
cryptographic encryption; prefer token auth over HTTPS.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLIENT_NAME = "SubfinCatalogDiff"
SUBSONIC_VERSION = "1.16.1"


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def norm_id(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().lower()
    for prefix in ("ar-", "al-", "pl-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.replace("-", "")


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def text_join(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    return str(value)


def signature(row: dict[str, Any]) -> str:
    return "\x1f".join([
        str(row.get("artist", "")).strip().casefold(),
        str(row.get("album", "")).strip().casefold(),
        str(row.get("title", "")).strip().casefold(),
        str(row.get("disc", "")).strip(),
        str(row.get("track", "")).strip(),
    ])


@dataclass
class HttpClient:
    timeout: int = 60
    retries: int = 3
    insecure: bool = False

    def __post_init__(self) -> None:
        self.ssl_context = ssl._create_unverified_context() if self.insecure else ssl.create_default_context()

    def get_bytes(self, url: str, params: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> bytes:
        if params:
            query = urllib.parse.urlencode(
                [(k, str(v)) for k, v in params.items() if v is not None], doseq=True
            )
            url += ("&" if "?" in url else "?") + query

        req_headers = {"User-Agent": f"{CLIENT_NAME}/1.0"}
        if headers:
            req_headers.update(headers)

        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(url, headers=req_headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                    return resp.read()
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                    raise
                last_exc = exc
                if attempt >= self.retries:
                    break
                delay = min(2 ** (attempt - 1), 5)
                eprint(f"[retry {attempt}/{self.retries}] {type(exc).__name__}: {exc}; waiting {delay}s")
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        return json.loads(self.get_bytes(url, params, headers).decode("utf-8"))

    def get_xml(self, url: str, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> ET.Element:
        root = ET.fromstring(self.get_bytes(url, params, headers))
        if root.attrib.get("status") == "failed":
            err = next((x for x in root.iter() if localname(x.tag) == "error"), None)
            if err is not None:
                raise RuntimeError(
                    f"Subfin error {err.attrib.get('code', '?')}: {err.attrib.get('message', 'unknown error')}"
                )
            raise RuntimeError("Subfin returned status=failed")
        return root


class JellyfinClient:
    def __init__(self, base_url: str, token: str, http: HttpClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.headers = {"X-Emby-Token": token}

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def get_me(self) -> dict[str, Any]:
        return self.http.get_json(self.url("Users/Me"), headers=self.headers)

    def resolve_user(self, user_id: str, username: str) -> dict[str, Any]:
        if user_id:
            return self.http.get_json(self.url(f"Users/{user_id}"), headers=self.headers)
        try:
            return self.get_me()
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
        users = self.http.get_json(self.url("Users"), headers=self.headers)
        matches = [user for user in users if user.get("Name", "").casefold() == username.casefold()]
        if len(matches) != 1:
            raise RuntimeError("API key has no user. Set --jellyfin-user-id to the Jellyfin user linked to Subfin.")
        eprint("[Jellyfin] API key has no user; selected matching username. Use --jellyfin-user-id to override.")
        return matches[0]

    def get_views(self, user_id: str) -> list[dict[str, Any]]:
        obj = self.http.get_json(self.url(f"Users/{user_id}/Views"), headers=self.headers)
        return list(obj.get("Items", []))

    def export_audio_under_parent(self, user_id: str, parent_id: str | None,
                                  library_name: str, page_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicate_ids: list[str] = []
        start = 0
        total_expected: int | None = None

        while True:
            params: dict[str, Any] = {
                "UserId": user_id,
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "StartIndex": start,
                "Limit": page_size,
                "EnableTotalRecordCount": "true",
                "Fields": "Path,ParentId,ProviderIds,MediaSources,SortName",
            }
            if parent_id:
                params["ParentId"] = parent_id

            obj = self.http.get_json(self.url("Items"), params=params, headers=self.headers)
            items = list(obj.get("Items", []))
            if total_expected is None:
                total_expected = as_int(obj.get("TotalRecordCount"), len(items))

            for item in items:
                rid = norm_id(item.get("Id"))
                if rid in seen:
                    duplicate_ids.append(rid)
                seen.add(rid)
                artist_items = item.get("ArtistItems") or []
                artist_ids = [
                    norm_id(x.get("Id")) for x in artist_items
                    if isinstance(x, dict) and x.get("Id")
                ]
                rows.append({
                    "id": rid,
                    "title": item.get("Name", ""),
                    "album": item.get("Album", ""),
                    "album_id": norm_id(item.get("AlbumId")),
                    "parent_id": norm_id(item.get("ParentId")),
                    "artist": text_join(item.get("Artists")),
                    "artist_ids": ";".join(artist_ids),
                    "album_artist": item.get("AlbumArtist", ""),
                    "path": item.get("Path", ""),
                    "container": item.get("Container", ""),
                    "disc": item.get("ParentIndexNumber", ""),
                    "track": item.get("IndexNumber", ""),
                    "year": item.get("ProductionYear", ""),
                    "library": library_name,
                })

            start += len(items)
            if not items or (total_expected is not None and start >= total_expected) or len(items) < page_size:
                break

        return rows, {
            "library": library_name,
            "parent_id": norm_id(parent_id),
            "total_expected": total_expected,
            "rows_received": len(rows),
            "unique_ids": len(seen),
            "duplicate_ids": duplicate_ids,
        }


class SubfinClient:
    def __init__(self, base_url: str, username: str, password: str | None,
                 api_key: str | None, auth_mode: str, http: HttpClient) -> None:
        base_url = base_url.rstrip("/")
        self.rest_url = base_url if base_url.endswith("/rest") else base_url + "/rest"
        self.username = username
        self.password = password
        self.api_key = api_key
        self.auth_mode = auth_mode
        self.http = http
        if auth_mode == "apikey" and not api_key:
            raise ValueError("--subfin-auth apikey requires --subfin-api-key/SUBFIN_API_KEY")
        if auth_mode in {"token", "enc"} and not password:
            raise ValueError(f"--subfin-auth {auth_mode} requires --subfin-password/SUBFIN_PASSWORD")

    def auth_params(self) -> dict[str, str]:
        base = {"u": self.username, "v": SUBSONIC_VERSION, "c": CLIENT_NAME, "f": "xml"}
        if self.auth_mode == "apikey":
            base["apiKey"] = self.api_key or ""
        elif self.auth_mode == "enc":
            base["p"] = "enc:" + (self.password or "").encode("utf-8").hex()
        else:
            salt = secrets.token_hex(8)
            base["s"] = salt
            base["t"] = hashlib.md5(((self.password or "") + salt).encode("utf-8")).hexdigest()
        return base

    def call(self, method: str, params: dict[str, Any] | None = None) -> ET.Element:
        all_params: dict[str, Any] = self.auth_params()
        if params:
            all_params.update(params)
        return self.http.get_xml(f"{self.rest_url}/{method}.view", params=all_params)

    def get_music_folders(self) -> list[dict[str, str]]:
        root = self.call("getMusicFolders")
        return [
            {"id": e.attrib.get("id", ""), "name": e.attrib.get("name", "")}
            for e in root.iter() if localname(e.tag) == "musicFolder"
        ]

    def get_artists_album_count(self) -> tuple[int, int]:
        root = self.call("getArtists")
        counts = [as_int(e.attrib.get("albumCount"), 0)
                  for e in root.iter() if localname(e.tag) == "artist"]
        return sum(counts), len(counts)

    @staticmethod
    def song_from_elem(elem: ET.Element, source: str) -> dict[str, Any]:
        a = elem.attrib
        return {
            "id": norm_id(a.get("id")),
            "title": a.get("title", a.get("name", "")),
            "album": a.get("album", ""),
            "album_id": norm_id(a.get("albumId", a.get("parent", ""))),
            "parent_id": norm_id(a.get("parent", "")),
            "artist": a.get("artist", ""),
            "artist_id": norm_id(a.get("artistId", "")),
            "path": a.get("path", ""),
            "suffix": a.get("suffix", ""),
            "disc": a.get("discNumber", ""),
            "track": a.get("track", ""),
            "year": a.get("year", ""),
            "source": source,
        }

    @staticmethod
    def album_from_elem(elem: ET.Element, offset: int) -> dict[str, Any]:
        a = elem.attrib
        return {
            "id": norm_id(a.get("id")),
            "name": a.get("name", a.get("album", a.get("title", ""))),
            "artist": a.get("artist", ""),
            "artist_id": norm_id(a.get("artistId", "")),
            "song_count_declared": as_int(a.get("songCount"), 0),
            "duration": as_int(a.get("duration"), 0),
            "year": a.get("year", ""),
            "genre": a.get("genre", ""),
            "page_offset": offset,
        }

    def export_search3_songs(self, page_size: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_no = 0
        page_signatures: set[tuple[str, ...]] = set()
        while True:
            root = self.call("search3", {
                "query": "", "artistCount": 0, "albumCount": 0,
                "songCount": page_size, "songOffset": offset,
            })
            page: list[dict[str, Any]] = []
            for parent in root.iter():
                if localname(parent.tag) == "searchResult3":
                    page = [self.song_from_elem(c, "search3") for c in list(parent)
                            if localname(c.tag) == "song"]
                    break
            sig = tuple(x["id"] for x in page)
            if sig and sig in page_signatures:
                raise RuntimeError(f"search3 repeated a page at offset={offset}; pagination is unstable")
            if sig:
                page_signatures.add(sig)
            rows.extend(page)
            eprint(f"[Subfin search3] page={page_no} offset={offset} received={len(page)}")
            page_no += 1
            if len(page) < page_size:
                break
            offset += page_size
        ids = [x["id"] for x in rows if x["id"]]
        return rows, {
            "rows_received": len(rows), "unique_ids": len(set(ids)),
            "duplicate_ids": [k for k, v in Counter(ids).items() if v > 1],
            "pages": page_no,
        }

    def export_albums(self, page_size: int = 500) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_ids: dict[int, list[str]] = {}
        page_signatures: set[tuple[str, ...]] = set()
        while True:
            root = self.call("getAlbumList2", {
                "type": "alphabeticalByName", "size": page_size, "offset": offset,
            })
            page: list[dict[str, Any]] = []
            for parent in root.iter():
                if localname(parent.tag) == "albumList2":
                    page = [self.album_from_elem(c, offset) for c in list(parent)
                            if localname(c.tag) == "album"]
                    break
            sig = tuple(x["id"] for x in page)
            if sig and sig in page_signatures:
                raise RuntimeError(f"getAlbumList2 repeated a page at offset={offset}; pagination is unstable")
            if sig:
                page_signatures.add(sig)
            page_ids[offset] = [x["id"] for x in page]
            rows.extend(page)
            eprint(f"[Subfin albums] offset={offset} received={len(page)}")
            if len(page) < page_size:
                break
            offset += page_size
        return rows, page_ids

    def get_album_songs(self, album: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
        album_id = album["id"]
        try:
            root = self.call("getAlbum", {"id": album_id})
            songs: list[dict[str, Any]] = []
            for parent in root.iter():
                if localname(parent.tag) == "album":
                    songs = [self.song_from_elem(c, "getAlbum") for c in list(parent)
                             if localname(c.tag) == "song"]
                    break
            return album_id, songs, None
        except (urllib.error.URLError, TimeoutError, RuntimeError, ET.ParseError) as exc:
            return album_id, [], f"{type(exc).__name__}: {exc}"


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    seen: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        rid = norm_id(row.get("id"))
        if not rid:
            continue
        if rid in seen:
            duplicates.append(rid)
            if row.get("library") and seen[rid].get("library") != row.get("library"):
                libs = {x.strip() for x in (str(seen[rid].get("library", "")).split(";") +
                                             str(row.get("library", "")).split(";")) if x.strip()}
                seen[rid]["library"] = ";".join(sorted(libs))
            continue
        copy = dict(row)
        copy["id"] = rid
        seen[rid] = copy
    return list(seen.values()), duplicates


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys or ["id"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export and compare Jellyfin-native and Subfin music catalogs.")
    p.add_argument("--jellyfin-url", default=os.environ.get("JELLYFIN_URL", ""))
    p.add_argument("--jellyfin-token", default=os.environ.get("JELLYFIN_TOKEN", ""))
    p.add_argument("--jellyfin-user-id", default=os.environ.get("JELLYFIN_USER_ID", ""),
                   help="Jellyfin user linked to Subfin; API keys otherwise resolve by Subfin username")
    p.add_argument("--subfin-url", default=os.environ.get("SUBFIN_URL", ""),
                   help="Defaults to --jellyfin-url; may already end in /rest")
    p.add_argument("--subfin-user", default=os.environ.get("SUBFIN_USER", ""))
    p.add_argument("--subfin-password", default=os.environ.get("SUBFIN_PASSWORD", ""))
    p.add_argument("--subfin-api-key", default=os.environ.get("SUBFIN_API_KEY", ""))
    p.add_argument("--subfin-auth", choices=("token", "enc", "apikey"),
                   default=os.environ.get("SUBFIN_AUTH", "token"))
    p.add_argument("--out-dir", default="catalog-diff")
    p.add_argument("--page-size", type=int, default=500)
    p.add_argument("--album-page-limit", type=int, default=0,
                   help="Optional client album-page limit to simulate; 0 checks all pages")
    p.add_argument("--native-page-size", type=int, default=5000)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--all-native-audio", action="store_true")
    p.add_argument("--skip-search3", action="store_true")
    p.add_argument("--skip-album-details", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--timeout", type=int, default=60)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.jellyfin_url:
        raise SystemExit("Missing --jellyfin-url or JELLYFIN_URL")
    if not args.jellyfin_token:
        raise SystemExit("Missing --jellyfin-token or JELLYFIN_TOKEN")
    if not args.subfin_user:
        raise SystemExit("Missing --subfin-user or SUBFIN_USER")
    if args.album_page_limit < 0:
        raise SystemExit("album-page-limit must be >= 0")
    if args.page_size <= 0 or args.native_page_size <= 0 or args.workers <= 0:
        raise SystemExit("page sizes and workers must be > 0")

    subfin_url = args.subfin_url or args.jellyfin_url
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    http = HttpClient(timeout=args.timeout, retries=3, insecure=args.insecure)
    jf = JellyfinClient(args.jellyfin_url, args.jellyfin_token, http)
    sf = SubfinClient(subfin_url, args.subfin_user, args.subfin_password or None,
                      args.subfin_api_key or None, args.subfin_auth, http)

    summary: dict[str, Any] = {
        "jellyfin_url": args.jellyfin_url,
        "subfin_url": subfin_url,
        "subfin_auth": args.subfin_auth,
        "page_size": args.page_size,
        "native_page_size": args.native_page_size,
        "workers": args.workers,
    }

    me = jf.resolve_user(args.jellyfin_user_id, args.subfin_user)
    user_id = str(me.get("Id", ""))
    if not user_id:
        raise RuntimeError("/Users/Me did not return a user Id")
    summary["jellyfin_user"] = {"id": norm_id(user_id), "name": me.get("Name", "")}

    native_views = jf.get_views(user_id)
    music_views = [v for v in native_views if str(v.get("CollectionType", "")).casefold() == "music"]
    subfin_folders = sf.get_music_folders()
    summary["jellyfin_music_views"] = [
        {"id": norm_id(v.get("Id")), "name": v.get("Name", "")} for v in music_views
    ]
    summary["subfin_music_folders"] = subfin_folders

    selected_views = music_views
    if not args.all_native_audio and subfin_folders and music_views:
        wanted = {x["name"].casefold() for x in subfin_folders if x.get("name")}
        matched = [v for v in music_views if str(v.get("Name", "")).casefold() in wanted]
        if matched:
            selected_views = matched
        else:
            eprint("[warn] Could not match Subfin music folders to Jellyfin views; using all music views")

    native_raw: list[dict[str, Any]] = []
    native_page_meta: list[dict[str, Any]] = []
    if args.all_native_audio or not selected_views:
        rows, meta = jf.export_audio_under_parent(user_id, None, "(all accessible audio)", args.native_page_size)
        native_raw.extend(rows)
        native_page_meta.append(meta)
    else:
        for view in selected_views:
            name, vid = str(view.get("Name", "")), str(view.get("Id", ""))
            eprint(f"[Jellyfin] exporting music view: {name} ({vid})")
            rows, meta = jf.export_audio_under_parent(user_id, vid, name, args.native_page_size)
            native_raw.extend(rows)
            native_page_meta.append(meta)

    native_rows, native_dups = dedupe_rows(native_raw)
    native_by_id = {r["id"]: r for r in native_rows}
    write_csv(out_dir / "jellyfin_tracks.csv", sorted(native_rows, key=lambda r: r["id"]))
    summary["jellyfin"] = {
        "rows_raw": len(native_raw), "unique_tracks": len(native_rows),
        "duplicate_ids_after_view_merge": sorted(set(native_dups)), "pages": native_page_meta,
    }
    eprint(f"[Jellyfin] unique tracks: {len(native_rows)}")

    search_rows: list[dict[str, Any]] = []
    search_meta: dict[str, Any] = {"skipped": True}
    if not args.skip_search3:
        search_raw, search_meta = sf.export_search3_songs(args.page_size)
        search_rows, search_dups = dedupe_rows(search_raw)
        search_meta["dedupe_duplicate_ids"] = sorted(set(search_dups))
        write_csv(out_dir / "subfin_search_tracks.csv", sorted(search_rows, key=lambda r: r["id"]))
        eprint(f"[Subfin search3] unique tracks: {len(search_rows)}")
    search_by_id = {r["id"]: r for r in search_rows}
    summary["subfin_search3"] = search_meta

    artist_album_count, artist_count = sf.get_artists_album_count()
    albums_raw, album_pages = sf.export_albums(page_size=min(args.page_size, 500))
    albums, album_dups = dedupe_rows(albums_raw)
    album_by_id = {r["id"]: r for r in albums}
    write_csv(out_dir / "subfin_albums.csv", sorted(albums, key=lambda r: r["id"]))

    client_offsets = sorted(album_pages)
    if args.album_page_limit:
        client_offsets = client_offsets[:args.album_page_limit]
    client_album_ids: set[str] = set()
    for off in client_offsets:
        client_album_ids.update(album_pages.get(off, []))
    exhaustive_album_ids = set(album_by_id)
    missed_budget = exhaustive_album_ids - client_album_ids
    summary["subfin_albums"] = {
        "artist_count_from_getArtists": artist_count,
        "sum_artist_albumCount": artist_album_count,
        "exhaustive_album_rows": len(albums_raw),
        "exhaustive_unique_albums": len(albums),
        "duplicate_album_ids": sorted(set(album_dups)),
        "client_album_page_limit": args.album_page_limit,
        "client_requested_offsets": client_offsets,
        "client_album_ids_from_those_pages": len(client_album_ids),
        "albums_missed_by_client_page_budget": len(missed_budget),
        "albums_missed_by_client_page_budget_ids": sorted(missed_budget),
    }

    album_tracks_rows: list[dict[str, Any]] = []
    album_errors: dict[str, str] = {}
    album_song_ids: dict[str, list[str]] = {}
    if not args.skip_album_details:
        eprint(f"[Subfin getAlbum] fetching {len(albums)} albums with {args.workers} workers")
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(sf.get_album_songs, album): album["id"] for album in albums}
            for fut in concurrent.futures.as_completed(futures):
                album_id, songs, error = fut.result()
                completed += 1
                if error:
                    album_errors[album_id] = error
                album_song_ids[album_id] = [s["id"] for s in songs if s["id"]]
                album_tracks_rows.extend(songs)
                if completed % 100 == 0 or completed == len(albums):
                    eprint(f"[Subfin getAlbum] {completed}/{len(albums)}")
        album_tracks, album_track_dups = dedupe_rows(album_tracks_rows)
        write_csv(out_dir / "subfin_album_tracks.csv", sorted(album_tracks, key=lambda r: r["id"]))
    else:
        album_tracks, album_track_dups = [], []
    album_track_by_id = {r["id"]: r for r in album_tracks}
    summary["subfin_getAlbum"] = {
        "skipped": args.skip_album_details, "rows_raw": len(album_tracks_rows),
        "unique_tracks": len(album_tracks), "duplicate_track_ids": sorted(set(album_track_dups)),
        "album_errors": album_errors,
    }

    native_ids, search_ids, album_track_ids = set(native_by_id), set(search_by_id), set(album_track_by_id)

    if not args.skip_search3:
        missing_search_ids = native_ids - search_ids
        extra_search_ids = search_ids - native_ids
        write_csv(out_dir / "missing_from_subfin_search.csv", [native_by_id[x] for x in sorted(missing_search_ids)])
        write_csv(out_dir / "extra_in_subfin_search.csv", [search_by_id[x] for x in sorted(extra_search_ids)])
    else:
        missing_search_ids = extra_search_ids = set()

    if not args.skip_album_details:
        missing_album_ids = native_ids - album_track_ids
        extra_album_track_ids = album_track_ids - native_ids
        search_only_ids = search_ids - album_track_ids if not args.skip_search3 else set()
        write_csv(out_dir / "missing_from_subfin_album_traversal.csv", [native_by_id[x] for x in sorted(missing_album_ids)])
        write_csv(out_dir / "extra_in_subfin_album_traversal.csv", [album_track_by_id[x] for x in sorted(extra_album_track_ids)])
        write_csv(out_dir / "search_only_not_album_traversal.csv", [search_by_id[x] for x in sorted(search_only_ids)])
    else:
        missing_album_ids = extra_album_track_ids = search_only_ids = set()

    native_by_album_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    native_by_parent_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in native_rows:
        if row.get("album_id"):
            native_by_album_id[norm_id(row["album_id"])].append(row)
        if row.get("parent_id"):
            native_by_parent_id[norm_id(row["parent_id"])].append(row)

    album_diag: list[dict[str, Any]] = []
    if not args.skip_album_details:
        for album_id, album in album_by_id.items():
            via_album = native_by_album_id.get(album_id, [])
            via_parent = native_by_parent_id.get(album_id, [])
            sf_ids = set(album_song_ids.get(album_id, []))
            nested = [x for x in via_album if norm_id(x.get("parent_id")) and norm_id(x.get("parent_id")) != album_id]
            album_diag.append({
                "album_id": album_id,
                "album_name": album.get("name", ""),
                "artist": album.get("artist", ""),
                "subfin_declared_song_count": album.get("song_count_declared", 0),
                "subfin_getAlbum_song_count": len(sf_ids),
                "jellyfin_AlbumId_song_count": len(via_album),
                "jellyfin_direct_ParentId_song_count": len(via_parent),
                "jellyfin_nested_under_album_count": len(nested),
                "album_in_client_page_budget": album_id in client_album_ids,
                "getAlbum_error": album_errors.get(album_id, ""),
            })
        write_csv(out_dir / "album_diagnostics.csv", album_diag)

    missing_analysis: list[dict[str, Any]] = []
    if not args.skip_album_details:
        for tid in sorted(native_ids - album_track_ids):
            n = native_by_id[tid]
            album_id, parent_id = norm_id(n.get("album_id")), norm_id(n.get("parent_id"))
            in_search = tid in search_ids if not args.skip_search3 else False
            album_listed = album_id in album_by_id if album_id else False
            album_in_client = album_id in client_album_ids if album_id else False
            if not args.skip_search3 and not in_search:
                reason = "missing from Subfin search3 too: flat Subfin/Jellyfin query or library scoping"
            elif album_id and not album_listed:
                reason = "album absent from exhaustive getAlbumList2"
            elif album_id and album_listed and parent_id and parent_id != album_id:
                reason = "likely getAlbum non-recursive issue: track ParentId differs from AlbumId"
            elif album_id and album_listed and not album_in_client:
                reason = "album exists but falls outside Client's album page-count budget"
            elif album_id and album_listed:
                reason = "album is listed but getAlbum omitted this track"
            else:
                reason = "track has no usable AlbumId / not reachable through album traversal"
            missing_analysis.append({
                **n,
                "in_subfin_search3": in_search,
                "album_listed_by_subfin": album_listed,
                "album_in_client_page_budget": album_in_client,
                "parent_equals_album": bool(album_id and parent_id == album_id),
                "likely_reason": reason,
            })
        write_csv(out_dir / "missing_analysis.csv", missing_analysis)

    possible_matches: list[dict[str, Any]] = []
    if not args.skip_search3:
        sf_by_sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in search_rows:
            sf_by_sig[signature(row)].append(row)
        for tid in sorted(native_ids - search_ids):
            n = native_by_id[tid]
            for s in sf_by_sig.get(signature(n), []):
                possible_matches.append({
                    "jellyfin_id": tid, "subfin_id": s["id"],
                    "title": n.get("title", ""), "album": n.get("album", ""),
                    "artist": n.get("artist", ""), "disc": n.get("disc", ""),
                    "track": n.get("track", ""), "jellyfin_path": n.get("path", ""),
                    "subfin_path": s.get("path", ""),
                })
        write_csv(out_dir / "possible_signature_matches.csv", possible_matches)

    reason_counts = Counter(x["likely_reason"] for x in missing_analysis)
    summary["comparison"] = {
        "jellyfin_unique_tracks": len(native_ids),
        "subfin_search3_unique_tracks": None if args.skip_search3 else len(search_ids),
        "subfin_getAlbum_unique_tracks": None if args.skip_album_details else len(album_track_ids),
        "missing_from_subfin_search3": None if args.skip_search3 else len(native_ids - search_ids),
        "extra_in_subfin_search3": None if args.skip_search3 else len(search_ids - native_ids),
        "missing_from_subfin_album_traversal": None if args.skip_album_details else len(native_ids - album_track_ids),
        "search3_present_but_album_traversal_missing": (
            None if args.skip_search3 or args.skip_album_details else len(search_ids - album_track_ids)
        ),
        "missing_reason_counts": dict(reason_counts),
    }
    write_json(out_dir / "summary.json", summary)

    print("\n=== Catalog comparison ===")
    print(f"Jellyfin native unique tracks:       {len(native_ids)}")
    if not args.skip_search3:
        print(f"Subfin search3 unique tracks:        {len(search_ids)}")
        print(f"Missing from Subfin search3:         {len(native_ids - search_ids)}")
        print(f"Extra in Subfin search3:             {len(search_ids - native_ids)}")
    if not args.skip_album_details:
        print(f"Subfin getAlbum unique tracks:       {len(album_track_ids)}")
        print(f"Missing from album traversal:        {len(native_ids - album_track_ids)}")
        if not args.skip_search3:
            print(f"Search3 present, getAlbum missing:   {len(search_ids - album_track_ids)}")
    print(f"Subfin exhaustive albums:            {len(exhaustive_album_ids)}")
    print(f"Sum getArtists albumCount:           {artist_album_count}")
    print(f"Client page-budget album count:     {len(client_album_ids)}")
    print(f"Albums outside Client page budget:  {len(missed_budget)}")
    if reason_counts:
        print("\nLikely reasons for tracks missing from getAlbum traversal:")
        for reason, count in reason_counts.most_common():
            print(f"  {count:6d}  {reason}")
    print(f"\nDetailed output: {out_dir.resolve()}")
    print("Start with: summary.json, missing_analysis.csv, album_diagnostics.csv")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("\nInterrupted.")
        raise SystemExit(130)
    except Exception as exc:
        eprint(f"\nERROR: {type(exc).__name__}: {exc}")
        raise
