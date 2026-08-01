"""Host bridge — parent-side RPC server for in-kernel ``host.*`` calls.

One tiny Unix-socket JSON server per workspace, token-authed (mirrors the
PTC RPC design in ``tools/code_execution_tool.py``): credentials and provider
clients stay parent-side; kernel code sees only a socket + one-shot token.

Every call is recorded as one ``host_call_log`` row (child of the executing
cell), with large results spilled to ``content_snapshots`` — this is what
makes the *inside* of a cell reproducible, and it delivers "LLM as a data
primitive" (``host.llm`` / ``host.llm_batch``) without handing API keys to
the kernel.

Methods:
- ``llm(prompt, model?, system?, max_tokens?)`` — one model call via the
  auxiliary-client seam (config task ``science_llm``; falls back to the
  default auxiliary provider).
- ``llm_batch(prompts, max_concurrency?)`` — parallel fan-out, one log row.
- ``tool(name, args)`` — invoke a opencodon tool, gated by the
  ``science.host_tools`` allowlist in config.yaml (empty by default: no
  tool is reachable from the kernel unless the user opts in).
- ``models()`` — the resolved models, so kernel code can route: ``default``
  for ordinary calls and ``cheap`` for the high-volume per-page kind. Both
  are resolved here rather than in the kernel because only the host knows
  which provider is configured, and a model slug is only valid against one.

Calls are only served while a cell is executing (``current_cell`` context);
out-of-cell connections are refused so nothing escapes the execution trace.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MAX_BATCH = 10_000
_MAX_CONCURRENCY = 16


def _config_host_tools() -> List[str]:
    """The user's kernel-reachable tool allowlist (config: science.host_tools)."""
    try:
        from opencodon.config import load_config

        cfg = load_config() or {}
        tools = ((cfg.get("science") or {}).get("host_tools")) or []
        return [str(t) for t in tools]
    except Exception:
        return []


