using System.Net;
using System.Net.Http.Headers;
using Jellyfin.Plugin.Subsonic.Auth;
using Jellyfin.Plugin.Subsonic.Configuration;
using Jellyfin.Plugin.Subsonic.Controllers;
using Jellyfin.Plugin.Subsonic.Transcoding;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Xunit;

namespace Jellyfin.Plugin.Subsonic.Tests;

public class AudioTranscodingTests
{
    private static PluginConfiguration Enabled() => new() { AutomaticTranscodingEnabled = true };

    [Theory]
    [InlineData("dsd_lsbf_planar")]
    [InlineData("DSD_MSBF")]
    [InlineData(null)]
    public void UnsupportedOrUnknownCodecUsesConfiguredTarget(string? codec)
    {
        var config = Enabled();
        config.AutomaticTranscodingFormat = "flac";
        var plan = AudioTranscodingPolicy.ForStream(codec, 5_644_800, null, 0, 0, config)!;
        Assert.Equal("flac", plan.Format.Codec);
        Assert.Equal(48000, plan.SampleRate);
        Assert.Null(plan.BitRate);
    }

    [Theory]
    [InlineData("mp3")]
    [InlineData("FLAC")]
    [InlineData("aac")]
    public void SupportedCodecRemainsOriginal(string codec) =>
        Assert.Null(AudioTranscodingPolicy.ForStream(codec, 128_000, null, 0, 0, Enabled()));

