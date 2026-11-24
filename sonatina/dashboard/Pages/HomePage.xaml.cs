using System.Text.Json.Nodes;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;

namespace SonatinaDashboard.Pages;

public sealed partial class HomePage : Page, IStatusView
{
    private bool _updating;
    private DateTime _volumeTouched = DateTime.MinValue;
    private readonly DispatcherQueueTimer _volumeTimer;
    private string _chips = "";
    private string _outputs = "";
    private readonly List<string> _fallbackIds = new();

    public HomePage()
    {
        InitializeComponent();
        Mode.Picked += mode => _ = Commands.SetMicMode(mode);
        _volumeTimer = DispatcherQueue.GetForCurrentThread().CreateTimer();
        _volumeTimer.Interval = TimeSpan.FromMilliseconds(120);
        _volumeTimer.IsRepeating = false;
        _volumeTimer.Tick += async (_, _) =>
            await Ui.Run("set_volume", new JsonObject { ["percent"] = (int)Volume.Value });
    }

    private void Root_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        Ui.TwoUp(Row1, e.NewSize.Width);
        Ui.TwoUp(Row2, e.NewSize.Width);
    }

    public void Update(JsonObject s)
    {
        _updating = true;
        try
        {
            var host = Status.Host(s);
            bool autostart = J.B(s, "app", "autostart");
            string adapter = host is not null
                ? L.T($"chiavetta {Ui.Short(J.S(host, "transport"))} pronta", $"adapter {Ui.Short(J.S(host, "transport"))} ready")
                : J.S(s, "app", "tooltip") ?? "";
            SubTitle.Text = (autostart ? L.T("Sonatina parte con Windows", "Sonatina starts with Windows")
                                       : L.T("Sonatina non parte con Windows", "Sonatina does not start with Windows"))
                            + " · " + adapter;

            // call banner
            bool inCall = Status.InCall(s);
            CallBar.IsOpen = inCall && J.S(s, "host", "mic", "mode") == "auto";
            if (CallBar.IsOpen)
            {
                string apps = Status.Apps(s);
                CallBar.Title = apps.Length > 0
                    ? L.T($"In chiamata: {apps} sta usando il microfono", $"In a call: {apps} is using the microphone")
                    : L.T("In chiamata", "In a call");
                CallBar.Message = L.T("La musica arriva in qualità telefonica e torna in alta qualità da sola quando l’app chiude il microfono.",
                                      "Music plays in phone quality and returns to high quality by itself when the app closes the microphone.");
            }

            // headphones
            var headset = Status.CurrentHeadset(s);
            HeadsetName.Text = J.S(headset, "name") ?? (host is null ? "Sonatina" : L.T("Nessuna cuffia", "No headphones"));
            var (stateText, kind) = Status.State(s);
            StatePill.Set(stateText, kind);
            string? battery = Status.Battery(headset, J.B(headset, "connected"));
            BatteryText.Text = battery ?? "";
            BatteryText.Visibility = battery is null ? Visibility.Collapsed : Visibility.Visible;
            if (J.I(headset, "battery") is int low && low <= 15 && J.B(headset, "connected"))
                BatteryText.Foreground = Ui.Res("SystemFillColorCriticalBrush");
            else
                BatteryText.ClearValue(TextBlock.ForegroundProperty);
            CodecLine.Text = Status.CodecLine(s);
            LatencyLine.Text = Status.LatencyLine(s);
            LatencyLine.Visibility = LatencyLine.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
            var (bars, word, line) = Status.Signal(s);
            SignalPanel.Visibility = bars > 0 ? Visibility.Visible : Visibility.Collapsed;
            SignalLine.Text = line;
            var on = Ui.Res(bars == 1 ? "SystemFillColorCriticalBrush" : "TextFillColorPrimaryBrush");
            var off = Ui.Res("ControlStrongFillColorDisabledBrush");
            Bar1.Fill = bars >= 1 ? on : off;
            Bar2.Fill = bars >= 2 ? on : off;
            Bar3.Fill = bars >= 3 ? on : off;
            Bar4.Fill = bars >= 4 ? on : off;

            UpdateOutput(s);
            UpdateNotices(s);

            var percent = J.I(s, "host", "volume", "percent");
            Volume.IsEnabled = percent is not null;
            if (percent is int p && DateTime.UtcNow - _volumeTouched > TimeSpan.FromSeconds(1.5))
                Volume.Value = p;
            VolumeText.Text = percent is null ? "-" : $"{(int)Volume.Value} %";

            // microphone
            Mode.Set(J.S(s, "host", "mic", "mode") ?? "auto");
            Mode.IsHitTestVisible = host is not null;
            var (micTitle, micText) = Status.Mic(s);
            MicTitle.Text = micTitle;
            MicText.Text = micText;
            MeterHint.Text = Status.MeterHint(s);
            Meter.SetLevel(inCall ? J.D(s, "host", "mic", "level") ?? 0 : 0);
            Hd.IsOn = J.B(s, "host", "options", "hfp_msbc");
            Hd.IsEnabled = host is not null;

            // options
            AbsoluteVolume.IsOn = J.B(s, "host", "options", "absolute_volume");
            LowLatency.IsOn = J.B(s, "host", "low_latency", "active");   // also when an app turned it on
            MediaKeys.IsOn = J.B(s, "host", "options", "media_keys");
            AbsoluteVolume.IsEnabled = LowLatency.IsEnabled = MediaKeys.IsEnabled = host is not null;
            Autostart.IsOn = autostart;

            UpdateChips(s, headset, inCall);

            // link
            var ring = J.I(s, "host", "link", "ring_ms");
            BufferText.Text = ring is int r ? $"{r} ms" : "-";
            SignalText.Text = word;
            LostText.Text = Status.Seconds(J.I(s, "host", "radio", "lost_ms_hour"));
            WifiText.Text = Status.WifiAvoided(s);
        }
        finally
        {
            _updating = false;
        }
    }

    /// <summary>The "Windows output" card and the banner shown while the headphones are off.</summary>
    private void UpdateOutput(JsonObject s)
    {
        var output = J.At(s, "host", "output");
        bool known = output is JsonObject;
        AutoOutput.IsOn = J.B(output, "auto");
        PauseOnDisconnect.IsOn = J.B(output, "pause");
        AutoOutput.IsEnabled = PauseOnDisconnect.IsEnabled = FallbackCombo.IsEnabled = known;

        string? switched = J.S(output, "switched");
        bool playing = J.S(s, "host", "state") == "streaming" || Status.InCall(s);
        OutputBar.IsOpen = switched is not null && !playing;
        if (OutputBar.IsOpen)
        {
            OutputBar.Title = L.T($"Cuffie spente: Windows suona su «{switched}»", $"Headphones off: Windows plays on “{switched}”");
            OutputBar.Message = L.T("Quando le riaccendi l’audio torna da solo su «Sonatina Audio».",
                                    "When you turn them on again the sound goes back to “Sonatina Audio” by itself.");
        }
        string? current = J.S(output, "current");
        OutputNow.Text = current is null ? "" : L.T($"Adesso: {current}", $"Now: {current}");

        // the choices, rebuilt only when the list changes
        string auto = J.S(output, "automatic_name") ?? "";
        string chosen = J.S(output, "fallback_id") ?? "";
        string chosenName = J.S(output, "fallback_name") ?? chosen;
        var items = J.A(output, "outputs");
        string key = auto + "|" + chosen + "|" + string.Join("|", items.Select(i => J.S(i, "id") + "=" + J.S(i, "name")));
        if (key != _outputs)
        {
            _outputs = key;
            FallbackCombo.Items.Clear();
            _fallbackIds.Clear();
            FallbackCombo.Items.Add(auto.Length > 0 ? L.T($"Automatico ({auto})", $"Automatic ({auto})")
                                                    : L.T("Automatico", "Automatic"));
            _fallbackIds.Add("");
            foreach (var item in items)
            {
                FallbackCombo.Items.Add(J.S(item, "name") ?? "?");
                _fallbackIds.Add(J.S(item, "id") ?? "");
            }
            if (chosen.Length > 0 && !_fallbackIds.Contains(chosen))
            {   // the chosen device is not plugged in now: show it anyway
                FallbackCombo.Items.Add(L.T($"{chosenName} (non collegato)", $"{chosenName} (not connected)"));
                _fallbackIds.Add(chosen);
            }
        }
        int index = _fallbackIds.IndexOf(chosen);
        if (FallbackCombo.SelectedIndex != Math.Max(0, index))
            FallbackCombo.SelectedIndex = Math.Max(0, index);
    }

    private async void Fallback_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_updating || FallbackCombo.SelectedIndex < 0 || FallbackCombo.SelectedIndex >= _fallbackIds.Count)
            return;
        await Ui.Run("set_fallback_output", new JsonObject { ["id"] = _fallbackIds[FallbackCombo.SelectedIndex] });
    }

    private void Details_Click(object sender, RoutedEventArgs e) => App.Shell?.Go("diagnostics");

    private void UpdateChips(JsonObject s, JsonNode? headset, bool inCall)
    {
        var supported = J.Strings(headset, "supported").ToHashSet();
        string? inUse = inCall ? null : J.S(s, "host", "stream", "codec");
        string key = string.Join(",", supported) + "|" + inUse;
        if (key == _chips)
            return;
        _chips = key;
        Chips.Children.Clear();
        foreach (var codec in Ui.Chain)
        {
            bool known = supported.Count == 0 || supported.Contains(codec);
            bool active = codec == inUse;
            var text = new TextBlock
            {
                Text = active ? L.T($"{Ui.CodecLabel(codec)} · in uso", $"{Ui.CodecLabel(codec)} · in use") : Ui.CodecLabel(codec),
                FontWeight = active ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Normal,
                Foreground = Ui.Res(active ? "TextOnAccentFillColorPrimaryBrush"
                                    : known ? "TextFillColorPrimaryBrush" : "TextFillColorTertiaryBrush"),
                TextDecorations = known ? Windows.UI.Text.TextDecorations.None : Windows.UI.Text.TextDecorations.Strikethrough,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Chips.Children.Add(new Border
            {
                Height = 28,
                Padding = new Thickness(12, 0, 12, 0),
                CornerRadius = new CornerRadius(14),
                BorderThickness = new Thickness(1),
                BorderBrush = Ui.Res(active ? "AccentFillColorDefaultBrush" : "ControlStrokeColorDefaultBrush"),
                Background = active ? Ui.Res("AccentFillColorDefaultBrush") : null,
                Child = text,
            });
        }
        ChipsHint.Visibility = supported.Count > 0 && supported.Count < Ui.Chain.Length ? Visibility.Visible : Visibility.Collapsed;
    }

    private void Volume_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (VolumeText is null)
            return;
        VolumeText.Text = $"{(int)e.NewValue} %";
        if (_updating)
            return;
        _volumeTouched = DateTime.UtcNow;
        _volumeTimer.Stop();
        _volumeTimer.Start();
    }

    private async void Hd_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_option", new JsonObject { ["name"] = "hfp_msbc", ["value"] = Hd.IsOn });
    }

    private async void Option_Toggled(object sender, RoutedEventArgs e)
    {
        if (_updating || sender is not ToggleSwitch toggle)
            return;
        await Ui.Run("set_option", new JsonObject { ["name"] = (string)toggle.Tag, ["value"] = toggle.IsOn });
    }

    private async void Autostart_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_autostart", new JsonObject { ["value"] = Autostart.IsOn });
    }

    /// <summary>N2 and N4 banners: shown only while they apply, they go away by themselves.</summary>
    private void UpdateNotices(JsonObject s)
    {
        string? held = J.S(s, "host", "conflict", "name");
        ConflictBar.IsOpen = held is not null && J.S(s, "host", "state") != "streaming";
        if (ConflictBar.IsOpen)
        {
            ConflictBar.Title = L.T($"Le {held} sono collegate al Bluetooth di Windows",
                                    $"{held} are connected to Windows' own Bluetooth");
            ConflictBar.Message = L.T("Per questo Sonatina non riesce a raggiungerle. Scollegale da Windows (o rimuovile da lì, se le usi solo con Sonatina) e si collegheranno qui da sole.",
                                      "That is why Sonatina cannot reach them. Disconnect them in Windows (or remove them there, if you only use them with Sonatina) and they will connect here by themselves.");
        }
        string? cable = J.S(s, "host", "output", "cable_name");
        if (!J.B(s, "host", "options", "cable_rename_hint"))
            _cableDismissed = false;       // the host has it off now: the flag is no longer needed
        CableBar.IsOpen = J.B(s, "host", "output", "cable_hint") && cable is not null && !_cableDismissed;
        if (CableBar.IsOpen)
        {
            CableBar.Title = L.T($"Il cavo audio si chiama «{cable}»", $"The audio cable is called “{cable}”");
            CableBar.Message = L.T("Windows l’ha installato con un nome che si confonde con gli altoparlanti veri. Sonatina può chiamarlo «Sonatina Audio» (chiede il permesso di amministratore una volta).",
                                   "Windows installed it with a name easy to mistake for the real speakers. Sonatina can call it “Sonatina Audio” (it asks for administrator permission once).");
        }
    }

    private bool _cableDismissed;

    private async void BluetoothSettings_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await Windows.System.Launcher.LaunchUriAsync(new Uri("ms-settings:bluetooth"));
        }
        catch (Exception)
        {
            await Ui.Run("open_bluetooth_settings");
        }
    }

    private async void RenameCable_Click(object sender, RoutedEventArgs e) => await Ui.Run("rename_cable");

    private async void CableBar_Close(InfoBar sender, object args)
    {
        _cableDismissed = true;            // closed: not again (Informazioni turns it back on)
        await Ui.Run("set_option", new JsonObject { ["name"] = "cable_rename_hint", ["value"] = false });
    }

    private async void EndCall_Click(object sender, RoutedEventArgs e) => await Ui.Run("end_call");

    private void Change_Click(object sender, RoutedEventArgs e) => App.Shell?.Go("headsets");

    private void Codec_Click(object sender, RoutedEventArgs e) => App.Shell?.Go("quality");

    private async void Restart_Click(object sender, RoutedEventArgs e)
    {
        if ((await Ui.Run("restart")).ok)
            App.Shell?.ShowMessage(L.T("Riavvio della connessione Bluetooth…", "Restarting the Bluetooth connection…"));
    }

    private async void OpenLog_Click(object sender, RoutedEventArgs e) => await Ui.Run("open_log");
}

/// <summary>Commands used by more than one page.</summary>
public static class Commands
{
    public static async Task SetMicMode(string mode)
    {
        var (ok, result) = await Ui.Run("set_hfp_mode", new JsonObject { ["mode"] = mode });
        if (ok && J.S(result) == "restarting")
            App.Shell?.ShowMessage(L.T("Sonatina si riavvia per applicare la nuova modalità del microfono (qualche secondo).",
                                      "Sonatina restarts to apply the new microphone mode (a few seconds)."));
    }
}
