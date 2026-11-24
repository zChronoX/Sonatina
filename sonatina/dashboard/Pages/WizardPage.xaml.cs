using System.Text.Json.Nodes;
using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace SonatinaDashboard.Pages;

/// <summary>Setup guide (M6, 1.3.0): adapter, audio output, microphone, headphones. Every
/// check is read from the live status, so a fixed item turns green by itself.</summary>
public sealed partial class WizardPage : Page, IStatusView
{
    private static readonly (string It, string En)[] StepNames =
    [
        ("Chiavetta", "Adapter"),
        ("Uscita audio", "Audio output"),
        ("Microfono", "Microphone"),
        ("Cuffie", "Headphones"),
    ];

    private int _step;
    private JsonObject? _last;
    private string _shown = "";

    private sealed record Check(bool Ok, string Title, string Text, string? Button = null, System.Action? Act = null);

    public WizardPage()
    {
        InitializeComponent();
        Render();
    }

    public void Update(JsonObject s)
    {
        _last = s;
        Render();
    }

    private void Render()
    {
        BuildStepper();
        var (title, intro, checks) = Describe(_step, _last);
        StepTitle.Text = title;
        StepIntro.Text = intro;
        // rebuild only when something changed (keeps buttons under the mouse stable)
        string key = _step + "|" + string.Join("|", checks.Select(c => $"{c.Ok}{c.Title}{c.Text}{c.Button}"));
        if (key != _shown)
        {
            _shown = key;
            Checks.Children.Clear();
            foreach (var c in checks)
                Checks.Children.Add(Row(c));
        }
        BackButton.IsEnabled = _step > 0;
        NextButton.Content = _step == StepNames.Length - 1 ? L.T("Fine", "Done") : L.T("Avanti", "Next");
    }

    private void BuildStepper()
    {
        Stepper.Children.Clear();
        for (int i = 0; i < StepNames.Length; i++)
        {
            bool done = i < _step, current = i == _step;
            var accent = Ui.Res("AccentFillColorDefaultBrush");
            var off = Ui.Res("ControlStrongFillColorDisabledBrush");
            var column = new StackPanel { Spacing = 6 };
            column.Children.Add(new Rectangle { Height = 4, RadiusX = 2, RadiusY = 2, Fill = done || current ? accent : off });
            var dot = new Grid
            {
                Width = 22, Height = 22, CornerRadius = new CornerRadius(11),
                Background = done ? Ui.Res("SystemFillColorSuccessBrush") : current ? accent : off,
            };
            dot.Children.Add(new TextBlock
            {
                Text = done ? "✓" : (i + 1).ToString(), FontSize = 12, FontWeight = FontWeights.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center,
                Foreground = Ui.Res(done || current ? "TextOnAccentFillColorPrimaryBrush" : "TextFillColorPrimaryBrush"),
            });
            var label = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            label.Children.Add(dot);
            label.Children.Add(new TextBlock
            {
                Text = L.T(StepNames[i].It, StepNames[i].En), VerticalAlignment = VerticalAlignment.Center,
                FontWeight = current ? FontWeights.SemiBold : FontWeights.Normal,
            });
            int target = i;
            var button = new Button
            {
                Content = label, Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
                BorderThickness = new Thickness(0), Padding = new Thickness(0), HorizontalAlignment = HorizontalAlignment.Left,
            };
            button.Click += (_, _) => { _step = target; Render(); };
            column.Children.Add(button);
            Grid.SetColumn(column, i);
            Stepper.Children.Add(column);
        }
    }

    private static UIElement Row(Check c)
    {
        var grid = new Grid
        {
            ColumnSpacing = 12, Padding = new Thickness(14, 12, 14, 12), CornerRadius = new CornerRadius(6),
            Background = Ui.Res("SubtleFillColorSecondaryBrush"),
        };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var mark = new Grid
        {
            Width = 22, Height = 22, CornerRadius = new CornerRadius(11), VerticalAlignment = VerticalAlignment.Top,
            Background = Ui.Res(c.Ok ? "SystemFillColorSuccessBrush" : "SystemFillColorCautionBrush"),
        };
        mark.Children.Add(new TextBlock
        {
            Text = c.Ok ? "✓" : "!", FontWeight = FontWeights.Bold,
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center,
            Foreground = Ui.Res("TextOnAccentFillColorPrimaryBrush"),
        });
        grid.Children.Add(mark);
        var text = new StackPanel { Spacing = 2 };
        text.Children.Add(new TextBlock { Text = c.Title, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.WrapWholeWords });
        text.Children.Add(new TextBlock
        {
            Text = c.Text, TextWrapping = TextWrapping.WrapWholeWords,
            Foreground = Ui.Res("TextFillColorSecondaryBrush"),
        });
        Grid.SetColumn(text, 1);
        grid.Children.Add(text);
        if (c.Button is not null && c.Act is not null)
        {
            var button = new Button { Content = c.Button, VerticalAlignment = VerticalAlignment.Center };
            button.Click += (_, _) => c.Act();
            Grid.SetColumn(button, 2);
            grid.Children.Add(button);
        }
        return grid;
    }

