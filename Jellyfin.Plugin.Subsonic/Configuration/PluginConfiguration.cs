using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Subsonic.Configuration;

/// <summary>Plugin configuration (stored in Jellyfin config directory).</summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// AES-256-GCM key salt (base64). Auto-generated on first run if empty.
    /// Never expose this in the UI or logs.
    /// </summary>
    public string Salt { get; set; } = string.Empty;

    /// <summary>Last.fm API key for getArtistInfo/getAlbumInfo. Optional — leave empty to disable.</summary>
    public string LastFmApiKey { get; set; } = string.Empty;

    /// <summary>Log all incoming /rest/* requests at Debug level.</summary>
    public bool LogRestRequests { get; set; } = false;

    /// <summary>Enable the sharing feature (createShare, getShares, share pages). Disable to prevent users from creating or accessing shares.</summary>
    public bool SharingEnabled { get; set; } = true;

    public bool HideArtwork { get; set; } = false;

    public bool AutomaticTranscodingEnabled { get; set; } = false;

    public string SupportedAudioCodecs { get; set; } = "aac,mp3,flac,alac,vorbis,opus,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le";

    public string AutomaticTranscodingFormat { get; set; } = "mp3";

    public int TranscodingBitRate { get; set; } = 192;

    public int TranscodingSampleRate { get; set; } = 48000;

    public bool TranscodeDownloads { get; set; } = false;

    /// <summary>
    /// Public URL used to access Subfin through a reverse proxy.
    /// Example: https://example.com:4433/jellyfin
    /// Leave empty to auto-detect from Jellyfin/request information.
    /// </summary>
    public string ExternalBaseUrl { get; set; } = string.Empty;

    /// <summary>
    /// CORS origins allowed for /rest/* and /subfin/* (comma-separated).
    /// Leave empty to allow all origins (default for local dev).
    /// </summary>
    public string CorsOrigins { get; set; } = string.Empty;
}
