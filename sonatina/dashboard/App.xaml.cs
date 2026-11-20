using System.Threading;
using Microsoft.UI.Xaml;

namespace SonatinaDashboard;

/// <summary>One window per user session: a second launch (tray "Apri Sonatina", Start menu)
/// only brings the existing window to the front.</summary>
public partial class App : Application
{
    public static IpcClient Ipc { get; } = new();
    public static MainWindow? Shell { get; private set; }

    private static Mutex? _single;
    private static EventWaitHandle? _show;

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) =>
        {
            Log.Write("unhandled: " + e.Exception);
            e.Handled = true;
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _single = new Mutex(true, @"Local\SonatinaDashboard", out bool first);
        _show = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\SonatinaDashboardShow");
        if (!first)
        {
            _show.Set();
            Environment.Exit(0);
            return;
        }

        Shell = new MainWindow();
        Shell.Activate();
        var queue = Shell.DispatcherQueue;
        var waiter = new Thread(() =>
        {
            while (true)
            {
                _show.WaitOne();
                queue.TryEnqueue(() => Shell?.BringToFront());
            }
        }) { IsBackground = true, Name = "show" };
        waiter.Start();
        Ipc.Start();
    }
}