    private static (string Title, string Intro, List<Check> Checks) Describe(int step, JsonObject? s)
    {
        var host = s is null ? null : Status.Host(s);
        var checks = new List<Check>();
        switch (step)
        {
            case 0:
            {
                bool ready = host is not null;
                checks.Add(ready
                    ? new Check(true, L.T("Chiavetta pronta", "Adapter ready"),
                        L.T($"{Ui.Short(J.S(host, "transport"))}: driver WinUSB, firmware caricato.",
                            $"{Ui.Short(J.S(host, "transport"))}: WinUSB driver, firmware loaded."))
                    : new Check(false, L.T("Chiavetta non trovata", "Adapter not found"),
                        L.T("Collegala alla porta USB. Se è nuova, riavvia l’installer di Sonatina per dedicarla.",
                            "Plug it into a USB port. If it is a new one, run the Sonatina installer again to dedicate it.")));
                bool? winBt = s is null ? null : J.At(s, "host", "setup", "windows_bt") is JsonValue v && v.TryGetValue(out bool b) ? b : null;
                if (winBt == true)
                    checks.Add(new Check(true, L.T("Windows ha un altro Bluetooth", "Windows has another Bluetooth"),
                        L.T("Resta a Windows per mouse, tastiere e telefoni. Non abbinarci le cuffie che usi con Sonatina.",
                            "It stays with Windows for mice, keyboards and phones. Do not pair it with the headphones you use with Sonatina.")));
                else if (winBt == false)
                    checks.Add(new Check(true, L.T("Nessun altro Bluetooth su Windows", "No other Bluetooth on Windows"),
                        L.T("Va bene: Sonatina ha la sua chiavetta. Per mouse e tastiere Bluetooth servirebbe una seconda chiavetta.",
                            "That is fine: Sonatina has its own adapter. Bluetooth mice and keyboards would need a second adapter.")));
                return (L.T("Chiavetta Bluetooth", "Bluetooth adapter"),
                        L.T("Sonatina usa una chiavetta USB tutta sua: Windows non la vede più come Bluetooth.",
                            "Sonatina uses a USB adapter of its own: Windows no longer sees it as Bluetooth."), checks);
            }
            case 1:
            {
                string? cable = J.S(host, "output", "cable_name");
                if (cable is null)
                    checks.Add(new Check(false, L.T("VB-CABLE non trovato", "VB-CABLE not found"),
                        L.T("Sonatina riceve l’audio di Windows da questo cavo virtuale gratuito. Installalo e riavvia il PC.",
                            "Sonatina gets the sound of Windows from this free virtual cable. Install it and restart the PC."),
                        L.T("Scarica VB-CABLE", "Download VB-CABLE"), () => Open("https://vb-audio.com/Cable/")));
                else if (J.B(host, "output", "cable_renamed"))
                    checks.Add(new Check(true, L.T("Nome: «Sonatina Audio»", "Name: “Sonatina Audio”"),
                        L.T("Così non si confonde con gli altoparlanti veri.", "So it is not mistaken for the real speakers.")));
                else
                    checks.Add(new Check(false, L.T($"Nome attuale: «{cable}»", $"Current name: “{cable}”"),
                        L.T("Si confonde con gli altoparlanti veri. Windows chiederà il permesso di amministratore.",
                            "Easy to mistake for the real speakers. Windows will ask for administrator permission."),
                        L.T("Rinomina in «Sonatina Audio»", "Rename to “Sonatina Audio”"), () => _ = Ui.Run("rename_cable")));
                bool auto = J.B(host, "output", "auto");
                string? target = J.S(host, "output", "target");
                checks.Add(auto
                    ? new Check(true, L.T("Uscita automatica attiva", "Automatic output on"),
                        L.T($"Cuffie accese: Sonatina Audio. Spente: {target ?? "l’uscita che usavi prima"}.",
                            $"Headphones on: Sonatina Audio. Off: {target ?? "the output you used before"}."))
                    : new Check(false, L.T("Uscita automatica spenta", "Automatic output off"),
                        L.T("Windows resta sul cavo anche a cuffie spente: niente audio finché non cambi uscita a mano.",
                            "Windows stays on the cable with the headphones off: no sound until you change output by hand."),
                        L.T("Attiva", "Turn on"), () => _ = Ui.Run("set_option", new JsonObject { ["name"] = "auto_output", ["value"] = true })));
                return (L.T("Uscita audio di Windows", "Windows audio output"),
                        L.T("Windows suona su un cavo virtuale (VB-CABLE); Sonatina lo prende e lo manda alle cuffie.",
                            "Windows plays to a virtual cable (VB-CABLE); Sonatina takes it and sends it to the headphones."), checks);
            }
            case 2:
            {
                string mode = J.S(host, "mic", "mode") ?? "auto";
                string? mic = J.S(host, "mic", "cable");
                if (mode == "off")
                    checks.Add(new Check(true, L.T("Microfono spento", "Microphone off"),
                        L.T("Solo musica: va benissimo. Lo accendi da Microfono quando ti serve.",
                            "Music only: perfectly fine. Turn it on from Microphone when you need it.")));
                else if (mic is null)
                    checks.Add(new Check(false, L.T("Hi-Fi Cable non trovato", "Hi-Fi Cable not found"),
                        L.T("Serve solo per usare il microfono delle cuffie nelle app (gratuito, di VB-Audio).",
                            "Only needed to use the headset microphone in apps (free, by VB-Audio)."),
                        L.T("Scarica Hi-Fi Cable", "Download Hi-Fi Cable"), () => Open("https://vb-audio.com/Cable/")));
                else
                    checks.Add(new Check(true, L.T("Cavo del microfono pronto", "Microphone cable ready"),
                        L.T($"Le app usano «{mic}» come microfono.", $"Apps use “{mic}” as the microphone.")));
                if (mode != "off")
                    checks.Add(new Check(true, mode == "auto" ? L.T("Modalità: Automatico", "Mode: Automatic") : L.T("Modalità: Sempre attivo", "Mode: Always on"),
                        mode == "auto" ? L.T("Le cuffie passano in chiamata quando un’app apre il microfono.", "The headphones switch to call mode when an app opens the microphone.")
                                       : L.T("Le cuffie restano in modalità chiamata.", "The headphones stay in call mode.")));
                return (L.T("Microfono delle cuffie (facoltativo)", "Headset microphone (optional)"),
                        L.T("Le app vedono il microfono delle cuffie tramite un secondo cavo virtuale gratuito, Hi-Fi Cable.",
                            "Apps see the headset microphone through a second free virtual cable, Hi-Fi Cable."), checks);
            }
            default:
            {
                foreach (var h in J.A(host, "headsets"))
                {
                    if (!J.B(h, "paired"))
                        continue;
                    var parts = new List<string> { L.T("Abbinate", "Paired") };
                    if (J.B(h, "connected"))
                        parts.Add(L.T("collegate", "connected"));
                    parts.Add(Ui.CodecLabel(J.S(h, "codec")));
                    if (J.I(h, "battery") is int b)
                        parts.Add(L.T($"batteria {b} %", $"battery {b} %"));
                    checks.Add(new Check(true, J.S(h, "name") ?? J.S(h, "address") ?? "?", string.Join(" · ", parts)));
                }
                bool none = checks.Count == 0;
                checks.Add(new Check(!none, none ? L.T("Nessuna cuffia abbinata", "No headphones paired") : L.T("Altre cuffie?", "Other headphones?"),
                    L.T("Mettile in modalità abbinamento.", "Put them in pairing mode."),
                    L.T("Abbina nuove cuffie", "Pair new headphones"), () => App.Shell?.Go("headsets")));
                return (L.T("Cuffie", "Headphones"),
                        L.T("Le cuffie abbinate si collegano da sole quando le accendi.",
                            "Paired headphones connect by themselves when you turn them on."), checks);
            }
        }
    }

    private static async void Open(string url)
    {
        try
        {
            await Windows.System.Launcher.LaunchUriAsync(new Uri(url));
        }
        catch (Exception error)
        {
            Log.Write("open " + url + ": " + error.Message);
        }
    }

    private async void Finish()
    {
        await Ui.Run("set_option", new JsonObject { ["name"] = "setup_done", ["value"] = true });
        _step = 0;
        App.Shell?.Go("home");
    }

    private void Skip_Click(object sender, RoutedEventArgs e) => Finish();

    private void Back_Click(object sender, RoutedEventArgs e)
    {
        _step = Math.Max(0, _step - 1);
        Render();
    }

    private void Next_Click(object sender, RoutedEventArgs e)
    {
        if (_step == StepNames.Length - 1)
        {
            Finish();
            return;
        }
        _step++;
        Render();
    }
}
