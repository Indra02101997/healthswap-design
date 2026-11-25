using System.Collections.Concurrent;
using StackExchange.Redis;

namespace HealthSwap.Platform.Cache;

/// <summary>Minimal Redis-shaped key-value API used by the hot path (HLD 10.2).</summary>
public interface ICacheStore
{
    Task<string?> GetAsync(string key);
    Task<string?[]> MGetAsync(IReadOnlyList<string> keys);
    Task SetAsync(string key, string value, TimeSpan? ttl = null);
    Task<bool> DeleteAsync(string key);
    Task HSetAsync(string key, IReadOnlyList<KeyValuePair<string, string>> fields, TimeSpan? ttl = null);
    Task<string?[]> HMGetAsync(string key, IReadOnlyList<string> fields);
    Task<Dictionary<string, string>> HGetAllAsync(string key);
    Task HDelAsync(string key, IReadOnlyList<string> fields);
    Task SAddAsync(string key, IReadOnlyList<string> members, TimeSpan? ttl = null);
    Task<string[]> SMembersAsync(string key);
    Task SRemAsync(string key, IReadOnlyList<string> members);
    Task<long> CountAsync(string prefix);
    Task<int> DeleteByPrefixAsync(string prefix);
}

/// <summary>In-process store with Redis semantics and TTLs (local development, tests, single-node demos).</summary>
public sealed class MemoryCacheStore : ICacheStore
{
    private sealed class Entry
    {
        public object Value = "";
        public long ExpiresAtTicks; // 0 = no expiry
        public bool Expired(long now) => ExpiresAtTicks != 0 && now >= ExpiresAtTicks;
    }

    private readonly ConcurrentDictionary<string, Entry> _data = new(StringComparer.Ordinal);

    private static long Now => DateTime.UtcNow.Ticks;
    private static long Expiry(TimeSpan? ttl) => ttl is { } t ? Now + t.Ticks : 0;

    private Entry? Live(string key)
    {
        if (!_data.TryGetValue(key, out var e)) return null;
        if (!e.Expired(Now)) return e;
        _data.TryRemove(key, out _);
        return null;
    }

    public Task<string?> GetAsync(string key) => Task.FromResult(Live(key)?.Value as string);

    public Task<string?[]> MGetAsync(IReadOnlyList<string> keys) => Task.FromResult(keys.Select(k => Live(k)?.Value as string).ToArray());

    public Task SetAsync(string key, string value, TimeSpan? ttl = null)
    {
        _data[key] = new Entry { Value = value, ExpiresAtTicks = Expiry(ttl) };
        return Task.CompletedTask;
    }

    public Task<bool> DeleteAsync(string key) => Task.FromResult(_data.TryRemove(key, out _));

    private ConcurrentDictionary<string, string> Hash(string key, TimeSpan? ttl)
    {
        var e = _data.AddOrUpdate(key,
            _ => new Entry { Value = new ConcurrentDictionary<string, string>(StringComparer.Ordinal), ExpiresAtTicks = Expiry(ttl) },
            (_, existing) =>
            {
                if (existing.Expired(Now) || existing.Value is not ConcurrentDictionary<string, string>)
                    return new Entry { Value = new ConcurrentDictionary<string, string>(StringComparer.Ordinal), ExpiresAtTicks = Expiry(ttl) };
                if (ttl is not null) existing.ExpiresAtTicks = Expiry(ttl);
                return existing;
            });
        return (ConcurrentDictionary<string, string>)e.Value;
    }

    public Task HSetAsync(string key, IReadOnlyList<KeyValuePair<string, string>> fields, TimeSpan? ttl = null)
    {
        var h = Hash(key, ttl);
        foreach (var (f, v) in fields) h[f] = v;
        return Task.CompletedTask;
    }

    public Task<string?[]> HMGetAsync(string key, IReadOnlyList<string> fields)
    {
        var h = Live(key)?.Value as ConcurrentDictionary<string, string>;
        return Task.FromResult(fields.Select(f => h is not null && h.TryGetValue(f, out var v) ? v : null).ToArray());
    }

    public Task<Dictionary<string, string>> HGetAllAsync(string key)
    {
        var h = Live(key)?.Value as ConcurrentDictionary<string, string>;
        return Task.FromResult(h is null ? new Dictionary<string, string>() : new Dictionary<string, string>(h));
    }

    public Task HDelAsync(string key, IReadOnlyList<string> fields)
    {
        if (Live(key)?.Value is ConcurrentDictionary<string, string> h)
            foreach (var f in fields) h.TryRemove(f, out _);
        return Task.CompletedTask;
    }

