"""Administrative listeners shared by the task's services.

A service serves its administrative surface on exactly one of two listeners, chosen by its
environment, and never on its public port:

* a Unix domain socket (``<NAME>_ADMIN_SOCKET``): the socket file is mode 0600 in a directory the
  starter keeps at mode 0700, and every connection is accepted only when the peer's kernel
  credentials (SO_PEERCRED) name uid 0 or the uid the service runs as; or
* a loopback TCP port (``<NAME>_ADMIN_PORT``), for a development environment whose only user is the
  service user.

With neither set, the administrative surface is off. Every administrative request, accepted or
refused, is appended as one JSON line to ``<NAME>_ADMIN_LOG`` when that is set, with the peer uid
the kernel reported (null on a TCP listener).
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOG_LOCK = threading.Lock()


def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Return (pid, uid, gid) of the process on the other end of a Unix stream socket."""
    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


def record(log_path: str | None, entry: dict) -> None:
    if not log_path:
        return
    line = json.dumps(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry},
        sort_keys=True,
    )
    with _LOG_LOCK:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class AdminUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A Unix-socket HTTP listener that refuses any peer whose uid is not allowed."""

    daemon_threads = True
    is_admin = True

    def __init__(
        self,
        path: str,
        handler,
        *,
        allowed_uids: frozenset[int],
        log_path: str | None,
        service: str,
    ):
        self.allowed_uids = allowed_uids
        self.log_path = log_path
        self.service = service
        if os.path.exists(path):
            os.unlink(path)
        old_umask = os.umask(0o177)
        try:
            super().__init__(path, handler)
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)

    def verify_request(self, request, client_address) -> bool:  # noqa: ARG002 - fixed signature
        try:
            pid, uid, _gid = peer_credentials(request)
        except OSError:
            pid, uid = -1, -1
        allowed = uid in self.allowed_uids
        if not allowed:
            record(
                self.log_path,
                {
                    "service": self.service,
                    "via": "socket",
                    "peer_uid": uid,
                    "peer_pid": pid,
                    "method": None,
                    "path": None,
                    "accepted": False,
                    "status": None,
                },
            )
        return allowed


class AdminTCPServer(ThreadingHTTPServer):
    daemon_threads = True
    is_admin = True

    def __init__(self, port: int, handler, *, log_path: str | None, service: str):
        self.log_path = log_path
        self.service = service
        super().__init__(("127.0.0.1", port), handler)


class AdminAwareHandler(BaseHTTPRequestHandler):
    """Request handler mixin: knows whether its request came through an administrative listener."""

    admin_status: int | None = None

    def setup(self) -> None:
        super().setup()
        self.peer_uid: int | None = None
        self.peer_pid: int | None = None
        if isinstance(self.server, AdminUnixServer):
            try:
                self.peer_pid, self.peer_uid, _gid = peer_credentials(self.connection)
            except OSError:
                pass

    def is_admin_request(self) -> bool:
        return bool(getattr(self.server, "is_admin", False))

    def address_string(self) -> str:
        if isinstance(self.client_address, tuple) and self.client_address:
            return str(self.client_address[0])
        return f"unix:uid={self.peer_uid}"

    def listener_label(self) -> str:
        address = self.server.server_address
        return str(address[1]) if isinstance(address, tuple) else "admin-socket"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature fixed by the base class
        sys.stderr.write(
            "%s:%s - %s\n"
            % (self.address_string(), self.listener_label(), format % args)
        )

    def send_response(self, code: int, message: str | None = None) -> None:
        self.admin_status = code
        super().send_response(code, message)

    def audit(self, accepted: bool, via: str) -> None:
        record(
            getattr(self.server, "log_path", None) or os.environ.get(self.log_env, ""),
            {
                "service": self.service_name,
                "via": via,
                "peer_uid": self.peer_uid,
                "peer_pid": self.peer_pid,
                "method": self.command,
                "path": self.path,
                "accepted": accepted,
                "status": self.admin_status,
            },
        )

    service_name = "service"
    log_env = ""


def start_admin_listener(handler, *, service: str, prefix: str) -> str:
    """Start the administrative listener the environment selects; return a description of it."""
    socket_path = os.environ.get(f"{prefix}_ADMIN_SOCKET", "").strip()
    port = int(os.environ.get(f"{prefix}_ADMIN_PORT", "0") or 0)
    log_path = os.environ.get(f"{prefix}_ADMIN_LOG", "").strip() or None
    if socket_path:
        server = AdminUnixServer(
            socket_path,
            handler,
            allowed_uids=frozenset({0, os.getuid()}),
            log_path=log_path,
            service=service,
        )
        description = f"admin socket {socket_path} (uids 0 and {os.getuid()})"
    elif port:
        server = AdminTCPServer(port, handler, log_path=log_path, service=service)
        description = f"admin port 127.0.0.1:{port}"
    else:
        return "no admin listener"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return description
