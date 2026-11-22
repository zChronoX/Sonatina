using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace SonatinaDashboard.Pages;

public sealed partial class QualityPage : Page, IStatusView
{
    private static readonly Dictionary<string, (string It, string En)> Hints = new()
    {
        ["ldac"] = ("Fino a 990 kbit/s, 24 bit: il più dettagliato (Sony e molte cuffie Android)",
                    "Up to 990 kbit/s, 24 bit: the most detailed (Sony and many Android headphones)"),
        ["aptx_hd"] = ("576 kbit/s, 24 bit", "576 kbit/s, 24 bit"),
        ["aptx"] = ("384 kbit/s, ritardo basso", "384 kbit/s, low delay"),
        ["aac"] = ("Fino a 320 kbit/s: ottimo con le cuffie Apple", "Up to 320 kbit/s: great with Apple headphones"),
        ["sbc"] = ("Il codec di base: tutte le cuffie lo supportano", "The basic codec: every headphone supports it"),
    };

    private static readonly (string Key, string It, string En)[] LdacQualities =
    [
        ("hq", "Massima · 990 kbit/s", "Best · 990 kbit/s"),
        ("sq", "Standard · 660 kbit/s", "Standard · 660 kbit/s"),
        ("mq", "Connessione · 330 kbit/s", "Connection · 330 kbit/s"),
    ];

    private bool _updating;
    private string? _address;
    private string _boxKey = "";
    private string _codecKey = "";
    private JsonObject? _last;

    public QualityPage()
    {
        InitializeComponent();
        foreach (var (key, it, en) in LdacQualities)
            LdacBox.Items.Add(new ComboBoxItem { Content = L.T(it, en), Tag = key });
    }

    private JsonNode? Selected(JsonObject s)
    {
        foreach (var h in J.A(s, "host", "headsets"))
            if (J.S(h, "address") == _address)
                return h;
        return null;
    }

    public void Update(JsonObject s)
    {
        _last = s;
        _updating = true;
        try
        {
            // now
            bool inCall = Status.InCall(s);
            var stream = J.At(s, "host", "stream");
            NowCodec.Text = inCall ? L.T("Chiamata", "Call")
                          : stream is JsonObject ? J.S(stream, "name") ?? Ui.CodecLabel(J.S(stream, "codec")) : "-";
            NowRate.Text = !inCall && J.I(stream, "kbps") is int kbps ? $"{kbps} kbit/s" : "-";
            NowDelay.Text = !inCall && J.I(stream, "latency_ms") is int ms ? $"~{ms} ms" : "-";
            LowLatency.IsOn = J.B(s, "host", "options", "low_latency");
            LowLatency.IsEnabled = Status.Host(s) is not null;

            // headset picker
            var headsets = J.A(s, "host", "headsets");
            string boxKey = string.Join("|", headsets.Select(h => J.S(h, "address") + "=" + J.S(h, "name")));
            if (boxKey != _boxKey)
            {
                _boxKey = boxKey;
                HeadsetBox.Items.Clear();
                foreach (var h in headsets)
                    HeadsetBox.Items.Add(new ComboBoxItem { Content = J.S(h, "name") ?? J.S(h, "address"), Tag = J.S(h, "address") });
                if (Selected(s) is null)
                    _address = J.S(Status.CurrentHeadset(s), "address");
                foreach (var item in HeadsetBox.Items)
                    if (item is ComboBoxItem cbi && (string?)cbi.Tag == _address)
                        HeadsetBox.SelectedItem = cbi;
            }
            HeadsetBox.IsEnabled = headsets.Count > 1;

            var headset = Selected(s);
            var supported = J.Strings(headset, "supported").ToHashSet();
            string preferred = Ui.Effective(J.S(headset, "codec"), supported);
            string codecKey = _address + "|" + preferred + "|" + string.Join(",", supported);
            if (codecKey != _codecKey)
            {
                _codecKey = codecKey;
                BuildCodecList(headset is not null, preferred, supported);
            }
            LdacRow.Visibility = preferred == "ldac" && headset is not null ? Visibility.Visible : Visibility.Collapsed;
            AdaptiveRow.Visibility = LdacRow.Visibility;
            Adaptive.IsOn = J.B(s, "host", "options", "ldac_adaptive");
            Adaptive.IsEnabled = Status.Host(s) is not null;
            string quality = J.S(headset, "ldac_quality") ?? "sq";
            foreach (var item in LdacBox.Items)
                if (item is ComboBoxItem cbi && (string)cbi.Tag == quality && !ReferenceEquals(LdacBox.SelectedItem, cbi))
                    LdacBox.SelectedItem = cbi;
        }
        finally
        {
            _updating = false;
        }
    }

    private void BuildCodecList(bool enabled, string preferred, HashSet<string> supported)
    {
        CodecList.Children.Clear();
        foreach (var codec in Ui.Chain)
        {
            bool known = supported.Count == 0 || supported.Contains(codec);
            var hint = Hints[codec];
            var content = new StackPanel();
            content.Children.Add(new TextBlock { Text = Ui.CodecLabel(codec) });
            content.Children.Add(new TextBlock
            {
                Text = known ? L.T(hint.It, hint.En) : L.T("Non supportato da queste cuffie", "Not supported by these headphones"),
                Style = (Style)Application.Current.Resources["Hint"],
            });
            var radio = new RadioButton
            {
                GroupName = "codec",
                Tag = codec,
                Content = content,
                IsChecked = codec == preferred,
                IsEnabled = enabled && known,
                Margin = new Thickness(0, 2, 0, 2),
            };
            radio.Checked += Codec_Checked;
            CodecList.Children.Add(radio);
        }
        if (supported.Count == 0 && enabled)
            CodecList.Children.Add(new TextBlock
            {
                Text = L.T("Quali codec supportano queste cuffie si saprà al prossimo collegamento.",
                           "Which codecs these headphones support will be known at the next connection."),
                Style = (Style)Application.Current.Resources["Hint"],
            });
    }

    private async void Codec_Checked(object sender, RoutedEventArgs e)
    {
        if (_updating || sender is not RadioButton radio || _address is null)
            return;
        await SetCodec((string)radio.Tag, null);
    }

    private async void LdacBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_updating || _address is null || LdacBox.SelectedItem is not ComboBoxItem item)
            return;
        await SetCodec("ldac", (string)item.Tag);
    }

    private async Task SetCodec(string codec, string? quality)
    {
        var args = new JsonObject { ["address"] = _address, ["codec"] = codec };
        if (quality is not null)
            args["ldac_quality"] = quality;
        if ((await Ui.Run("set_codec", args)).ok)
            App.Shell?.ShowMessage(L.T($"Codec preferito: {Ui.CodecLabel(codec)}. Le cuffie si ricollegano se sono accese.",
                                      $"Preferred codec: {Ui.CodecLabel(codec)}. The headphones reconnect if they are on."));
    }

    private void HeadsetBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_updating || HeadsetBox.SelectedItem is not ComboBoxItem item)
            return;
        _address = (string?)item.Tag;
        _codecKey = "";
        if (_last is not null)
            Update(_last);
    }

    private async void Adaptive_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_option", new JsonObject { ["name"] = "ldac_adaptive", ["value"] = Adaptive.IsOn });
    }

    private async void LowLatency_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_option", new JsonObject { ["name"] = "low_latency", ["value"] = LowLatency.IsOn });
    }
}
