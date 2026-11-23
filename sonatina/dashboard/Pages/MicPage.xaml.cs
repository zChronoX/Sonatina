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
        Strength.Items.Add(L.T("Normale", "Normal"));
        Strength.Items.Add(L.T("Forte", "Strong"));
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

            // voice cleaning (1.2.0)
            var voice = J.At(mic, "voice");
            bool available = host is not null && J.B(voice, "available");
            string denoise = J.S(voice, "denoise") ?? "normal";
            Denoise.IsOn = denoise != "off";
            Strength.SelectedIndex = denoise == "strong" ? 1 : 0;
            Strength.IsEnabled = available && Denoise.IsOn;
            Agc.IsOn = J.B(voice, "agc");
            Gate.IsOn = J.B(voice, "gate");
            BatteryOption.IsOn = J.B(s, "host", "options", "hfp_battery");
            Denoise.IsEnabled = Agc.IsEnabled = Gate.IsEnabled = available;
            BatteryOption.IsEnabled = host is not null;
            MeterIn.SetLevel(inCall ? J.D(voice, "level_in") ?? 0 : 0);
            MeterOut.SetLevel(inCall ? J.D(mic, "level") ?? 0 : 0);
            if (!available)
                VoiceInfo.Text = L.T("Non disponibile con questa versione della libreria dei codec.",
                                     "Not available with this version of the codec library.");
            else if (!inCall)
                VoiceInfo.Text = L.T("Si attiva solo in chiamata.", "Works during calls only.");
            else
            {
                double vad = J.D(voice, "vad") ?? 0;
                string gain = J.D(voice, "gain_db") is double g ? $"{(g >= 0 ? "+" : "")}{g:0} dB" : "-";
                VoiceInfo.Text = (vad > 0.5 ? L.T("Voce rilevata", "Voice detected") : L.T("Nessuna voce", "No voice"))
                                 + L.T($" · guadagno {gain}", $" · gain {gain}");
            }
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

    private async void Denoise_Toggled(object sender, RoutedEventArgs e)
    {
        if (_updating)
            return;
        string mode = !Denoise.IsOn ? "off" : Strength.SelectedIndex == 1 ? "strong" : "normal";
        await Ui.Run("set_mic_denoise", new JsonObject { ["mode"] = mode });
    }

    private async void Strength_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_updating || !Denoise.IsOn || Strength.SelectedIndex < 0)
            return;
        await Ui.Run("set_mic_denoise", new JsonObject { ["mode"] = Strength.SelectedIndex == 1 ? "strong" : "normal" });
    }

    private async void Option_Toggled(object sender, RoutedEventArgs e)
    {
        if (_updating || sender is not ToggleSwitch toggle)
            return;
        await Ui.Run("set_option", new JsonObject { ["name"] = (string)toggle.Tag, ["value"] = toggle.IsOn });
    }
}