    public Task SAddAsync(string key, IReadOnlyList<string> members, TimeSpan? ttl = null)
    {
        var e = _data.AddOrUpdate(key,
            _ => new Entry { Value = new ConcurrentDictionary<string, byte>(StringComparer.Ordinal), ExpiresAtTicks = Expiry(ttl) },
            (_, existing) => existing.Expired(Now) || existing.Value is not ConcurrentDictionary<string, byte>
                ? new Entry { Value = new ConcurrentDictionary<string, byte>(StringComparer.Ordinal), ExpiresAtTicks = Expiry(ttl) }
                : existing);
        var set = (ConcurrentDictionary<string, byte>)e.Value;
        foreach (var m in members) set[m] = 0;
        return Task.CompletedTask;
    }

    public Task<string[]> SMembersAsync(string key) =>
        Task.FromResult(Live(key)?.Value is ConcurrentDictionary<string, byte> s ? s.Keys.ToArray() : []);

    public Task SRemAsync(string key, IReadOnlyList<string> members)
    {
        if (Live(key)?.Value is ConcurrentDictionary<string, byte> s)
            foreach (var m in members) s.TryRemove(m, out _);
        return Task.CompletedTask;
    }

    public Task<long> CountAsync(string prefix) =>
        Task.FromResult((long)_data.Count(kv => kv.Key.StartsWith(prefix, StringComparison.Ordinal) && !kv.Value.Expired(Now)));

    public Task<int> DeleteByPrefixAsync(string prefix)
    {
        var n = 0;
        foreach (var k in _data.Keys.Where(k => k.StartsWith(prefix, StringComparison.Ordinal)).ToList())
            if (_data.TryRemove(k, out _)) n++;
        return Task.FromResult(n);
    }
}

/// <summary>Production store on Redis (Azure Managed Redis or any Redis 6+). One-to-one mapping of the cache API.</summary>
public sealed class RedisCacheStore(IConnectionMultiplexer redis) : ICacheStore
{
    private IDatabase Db => redis.GetDatabase();

    public async Task<string?> GetAsync(string key) => await Db.StringGetAsync(key);

    public async Task<string?[]> MGetAsync(IReadOnlyList<string> keys)
    {
        if (keys.Count == 0) return [];
        var values = await Db.StringGetAsync(keys.Select(k => (RedisKey)k).ToArray());
        return values.Select(v => v.HasValue ? (string?)v.ToString() : null).ToArray();
    }

    public Task SetAsync(string key, string value, TimeSpan? ttl = null) =>
        ttl is { } t ? Db.StringSetAsync(key, value, t) : Db.StringSetAsync(key, value);

    public Task<bool> DeleteAsync(string key) => Db.KeyDeleteAsync(key);

    public async Task HSetAsync(string key, IReadOnlyList<KeyValuePair<string, string>> fields, TimeSpan? ttl = null)
    {
        await Db.HashSetAsync(key, fields.Select(f => new HashEntry(f.Key, f.Value)).ToArray());
        if (ttl is { } t) await Db.KeyExpireAsync(key, t);
    }

    public async Task<string?[]> HMGetAsync(string key, IReadOnlyList<string> fields)
    {
        if (fields.Count == 0) return [];
        var values = await Db.HashGetAsync(key, fields.Select(f => (RedisValue)f).ToArray());
        return values.Select(v => v.HasValue ? (string?)v.ToString() : null).ToArray();
    }

    public async Task<Dictionary<string, string>> HGetAllAsync(string key) =>
        (await Db.HashGetAllAsync(key)).ToDictionary(e => e.Name.ToString(), e => e.Value.ToString());

    public Task HDelAsync(string key, IReadOnlyList<string> fields) => Db.HashDeleteAsync(key, fields.Select(f => (RedisValue)f).ToArray());

    public async Task SAddAsync(string key, IReadOnlyList<string> members, TimeSpan? ttl = null)
    {
        await Db.SetAddAsync(key, members.Select(m => (RedisValue)m).ToArray());
        if (ttl is { } t) await Db.KeyExpireAsync(key, t);
    }

    public async Task<string[]> SMembersAsync(string key) => (await Db.SetMembersAsync(key)).Select(v => v.ToString()).ToArray();

    public Task SRemAsync(string key, IReadOnlyList<string> members) => Db.SetRemoveAsync(key, members.Select(m => (RedisValue)m).ToArray());

    public Task<long> CountAsync(string prefix)
    {
        long n = 0;
        foreach (var ep in redis.GetEndPoints())
            n += redis.GetServer(ep).Keys(pattern: prefix + "*", pageSize: 1000).LongCount();
        return Task.FromResult(n);
    }

    public async Task<int> DeleteByPrefixAsync(string prefix)
    {
        var n = 0;
        foreach (var ep in redis.GetEndPoints())
            foreach (var key in redis.GetServer(ep).Keys(pattern: prefix + "*", pageSize: 1000))
                if (await Db.KeyDeleteAsync(key)) n++;
        return n;
    }
}