class HostBridge:
    """One RPC server bound to one workspace (and its ScienceStore)."""

    def __init__(self, workspace: Path, store, *, allowed_tools: Optional[List[str]] = None):
        self._workspace = Path(workspace)
        self._store = store
        self._allowed_tools = allowed_tools
        self._token = secrets.token_hex(16)
        # Unix-socket paths have a ~104-char limit on macOS; anchor in the
        # temp dir keyed by a workspace hash instead of inside the workspace.
        digest = hashlib.sha256(str(self._workspace).encode()).hexdigest()[:16]
        self._sock_path = os.path.join(
            tempfile.gettempdir(), f"opencodon-science-{digest}.sock"
        )
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cell_lock = threading.Lock()
        self._current_cells: List[str] = []

    # ── lifecycle ───────────────────────────────────────────────────

    @property
    def endpoint(self) -> Dict[str, Any]:
        """The ``host`` entry written into cell.json."""
        return {"socket": self._sock_path, "token": self._token, "timeout": 600}

    def start(self) -> None:
        if self._thread is not None:
            return
        with contextlib.suppress(OSError):
            os.unlink(self._sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._sock_path)
        os.chmod(self._sock_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(
            target=self._serve, name="science-host-bridge", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.close()
            self._server = None
        with contextlib.suppress(OSError):
            os.unlink(self._sock_path)
        self._thread = None

    @contextlib.contextmanager
    def current_cell(self, execution_id: str):
        """Serve host.* calls for the duration of one executing cell."""
        with self._cell_lock:
            self._current_cells.append(execution_id)
        try:
            yield
        finally:
            with self._cell_lock:
                with contextlib.suppress(ValueError):
                    self._current_cells.remove(execution_id)

    def _active_cell(self) -> Optional[str]:
        with self._cell_lock:
            return self._current_cells[-1] if self._current_cells else None

    # ── server loop ─────────────────────────────────────────────────

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with contextlib.suppress(Exception), contextlib.closing(conn):
            conn.settimeout(600)
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_REQUEST_BYTES:
                    conn.sendall(b'{"error": "request too large"}\n')
                    return
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
            try:
                request = json.loads(b"".join(chunks).decode("utf-8"))
            except ValueError:
                conn.sendall(b'{"error": "malformed request"}\n')
                return
            reply = self._dispatch(request)
            conn.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")

    def _dispatch(self, request: dict) -> dict:
        if not secrets.compare_digest(
            str(request.get("token") or ""), self._token
        ):
            return {"error": "invalid token"}
        cell_id = self._active_cell()
        if cell_id is None:
            return {"error": "host bridge is only available while a cell is executing"}
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        handler = {
            "llm": self._do_llm,
            "llm_batch": self._do_llm_batch,
            "tool": self._do_tool,
            "models": self._do_models,
        }.get(method)
        if handler is None:
            return {"error": f"unknown host method {method!r}"}
        try:
            data, log_args, derivable = handler(params)
            error = None
        except Exception as exc:
            data, log_args, derivable = None, params, False
            error = f"{type(exc).__name__}: {exc}"
        try:
            self._store.record_host_call(
                cell_id,
                method,
                log_args,
                result=data,
                derivable=derivable,
                error=error,
            )
        except Exception:
            logger.exception("failed to record host call %s", method)
        if error is not None:
            return {"error": error}
        return {"data": data}

    # ── handlers ────────────────────────────────────────────────────

    def _llm_client(self, model: Optional[str]):
        from opencodon.core.auxiliary_client import get_text_auxiliary_client

        client, default_model = get_text_auxiliary_client("science_llm")
        if client is None:
            raise RuntimeError(
                "no auxiliary model provider is configured (set one up with "
                "`opencodon setup`, or pin auxiliary.science_llm in config.yaml)"
            )
        return client, (model or default_model)

    def _one_completion(self, client, model, prompt, system, max_tokens) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append({"role": "user", "content": str(prompt)})
        kwargs: Dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)
        response = client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()

    def _do_llm(self, params: dict):
        prompt = params.get("prompt")
        if not prompt:
            raise ValueError("llm requires a prompt")
        client, model = self._llm_client(params.get("model"))
        text = self._one_completion(
            client, model, prompt, params.get("system"), params.get("max_tokens")
        )
        log_args = {
            "prompt": _elide(str(prompt)),
            "model": model,
            "system": _elide(str(params.get("system") or "")) or None,
        }
        return text, log_args, False

    def _do_llm_batch(self, params: dict):
        prompts = params.get("prompts") or []
        if not isinstance(prompts, list) or not prompts:
            raise ValueError("llm_batch requires a non-empty prompts list")
        if len(prompts) > _MAX_BATCH:
            raise ValueError(f"llm_batch is capped at {_MAX_BATCH} prompts")
        concurrency = min(int(params.get("max_concurrency") or 8), _MAX_CONCURRENCY)
        client, model = self._llm_client(params.get("model"))
        system = params.get("system")
        max_tokens = params.get("max_tokens")

        def _run(prompt):
            try:
                return {"ok": True, "text": self._one_completion(
                    client, model, prompt, system, max_tokens)}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(_run, prompts))
        log_args = {"count": len(prompts), "model": model,
                    "max_concurrency": concurrency}
        return results, log_args, False

    def _do_tool(self, params: dict):
        name = str(params.get("name") or "")
        args = params.get("args") or {}
        allowed = (
            self._allowed_tools
            if self._allowed_tools is not None
            else _config_host_tools()
        )
        if name not in allowed:
            raise PermissionError(
                f"tool {name!r} is not in the science.host_tools allowlist "
                f"(config.yaml); allowed: {sorted(allowed) or 'none'}"
            )
        from model_tools import handle_function_call

        raw = handle_function_call(name, dict(args), task_id=None)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = raw
        return data, {"name": name, "args": args}, False

    def _do_models(self, params: dict):
        _client, model = self._llm_client(None)
        return (
            {
                "default": model,
                "cheap": cheap_model(model),
                "reasoning": reasoning_model(model),
            },
            {},
            True,
        )


