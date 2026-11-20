using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.Media;

namespace SonatinaDashboard;

/// <summary>Italian when Windows is in Italian, English otherwise (like the tray menu).</summary>
public static class L
{
    public static readonly bool Italian =
        CultureInfo.CurrentUICulture.TwoLetterISOLanguageName.Equals("it", StringComparison.OrdinalIgnoreCase);

    public static string T(string italian, string english) => Italian ? italian : english;
}

/// <summary>XAML text in both languages: Text="{l:Tr It='Volume', En='Volume'}".</summary>
[MarkupExtensionReturnType(ReturnType = typeof(string))]
public sealed class Tr : MarkupExtension
{
    public string It { get; set; } = "";
    public string En { get; set; } = "";

    protected override object ProvideValue() => L.T(It, En);
}

/// <summary>Null-safe reads from the status JSON.</summary>
public static class J
{
    public static JsonNode? At(JsonNode? node, params string[] path)
    {
        foreach (var key in path)
        {
            if (node is not JsonObject obj || !obj.TryGetPropertyValue(key, out node))
                return null;
        }
        return node;
    }

    public static string? S(JsonNode? node, params string[] path)
    {
        if (At(node, path) is not JsonValue value)
            return null;
        if (value.TryGetValue<JsonElement>(out var e))
            return e.ValueKind switch
            {
                JsonValueKind.String => e.GetString(),
                JsonValueKind.Null => null,
                _ => e.GetRawText(),
            };
        if (value.TryGetValue<string>(out var s))
            return s;
        return value.ToJsonString();
    }

    public static double? D(JsonNode? node, params string[] path)
    {
        if (At(node, path) is not JsonValue value)
            return null;
        if (value.TryGetValue<JsonElement>(out var e))
            return e.ValueKind == JsonValueKind.Number ? e.GetDouble() : null;
        if (value.TryGetValue<double>(out var d))
            return d;
        if (value.TryGetValue<int>(out var i))
            return i;
        if (value.TryGetValue<long>(out var l))
            return l;
        return null;
    }

    public static int? I(JsonNode? node, params string[] path) =>
        D(node, path) is double d ? (int)Math.Round(d) : null;

    public static bool B(JsonNode? node, params string[] path)
    {
        if (At(node, path) is not JsonValue value)
            return false;
        if (value.TryGetValue<JsonElement>(out var e))
            return e.ValueKind == JsonValueKind.True;
        return value.TryGetValue<bool>(out var b) && b;
    }

    public static JsonArray A(JsonNode? node, params string[] path) =>
        At(node, path) as JsonArray ?? new JsonArray();

    public static IEnumerable<string> Strings(JsonNode? node, params string[] path)
    {
        foreach (var item in A(node, path))
        {
            var text = S(item);
            if (text is not null)
                yield return text;
        }
    }
}

public static class Ui
{
    public static readonly string[] Chain = ["ldac", "aptx_hd", "aptx", "aac", "sbc"];

    public static string CodecLabel(string? key) => key switch
    {
        "ldac" => "LDAC",
        "aptx_hd" => "aptX HD",
        "aptx" => "aptX",
        "aac" => "AAC",
        "sbc" => "SBC",
        "msbc" => "mSBC",
        "cvsd" => "CVSD",
        null or "" => "-",
        _ => key,
    };

    /// <summary>The codec Sonatina really aims for: the preferred one, or the next one in the
    /// chain that the headphones support (the preference may name an unsupported codec).</summary>
    public static string Effective(string? preferred, ICollection<string> supported)
    {
        preferred ??= "ldac";
        if (supported.Count == 0)
            return preferred;
        int start = Math.Max(0, Array.IndexOf(Chain, preferred));
        for (int i = start; i < Chain.Length; i++)
            if (supported.Contains(Chain[i]))
                return Chain[i];
        return "sbc";
    }

    public static Brush Res(string key) => (Brush)Application.Current.Resources[key];

    /// <summary>Two cards side by side, stacked when the page is narrow.</summary>
    public static void TwoUp(Microsoft.UI.Xaml.Controls.Grid grid, double width, double breakpoint = 860)
    {
        bool narrow = width < breakpoint;
        var children = grid.Children;
        if (grid.ColumnDefinitions.Count < 2 || grid.RowDefinitions.Count < 2 || children.Count < 2)
            return;
        grid.ColumnDefinitions[1].Width = narrow ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
        grid.ColumnSpacing = narrow ? 0 : 16;
        grid.RowSpacing = narrow ? 16 : 0;
        var second = (FrameworkElement)children[1];
        Microsoft.UI.Xaml.Controls.Grid.SetColumn(second, narrow ? 0 : 1);
        Microsoft.UI.Xaml.Controls.Grid.SetRow(second, narrow ? 1 : 0);
    }

    /// <summary>Runs a command; errors go to the window's banner. Null when it failed.</summary>
    public static async Task<(bool ok, JsonNode? result)> Run(string command, JsonObject? args = null, int timeoutMs = 15000)
    {
        try
        {
            return (true, await App.Ipc.SendAsync(command, args, timeoutMs));
        }
        catch (Exception error)
        {
            App.Shell?.ShowError(error.Message);
            return (false, null);
        }
    }

    public static string Short(string? transport)
    {
        if (string.IsNullOrEmpty(transport))
            return "-";
        return transport.StartsWith("usb:", StringComparison.OrdinalIgnoreCase) ? transport[4..].ToUpperInvariant() : transport;
    }

    public static string Signed(int value) => value > 0 ? "+" + value : value.ToString(CultureInfo.CurrentCulture);
}

/// <summary>A page that shows the live status.</summary>
public interface IStatusView
{
    void Update(JsonObject status);
}
