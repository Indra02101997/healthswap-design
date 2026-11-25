using System.Globalization;

namespace HealthSwap.Domain;

/// <summary>ISO 4217 helpers. Money is always decimal; rounding is half-even to the currency minor unit (HLD 9.2).</summary>
public static class Currency
{
    public static int MinorUnits(string currency) => currency switch
    {
        "JPY" or "KRW" or "VND" => 0,
        "BHD" or "KWD" or "OMR" or "JOD" or "TND" => 3,
        _ => 2,
    };

    public static decimal Round(decimal amount, string currency) =>
        Math.Round(amount, MinorUnits(currency), MidpointRounding.ToEven);

    public static string Format(decimal amount, string currency) =>
        Round(amount, currency).ToString("F" + MinorUnits(currency), CultureInfo.InvariantCulture);

    public static decimal Parse(string value) =>
        decimal.Parse(value, NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture);

    public static bool TryParse(string? value, out decimal amount) =>
        decimal.TryParse(value, NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out amount);
}

/// <summary>Monetary amount with an ISO 4217 currency.</summary>
public readonly record struct Money(decimal Amount, string Currency)
{
    public static Money Zero(string currency) => new(0m, currency);
    public Money Rounded() => new(HealthSwap.Domain.Currency.Round(Amount, Currency), Currency);
    public string AmountText => HealthSwap.Domain.Currency.Format(Amount, Currency);
    public override string ToString() => $"{AmountText} {Currency}";
}

/// <summary>Net quantity of a pack, normalized to g, ml or pcs.</summary>
public readonly record struct NetQuantity(decimal Value, string Unit)
{
    public static NetQuantity? Normalize(decimal? value, string? unit)
    {
        if (value is null || value <= 0 || string.IsNullOrWhiteSpace(unit)) return null;
        return unit.Trim().ToLowerInvariant() switch
        {
            "kg" => new NetQuantity(value.Value * 1000m, "g"),
            "g" => new NetQuantity(value.Value, "g"),
            "l" => new NetQuantity(value.Value * 1000m, "ml"),
            "ml" => new NetQuantity(value.Value, "ml"),
            "pcs" => new NetQuantity(value.Value, "pcs"),
            "oz" => new NetQuantity(Math.Round(value.Value * 28.3495m, 1), "g"),
            "fl oz" or "floz" => new NetQuantity(Math.Round(value.Value * 29.5735m, 1), "ml"),
            _ => null,
        };
    }

    /// <summary>Sizes are comparable only in the same mass or volume unit; HealthSwap never guesses densities.</summary>
    public bool IsComparableTo(NetQuantity other) => Unit == other.Unit && Unit != "pcs";
}

/// <summary>GTIN normalization (doc 02 section 3): GTIN-8/12/13/14 to 14 digits with a valid mod-10 check digit.</summary>
public static class Gtin
{
    public static bool TryNormalize(string? raw, out string gtin14)
    {
        gtin14 = string.Empty;
        if (string.IsNullOrWhiteSpace(raw)) return false;
        var digits = raw.Trim();
        if (digits.Length is not (8 or 12 or 13 or 14) || !digits.All(char.IsAsciiDigit)) return false;
        var padded = digits.PadLeft(14, '0');
        if (!HasValidCheckDigit(padded)) return false;
        gtin14 = padded;
        return true;
    }

    public static bool HasValidCheckDigit(string digits)
    {
        if (digits.Length == 0 || !digits.All(char.IsAsciiDigit)) return false;
        var sum = 0;
        // Weights 3,1,3,1... from the rightmost digit before the check digit.
        for (int i = digits.Length - 2, w = 3; i >= 0; i--, w = w == 3 ? 1 : 3)
            sum += (digits[i] - '0') * w;
        var check = (10 - sum % 10) % 10;
        return check == digits[^1] - '0';
    }

    public static string WithCheckDigit(string withoutCheck)
    {
        var sum = 0;
        for (int i = withoutCheck.Length - 1, w = 3; i >= 0; i--, w = w == 3 ? 1 : 3)
            sum += (withoutCheck[i] - '0') * w;
        return withoutCheck + ((10 - sum % 10) % 10).ToString(CultureInfo.InvariantCulture);
    }

    /// <summary>
    /// Restricted-circulation numbers (GTIN-13 prefixes 02x and 2xx, which include UPC-A numbers starting with 2) are
    /// retailer-specific and must never be used as global keys; map them by partner SKU instead.
    /// </summary>
    public static bool IsRestrictedCirculation(string gtin14) =>
        gtin14.Length == 14 && gtin14[0] == '0' && (gtin14[1] == '2' || (gtin14[1] == '0' && gtin14[2] == '2'));
}

/// <summary>Time abstraction so billing windows, return windows and settlement can be tested (and time-travelled locally).</summary>
public interface IClock
{
    DateTime UtcNow { get; }
}

public sealed class SystemClock : IClock
{
    public DateTime UtcNow => DateTime.UtcNow;
}

/// <summary>Clock with an adjustable offset (development only; used by the manual test guide to skip return windows).</summary>
public sealed class OffsetClock : IClock
{
    private long _offsetTicks;
    public TimeSpan Offset { get => TimeSpan.FromTicks(Interlocked.Read(ref _offsetTicks)); set => Interlocked.Exchange(ref _offsetTicks, value.Ticks); }
    public DateTime UtcNow => DateTime.UtcNow + Offset;
}

public static class Ids
{
    /// <summary>UUIDv7: time-ordered, not sequential-guessable (OWASP API1 mitigation in HLD 12).</summary>
    public static Guid NewId() => Guid.CreateVersion7();
}

public static class Period
{
    /// <summary>Parses a billing month (YYYY-MM) into [start, end) UTC.</summary>
    public static bool TryParse(string? text, out DateTime start, out DateTime end)
    {
        start = end = default;
        if (text is null || !DateTime.TryParseExact(text + "-01", "yyyy-MM-dd", CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var s)) return false;
        start = DateTime.SpecifyKind(s, DateTimeKind.Utc);
        end = start.AddMonths(1);
        return true;
    }

    public static string Of(DateTime utc) => utc.ToString("yyyy-MM", CultureInfo.InvariantCulture);
}
