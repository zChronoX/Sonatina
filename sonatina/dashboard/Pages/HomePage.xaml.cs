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
            CodecLine.Text = Status.CodecLine(s);
            LatencyLine.Text = Status.LatencyLine(s);
            LatencyLine.Visibility = LatencyLine.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

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
            LowLatency.IsOn = J.B(s, "host", "options", "low_latency");
            MediaKeys.IsOn = J.B(s, "host", "options", "media_keys");
            AbsoluteVolume.IsEnabled = LowLatency.IsEnabled = MediaKeys.IsEnabled = host is not null;
            Autostart.IsOn = autostart;

            UpdateChips(s, headset, inCall);

            // link
            var ring = J.I(s, "host", "link", "ring_ms");
            BufferText.Text = ring is int r ? $"{r} ms" : "-";
            var ppm = J.I(s, "host", "link", "rate_ppm");
            DriftText.Text = ppm is int d ? $"{Ui.Signed(d)} ppm" : "-";
            UnderrunText.Text = J.I(s, "host", "link", "underruns")?.ToString() ?? "-";
            AdapterText.Text = Ui.Short(J.S(s, "host", "transport"));
        }
        finally
        {
            _updating = false;
        }
    }

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
