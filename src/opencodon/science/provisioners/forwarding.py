"""Local plaintext → remote TLS forwarding for ZMQ channels.

A Jupyter kernel speaks ZMQ over five raw TCP sockets, and pyzmq cannot speak
TLS. Remote backends expose their ports behind TLS, so something has to sit in
between: this accepts plaintext on ``127.0.0.1`` and relays each connection
over TLS to the remote endpoint.

The alternative — asking the backend for unencrypted ports — is a trap worth
naming. Jupyter's wire protocol HMAC-*signs* messages but does not encrypt
them, so plaintext ports would put every cell's source, stdout and data on the
public internet in the clear. The forwarder exists so that never has to be the
trade.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

BUFFER = 65536
CONNECT_TIMEOUT_S = 30.0


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes one way until either end closes."""
    try:
        while True:
            data = src.recv(BUFFER)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class TLSForwarder:
    """One local port relaying to one TLS endpoint.

    Binds port 0 and reports what the OS assigned, rather than picking a
    number: two sessions to two remote kernels would otherwise collide on a
    fixed port, and the failure would look like a kernel fault rather than a
    port clash.
    """

    def __init__(self, remote_host: str, remote_port: int):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.local_port: int = self._listener.getsockname()[1]
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            try:
                context = ssl.create_default_context()
                raw = socket.create_connection(
                    (self.remote_host, self.remote_port), timeout=CONNECT_TIMEOUT_S
                )
                upstream = context.wrap_socket(raw, server_hostname=self.remote_host)
            except Exception as exc:
                logger.warning(
                    "tls forward to %s:%s failed: %s",
                    self.remote_host, self.remote_port, exc,
                )
                client.close()
                continue
            # ZMQ reconnects on its own schedule, so each accepted connection
            # gets its own pair of pumps rather than a single shared relay.
            threading.Thread(target=_pump, args=(client, upstream), daemon=True).start()
            threading.Thread(target=_pump, args=(upstream, client), daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass


class ForwarderSet:
    """The five channel forwarders for one kernel, torn down together."""

    def __init__(self):
        self._forwarders: List[TLSForwarder] = []

    def add(self, remote: Tuple[str, int]) -> int:
        forwarder = TLSForwarder(*remote)
        self._forwarders.append(forwarder)
        return forwarder.local_port

    def close(self) -> None:
        for forwarder in self._forwarders:
            forwarder.close()
        self._forwarders.clear()

    def __len__(self) -> int:
        return len(self._forwarders)
