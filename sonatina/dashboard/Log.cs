using System.IO;

namespace SonatinaDashboard;

/// <summary>Tiny log next to Sonatina's own (%LOCALAPPDATA%\Sonatina\dashboard.log), kept short.</summary>
public static class Log
{
    private static readonly object Gate = new();
    public static readonly string Folder = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Sonatina");
    private static readonly string FilePath = Path.Combine(Folder, "dashboard.log");

    public static void Write(string text)
    {
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Folder);
                var info = new FileInfo(FilePath);
                if (info.Exists && info.Length > 256 * 1024)
                    File.Delete(FilePath);
                File.AppendAllText(FilePath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {text}{Environment.NewLine}");
            }
        }
        catch
        {
            // logging must never break the window
        }
    }
}
