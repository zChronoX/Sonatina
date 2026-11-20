using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace SonatinaDashboard.Pages;

public sealed partial class HeadsetsPage : Page, IStatusView
{
    private string _listKey = "";
    private string _foundKey = "";
    private bool _searching;

    public HeadsetsPage()
    {
        InitializeComponent();
    }

    public void Update(JsonObject s)
    {
        var host = Status.Host(s);
        PairButton.IsEnabled = host is not null;

        // paired headphones
        var headsets = J.A(s, "host", "headsets");
        string listKey = (headsets.ToJsonString()) + "|" + J.S(s, "host", "stream", "name")
                         + J.S(s, "host", "stream", "kbps") + Status.InCall(s) + J.S(s, "host", "state");
        if (listKey != _listKey)
        {
            _listKey = listKey;
            HeadsetList.Children.Clear();
            foreach (var h in headsets)
                HeadsetList.Children.Add(HeadsetRow(s, h));
            Empty.Visibility = headsets.Count == 0 && host is not null ? Visibility.Visible : Visibility.Collapsed;
        }

        // pairing
        bool discovering = J.B(host, "discovering");
        string? pairing = J.S(host, "pairing");
        var found = J.A(host, "found");
        if (discovering || pairing is not null)
            PairCard.Visibility = Visibility.Visible;
        PairProgress.Visibility = discovering || pairing is not null ? Visibility.Visible : Visibility.Collapsed;
        SearchAgain.Visibility = discovering || pairing is not null ? Visibility.Collapsed : Visibility.Visible;
        if (pairing is not null)
            PairText.Text = L.T($"Abbinamento con {NameOf(s, pairing)}…", $"Pairing with {NameOf(s, pairing)}…");
        else if (discovering)
            PairText.Text = L.T("Metti le cuffie in modalità abbinamento (di solito tieni premuto il tasto finché la luce lampeggia). Ricerca in corso…",
                                "Put the headphones in pairing mode (usually hold the button until the light blinks). Searching…");
        else if (found.Count == 0)
            PairText.Text = L.T("Nessuna cuffia trovata. Controlla che siano in modalità abbinamento e cerca di nuovo.",
                                "No headphones found. Check that they are in pairing mode and search again.");
        else
            PairText.Text = L.T("Scegli le cuffie da abbinare:", "Choose the headphones to pair:");

        string foundKey = found.ToJsonString() + pairing;
        if (foundKey != _foundKey)
        {
            _foundKey = foundKey;
            FoundList.Children.Clear();
            foreach (var f in found)
            {
                string address = J.S(f, "address") ?? "";
                string name = J.S(f, "name") ?? address;
                var row = new Grid { ColumnSpacing = 12, MinHeight = 40 };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var label = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
                label.Children.Add(new TextBlock { Text = name });
                label.Children.Add(new TextBlock { Text = address, Style = (Style)Application.Current.Resources["Hint"] });
                row.Children.Add(label);
                var pair = new Button
                {
                    Content = L.T("Abbina", "Pair"),
                    VerticalAlignment = VerticalAlignment.Center,
                    IsEnabled = pairing is null,
                };
                pair.Click += async (_, _) => await Pair(address, name);
                Grid.SetColumn(pair, 1);
                row.Children.Add(pair);
                FoundList.Children.Add(row);
            }
        }
    }

    private static string NameOf(JsonObject s, string address)
    {
        foreach (var f in J.A(s, "host", "found").Concat(J.A(s, "host", "headsets")))
            if (J.S(f, "address") == address)
                return J.S(f, "name") ?? address;
        return address;
    }

