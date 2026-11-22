using System.Text.Json.Nodes;

namespace SonatinaDashboard;

/// <summary>Texts derived from the status, shared by the pages.</summary>
public static class Status
{
    public static JsonNode? Host(JsonObject s) => s["host"];
    public static JsonNode? App(JsonObject s) => s["app"];

    /// <summary>The connected headset, else the preferred one.</summary>
    public static JsonNode? CurrentHeadset(JsonObject s)
    {
        JsonNode? preferred = null;
        foreach (var h in J.A(s, "host", "headsets"))
        {
            if (J.B(h, "connected"))
                return h;
            if (J.B(h, "preferred"))
                preferred = h;
        }
        return preferred;
    }

    public static bool InCall(JsonObject s) => J.B(s, "host", "mic", "in_call");

    public static string Apps(JsonObject s) => string.Join(", ", J.Strings(s, "host", "mic", "apps")
        .Select(a => a.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? a[..^4] : a)
        .Select(a => a.Length > 0 ? char.ToUpperInvariant(a[0]) + a[1..] : a)
        .Distinct());

    /// <summary>(text, pill kind) for the headset state.</summary>
    public static (string Text, string Kind) State(JsonObject s)
    {
        var host = Host(s);
        if (host is null)
            return J.B(s, "app", "adapter")
                ? (L.T("Avvio…", "Starting…"), "neutral")
                : (L.T("Chiavetta non trovata", "Adapter not found"), "critical");
        if (InCall(s))
            return (L.T("In chiamata", "In a call"), "attention");
        if (J.S(host, "pairing") is not null)
            return (L.T("Abbinamento…", "Pairing…"), "caution");
        return J.S(host, "state") switch
        {
            "streaming" => (L.T("In riproduzione", "Playing"), "success"),
            "securing" or "negotiating" => (L.T("Collegamento…", "Connecting…"), "caution"),
            "error" => (L.T("Errore", "Error"), "critical"),
            _ => J.S(host, "peer") is not null
                ? (L.T("Collegate", "Connected"), "success")
                : (L.T("Non collegate", "Not connected"), "neutral"),
        };
    }

    /// <summary>"aptX · 384 kbit/s · 48 kHz", or the call line.</summary>
    public static string CodecLine(JsonObject s)
    {
        var host = Host(s);
        if (host is null)
            return J.S(s, "app", "tooltip") ?? "";
        if (InCall(s))
        {
            string voice = J.S(host, "mic", "codec") == "msbc" ? "mSBC 16 kHz" : "CVSD 8 kHz";
            string cable = J.S(host, "mic", "cable") ?? "Hi-Fi Cable";
            return L.T($"Chiamata · {voice} · microfono su {cable}", $"Call · {voice} · microphone on {cable}");
        }
        var stream = J.At(host, "stream");
        if (stream is JsonObject)
        {
            string name = J.S(stream, "name") ?? Ui.CodecLabel(J.S(stream, "codec"));
            var kbps = J.I(stream, "kbps");
            return kbps is int k ? $"{name} · {k} kbit/s · 48 kHz" : $"{name} · 48 kHz";
        }
        if (J.S(host, "peer") is not null)
            return L.T("Collegate, in attesa di audio", "Connected, waiting for audio");
        if (J.B(host, "discovering"))
            return L.T("Ricerca di cuffie in modalità abbinamento…", "Looking for headphones in pairing mode…");
        return L.T("Accendi le cuffie: Sonatina si collega da sola", "Turn the headphones on: Sonatina connects by itself");
    }

    public static string LatencyLine(JsonObject s)
    {
        if (InCall(s))
            return L.T("La musica in alta qualità riprende appena la chiamata finisce",
                       "High quality music resumes as soon as the call ends");
        var ms = J.I(s, "host", "stream", "latency_ms");
        if (ms is not int value)
            return "";
        return J.B(s, "host", "options", "low_latency")
            ? L.T($"Ritardo stimato ~{value} ms (bassa latenza)", $"Estimated delay ~{value} ms (low latency)")
            : L.T($"Ritardo stimato ~{value} ms", $"Estimated delay ~{value} ms");
    }

