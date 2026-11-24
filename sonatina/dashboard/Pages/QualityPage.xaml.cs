using System.Text.Json.Nodes;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;

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

    private static readonly (string Key, string It, string En)[] LatencyModes =
    [
        ("off", "Spenta", "Off"),
        ("on", "Sempre", "Always"),
        ("apps", "Solo con queste app", "Only with these apps"),
    ];

    private static readonly (string Key, string It, string En)[] EqPresetNames =
    [
        ("flat", "Neutro", "Flat"),
        ("bass", "Più bassi", "More bass"),
        ("voice", "Voce", "Voice"),
        ("treble", "Più alti", "More treble"),
        ("custom", "Personalizzato", "Custom"),
    ];

    private static readonly string[] EqLabels = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"];

    private bool _updating;
    private string? _address;
    private string _appsKey = "";
    private string _runningKey = "";
    private List<string> _apps = [];
    private readonly Slider[] _bands = new Slider[10];
    private readonly TextBlock[] _bandValues = new TextBlock[10];
    private readonly DispatcherQueueTimer _eqTimer;
    private DateTime _eqTouched = DateTime.MinValue;
    private string _boxKey = "";
    private string _codecKey = "";
    private JsonObject? _last;

    public QualityPage()
    {
        InitializeComponent();
        foreach (var (key, it, en) in LdacQualities)
            LdacBox.Items.Add(new ComboBoxItem { Content = L.T(it, en), Tag = key });
        LatencyMode.SetOptions(LatencyModes.Select(m => (m.Key, L.T(m.It, m.En))));
        LatencyMode.Picked += mode => _ = Ui.Run("set_low_latency", new JsonObject { ["mode"] = mode });
        EqPresets.SetOptions(EqPresetNames.Select(p => (p.Key, L.T(p.It, p.En))));
        EqPresets.Picked += preset => _ = SetEq(new JsonObject { ["preset"] = preset });
        BuildBands();
        _eqTimer = DispatcherQueue.GetForCurrentThread().CreateTimer();
        _eqTimer.Interval = TimeSpan.FromMilliseconds(250);
        _eqTimer.IsRepeating = false;
        _eqTimer.Tick += (_, _) =>
            _ = SetEq(new JsonObject { ["custom"] = new JsonArray(_bands.Select(b => (JsonNode)(int)b.Value).ToArray()) });
    }

    private void BuildBands()
    {
        for (int i = 0; i < EqLabels.Length; i++)
        {
            EqBands.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var column = new StackPanel { Spacing = 6, HorizontalAlignment = HorizontalAlignment.Center };
            _bandValues[i] = new TextBlock { HorizontalAlignment = HorizontalAlignment.Center, FontSize = 12 };
            _bands[i] = new Slider
            {
                Orientation = Orientation.Vertical, Minimum = -12, Maximum = 12, StepFrequency = 1,
                Height = 160, HorizontalAlignment = HorizontalAlignment.Center,
                TickFrequency = 6, TickPlacement = TickPlacement.Outside,
            };
            Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(_bands[i], EqLabels[i] + " Hz");
            int band = i;
            _bands[i].ValueChanged += (_, e) =>
            {
                _bandValues[band].Text = Ui.Signed((int)e.NewValue);
                if (_updating)
                    return;
                _eqTouched = DateTime.UtcNow;
                _eqTimer.Stop();
                _eqTimer.Start();
                UpdateEqHint(_bands.Select(b => (int)b.Value).ToList(), true, "custom");
            };
            column.Children.Add(_bandValues[i]);
            column.Children.Add(_bands[i]);
            column.Children.Add(new TextBlock
            {
                Text = EqLabels[i], HorizontalAlignment = HorizontalAlignment.Center,
                Style = (Style)Application.Current.Resources["Hint"],
            });
            Grid.SetColumn(column, i);
            EqBands.Children.Add(column);
        }
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
            // adaptive quality (M1): LDAC, SBC and AAC; aptX has one bitrate only
            AdaptiveRow.Visibility = preferred is "ldac" or "aac" or "sbc" ? Visibility.Visible : Visibility.Collapsed;
            Adaptive.IsOn = J.B(s, "host", "options", "ldac_adaptive");
            Adaptive.IsEnabled = Status.Host(s) is not null;
            var levels = J.A(stream, "levels");
            int level = J.I(stream, "level") ?? 0, ceiling = J.I(stream, "ceiling") ?? 0;
            AdaptiveNow.Text = !inCall && level > ceiling && level < levels.Count && ceiling < levels.Count
                ? L.T($"Adesso {levels[level]} kbit/s invece di {levels[ceiling]}: la radio fa fatica, risale da sola",
                      $"Now {levels[level]} kbit/s instead of {levels[ceiling]}: the radio is struggling, it comes back by itself")
                : "";
            AdaptiveNow.Visibility = AdaptiveNow.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
            LiveSwitch.IsOn = J.B(s, "host", "options", "codec_switch_live");
            LiveSwitch.IsEnabled = Status.Host(s) is not null;

            UpdateLatency(s);
            UpdateEq(s, headset);
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
            App.Shell?.ShowMessage(J.B(_last, "host", "options", "codec_switch_live")
                ? L.T($"Codec preferito: {Ui.CodecLabel(codec)}. Se le cuffie sono accese cambia subito.",
                      $"Preferred codec: {Ui.CodecLabel(codec)}. If the headphones are on it changes at once.")
                : L.T($"Codec preferito: {Ui.CodecLabel(codec)}. Le cuffie si ricollegano se sono accese.",
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

    private async void Option_Toggled(object sender, RoutedEventArgs e)
    {
        if (_updating || sender is not ToggleSwitch toggle)
            return;
        await Ui.Run("set_option", new JsonObject { ["name"] = (string)toggle.Tag, ["value"] = toggle.IsOn });
    }

    // -- low latency (M3) -------------------------------------------------------
    private static string AppLabel(string exe)
    {
        string name = exe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? exe[..^4] : exe;
        return name.Length > 0 ? char.ToUpperInvariant(name[0]) + name[1..] : name;
    }

    private void UpdateLatency(JsonObject s)
    {
        var lowlat = J.At(s, "host", "low_latency");
        bool known = lowlat is JsonObject;
        string mode = J.S(lowlat, "mode") ?? "off";
        LatencyMode.Set(mode);
        LatencyMode.IsHitTestVisible = known;
        AppsPanel.Visibility = mode == "apps" ? Visibility.Visible : Visibility.Collapsed;

        _apps = J.Strings(lowlat, "apps").ToList();
        string appsKey = string.Join("|", _apps);
        if (appsKey != _appsKey)
        {
            _appsKey = appsKey;
            AppChips.Children.Clear();
            foreach (var app in _apps)
            {
                var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 4 };
                row.Children.Add(new TextBlock { Text = AppLabel(app), VerticalAlignment = VerticalAlignment.Center });
                var remove = new Button
                {
                    Content = new FontIcon { Glyph = "\uE711", FontSize = 10 }, Padding = new Thickness(6),
                    Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent),
                    BorderThickness = new Thickness(0),
                };
                Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(remove, L.T($"Togli {app}", $"Remove {app}"));
                string target = app;
                remove.Click += (_, _) => _ = SetApps(_apps.Where(a => a != target).ToList());
                row.Children.Add(remove);
                AppChips.Children.Add(new Border
                {
                    Child = row, CornerRadius = new CornerRadius(16), Padding = new Thickness(12, 2, 4, 2),
                    BorderThickness = new Thickness(1), BorderBrush = Ui.Res("CardStrokeColorDefaultBrush"),
                    Background = Ui.Res("SubtleFillColorSecondaryBrush"),
                });
            }
            _runningKey = "";
        }
        NoApps.Visibility = _apps.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        var running = J.Strings(lowlat, "running")
            .Where(r => !_apps.Contains(r, StringComparer.OrdinalIgnoreCase)).ToList();
        string runningKey = string.Join("|", running);
        if (runningKey != _runningKey)
        {
            _runningKey = runningKey;
            RunningApps.Children.Clear();
            foreach (var app in running)
            {
                var add = new Button { Content = "+ " + AppLabel(app) };
                string target = app;
                add.Click += (_, _) => _ = SetApps(_apps.Append(target).ToList());
                RunningApps.Children.Add(add);
            }
            if (running.Count == 0)
                RunningApps.Children.Add(new TextBlock
                {
                    Text = L.T("Nessuna app con l’audio aperto adesso: apri il gioco o il lettore e torna qui.",
                               "No app has audio open right now: start the game or the player and come back here."),
                    Style = (Style)Application.Current.Resources["Hint"],
                });
        }

        bool active = J.B(lowlat, "active");
        string? because = J.S(lowlat, "because");
        string delay = J.I(s, "host", "stream", "latency_ms") is int ms ? L.T($" · ritardo stimato ~{ms} ms", $" · estimated delay ~{ms} ms") : "";
        LatencyNow.Text = (mode, active) switch
        {
            ("off", _) => L.T("Adesso: spenta", "Now: off"),
            ("on", _) => L.T("Adesso: attiva", "Now: on"),
            (_, true) => because is not null ? L.T($"Adesso: attiva perché {AppLabel(because)} è aperto", $"Now: on because {AppLabel(because)} is open")
                                            : L.T("Adesso: attiva", "Now: on"),
            _ => L.T("Adesso: in attesa (nessuna di queste app è aperta)", "Now: waiting (none of these apps is open)"),
        } + delay;
        LatencyDot.Fill = Ui.Res(active ? "SystemFillColorSuccessBrush" : "ControlStrongFillColorDisabledBrush");
    }

    private async Task SetApps(List<string> apps)
    {
        var list = new JsonArray(apps.Select(a => (JsonNode)a).ToArray());
        await Ui.Run("set_low_latency", new JsonObject { ["mode"] = "apps", ["apps"] = list });
    }

    // -- equalizer (M4) ---------------------------------------------------------
    private void UpdateEq(JsonObject s, JsonNode? headset)
    {
        bool available = J.At(s, "host", "eq") is JsonObject;
        EqCard.Visibility = available ? Visibility.Visible : Visibility.Collapsed;
        if (!available)
            return;
        bool on = J.B(headset, "eq", "on");
        string preset = J.S(headset, "eq", "preset") ?? "flat";
        var values = preset == "custom"
            ? J.A(headset, "eq", "custom").Select(v => (int)(v?.GetValue<double>() ?? 0)).ToList()
            : J.A(s, "host", "eq", "presets", preset).Select(v => (int)(v?.GetValue<double>() ?? 0)).ToList();
        while (values.Count < 10)
            values.Add(0);
        EqFor.Text = headset is null ? "" : L.T($"Per {J.S(headset, "name")} · ricordato per ogni cuffia",
                                                $"For {J.S(headset, "name")} · remembered for each pair of headphones");
        EqSwitch.IsOn = on;
        EqSwitch.IsEnabled = headset is not null;
        EqPresets.Set(preset);
        EqPresets.IsHitTestVisible = on;
        EqPresets.Opacity = EqBands.Opacity = on ? 1.0 : 0.45;
        bool dragging = DateTime.UtcNow - _eqTouched < TimeSpan.FromSeconds(1.5);
        for (int i = 0; i < 10; i++)
        {
            _bands[i].IsEnabled = on && preset == "custom";
            if (!dragging)
                _bands[i].Value = values[i];
            _bandValues[i].Text = Ui.Signed((int)_bands[i].Value);
        }
        if (!dragging)
            UpdateEqHint(values, on, preset);
    }

    private void UpdateEqHint(List<int> values, bool on, string preset)
    {
        int boost = Math.Max(0, values.Count > 0 ? values.Max() : 0);
        EqHint.Text = !on
            ? L.T("Spento per queste cuffie: suono originale.", "Off for these headphones: original sound.")
            : (boost > 0 ? L.T($"Il volume viene abbassato di {boost} dB per evitare distorsioni. ", $"The volume is lowered by {boost} dB to avoid distortion. ") : "")
              + (preset == "custom" ? L.T("Trascina le bande (±12 dB).", "Drag the bands (±12 dB).")
                                    : L.T("Scegli «Personalizzato» per regolare le bande.", "Pick “Custom” to adjust the bands."));
    }

    private async void EqSwitch_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await SetEq(new JsonObject { ["on"] = EqSwitch.IsOn });
    }

    private async Task SetEq(JsonObject args)
    {
        if (_address is null)
            return;
        args["address"] = _address;
        await Ui.Run("set_eq", args);
    }
}
