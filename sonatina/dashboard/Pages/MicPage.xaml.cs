using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace SonatinaDashboard.Pages;

public sealed partial class MicPage : Page, IStatusView
{
    private bool _updating;

    public MicPage()
    {
        InitializeComponent();
        Mode.Picked += mode => _ = Commands.SetMicMode(mode);
    }

    private void Root_SizeChanged(object sender, SizeChangedEventArgs e) => Ui.TwoUp(Row1, e.NewSize.Width);

    public void Update(JsonObject s)
    {
        _updating = true;
        try
        {
            var host = Status.Host(s);
            var mic = J.At(s, "host", "mic");
            bool inCall = Status.InCall(s);
            string mode = J.S(mic, "mode") ?? "auto";

            SubTitle.Text = J.S(mic, "status") ?? J.S(s, "app", "tooltip") ?? "";
            CableBar.IsOpen = host is not null && mode != "off" && J.S(mic, "cable") is null;
            Mode.Set(mode);
            Mode.IsHitTestVisible = host is not null;
            var (title, text) = Status.Mic(s);
            MicTitle.Text = title;
            MicText.Text = text;
            MeterHint.Text = Status.MeterHint(s);
            Meter.SetLevel(inCall ? J.D(mic, "level") ?? 0 : 0);
            EndCall.Visibility = inCall && mode == "auto" ? Visibility.Visible : Visibility.Collapsed;
            Hd.IsOn = J.B(s, "host", "options", "hfp_msbc");
            Hd.IsEnabled = host is not null;

            string? codec = J.S(mic, "codec");
            VoiceCodec.Text = !inCall || codec is null ? "-" : codec == "msbc" ? "mSBC 16 kHz" : "CVSD 8 kHz";
            int ok = J.I(mic, "frames_ok") ?? 0, lost = J.I(mic, "frames_lost") ?? 0;
            LostFrames.Text = inCall && codec == "msbc" && ok + lost > 0
                ? $"{lost} ({100.0 * lost / (ok + lost):0.#} %)"
                : "-";
            Cable.Text = J.S(mic, "cable") ?? L.T("non trovato", "not found");
            string apps = Status.Apps(s);
            Apps.Text = apps.Length > 0 ? apps : L.T("nessuna", "none");
        }
        finally
        {
            _updating = false;
        }
    }

    private async void Hd_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_option", new JsonObject { ["name"] = "hfp_msbc", ["value"] = Hd.IsOn });
    }

    private async void EndCall_Click(object sender, RoutedEventArgs e) => await Ui.Run("end_call");
}
