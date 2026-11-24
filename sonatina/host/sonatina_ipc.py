"""Local channel between the tray process and the dashboard window.

TCP on 127.0.0.1 only, port chosen by Windows, newline-delimited JSON. The port and a
random token are written to %LOCALAPPDATA%\\Sonatina\\ipc.json (readable by the user only,
like everything in their profile); a client must send the token first.

    client -> {"hello": "<token>", "client": "dashboard", "version": 1}
    server -> {"ok": true, "version": 1, "app": "<version>"}
    client -> {"id": 7, "cmd": "set_option", "args": {"name": "low_latency", "value": true}}
    server -> {"id": 7, "ok": true, "result": ...}          or {"id": 7, "ok": false, "error": "..."}
    client -> {"id": 8, "cmd": "subscribe"}
    server -> {"event": "status", "data": {...}}           at once and on every change (<= 4/s)

The server runs on its own thread and event loop, so it keeps working while the Bluetooth
host restarts (adapter unplugged, PC resumed). Commands are carried out by a controller
(the tray app; a fake one in the tests): snapshot() -> dict, await command(name, args).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time

log = logging.getLogger("sonatina.ipc")

PROTOCOL = 1
MAX_LINE = 64 * 1024
STATUS_INTERVAL = 0.25


class IpcServer:
    def __init__(self, controller, info_path: str, app_version: str = "dev") -> None:
        self.controller = controller
        self.info_path = info_path
        self.app_version = app_version
        self.token = secrets.token_hex(16)
        self.port: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.clients: set = set()
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()

    # -- lifecycle -------------------------------------------------------------
    def start_thread(self) -> None:
        self.thread = threading.Thread(target=self._thread, name="sonatina-ipc", daemon=True)
        self.thread.start()
        self.ready.wait(5.0)

    def _thread(self) -> None:
        try:
            asyncio.run(self.serve_forever())
        except Exception as error:
            log.error("dashboard channel stopped: %r", error)
            self.ready.set()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=MAX_LINE)
        self.port = self.server.sockets[0].getsockname()[1]
        self._write_info()
        log.info("dashboard channel on 127.0.0.1:%d", self.port)
        self.ready.set()

    async def serve_forever(self) -> None:
        await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.clients):
            writer.close()
        try:
            with open(self.info_path, encoding="utf-8") as f:
                if json.load(f).get("token") == self.token:
                    os.remove(self.info_path)
        except Exception:
            pass

    def _write_info(self) -> None:
        os.makedirs(os.path.dirname(self.info_path), exist_ok=True)
        tmp = self.info_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid(),
                       "protocol": PROTOCOL, "app": self.app_version}, f)
        os.replace(tmp, self.info_path)

    # -- one client ------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sender = _Sender(writer)
        subscription: asyncio.Task | None = None
        try:
            hello = await asyncio.wait_for(_read(reader), 5.0)
            if not isinstance(hello, dict) or not secrets.compare_digest(str(hello.get("hello", "")), self.token):
                await sender.send({"ok": False, "error": "bad token"})
                return
            await sender.send({"ok": True, "version": PROTOCOL, "app": self.app_version})
            self.clients.add(writer)
            log.info("dashboard connected (%s)", hello.get("client", "?"))
            while True:
                msg = await _read(reader)
                if msg is None:
                    return
                if not isinstance(msg, dict):
                    continue
                cmd, rid, args = msg.get("cmd"), msg.get("id"), msg.get("args") or {}
                if cmd == "subscribe":
                    if subscription is None:
                        subscription = asyncio.ensure_future(self._push_status(sender))
                    await sender.send({"id": rid, "ok": True, "result": None})
                    continue
                asyncio.ensure_future(self._run(sender, rid, cmd, args))
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as error:
            log.debug("dashboard client: %r", error)
        finally:
            if subscription is not None:
                subscription.cancel()
            self.clients.discard(writer)
            writer.close()

    async def _run(self, sender: "_Sender", rid, cmd, args) -> None:
        try:
            if cmd == "status":
                result = self.controller.snapshot()
            elif cmd == "ping":
                result = time.time()
            else:
                result = await self.controller.command(str(cmd), dict(args))
            await sender.send({"id": rid, "ok": True, "result": result})
        except Exception as error:
            log.info("dashboard command %s failed: %r", cmd, error)
            await sender.send({"id": rid, "ok": False, "error": str(error) or type(error).__name__})

    async def _push_status(self, sender: "_Sender") -> None:
        last = None
        while True:
            try:
                snap = self.controller.snapshot()
            except Exception as error:
                snap = {"error": repr(error)}
            text = json.dumps(snap, sort_keys=True, default=str)
            if text != last:
                last = text
                await sender.send_raw('{"event":"status","data":' + text + "}")
            await asyncio.sleep(STATUS_INTERVAL)


class _Sender:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()

    async def send(self, obj) -> None:
        await self.send_raw(json.dumps(obj, default=str))

    async def send_raw(self, text: str) -> None:
        async with self.lock:
            self.writer.write(text.encode("utf-8") + b"\n")
            await self.writer.drain()


async def _read(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return {}


# ----------------------------------------------------------------------------- client
class IpcClient:
    """Minimal client, used by the tests and by `sonatina_tray.py --status`."""

    def __init__(self, info_path: str) -> None:
        self.info_path = info_path
        self.reader = None
        self.writer = None
        self.next_id = 0

    async def connect(self) -> dict | None:
        with open(self.info_path, encoding="utf-8") as f:
            info = json.load(f)
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", info["port"], limit=MAX_LINE * 16)
        await self._send({"hello": info["token"], "client": "test", "version": PROTOCOL})
        return await self._read()

    async def _send(self, obj) -> None:
        self.writer.write(json.dumps(obj).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def _read(self):
        line = await self.reader.readline()
        return json.loads(line) if line else None

    async def call(self, cmd: str, **args):
        self.next_id += 1
        rid = self.next_id
        await self._send({"id": rid, "cmd": cmd, "args": args})
        while True:
            msg = await self._read()
            if msg is None:
                raise ConnectionError("closed")
            if msg.get("id") == rid:
                if not msg.get("ok"):
                    raise RuntimeError(msg.get("error"))
                return msg.get("result")

    async def next_status(self) -> dict:
        while True:
            msg = await self._read()
            if msg is None:
                raise ConnectionError("closed")
            if msg.get("event") == "status":
                return msg["data"]

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
