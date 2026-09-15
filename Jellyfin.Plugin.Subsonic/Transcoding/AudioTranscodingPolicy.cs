using Jellyfin.Plugin.Subsonic.Configuration;

namespace Jellyfin.Plugin.Subsonic.Transcoding;

internal record AudioOutputFormat(string Container, string Codec, string ContentType);

internal record AudioTranscodingPlan(AudioOutputFormat Format, int SampleRate, int? BitRate);

internal static class AudioTranscodingPolicy
{
    internal static AudioOutputFormat? FindFormat(string? format) => format?.Trim().ToLowerInvariant() switch
    {
        "mp3" => new("mp3", "mp3", "audio/mpeg"),
        "aac" => new("aac", "aac", "audio/aac"),
        "flac" => new("flac", "flac", "audio/flac"),
        "ogg" => new("ogg", "vorbis", "audio/ogg"),
        "opus" => new("webm", "opus", "audio/webm"),
        _ => null,
    };

    internal static AudioOutputFormat? AutomaticFormat(string? codec, PluginConfiguration? config)
    {
        if (config?.AutomaticTranscodingEnabled != true) return null;
        var supported = (config.SupportedAudioCodecs ?? "").Split(
            new[] { ',', ';', ' ', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (codec != null && supported.Contains(codec.Trim(), StringComparer.OrdinalIgnoreCase)) return null;
        return FindFormat(config.AutomaticTranscodingFormat)
            ?? throw new InvalidOperationException("Automatic transcoding target must be mp3, aac, flac, ogg, or opus.");
    }

    internal static AudioTranscodingPlan? ForStream(
        string? codec, string? requestedFormat, int maxBitRate, int timeOffset, PluginConfiguration config)
    {
        if (string.Equals(requestedFormat, "raw", StringComparison.OrdinalIgnoreCase)) return null;
        var format = requestedFormat == null
            ? AutomaticFormat(codec, config)
            : FindFormat(requestedFormat) ?? throw new ArgumentException("Unsupported output format.", nameof(requestedFormat));
        if (format == null && maxBitRate <= 0 && timeOffset <= 0) return null;
        format ??= FindFormat("mp3")!;
        if (config.TranscodingSampleRate is not (8000 or 11025 or 12000 or 16000 or 22050 or 24000 or 32000 or 44100 or 48000))
            throw new InvalidOperationException("Invalid transcoding sample rate.");
        if (config.TranscodingBitRate is < 8 or > 320)
            throw new InvalidOperationException("Transcoding bitrate must be between 8 and 320 kbps.");
        var sampleRate = format.Codec == "opus" ? 48000 : config.TranscodingSampleRate;
        var bitRate = maxBitRate > 0 ? Math.Min(maxBitRate, config.TranscodingBitRate) : config.TranscodingBitRate;
        return new(format, sampleRate, format.Codec == "flac" ? null : bitRate);
    }
}
