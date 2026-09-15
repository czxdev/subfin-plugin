using System;
using System.Collections.Generic;
using System.Reflection;
using System.Linq;
using Jellyfin.Data.Entities;
using Jellyfin.Plugin.Subsonic.Controllers;
using Jellyfin.Plugin.Subsonic.Mappers;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Audio;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Playlists;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Primitives;
using Xunit;

namespace Jellyfin.Plugin.Subsonic.Tests;

public class ItemMapperTests
{
    [Fact]
    public void MultiDiscSong_ReferencesAlbumInsteadOfDiscFolder()
    {
        var album = new MusicAlbum { Id = Guid.NewGuid(), Name = "Album" };
        var disc = new Folder { Id = Guid.NewGuid(), ParentId = album.Id };
        var song = new TestAudio { Id = Guid.NewGuid(), ParentId = disc.Id, Name = "Song" };
        var library = DispatchProxy.Create<ILibraryManager, ParentLibrary>();
        ((ParentLibrary)library).Items = new() { [album.Id] = album, [disc.Id] = disc };
        var previous = BaseItem.LibraryManager;
        BaseItem.LibraryManager = library;
        try
        {
            var mapped = ItemMapper.ToSong(song);
            Assert.Equal(album.Id.ToString("N"), mapped["albumId"]);
            Assert.Equal(album.Id.ToString("N"), mapped["parent"]);
            Assert.Equal("explicit-album", ItemMapper.ToSong(song, albumId: "explicit-album")["albumId"]);
        }
        finally
        {
            BaseItem.LibraryManager = previous;
        }
    }

    public class ParentLibrary : DispatchProxy
    {
        public Dictionary<Guid, BaseItem> Items { get; set; } = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "GetItemById")
                return Items.GetValueOrDefault(Guid.Parse(args![0]!.ToString()!));
            if (method?.Name == "GetItemList" && args?[0] is InternalItemsQuery query)
                return Items.Values.OfType<Audio>().Where(song =>
                    query.Recursive ? song.GetParents().Any(p => p.Id == query.ParentId) : song.ParentId == query.ParentId)
                    .Cast<BaseItem>().ToList();
            throw new NotSupportedException(method?.Name);
        }
    }

    private class TestAudio : Audio
    {
        public override List<MediaBrowser.Model.Entities.MediaStream> GetMediaStreams() => new();
        public override bool IsVisible(User user, bool skipAllowedTagsCheck = false) => true;
    }

    private static SubsonicController Controller(ILibraryManager library) =>
        new(null!, null!, library, null!, null!, null!, null!, null!, null!, null!, null!, null!);

    [Fact]
    public void GetAlbum_IncludesSongsInsideDiscFolders()
    {
        var album = new MusicAlbum { Id = Guid.NewGuid(), Name = "Album" };
        var disc = new Folder { Id = Guid.NewGuid(), ParentId = album.Id };
        var direct = new TestAudio { Id = Guid.NewGuid(), ParentId = album.Id, Name = "Direct" };
        var nested = new TestAudio { Id = Guid.NewGuid(), ParentId = disc.Id, Name = "Disc song" };
        var library = DispatchProxy.Create<ILibraryManager, ParentLibrary>();
        ((ParentLibrary)library).Items = new()
        {
            [album.Id] = album, [disc.Id] = disc, [direct.Id] = direct, [nested.Id] = nested,
        };
        var previous = BaseItem.LibraryManager;
        BaseItem.LibraryManager = library;
        try
        {
            var query = new QueryParams(new QueryCollection(new Dictionary<string, StringValues> { ["id"] = album.Id.ToString("N") }));
            var response = Assert.IsType<ContentResult>(Controller(library).GetAlbum(null!, new User("test", "auth", "reset"), query, "xml"));
            var xml = System.Xml.Linq.XDocument.Parse(response.Content!);
            var songs = xml.Descendants().Where(e => e.Name.LocalName == "song").ToList();
            Assert.Equal(2, songs.Count);
            Assert.Contains(songs, s => (string?)s.Attribute("id") == nested.Id.ToString("N"));
            Assert.All(songs, s => Assert.Equal(album.Id.ToString("N"), (string?)s.Attribute("albumId")));
        }
        finally
        {
            BaseItem.LibraryManager = previous;
        }
    }

    [Fact]
    public void Playlist_ResolvesUncachedLinksAndPreservesDuplicates()
    {
        var song = new TestAudio { Id = Guid.NewGuid(), Name = "Song", RunTimeTicks = 20_000_000 };
        var playlist = new Playlist
        {
            Id = Guid.NewGuid(), Name = "Playlist",
            LinkedChildren = new[]
            {
                new LinkedChild { LibraryItemId = song.Id.ToString("N") },
                new LinkedChild { ItemId = song.Id },
            },
        };
        var library = DispatchProxy.Create<ILibraryManager, ParentLibrary>();
        ((ParentLibrary)library).Items = new() { [song.Id] = song };
        var previous = BaseItem.LibraryManager;
        BaseItem.LibraryManager = library;
        try
        {
            var controller = Controller(library);
            var user = new User("test", "auth", "reset");
            var mapped = controller.MapPlaylist(playlist, user, true);
            Assert.Equal(2, mapped["songCount"]);
            Assert.Equal(4, mapped["duration"]);
            Assert.Equal(2, Assert.IsType<List<Dictionary<string, object?>>>(mapped["entry"]).Count);
            var summary = controller.MapPlaylist(playlist, user, false);
            Assert.Equal(mapped["songCount"], summary["songCount"]);
            Assert.Equal(mapped["duration"], summary["duration"]);
        }
        finally
        {
            BaseItem.LibraryManager = previous;
        }
    }

    [Theory]
    [InlineData("ar-abc123", "abc123")]
    [InlineData("al-def456", "def456")]
    [InlineData("pl-xyz789", "xyz789")]
    [InlineData("rawguid", "rawguid")]
    [InlineData("AR-UPPERCASE", "UPPERCASE")]
    public void StripPrefix_RemovesPrefixes(string input, string expected)
    {
        Assert.Equal(expected, ItemMapper.StripPrefix(input));
    }

    [Theory]
    [InlineData("ABBA", "A")]
    [InlineData("Beatles", "B")]
    [InlineData("123band", "1")]
    [InlineData("", "#")]
    [InlineData(null, "#")]
    [InlineData("élan", "#")]
    public void IndexLetter_ReturnsCorrectLetter(string? name, string expected)
    {
        Assert.Equal(expected, ItemMapper.IndexLetter(name));
    }

    [Theory]
    [InlineData(10_000_000L, 1)]
    [InlineData(100_000_000L, 10)]
    [InlineData(0L, 0)]
    [InlineData(null, 0)]
    public void TicksToSeconds_Converts(long? ticks, int expected)
    {
        Assert.Equal(expected, ItemMapper.TicksToSeconds(ticks));
    }

    [Fact]
    public void ToArtistsIndex_GroupsByLetter()
    {
        var artists = new List<(string Id, string Name, int AlbumCount)>
        {
            ("id1", "ABBA", 5),
            ("id2", "Beatles", 3),
            ("id3", "AC/DC", 8),
        };
        var result = ItemMapper.ToArtistsIndex(artists);
        Assert.True(result.ContainsKey("ignoredArticles"));
        var index = result["index"] as System.Collections.IList;
        Assert.NotNull(index);
        // Should have at least A and B groups
        Assert.True(index!.Count >= 2);
    }
}
