# Subfin — Subsonic plugin for Jellyfin

Exposes an [OpenSubsonic](https://opensubsonic.netlify.app/)-compatible REST API directly from Jellyfin. Subsonic and Navidrome clients connect to your existing Jellyfin server — no separate process, no proxy.

## Requirements

- Jellyfin **10.10.x** (built against 10.10.7)
- .NET 8 runtime (included in Jellyfin 10.10.x)

## Installation

1. In Jellyfin, go to **Dashboard → Plugins → Repositories** and add:
   ```
   https://raw.githubusercontent.com/williamkray/subfin-plugin/main/jellyfin-plugin-subfin-manifest.json
   ```
2. Go to **Catalog**, find **Subfin**, and install it.
3. Restart Jellyfin.

## Client setup

Each Subsonic client needs its own app password. This is separate from your Jellyfin password.

1. Open **`http://<your-jellyfin>/subfin/`** in a browser (you must be logged in to Jellyfin).
2. Optionally enter a label (e.g. "DSub on Phone"), then click **Link Device**.
3. Copy the generated **username** and **password** — the password won't be shown again.
4. In your Subsonic client, configure the server:
   - **Server URL:** `http://<your-jellyfin>`
   - **Username / Password:** from step 3

To revoke a client's access, return to the device manager and click **Unlink**.

## Admin features

Admin settings are in **Dashboard → Plugins → Subfin** (the Jellyfin plugin config page).

### Catalog compatibility (10.10.5.5)

This build targets Jellyfin 10.10.7. Album queries include disc subfolders, and
song album IDs reference the containing album rather than a disc folder.
Playlist entries resolve uncached links and preserve order and duplicates.
Structured XML lyrics carry their text inside each line element.

After installing, restart Jellyfin and confirm the plugin version is **10.10.5.6**.
Rebuild an existing client's library if it previously imported disc folders as albums.

### Hide artwork

Enable **Hide artwork** to remove artwork references from XML and JSON responses
and return HTTP 404 from `getCoverArt` and `getAvatar`. Clients can then use their
default icons. This applies to all Subfin users and is disabled by default.
Jellyfin's own artwork is unchanged. Clear existing artwork caches in the client,
restart it, and disable display of embedded audio artwork. The plugin cannot delete
client caches or remove artwork from original audio files.

### Last.fm

Enter a Last.fm API key to enable artist biographies and images in clients that request them (`getArtistInfo`, `getArtistInfo2`). Leave blank to skip that data.

### Sharing

Any user can create shareable links via their Subsonic client (`createShare`). Shares are accessible at `/subfin/share/<uid>` — no Jellyfin login required.

**Managing shares** — the device manager at `/subfin/` shows:
- Your own shares (all users)
- All shares across all users (Jellyfin admins only)

Admins can delete any share from that page. Non-admins can only delete their own.

### Library selection

Each user can choose which Jellyfin music libraries are visible through Subsonic clients. Open the device manager and use the **Library Selection** section. Deselecting all libraries shows everything (no restriction).

### Catalog comparison

`scripts/compare_subfin_jellyfin.py` compares native audio with Subsonic search and
album traversal. Configure `JELLYFIN_URL`, `JELLYFIN_TOKEN`, `SUBFIN_USER`, and
`SUBFIN_PASSWORD` through environment variables, then run:

```sh
python3 scripts/compare_subfin_jellyfin.py --out-dir catalog-diff
```

API keys have no user, so Jellyfin's `/Users/Me` returns HTTP 400 for them. The
script then resolves the exact matching username. Use `--jellyfin-user-id` or
`JELLYFIN_USER_ID` if the linked device username differs from the Jellyfin user.

### Automatic audio transcoding (10.10.5.6)

Enable **Automatically transcode unsupported audio codecs** in the plugin settings.
**Supported audio codecs** is a server-wide, case-insensitive list of Jellyfin codec
names. Use the common set supported by your clients. This is administrator-defined
compatibility, not automatic detection of each client's capabilities. For example,
`dsd_lsbf_planar` is a codec; `dsf` is a container/extension and will not match it.
Unknown codecs and codecs outside the list use the selected target. An empty list
transcodes every codec. The feature is disabled by default.

Targets are MP3, AAC in ADTS, FLAC, Vorbis in Ogg, and Opus in WebM. Default output
is MP3 at 192 kbps and 48 kHz. The bitrate and sample rate are configurable; lower
client bitrate limits take precedence. FLAC ignores bitrate limits and Opus uses
48 kHz. Sample-rate conversion means that FLAC output is not a bit-perfect copy of
a DSD source. Jellyfin performs the conversion and manages its temporary cache;
the original audio files are unchanged.

Clients should select **server-selected format** for playback. An explicit output
format overrides the automatic target, and `format=raw` always returns the original
file (also bypassing bitrate and time-offset transcoding). Supported source codecs
are served directly unless the client requests a format, bitrate limit or offset.
Seeking in converted audio uses `timeOffset` in seconds; byte ranges refer to the
converted output and are delegated to Jellyfin.

**Also apply transcoding to downloads** is an independent, opt-in setting. With it
disabled, the download endpoint retains original-file behavior. With it enabled,
downloads use the same policy and the response filename/MIME describe the converted
format. Explicit `raw` still opts out. Clients that cache original downloads should
clear those files or request a fresh download after changing the policy. Clients
must honor the returned stream format; unsupported source extensions remain visible
as source metadata, while `transcodedSuffix` and `transcodedContentType` describe
the automatic output. Re-sync metadata after changing the target.

Transcoding failures are returned as errors rather than silently sending the
unsupported source. Existing Jellyfin API-key authorization and proxy URL handling
are reused; no media credentials appear in generated transcoding URLs.

Validation on Jellyfin 10.10.7: a `dsd_lsbf_planar` / DSF source was converted to all
five target formats at 48 kHz. Short samples decoded successfully; the MP3 path was
also checked through the plugin's policy and HTTP proxy using the complete song.
A server installation and client playback check remain necessary after upgrading.
