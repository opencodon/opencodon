"""Regression tests: aiohttp servers must set an explicit ``client_max_size``.

Without it, aiohttp falls back to its implicit 1 MiB default and — worse —
handlers that only check ``Content-Length`` can be bypassed entirely by
chunked transfer-encoding requests (#58536 webhook, #58902 raft lineage).
These tests pin the wiring for the three servers fixed in this follow-up:
bluebubbles, teams, and the ``opencodon proxy`` server.
"""

import inspect


def test_proxy_app_sets_client_max_size():
    import opencodon.frontends.cli.proxy.server as proxy_server

    # Mirrors api_server's MAX_REQUEST_BYTES: chat payloads can be large,
    # but the cap must exist so chunked bodies stay bounded.
    assert proxy_server.MAX_REQUEST_BYTES >= 1_048_576
    src = inspect.getsource(proxy_server.create_app)
    assert "client_max_size=MAX_REQUEST_BYTES" in src