    [Fact]
    public void DisabledPolicyLeavesDsdUnchanged() =>
        Assert.Null(AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, null, 0, 0, new PluginConfiguration()));

    [Fact]
    public void CodecListUsesExactNamesAndAcceptsWhitespace()
    {
        var config = Enabled();
        config.SupportedAudioCodecs = " FLAC, DSD_LSBF_PLANAR;\nmp3 ";
        Assert.Null(AudioTranscodingPolicy.AutomaticFormat("dsd_lsbf_planar", config));
        Assert.NotNull(AudioTranscodingPolicy.AutomaticFormat("dsd_lsbf", config));
        config.SupportedAudioCodecs = "dsf";
        Assert.NotNull(AudioTranscodingPolicy.AutomaticFormat("dsd_lsbf_planar", config));
        config.SupportedAudioCodecs = "";
        Assert.NotNull(AudioTranscodingPolicy.AutomaticFormat("mp3", config));
    }

    [Fact]
    public void ExplicitRawOverridesAutomaticPolicyAndBitrateLimit() =>
        Assert.Null(AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, "RAW", 128, 10, Enabled()));

    [Theory]
    [InlineData("mp3", "mp3", "audio/mpeg")]
    [InlineData("aac", "aac", "audio/aac")]
    [InlineData("flac", "flac", "audio/flac")]
    [InlineData("ogg", "ogg", "audio/ogg")]
    [InlineData("opus", "webm", "audio/webm")]
    public void ExplicitFormatOverridesAutomaticTarget(string target, string container, string mime)
    {
        var config = Enabled();
        config.AutomaticTranscodingFormat = "flac";
        var plan = AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, target, 0, 0, config)!;
        Assert.Equal(container, plan.Format.Container);
        Assert.Equal(mime, plan.Format.ContentType);
    }

    [Theory]
    [InlineData(128_000, 320)]
    [InlineData(320_000, 320)]
    public void SupportedCodecAtOrBelowBitrateLimitRemainsOriginal(int sourceBitRate, int maxBitRate)
    {
        var plan = AudioTranscodingPolicy.ForStream("mp3", sourceBitRate, null, maxBitRate, 0, Enabled());
        Assert.Null(plan);
    }

    [Fact]
    public void SupportedCodecAboveBitrateLimitUsesConfiguredTarget()
    {
        var config = Enabled();
        config.AutomaticTranscodingFormat = "aac";
        var plan = AudioTranscodingPolicy.ForStream("mp3", 320_000, null, 128, 0, config)!;
        Assert.Equal("aac", plan.Format.Codec);
        Assert.Equal(128, plan.BitRate);
    }

    [Fact]
    public void BitrateLimitStillAppliesWhenAutomaticCodecPolicyIsDisabled()
    {
        var config = new PluginConfiguration
        {
            AutomaticTranscodingEnabled = false,
            AutomaticTranscodingFormat = "aac",
        };
        var plan = AudioTranscodingPolicy.ForStream("mp3", 320_000, null, 128, 0, config)!;
        Assert.Equal("aac", plan.Format.Codec);
        Assert.Equal(128, plan.BitRate);
    }

    [Fact]
    public void UnknownSourceBitrateWithLimitTranscodesConservatively()
    {
        var plan = AudioTranscodingPolicy.ForStream("mp3", null, null, 128, 0, Enabled());
        Assert.NotNull(plan);
        Assert.Equal(128, plan!.BitRate);
    }

    [Fact]
    public void TimeOffsetStillRequestsTranscoding()
    {
        var plan = AudioTranscodingPolicy.ForStream("mp3", 128_000, null, 0, 20, Enabled());
        Assert.NotNull(plan);
        Assert.Equal("mp3", plan!.Format.Codec);
    }

    [Fact]
    public void UnsupportedCodecWithLooseBitrateLimitUsesConfiguredDefaultBitrate()
    {
        var plan = AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, null, 320, 0, Enabled())!;
        Assert.Equal(192, plan.BitRate);
    }

    [Fact]
    public void InvalidConfigurationAndFormatFailWithoutRawFallback()
    {
        var config = Enabled();
        config.AutomaticTranscodingFormat = "invalid";
        Assert.Throws<InvalidOperationException>(() => AudioTranscodingPolicy.AutomaticFormat("dsd_lsbf_planar", config));
        Assert.Throws<ArgumentException>(() => AudioTranscodingPolicy.ForStream("aac", 128_000, "invalid", 0, 0, config));
        config.AutomaticTranscodingFormat = "mp3";
        config.TranscodingSampleRate = 705600;
        Assert.Throws<InvalidOperationException>(() => AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, null, 0, 0, config));
    }

    [Fact]
    public void TranscodingUrlResamplesDsdAndPreservesSeekWithoutExposingCredentials()
    {
        var config = Enabled();
        var plan = AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, null, 128, 30, config)!;
        var auth = new AuthResult("user", "user-id", "device & 1", null);
        var url = SubsonicController.BuildTranscodingUrl("http://localhost:8096/prefix", Guid.Empty, auth, plan, 30);
        var query = Microsoft.AspNetCore.WebUtilities.QueryHelpers.ParseQuery(new Uri(url).Query);
        Assert.Contains("/prefix/Audio/", url);
        Assert.Equal("device & 1", query["deviceId"].ToString());
        Assert.Equal("48000", query["audioSampleRate"].ToString());
        Assert.Equal("128000", query["audioBitRate"].ToString());
        Assert.Equal("300000000", query["startTimeTicks"].ToString());
        Assert.Equal("false", query["allowAudioStreamCopy"].ToString());
        Assert.False(query.ContainsKey("api_key"));
    }

    [Fact]
    public void OpusUsesSupportedSampleRate()
    {
        var config = Enabled();
        config.TranscodingSampleRate = 44100;
        Assert.Equal(48000, AudioTranscodingPolicy.ForStream("dsd_lsbf_planar", 5_644_800, "opus", 0, 0, config)!.SampleRate);
    }

    [Theory]
    [InlineData(401)]
    [InlineData(403)]
    [InlineData(416)]
    [InlineData(500)]
    public async Task UpstreamFailureDoesNotBecomeSuccessfulAudio(int code)
    {
        using var handler = new ResponseHandler(new HttpResponseMessage((HttpStatusCode)code));
        var controller = Controller(handler);
        var result = await controller.ProxyTranscode("http://localhost/audio", "test-key", "audio/mpeg", null);
        Assert.Equal(code, Assert.IsType<StatusCodeResult>(result).StatusCode);
    }

    [Fact]
    public async Task ProxyPreservesTranscodedRangesMimeAndDownloadName()
    {
        using var response = new HttpResponseMessage(HttpStatusCode.PartialContent)
        {
            Content = new ByteArrayContent(new byte[] { 1, 2, 3 }),
        };
        response.Content.Headers.ContentRange = new ContentRangeHeaderValue(10, 12, 100);
        response.Headers.AcceptRanges.Add("bytes");
        using var handler = new ResponseHandler(response);
        var controller = Controller(handler);
        controller.Request.Headers.Range = "bytes=10-12";
        var result = Assert.IsType<FileStreamResult>(await controller.ProxyTranscode(
            "http://localhost/audio", "test-key", "audio/webm", "song.webm"));
        Assert.Equal(206, controller.Response.StatusCode);
        Assert.Equal("bytes 10-12/100", controller.Response.Headers.ContentRange.ToString());
        Assert.Equal(3, controller.Response.ContentLength);
        Assert.Equal("audio/webm", result.ContentType);
        Assert.Equal("song.webm", result.FileDownloadName);
        Assert.Equal("bytes=10-12", handler.Range);
        Assert.Equal("MediaBrowser Token=\"test-key\"", handler.Authorization);
    }

    private static SubsonicController Controller(ResponseHandler handler) => new(
        null!, null!, null!, null!, null!, null!, null!, new ClientFactory(handler), null!, null!, null!, null!)
    {
        ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() },
    };

    private sealed class ClientFactory(HttpMessageHandler handler) : IHttpClientFactory
    {
        public HttpClient CreateClient(string name) => new(handler, false);
    }

    private sealed class ResponseHandler(HttpResponseMessage response) : HttpMessageHandler
    {
        public string? Range { get; private set; }
        public string? Authorization { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Range = request.Headers.Range?.ToString();
            Authorization = request.Headers.GetValues("Authorization").Single();
            return Task.FromResult(response);
        }
    }
}