    /// <summary>Title and explanation for the microphone card.</summary>
    public static (string Title, string Text) Mic(JsonObject s)
    {
        var mic = J.At(s, "host", "mic");
        string mode = J.S(s, "host", "mic", "mode") ?? "auto";
        if (mic is null)
            return (L.T("Non disponibile", "Not available"),
                    L.T("Il Bluetooth di Sonatina non è attivo.", "Sonatina's Bluetooth is not running."));
        if (mode == "off")
            return (L.T("Spento", "Off"),
                    L.T("Le cuffie fanno solo musica. Le app non ricevono il microfono.",
                        "The headphones only play music. Apps get no microphone."));
        if (J.S(mic, "cable") is null)
            return (L.T("Microfono virtuale non trovato", "Virtual microphone not found"),
                    L.T("Installa Hi-Fi Cable (gratuito, VB-Audio) e riavvia Sonatina.",
                        "Install Hi-Fi Cable (free, VB-Audio) and restart Sonatina."));
        if (J.B(mic, "in_call"))
        {
            string apps = Apps(s);
            if (mode == "on")
                return (L.T("In chiamata", "In a call"),
                        L.T("Microfono sempre attivo: la musica arriva in qualità telefonica finché non cambi modalità.",
                            "Microphone always on: music plays in phone quality until you change mode."));
            return (L.T("In chiamata", "In a call"), apps.Length > 0
                ? L.T($"{apps} sta usando il microfono. La musica torna in alta qualità quando lo chiude.",
                      $"{apps} is using the microphone. Music returns to high quality when it closes it.")
                : L.T("Un’app sta usando il microfono. La musica torna in alta qualità quando lo chiude.",
                      "An app is using the microphone. Music returns to high quality when it closes it."));
        }
        if (J.B(mic, "dismissed"))
            return (L.T("Messo da parte", "Set aside"),
                    L.T("Hai scelto la musica: il microfono torna disponibile quando l’app lo chiude e lo riapre.",
                        "You chose music: the microphone is offered again when the app closes and reopens it."));
        if (J.B(mic, "ready"))
            return mode == "on"
                ? (L.T("Pronto", "Ready"), L.T("Il microfono si attiva appena le cuffie sono collegate.",
                                               "The microphone turns on as soon as the headphones are connected."))
                : (L.T("Pronto", "Ready"), L.T("Passa in chiamata quando un’app apre il microfono e torna alla musica quando lo chiude.",
                                               "Switches to a call when an app opens the microphone and back to music when it closes it."));
        return (L.T("In attesa delle cuffie", "Waiting for the headphones"),
                L.T("Il microfono è disponibile quando le cuffie sono collegate.",
                    "The microphone is available when the headphones are connected."));
    }

    public static string MeterHint(JsonObject s)
    {
        string cable = J.S(s, "host", "mic", "cable") ?? "Hi-Fi Cable";
        string output = cable.Replace("Input", "Output", StringComparison.OrdinalIgnoreCase);
        if (!output.Contains("Output", StringComparison.OrdinalIgnoreCase))
            output += " Output";
        return L.T($"Livello · alle app arriva come «{output}»", $"Level · apps receive it as “{output}”");
    }

    /// <summary>Signal bars (0-4), one word and the line under the codec (1.1.0, A4).</summary>
    public static (int Bars, string Word, string Line) Signal(JsonObject s)
    {
        var radio = J.At(s, "host", "radio");
        string level = J.S(radio, "level") ?? "none";
        if (Host(s) is null || level == "none" || J.S(s, "host", "peer") is null)
            return (0, "-", L.T("Nessun collegamento", "No link"));
        int bars = level switch { "excellent" => 4, "good" => 3, _ => 1 };
        string word = level switch
        {
            "excellent" => L.T("Ottimo", "Excellent"),
            "good" => L.T("Buono", "Good"),
            _ => L.T("Debole", "Weak"),
        };
        string line = L.T("Segnale ", "Signal ") + word.ToLowerInvariant();
        if (Power(radio) is string power)
            line += $" ({power})";
        var lost = J.I(radio, "lost_ms_minute") ?? 0;
        if (lost > 0)
            line += L.T($" · {lost / 1000.0:0.0} s persi nell’ultimo minuto", $" · {lost / 1000.0:0.0} s lost in the last minute");
        if (J.B(radio, "interference"))
            line += L.T(" · disturbo radio", " · radio interference");
        return (bars, word, line);
    }

    /// <summary>RSSI in words: BR/EDR controllers report dB from the ideal range, some dBm.</summary>
    public static string? Power(JsonNode? radio)
    {
        if (J.I(radio, "rssi") is not int v)
            return null;
        if (J.S(radio, "rssi_unit") == "dBm")
            return $"{v} dBm";
        return v >= 0 ? L.T("potenza nella norma", "power in range")
                      : L.T($"{-v} dB sotto la norma", $"{-v} dB below range");
    }

    /// <summary>"canale 6 · 1", "nessuno", "spento".</summary>
    public static string WifiAvoided(JsonObject s)
    {
        var wifi = J.At(s, "host", "radio", "wifi");
        if (wifi is null)
            return "-";
        if (!J.B(wifi, "enabled"))
            return L.T("spento", "off");
        var channels = J.Strings(wifi, "avoid").ToList();
        if (channels.Count == 0)
            return L.T("nessuno", "none");
        return (channels.Count == 1 ? L.T("canale ", "channel ") : L.T("canali ", "channels ")) + string.Join(" · ", channels);
    }

    public static string Seconds(int? ms) => ms is int v ? (v == 0 ? "0 s" : $"{v / 1000.0:0.0} s") : "-";
}
