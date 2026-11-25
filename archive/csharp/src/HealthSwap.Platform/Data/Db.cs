using System.Data;
using Dapper;
using Npgsql;

namespace HealthSwap.Platform.Data;

/// <summary>Database roles from db/schema.sql. Every unit of work runs as exactly one of them (SET LOCAL ROLE).</summary>
public enum DbRole { App, LedgerWriter, KbWriter, Ops, ReadOnly }

public sealed class DbOptions
{
    public string ConnectionString { get; set; } = "Host=127.0.0.1;Port=55432;Username=postgres;Password=postgres;Database=postgres;SSL Mode=Disable;Maximum Pool Size=16;Timeout=30;Command Timeout=60";
    /// <summary>Switch roles per transaction (recommended; required for row-level security when the login is privileged).</summary>
    public bool UseRoles { get; set; } = true;
}

/// <summary>
/// Entry point for PostgreSQL access. Unit-of-work transactions set the role and the tenant with transaction-local
/// settings only, so they are safe with connection pooling and with the PGlite multiplexing development server.
/// </summary>
public sealed class Db : IAsyncDisposable
{
    private readonly NpgsqlDataSource _dataSource;
    private readonly bool _useRoles;

    static Db()
    {
        DefaultTypeMap.MatchNamesWithUnderscores = true;
        SqlMapper.RemoveTypeMap(typeof(DateTime));
        SqlMapper.RemoveTypeMap(typeof(DateTime?));
        SqlMapper.AddTypeHandler(new UtcDateTimeHandler());
    }

    public Db(DbOptions options)
    {
        _dataSource = new NpgsqlDataSourceBuilder(options.ConnectionString).Build();
        _useRoles = options.UseRoles;
    }

    public static string RoleName(DbRole role) => role switch
    {
        DbRole.App => "hs_app",
        DbRole.LedgerWriter => "hs_ledger_writer",
        DbRole.KbWriter => "hs_kb_writer",
        DbRole.Ops => "hs_ops",
        DbRole.ReadOnly => "hs_readonly",
        _ => throw new ArgumentOutOfRangeException(nameof(role)),
    };

    public async Task<UnitOfWork> BeginAsync(DbRole role, Guid? partnerId, CancellationToken ct = default)
    {
        var conn = await _dataSource.OpenConnectionAsync(ct);
        try
        {
            var tx = await conn.BeginTransactionAsync(IsolationLevel.ReadCommitted, ct);
            var uow = new UnitOfWork(conn, tx, partnerId, _useRoles);
            await uow.SetRoleAsync(role, ct);
            if (partnerId is { } p)
                await conn.ExecuteAsync(new CommandDefinition("select set_config('app.partner_id', @p, true)", new { p = p.ToString() }, tx, cancellationToken: ct));
            return uow;
        }
        catch
        {
            await conn.DisposeAsync();
            throw;
        }
    }

    /// <summary>Runs a function in a unit of work and commits; rolls back on exceptions.</summary>
    public async Task<T> InTxAsync<T>(DbRole role, Guid? partnerId, Func<UnitOfWork, Task<T>> work, CancellationToken ct = default)
    {
        await using var uow = await BeginAsync(role, partnerId, ct);
        var result = await work(uow);
        await uow.CommitAsync(ct);
        return result;
    }

    public Task InTxAsync(DbRole role, Guid? partnerId, Func<UnitOfWork, Task> work, CancellationToken ct = default) =>
        InTxAsync<bool>(role, partnerId, async u => { await work(u); return true; }, ct);

    /// <summary>Raw connection for schema bootstrap only (no role switching).</summary>
    public ValueTask<NpgsqlConnection> OpenRawAsync(CancellationToken ct = default) => _dataSource.OpenConnectionAsync(ct);

    public ValueTask DisposeAsync() => _dataSource.DisposeAsync();

    private sealed class UtcDateTimeHandler : SqlMapper.TypeHandler<DateTime>
    {
        public override void SetValue(IDbDataParameter parameter, DateTime value) =>
            parameter.Value = value.Kind == DateTimeKind.Unspecified ? DateTime.SpecifyKind(value, DateTimeKind.Utc) : value.ToUniversalTime();

        public override DateTime Parse(object value) => value switch
        {
            DateTime d => d.Kind == DateTimeKind.Unspecified ? DateTime.SpecifyKind(d, DateTimeKind.Utc) : d.ToUniversalTime(),
            DateTimeOffset o => o.UtcDateTime,
            DateOnly d => d.ToDateTime(TimeOnly.MinValue, DateTimeKind.Utc),
            _ => DateTime.SpecifyKind(Convert.ToDateTime(value, System.Globalization.CultureInfo.InvariantCulture), DateTimeKind.Utc),
        };
    }
}

public sealed class UnitOfWork : IAsyncDisposable
{
    private readonly bool _useRoles;
    private bool _completed;

    internal UnitOfWork(NpgsqlConnection connection, NpgsqlTransaction transaction, Guid? partnerId, bool useRoles)
    {
        Connection = connection;
        Transaction = transaction;
        PartnerId = partnerId;
        _useRoles = useRoles;
    }

    public NpgsqlConnection Connection { get; }
    public NpgsqlTransaction Transaction { get; }
    public Guid? PartnerId { get; }
    public DbRole Role { get; private set; }

    /// <summary>Least privilege inside one transaction, for example hs_app for the order and hs_ledger_writer for postings.</summary>
    public async Task SetRoleAsync(DbRole role, CancellationToken ct = default)
    {
        Role = role;
        if (!_useRoles) return;
        await Connection.ExecuteAsync(new CommandDefinition("set local role " + Db.RoleName(role), transaction: Transaction, cancellationToken: ct));
    }

    public Task<IEnumerable<T>> QueryAsync<T>(string sql, object? param = null, CancellationToken ct = default) =>
        Connection.QueryAsync<T>(new CommandDefinition(sql, param, Transaction, cancellationToken: ct));

    public Task<T?> QueryFirstOrDefaultAsync<T>(string sql, object? param = null, CancellationToken ct = default) =>
        Connection.QueryFirstOrDefaultAsync<T>(new CommandDefinition(sql, param, Transaction, cancellationToken: ct));

    public Task<T> QuerySingleAsync<T>(string sql, object? param = null, CancellationToken ct = default) =>
        Connection.QuerySingleAsync<T>(new CommandDefinition(sql, param, Transaction, cancellationToken: ct));

    public Task<int> ExecuteAsync(string sql, object? param = null, CancellationToken ct = default) =>
        Connection.ExecuteAsync(new CommandDefinition(sql, param, Transaction, cancellationToken: ct));

    public Task<T?> ScalarAsync<T>(string sql, object? param = null, CancellationToken ct = default) =>
        Connection.ExecuteScalarAsync<T>(new CommandDefinition(sql, param, Transaction, cancellationToken: ct));

    public async Task CommitAsync(CancellationToken ct = default)
    {
        await Transaction.CommitAsync(ct);
        _completed = true;
    }

    public async ValueTask DisposeAsync()
    {
        if (!_completed)
        {
            try { await Transaction.RollbackAsync(); } catch { /* connection may be broken; disposal below cleans up */ }
        }
        await Transaction.DisposeAsync();
        await Connection.DisposeAsync();
    }
}