    private UIElement HeadsetRow(JsonObject s, JsonNode? h)
    {
        string address = J.S(h, "address") ?? "";
        string name = J.S(h, "name") ?? address;
        bool connected = J.B(h, "connected");
        bool preferred = J.B(h, "preferred");

        var parts = new List<string>();
        if (connected)
        {
            parts.Add(L.T("Collegate", "Connected"));
            if (Status.InCall(s))
                parts.Add(L.T("in chiamata", "in a call"));
            else if (J.S(s, "host", "stream", "name") is string stream)
                parts.Add(J.I(s, "host", "stream", "kbps") is int kbps ? $"{stream} {kbps} kbit/s" : stream);
        }
        else
        {
            parts.Add(L.T("Spente o lontane", "Off or out of range"));
        }
        string codec = Ui.CodecLabel(Ui.Effective(J.S(h, "codec"), J.Strings(h, "supported").ToHashSet()));
        parts.Add(L.T($"codec preferito {codec}", $"preferred codec {codec}"));
        if (J.B(h, "msbc_broken"))
            parts.Add(L.T("microfono in CVSD (mSBC non funziona)", "microphone in CVSD (mSBC does not work)"));

        var card = new Border
        {
            Style = (Style)Application.Current.Resources["Card"],
            Padding = new Thickness(20, 14, 20, 14),
        };
        var grid = new Grid { ColumnSpacing = 16 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var badge = new Border
        {
            Width = 40,
            Height = 40,
            CornerRadius = new CornerRadius(20),
            VerticalAlignment = VerticalAlignment.Center,
            Background = Ui.Res(connected ? "AccentFillColorDefaultBrush" : "SubtleFillColorSecondaryBrush"),
            Child = new FontIcon
            {
                Glyph = "",
                FontSize = 18,
                Foreground = Ui.Res(connected ? "TextOnAccentFillColorPrimaryBrush" : "TextFillColorSecondaryBrush"),
            },
        };
        grid.Children.Add(badge);

        var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Spacing = 2 };
        text.Children.Add(new TextBlock { Text = name, Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"] });
        text.Children.Add(new TextBlock { Text = string.Join(" · ", parts), Style = (Style)Application.Current.Resources["Secondary"] });
        Grid.SetColumn(text, 1);
        grid.Children.Add(text);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        if (preferred)
        {
            var pill = new Pill();
            pill.Set(L.T("Preferite", "Preferred"), "success");
            buttons.Children.Add(pill);
        }
        else
        {
            var use = new Button { Content = L.T("Usa queste", "Use these") };
            use.Click += async (_, _) =>
            {
                if ((await Ui.Run("select", new JsonObject { ["address"] = address })).ok)
                    App.Shell?.ShowMessage(L.T($"Sonatina passa a {name}: accendile se sono spente.",
                                              $"Sonatina switches to {name}: turn them on if they are off."));
            };
            buttons.Children.Add(use);
        }
        var forget = new Button { Content = L.T("Dimentica", "Forget") };
        forget.Click += async (_, _) => await Forget(address, name);
        buttons.Children.Add(forget);
        Grid.SetColumn(buttons, 2);
        grid.Children.Add(buttons);

        card.Child = grid;
        return card;
    }

    private async Task Forget(string address, string name)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = L.T($"Dimenticare {name}?", $"Forget {name}?"),
            Content = L.T("Per usarle di nuovo dovrai abbinarle un’altra volta.",
                          "To use them again you will have to pair them again."),
            PrimaryButtonText = L.T("Dimentica", "Forget"),
            CloseButtonText = L.T("Annulla", "Cancel"),
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dialog.ShowAsync() == ContentDialogResult.Primary)
            await Ui.Run("forget", new JsonObject { ["address"] = address });
    }

    private async void Pair_Click(object sender, RoutedEventArgs e)
    {
        if (_searching)
            return;
        _searching = true;
        PairCard.Visibility = Visibility.Visible;
        PairProgress.Visibility = Visibility.Visible;
        SearchAgain.Visibility = Visibility.Collapsed;
        PairText.Text = L.T("Ricerca in corso…", "Searching…");
        try
        {
            await Ui.Run("discover", new JsonObject { ["seconds"] = 12 }, 40000);
        }
        finally
        {
            _searching = false;
        }
    }

    private async Task Pair(string address, string name)
    {
        var (ok, result) = await Ui.Run("pair", new JsonObject { ["address"] = address, ["name"] = name }, 90000);
        if (!ok)
            return;
        if (J.B(result))
        {
            App.Shell?.ShowMessage(L.T($"{name} abbinate.", $"{name} paired."), InfoBarSeverity.Success);
            PairCard.Visibility = Visibility.Collapsed;
        }
        else
        {
            App.Shell?.ShowError(L.T("Abbinamento non riuscito: rimetti le cuffie in modalità abbinamento e riprova.",
                                    "Pairing failed: put the headphones back in pairing mode and try again."));
        }
    }

    private void ClosePair_Click(object sender, RoutedEventArgs e) => PairCard.Visibility = Visibility.Collapsed;
}
