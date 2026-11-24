using System.Reflection;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace SonatinaDashboard.Pages;

public sealed partial class AboutPage : Page, IStatusView
{
    private bool _updating;

    public AboutPage()
    {
        InitializeComponent();
        Versions.Text = L.T($"Finestra {DashboardVersion()}", $"Window {DashboardVersion()}");
    }

    private static string DashboardVersion()
    {
        var version = Assembly.GetExecutingAssembly().GetName().Version;
        return version is null ? "?" : $"{version.Major}.{version.Minor}.{version.Build}";
    }

    public void Update(JsonObject s)
    {
        _updating = true;
        try
        {
            Versions.Text = L.T($"Versione {J.S(s, "app", "version") ?? "?"} · finestra {DashboardVersion()}",
                                $"Version {J.S(s, "app", "version") ?? "?"} · window {DashboardVersion()}");
            Autostart.IsOn = J.B(s, "app", "autostart");
            LogPath.Text = J.S(s, "app", "log") ?? "";
            bool host = Status.Host(s) is not null;
            ConflictOption.IsOn = J.B(s, "host", "options", "bt_conflict_check");
            CableOption.IsOn = J.B(s, "host", "options", "cable_rename_hint");
            ConflictOption.IsEnabled = CableOption.IsEnabled = host;
        }
        finally
        {
            _updating = false;
        }
    }

    private async void Autostart_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_updating)
            await Ui.Run("set_autostart", new JsonObject { ["value"] = Autostart.IsOn });
    }

    private async void OpenLog_Click(object sender, RoutedEventArgs e) => await Ui.Run("open_log");

    private async void Option_Toggled(object sender, RoutedEventArgs e)
    {
        if (_updating || sender is not ToggleSwitch toggle)
            return;
        await Ui.Run("set_option", new JsonObject { ["name"] = (string)toggle.Tag, ["value"] = toggle.IsOn });
    }

    private void Wizard_Click(object sender, RoutedEventArgs e) => App.Shell?.ShowWizard();
}