def _elide(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[{len(text) - limit} chars elided]"


# ── Model-role resolution ───────────────────────────────────────────

# Skills route by *role* rather than by slug: a page-classification loop wants
# the smallest capable model, a figure-layout call wants the strongest one.
# Which slug that is depends entirely on the configured provider, so the roles
# are resolved here — the kernel cannot see which provider it is talking to.
CHEAP_MODEL_KEY = "cheap_model"
REASONING_MODEL_KEY = "reasoning_model"

# Used only when the configured provider is Anthropic's own API, where this
# alias is valid. Everywhere else an unconfigured caller gets the provider's
# own default, which is always a slug that provider accepts.
_ANTHROPIC_CHEAP_MODEL = "claude-haiku-4-5"
_ANTHROPIC_HOSTS = ("api.anthropic.com",)


def _configured_model(key: str) -> Optional[str]:
    """``auxiliary.science_llm.<key>`` from config.yaml, if set."""
    try:
        from opencodon.core.auxiliary_client import _get_auxiliary_task_config

        value = _get_auxiliary_task_config("science_llm").get(key)
    except Exception:
        return None
    value = str(value or "").strip()
    return value or None


def _configured_cheap_model() -> Optional[str]:
    """``auxiliary.science_llm.cheap_model`` from config.yaml, if set."""
    return _configured_model(CHEAP_MODEL_KEY)


def _provider_is_anthropic() -> bool:
    """Whether science_llm resolves to Anthropic's own API."""
    try:
        from opencodon.core.auxiliary_client import _resolve_task_provider_model

        provider, _model, base_url, _key, _mode = _resolve_task_provider_model(
            "science_llm"
        )
    except Exception:
        return False
    if str(provider or "").strip().lower() == "anthropic":
        return True
    host = str(base_url or "").lower()
    return any(known in host for known in _ANTHROPIC_HOSTS)


def cheap_model(default_model: Optional[str]) -> Optional[str]:
    """The model high-volume per-item work should use.

    Resolution order, most explicit first:

    1. ``auxiliary.science_llm.cheap_model`` — the user named one, so it is
       theirs to get right; it is passed through untouched.
    2. The Anthropic pin, but only when the configured provider is Anthropic's
       own API. A bare ``claude-haiku-4-5`` is not a valid slug on OpenRouter
       (which wants ``anthropic/claude-haiku-4-5``) and does not exist at all
       on OpenAI or a local endpoint.
    3. The provider's own default — never cheaper, but always a slug the
       configured provider will accept.
    """
    configured = _configured_cheap_model()
    if configured:
        return configured
    if _provider_is_anthropic():
        return _ANTHROPIC_CHEAP_MODEL
    return default_model


def reasoning_model(default_model: Optional[str]) -> Optional[str]:
    """The model work that needs the strongest reasoning should use.

    ``auxiliary.science_llm.reasoning_model`` when set, otherwise the
    provider's own default. There is no built-in pin here, unlike
    :func:`cheap_model`: the cheap role has a defensible default because
    "smallest capable model" is a claim benchmarks can support, while
    "strongest model" is a per-provider, per-budget judgement nobody can make
    on the user's behalf. The provider default is already the model every
    other ``host.llm`` call uses, so an unconfigured caller is no worse off.
    """
    return _configured_model(REASONING_MODEL_KEY) or default_model


# ── Per-workspace bridge registry ───────────────────────────────────

_bridges: Dict[str, HostBridge] = {}
_bridges_lock = threading.Lock()


def get_host_bridge(workspace: Path, store) -> HostBridge:
    key = str(Path(workspace))
    with _bridges_lock:
        bridge = _bridges.get(key)
        if bridge is None:
            bridge = HostBridge(workspace, store)
            bridge.start()
            _bridges[key] = bridge
        return bridge


def shutdown_bridges() -> None:
    with _bridges_lock:
        for bridge in _bridges.values():
            bridge.stop()
        _bridges.clear()
