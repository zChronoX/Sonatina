using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SonatinaDashboard;

public sealed class IpcException(string message) : Exception(message);

/// <summary>Client of sonatina_ipc.py: 127.0.0.1, port and token from
/// %LOCALAPPDATA%\Sonatina\ipc.json, newline-delimited JSON. Reconnects by itself every
/// second while the tray process is not running or restarting.</summary>
public sealed class IpcClient
{
    public const int Protocol = 1;

    /// <summary>Raised on a background thread with the full status ({"app":..,"host":..}).</summary>
    public event Action<JsonObject>? StatusChanged;
    /// <summary>Raised on a background thread when the channel opens or closes.</summary>
    public event Action<bool>? ConnectionChanged;

    public bool Connected { get; private set; }
    public string? AppVersion { get; private set; }

    public static readonly string InfoPath = Path.Combine(Log.Folder, "ipc.json");

    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonNode?>> _pending = new();
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private StreamWriter? _writer;
    private int _nextId;
    private bool _started;

    public void Start()
    {
        if (_started)
            return;
        _started = true;
        _ = Task.Run(RunAsync);
    }

    private async Task RunAsync()
    {
        while (true)
        {
            try
            {
                await SessionAsync();
            }
            catch (Exception error) when (error is IOException or SocketException or OperationCanceledException
                                              or JsonException or ObjectDisposedException or IpcException
                                              or UnauthorizedAccessException or InvalidOperationException)
            {
                // the tray app is not there (yet), or it closed the channel
            }
            catch (Exception error)
            {
                Log.Write("ipc: " + error);
            }
            _writer = null;
            foreach (var key in _pending.Keys)
                if (_pending.TryRemove(key, out var waiting))
                    waiting.TrySetException(new IpcException(L.T("Sonatina non risponde", "Sonatina is not responding")));
            SetConnected(false);
            await Task.Delay(1000);
        }
    }

    private async Task SessionAsync()
    {
        if (!File.Exists(InfoPath))
            return;
        JsonNode? info;
        using (var file = new FileStream(InfoPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            info = await JsonNode.ParseAsync(file);
        int port = J.I(info, "port") ?? 0;
        string token = J.S(info, "token") ?? "";
        if (port <= 0 || token.Length == 0)
            return;

        using var tcp = new TcpClient { NoDelay = true };
        using (var timeout = new CancellationTokenSource(2000))
            await tcp.ConnectAsync(IPAddress.Loopback, port, timeout.Token);
        await using var stream = tcp.GetStream();
        using var reader = new StreamReader(stream, new UTF8Encoding(false));
        var writer = new StreamWriter(stream, new UTF8Encoding(false)) { NewLine = "\n", AutoFlush = true };

        await writer.WriteLineAsync(new JsonObject
        {
            ["hello"] = token, ["client"] = "dashboard", ["version"] = Protocol,
        }.ToJsonString());
        using (var timeout = new CancellationTokenSource(5000))
        {
            var line = await reader.ReadLineAsync(timeout.Token);
            var hello = line is null ? null : JsonNode.Parse(line);
            if (!J.B(hello, "ok"))
                throw new IpcException(J.S(hello, "error") ?? "handshake");
            AppVersion = J.S(hello, "app");
        }

        _writer = writer;
        SetConnected(true);
        _ = SendAsync("subscribe");

        while (true)
        {
            var line = await reader.ReadLineAsync();
            if (line is null)
                return;
            if (line.Length == 0)
                continue;
            JsonNode? message;
            try
            {
                message = JsonNode.Parse(line);
            }
            catch (JsonException)
            {
                continue;
            }
            if (message is not JsonObject obj)
                continue;
            if (J.S(obj, "event") == "status")
            {
                if (obj["data"] is JsonObject data)
                {
                    obj.Remove("data");          // detach so the UI may keep it
                    StatusChanged?.Invoke(data);
                }
                continue;
            }
            int? id = J.I(obj, "id");
            if (id is int key && _pending.TryRemove(key, out var waiting))
            {
                if (J.B(obj, "ok"))
                {
                    var result = obj["result"];
                    obj.Remove("result");
                    waiting.TrySetResult(result);
                }
                else
                {
                    waiting.TrySetException(new IpcException(J.S(obj, "error") ?? "error"));
                }
            }
        }
    }

    private void SetConnected(bool value)
    {
        if (Connected == value)
            return;
        Connected = value;
        ConnectionChanged?.Invoke(value);
    }

    /// <summary>Sends a command and waits for its answer (IpcException on errors).</summary>
    public async Task<JsonNode?> SendAsync(string command, JsonObject? args = null, int timeoutMs = 15000)
    {
        var writer = _writer ?? throw new IpcException(L.T("Sonatina non è in esecuzione", "Sonatina is not running"));
        int id = Interlocked.Increment(ref _nextId);
        var done = new TaskCompletionSource<JsonNode?>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = done;
        var message = new JsonObject { ["id"] = id, ["cmd"] = command, ["args"] = args ?? new JsonObject() };
        await _writeLock.WaitAsync();
        try
        {
            await writer.WriteLineAsync(message.ToJsonString());
        }
        catch (Exception error)
        {
            _pending.TryRemove(id, out _);
            throw new IpcException(error.Message);
        }
        finally
        {
            _writeLock.Release();
        }
        var finished = await Task.WhenAny(done.Task, Task.Delay(timeoutMs));
        if (finished != done.Task)
        {
            _pending.TryRemove(id, out _);
            throw new IpcException(L.T("Sonatina non ha risposto in tempo", "Sonatina did not answer in time"));
        }
        return await done.Task;
    }
}
