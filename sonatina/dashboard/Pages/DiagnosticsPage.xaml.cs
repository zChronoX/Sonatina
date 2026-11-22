using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Shapes;
using Windows.ApplicationModel.DataTransfer;

namespace SonatinaDashboard.Pages;

public sealed partial class DiagnosticsPage : Page, IStatusView
{
    private static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true };

    private readonly (string It, string En, Func<JsonObject, string> Value)[] _metrics =
    [
        ("Stato", "State", s => J.S(s, "host", "state") ?? "-"),
        ("Buffer", "Buffer", s => J.I(s, "host", "link", "ring_ms") is int v ? $"{v} ms" : "-"),
        ("Obiettivo", "Target", s => J.I(s, "host", "link", "target_ms") is int v ? $"{v} ms" : "-"),
        ("Margine", "Margin", s => J.D(s, "host", "link", "margin_ms") is double v ? $"{v:0.#} ms" : "-"),
        ("Deriva del clock", "Clock drift", s => J.I(s, "host", "link", "rate_ppm") is int v ? $"{Ui.Signed(v)} ppm" : "-"),
        ("Interruzioni", "Dropouts", s => J.I(s, "host", "link", "underruns")?.ToString() ?? "-"),
        ("Pacchetti", "Packets", s => J.I(s, "host", "link", "packets")?.ToString() ?? "-"),
        ("Scartati (radio lenta)", "Dropped (slow radio)", s => J.I(s, "host", "link", "radio_dropped")?.ToString() ?? "-"),
        ("Codec in uso", "Codec in use", s => J.S(s, "host", "stream", "name") is string n
            ? (J.I(s, "host", "stream", "kbps") is int k ? $"{n} {k}" : n) : "-"),
        ("Ritardo cuffie", "Headphone delay", s => J.I(s, "host", "stream", "headset_delay_ms") is int v ? $"{v} ms" : "-"),
        ("Chiavetta", "Adapter", s => Ui.Short(J.S(s, "host", "transport"))),
    ];

    private readonly TextBlock[] _values;
    private string _raw = "";
    private bool _updating;
    private string? _exportPath;
    private JsonNode? _history;
    private string _historyKey = "";
    private string _rssiUnit = "dB";
    private readonly Rectangle[] _cells = new Rectangle[79];

    public DiagnosticsPage()
    {
        InitializeComponent();
        const int columns = 3;
        for (int c = 0; c < columns; c++)
            Metrics.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        _values = new TextBlock[_metrics.Length];
        for (int i = 0; i < _metrics.Length; i++)
        {
            if (i % columns == 0)
                Metrics.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var cell = new StackPanel();
            cell.Children.Add(new TextBlock
            {
                Text = L.T(_metrics[i].It, _metrics[i].En),
                Style = (Style)Application.Current.Resources["Hint"],
            });
            _values[i] = new TextBlock { Style = (Style)Application.Current.Resources["MetricValue"] };
            cell.Children.Add(_values[i]);
            Grid.SetRow(cell, i / columns);
            Grid.SetColumn(cell, i % columns);
            Metrics.Children.Add(cell);
        }
        for (int k = 0; k < _cells.Length; k++)
        {
            Strip.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            _cells[k] = new Rectangle { RadiusX = 2, RadiusY = 2 };
            Grid.SetColumn(_cells[k], k);
            Strip.Children.Add(_cells[k]);
        }
        WifiChannel.Items.Add(L.T("Automatico (dalle reti Wi-Fi vicine)", "Automatic (from the nearby Wi-Fi networks)"));
        for (int c = 1; c <= 13; c++)
            WifiChannel.Items.Add(L.T($"Canale {c}", $"Channel {c}"));
    }

    public void Update(JsonObject s)
    {
        _updating = true;
        try
        {
            UpdateRadio(s);
        }
        finally
        {
            _updating = false;
        }
        SubTitle.Text = $"Sonatina {J.S(s, "app", "version") ?? "?"} · " + (J.S(s, "host", "summary") ?? J.S(s, "app", "tooltip") ?? "");
        for (int i = 0; i < _metrics.Length; i++)
            _values[i].Text = _metrics[i].Value(s);
        string? call = J.S(s, "host", "link", "call");
        CallStats.Text = call is null ? "" : L.T("Chiamata: ", "Call: ") + call;
        CallStats.Visibility = call is null ? Visibility.Collapsed : Visibility.Visible;
        _raw = s.ToJsonString(Pretty);
        Raw.Text = _raw;
    }

    private void UpdateRadio(JsonObject s)
    {
        var radio = J.At(s, "host", "radio");
        var (_, word, _) = Status.Signal(s);
        LevelText.Text = word;
        RssiText.Text = Status.Power(radio) ?? "-";
        QualityText.Text = J.I(radio, "link_quality") is int q ? $"{q} / 255" : "-";
        LostText.Text = $"{Status.Seconds(J.I(radio, "lost_ms_minute"))} / {Status.Seconds(J.I(radio, "lost_ms_hour"))}";
        _rssiUnit = J.S(radio, "rssi_unit") ?? "dB";
        LineLegend.Text = _rssiUnit == "dBm" ? L.T("potenza (dBm)", "power (dBm)")
                                             : L.T("potenza (dB rispetto alla norma)", "power (dB from the ideal range)");
        _history = J.At(radio, "history");
        string key = _history?.ToJsonString() ?? "";
        if (key != _historyKey)
        {
            _historyKey = key;
            DrawChart();
        }

        // Wi-Fi and the hopping map
        var wifi = J.At(radio, "wifi");
        bool enabled = J.B(wifi, "enabled");
        int manual = J.I(wifi, "manual") ?? 0;
        WifiAvoid.IsEnabled = WifiChannel.IsEnabled = radio is not null;
        WifiAvoid.IsOn = enabled;
        WifiChannel.IsEnabled = radio is not null && enabled;
        if (WifiChannel.SelectedIndex != Math.Clamp(manual, 0, 13))
            WifiChannel.SelectedIndex = Math.Clamp(manual, 0, 13);
        var avoid = J.Strings(wifi, "avoid").ToList();
        int avoided = J.I(radio, "afh", "avoided") ?? 0;
        string info;
        if (radio is null)
            info = L.T("Il Bluetooth di Sonatina non è attivo.", "Sonatina's Bluetooth is not running.");
        else if (!enabled)
            info = L.T("Spento: il Bluetooth usa tutti i 79 canali (la chiavetta evita da sola solo quelli che misura come rumorosi).",
                       "Off: Bluetooth uses all 79 channels (the adapter only avoids those it measures as noisy).");
        else if (manual > 0)
            info = L.T($"Canale {manual} scelto a mano: {avoided} canali Bluetooth su 79 evitati.",
                       $"Channel {manual} chosen by hand: {avoided} of 79 Bluetooth channels avoided.");
        else
        {
            var nets = J.A(wifi, "networks").Where(n => J.S(n, "band") == "2.4")
                .Select(n => $"{J.S(n, "ssid")} ({L.T("canale", "channel")} {J.I(n, "channel")}, {J.I(n, "signal")}%"
                             + (J.B(n, "pc") ? L.T(", la rete del PC", ", the PC's network") : "") + ")")
                .Take(4).ToList();
            string? error = J.S(wifi, "error");
            if (nets.Count == 0)
                info = error is not null
                    ? L.T($"Reti Wi-Fi non leggibili ({error}): scegli il canale qui sopra.",
                          $"Wi-Fi networks not readable ({error}): choose the channel above.")
                    : L.T("Nessuna rete a 2,4 GHz vicina: niente da evitare.", "No 2.4 GHz network nearby: nothing to avoid.");
            else
                info = L.T("Reti a 2,4 GHz: ", "2.4 GHz networks: ") + string.Join(", ", nets) + ". "
                       + (avoid.Count > 0
                           ? L.T($"Evitati {avoided} canali Bluetooth su 79 (Wi-Fi {string.Join(", ", avoid)}).",
                                 $"{avoided} of 79 Bluetooth channels avoided (Wi-Fi {string.Join(", ", avoid)}).")
                           : L.T("Nessuna abbastanza forte da evitare.", "None strong enough to avoid."));
        }
        if (J.S(radio, "role") == "peripheral" && enabled)
            info += L.T(" Le cuffie hanno il ruolo centrale: i canali li sceglie la loro radio.",
                        " The headphones hold the central role: their radio chooses the channels.");
        if (J.S(radio, "afh", "error") is string afhError)
            info += L.T($" La chiavetta non accetta la mappa ({afhError}).", $" The adapter refuses the map ({afhError}).");
        WifiInfo.Text = info;

        string host = J.S(radio, "afh", "host_map") ?? "";
        string? used = J.S(radio, "afh", "link_map");
        for (int k = 0; k < _cells.Length; k++)
        {
            bool ours = k < host.Length && host[k] == '0';
            bool dropped = used is not null && k < used.Length && used[k] == '0';
            _cells[k].Fill = Ui.Res(ours ? "SystemFillColorCautionBrush"
                                    : dropped ? "ControlStrongFillColorDisabledBrush" : "AccentFillColorDefaultBrush");
        }

        // interference banner
        InterferenceBar.IsOpen = J.B(radio, "interference");
        if (InterferenceBar.IsOpen)
        {
            InterferenceBar.Title = L.T("Audio perso con un segnale forte: disturbo radio, non distanza",
                                        "Audio lost with a strong signal: radio interference, not distance");
            InterferenceBar.Message = L.T("Di solito è un Wi-Fi a 2,4 GHz vicino alla chiavetta (telefono, router). ",
                                          "Usually a 2.4 GHz Wi-Fi next to the adapter (phone, router). ")
                + (avoid.Count > 0
                    ? L.T($"Sonatina sta già evitando il canale {string.Join(", ", avoid)}; se continua, avvicina la chiavetta alle cuffie o porta il telefono sui 5 GHz.",
                          $"Sonatina already avoids channel {string.Join(", ", avoid)}; if it goes on, move the adapter closer to the headphones or the phone to 5 GHz.")
                    : L.T("Attiva «Evita i canali del Wi-Fi» qui sotto, avvicina la chiavetta alle cuffie o porta il telefono sui 5 GHz.",
                          "Turn on “Avoid the Wi-Fi channels” below, move the adapter closer to the headphones or the phone to 5 GHz."));
        }
    }

    private void Chart_SizeChanged(object sender, SizeChangedEventArgs e) => DrawChart();

    /// <summary>RSSI line and audio lost bars, one point per 5 s over 10 minutes.</summary>
    private void DrawChart()
    {
        Chart.Children.Clear();
        double w = Chart.ActualWidth, h = Chart.Height;
        if (w <= 60 || _history is null)
            return;
        var rssi = J.A(_history, "rssi");
        var lost = J.A(_history, "lost_ms");
        int n = Math.Max(1, Math.Max(rssi.Count, lost.Count));
        const double left = 36, top = 8;
        double plotW = w - left - 4, plotH = h - 28;
        bool dbm = _rssiUnit == "dBm";
        double hi = dbm ? -40 : 5, lo = dbm ? -95 : -30;
        double Y(double v) => top + (hi - Math.Clamp(v, lo, hi)) / (hi - lo) * plotH;
        var grid = Ui.Res("DividerStrokeColorDefaultBrush");
        var hint = (Style)Application.Current.Resources["Hint"];
        foreach (double v in dbm ? new[] { -40.0, -60, -80 } : new[] { 0.0, -10, -20 })
        {
            double y = Y(v);
            Chart.Children.Add(new Line { X1 = left, Y1 = y, X2 = w, Y2 = y, Stroke = grid, StrokeThickness = 1 });
            var label = new TextBlock { Text = v.ToString("0"), Style = hint };
            Canvas.SetLeft(label, 0);
            Canvas.SetTop(label, y - 9);
            Chart.Children.Add(label);
        }
        double maxLost = Math.Max(1000, lost.Select(x => J.D(x) ?? 0).DefaultIfEmpty(0).Max());
        double step = plotW / n;
        var red = Ui.Res("SystemFillColorCriticalBrush");
        for (int i = 0; i < lost.Count; i++)
        {
            double ms = J.D(lost[i]) ?? 0;
            if (ms <= 0)
                continue;
            double bh = Math.Max(3, ms / maxLost * plotH * 0.7);
            var bar = new Rectangle { Width = Math.Max(2, step - 1), Height = bh, Fill = red };
            Canvas.SetLeft(bar, left + i * step);
            Canvas.SetTop(bar, top + plotH - bh);
            Chart.Children.Add(bar);
        }
        var accent = Ui.Res("AccentFillColorDefaultBrush");
        Polyline? line = null;
        for (int i = 0; i < rssi.Count; i++)
        {
            if (J.D(rssi[i]) is not double v)
            {
                line = null;          // a gap: not connected
                continue;
            }
            if (line is null)
            {
                line = new Polyline { Stroke = accent, StrokeThickness = 2 };
                Chart.Children.Add(line);
            }
            line.Points.Add(new Windows.Foundation.Point(left + (i + 0.5) * step, Y(v)));
        }
        var start = new TextBlock { Text = L.T("−10 min", "−10 min"), Style = hint };
        Canvas.SetLeft(start, left);
        Canvas.SetTop(start, h - 18);
        Chart.Children.Add(start);
        var now = new TextBlock { Text = L.T("ora", "now"), Style = hint };
        Canvas.SetLeft(now, w - 28);
        Canvas.SetTop(now, h - 18);
        Chart.Children.Add(now);
    }

    private async void Export_Click(object sender, RoutedEventArgs e)
    {
        ExportButton.IsEnabled = false;
        try
        {
            var (ok, result) = await Ui.Run("export_diagnostics", null, 60000);
            if (ok && J.S(result) is string path)
            {
                _exportPath = path;
                ExportBar.Title = L.T("Diagnostica salvata", "Diagnostics saved");
                ExportBar.Message = path;
                ExportBar.IsOpen = true;
            }
        }
        finally
        {
            ExportButton.IsEnabled = true;
        }
    }

    private void Reveal_Click(object sender, RoutedEventArgs e)
    {
        if (_exportPath is null)
            return;
        try
        {
            Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{_exportPath}\"") { UseShellExecute = true });
        }
        catch (Exception error)
        {
            App.Shell?.ShowError(error.Message);
        }
    }

    private async void WifiAvoid_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_option", new JsonObject { ["name"] = "wifi_avoid", ["value"] = WifiAvoid.IsOn });
    }

    private async void WifiChannel_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_updating && WifiChannel.SelectedIndex >= 0)
            await Ui.Run("set_wifi_channel", new JsonObject { ["channel"] = WifiChannel.SelectedIndex });
    }

    private async void Restart_Click(object sender, RoutedEventArgs e)
    {
        if ((await Ui.Run("restart")).ok)
            App.Shell?.ShowMessage(L.T("Riavvio della connessione Bluetooth…", "Restarting the Bluetooth connection…"));
    }

    private async void OpenLog_Click(object sender, RoutedEventArgs e) => await Ui.Run("open_log");

    private void Folder_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo(Log.Folder) { UseShellExecute = true });
        }
        catch (Exception error)
        {
            App.Shell?.ShowError(error.Message);
        }
    }

    private void Copy_Click(object sender, RoutedEventArgs e)
    {
        var package = new DataPackage();
        package.SetText(_raw);
        Clipboard.SetContent(package);
        App.Shell?.ShowMessage(L.T("Stato copiato negli appunti.", "Status copied to the clipboard."), InfoBarSeverity.Success);
    }
}
