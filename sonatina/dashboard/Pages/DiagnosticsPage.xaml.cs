using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
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
    }

    public void Update(JsonObject s)
    {
        SubTitle.Text = $"Sonatina {J.S(s, "app", "version") ?? "?"} · " + (J.S(s, "host", "summary") ?? J.S(s, "app", "tooltip") ?? "");
        for (int i = 0; i < _metrics.Length; i++)
            _values[i].Text = _metrics[i].Value(s);
        string? call = J.S(s, "host", "link", "call");
        CallStats.Text = call is null ? "" : L.T("Chiamata: ", "Call: ") + call;
        CallStats.Visibility = call is null ? Visibility.Collapsed : Visibility.Visible;
        _raw = s.ToJsonString(Pretty);
        Raw.Text = _raw;
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
