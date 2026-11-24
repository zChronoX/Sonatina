using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using SonatinaDashboard.Pages;
using Windows.Graphics;

namespace SonatinaDashboard;

public sealed partial class MainWindow : Window
{
    private static readonly Dictionary<string, Type> PageTypes = new()
    {
        ["home"] = typeof(HomePage),
        ["headsets"] = typeof(HeadsetsPage),
        ["quality"] = typeof(QualityPage),
        ["mic"] = typeof(MicPage),
        ["diagnostics"] = typeof(DiagnosticsPage),
        ["about"] = typeof(AboutPage),
        ["wizard"] = typeof(WizardPage),   // not in the menu: first run, or Informazioni
    };

    private bool _setupChecked;

    /// <summary>Last status received ({"app":..,"host":..}); null while not connected.</summary>
    public JsonObject? Last { get; private set; }

    private readonly DispatcherQueueTimer _messageTimer;

    public MainWindow()
    {
        InitializeComponent();
        Title = "Sonatina";
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
        SystemBackdrop = new MicaBackdrop();

        var icon = Path.Combine(AppContext.BaseDirectory, "sonatina.ico");
        if (File.Exists(icon))
            AppWindow.SetIcon(icon);
        SizeAndCenter(1120, 840);

        _messageTimer = DispatcherQueue.CreateTimer();
        _messageTimer.Interval = TimeSpan.FromSeconds(8);
        _messageTimer.Tick += (_, _) =>
        {
            _messageTimer.Stop();
            MessageBar.IsOpen = false;
        };

        StartButton.Visibility = TrayExe() is null ? Visibility.Collapsed : Visibility.Visible;
        OfflineBar.IsOpen = !App.Ipc.Connected;
        App.Ipc.StatusChanged += status => DispatcherQueue.TryEnqueue(() => OnStatus(status));
        App.Ipc.ConnectionChanged += up => DispatcherQueue.TryEnqueue(() => OnConnection(up));

        Nav.SelectedItem = Nav.MenuItems[0];
        if (ContentFrame.Content is null)
            ContentFrame.Navigate(typeof(HomePage));
    }

    // -- window ---------------------------------------------------------------
    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hwnd);

    private IntPtr Handle => WinRT.Interop.WindowNative.GetWindowHandle(this);

    private void SizeAndCenter(int width, int height)
    {
        double scale = Math.Max(1.0, GetDpiForWindow(Handle) / 96.0);
        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
        int w = Math.Min((int)(width * scale), area.Width);
        int h = Math.Min((int)(height * scale), area.Height);
        AppWindow.MoveAndResize(new RectInt32(area.X + (area.Width - w) / 2, area.Y + (area.Height - h) / 2, w, h));
    }

    public void BringToFront()
    {
        if (AppWindow.Presenter is OverlappedPresenter presenter && presenter.State == OverlappedPresenterState.Minimized)
            presenter.Restore();
        Activate();
        SetForegroundWindow(Handle);
    }

    // -- navigation -----------------------------------------------------------
    public void Go(string tag)
    {
        foreach (var item in Nav.MenuItems.Concat(Nav.FooterMenuItems))
        {
            if (item is NavigationViewItem nvi && (string)nvi.Tag == tag)
            {
                Nav.SelectedItem = nvi;
                return;
            }
        }
    }

    /// <summary>The setup guide (M6) in place of the pages; the menu shows no selection.</summary>
    public void ShowWizard()
    {
        Nav.SelectedItem = null;
        if (ContentFrame.CurrentSourcePageType != typeof(WizardPage))
            ContentFrame.Navigate(typeof(WizardPage));
    }

    private void Nav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItemContainer?.Tag is string tag && PageTypes.TryGetValue(tag, out var type)
            && ContentFrame.CurrentSourcePageType != type)
            ContentFrame.Navigate(type);
    }

    private void ContentFrame_Navigated(object sender, NavigationEventArgs e)
    {
        if (Last is not null && ContentFrame.Content is IStatusView view)
            view.Update(Last);
    }

    // -- status ---------------------------------------------------------------
    private void OnStatus(JsonObject status)
    {
        Last = status;
        OfflineBar.IsOpen = false;
        CheckFirstRun(status);
        if (ContentFrame.Content is IStatusView view)
        {
            try
            {
                view.Update(status);
            }
            catch (Exception error)
            {
                Log.Write("update: " + error);
            }
        }
    }

    /// <summary>First run (nothing paired yet): the setup guide opens by itself, once. An
    /// existing installation updated to 1.3.0 just marks it done (Informazioni reopens it).</summary>
    private void CheckFirstRun(JsonObject status)
    {
        if (_setupChecked || Status.Host(status) is null || J.At(status, "host", "setup") is not JsonObject)
            return;
        _setupChecked = true;
        if (J.B(status, "host", "setup", "done"))
            return;
        bool paired = J.A(status, "host", "headsets").Any(h => J.B(h, "paired"));
        if (paired)
            _ = Ui.Run("set_option", new JsonObject { ["name"] = "setup_done", ["value"] = true });
        else
            ShowWizard();
    }

    private void OnConnection(bool up)
    {
        OfflineBar.IsOpen = !up;
        if (!up)
            Last = null;
    }

    public void ShowError(string text) => ShowMessage(text, InfoBarSeverity.Error);

    public void ShowMessage(string text, InfoBarSeverity severity = InfoBarSeverity.Informational)
    {
        MessageBar.Severity = severity;
        MessageBar.Title = severity == InfoBarSeverity.Error ? L.T("Non riuscito", "Failed") : "";
        MessageBar.Message = text;
        MessageBar.IsOpen = true;
        _messageTimer.Stop();
        _messageTimer.Start();
    }

    // -- starting the tray app ------------------------------------------------
    /// <summary>Sonatina.exe: the dashboard is installed in {app}\dashboard.</summary>
    private static string? TrayExe()
    {
        var path = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "Sonatina.exe"));
        return File.Exists(path) ? path : null;
    }

    private void Start_Click(object sender, RoutedEventArgs e)
    {
        var exe = TrayExe();
        if (exe is null)
            return;
        try
        {
            Process.Start(new ProcessStartInfo(exe) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(exe)! });
            ShowMessage(L.T("Avvio di Sonatina…", "Starting Sonatina…"));
        }
        catch (Exception error)
        {
            ShowError(error.Message);
        }
    }
}
