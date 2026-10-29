"""Record/replay for the real-Haiku ("llm"-marked) tests.

The seam is the Anthropic SDK object ``AnthropicClient`` already accepts: everything the app does
around the HTTP call (retries, cost, ``llm_calls`` rows, error mapping, logging) still runs for real;
only ``sdk.messages.create(**params)`` is answered from a cassette.

* Default (replay): the test's cassette ``tests/fixtures/llm_cassettes/<test module path>.json``
  (shared by the module's tests) answers each request, keyed by a hash of the exact request params. No key, no network, ~0 s.
  A request the cassette does not hold FAILS with the command to re-record.
  A module with no cassette file at all is skipped (reason names the recorder) unless ``SALES0_TEST_LLM_STRICT=1``.
* ``SALES0_TEST_LLM_REFRESH=1`` (record): the real Anthropic SDK is called (needs a real key) and
  the cassette is rewritten. Use ``scripts/record_llm_cassettes.sh``.

Wiring (tests/conftest.py)::

    pytest_plugins = ["tests.llm_cassette"]

The plugin patches ``anthropic.Anthropic`` for every test marked ``llm`` (not ``eval``, not ``e2e``), so
``get_llm_client()``, ``AnthropicClient(settings)`` and monkeypatched keys all go through the cassette.
Subprocesses (``python -m sales0.cli check-llm``) are covered through ``sitecustomize`` (see
``tests/llm_cassette_boot``): the plugin exports the cassette path/mode and a PYTHONPATH to it.

Keying: sha256 of the canonical JSON of the params with ISO dates/timestamps and UUIDs masked, plus a key class
("ok" or "bad" for a key containing "invalid"), so the bad-key tests do not collide with the good ones.
A second, looser key (all digits masked) is the fallback so ids / counters in prompts do not break replay.
Repeated identical requests replay the recorded responses in order (the last one repeats), which keeps
prompt-cache tests (first call writes the cache, second reads it) faithful.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CASSETTE_ROOT = REPO_ROOT / "tests" / "fixtures" / "llm_cassettes"
BOOT_DIR = Path(__file__).resolve().parent / "llm_cassette_boot"
REFRESH_ENV = "SALES0_TEST_LLM_REFRESH"
STRICT_ENV = "SALES0_TEST_LLM_STRICT"  # 1: a module without a cassette FAILS (CI) instead of skipping
FILE_ENV = "SALES0_LLM_CASSETTE_FILE"  # exported for subprocesses
RECORD_HINT = "re-record with: scripts/record_llm_cassettes.sh (SALES0_TEST_LLM_REFRESH=1, real key)"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{32}\b")
_DIGITS = re.compile(r"\d+")


def refresh_mode() -> bool:
    return os.environ.get(REFRESH_ENV, "") in {"1", "true", "yes"}


def key_class(api_key: str | None) -> str:
    return "bad" if api_key and "invalid" in api_key.lower() else "ok"


def _canonical(params: dict[str, Any], kclass: str) -> str:
    raw = json.dumps({"k": kclass, "p": params}, sort_keys=True, default=str, ensure_ascii=False)
    return _UUID.sub("<uuid>", _DATE.sub("<date>", raw))


def request_keys(params: dict[str, Any], kclass: str = "ok") -> tuple[str, str]:
    """(exact key, loose key). The loose key also masks every run of digits."""
    canon = _canonical(params, kclass)
    return (
        hashlib.sha256(canon.encode()).hexdigest()[:24],
        hashlib.sha256(_DIGITS.sub("#", canon).encode()).hexdigest()[:24],
    )


# ---------------------------------------------------------------------------- serialisation
def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_dump(v) for v in obj]
    return obj


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        # a tool_use block's ``input`` stays a plain dict (the adapter checks isinstance(..., dict))
        return SimpleNamespace(**{k: (v if k == "input" else _ns(v)) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


def _message_record(message: Any) -> dict[str, Any]:
    data = _dump(message)
    req_id = getattr(message, "_request_id", None)
    if req_id:
        data["_request_id"] = req_id
    return {"message": data}


def _error_record(exc: Exception) -> dict[str, Any]:
    rec: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    status = getattr(exc, "status_code", None)
    if status is not None:
        rec["status"] = status
        rec["body"] = getattr(exc, "body", None)
    return {"error": rec}


def _rebuild_error(rec: dict[str, Any]) -> Exception:
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cls = getattr(anthropic, rec["type"], anthropic.APIError)
    if "status" in rec:
        response = httpx.Response(rec["status"], request=request, headers={"retry-after": "0"})
        return cls(rec["message"], response=response, body=rec.get("body"))
    if cls is anthropic.APIConnectionError or cls is anthropic.APITimeoutError:
        return cls(request=request) if cls is anthropic.APITimeoutError else cls(message=rec["message"], request=request)
    return anthropic.APIError(rec["message"], request, body=None)


# ---------------------------------------------------------------------------- the cassette
class CassetteMiss(AssertionError):
    pass


MISSES: list[str] = []  # the adapter maps SDK errors to LLMBadResponse (hiding this one): the fixture fails the test on any


class Cassette:
    def __init__(self, path: Path, *, record: bool, fresh: bool = False) -> None:
        self.path = path
        self.record = record
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self.entries: list[dict[str, Any]] = []
        if not record:
            self.entries = self._read()
        elif fresh:
            self.path.unlink(missing_ok=True)

    def reset_counters(self) -> None:
        with self._lock:
            self._counts.clear()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return list(json.loads(self.path.read_text())["entries"])

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"entries": self.entries}, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def add(self, params: dict[str, Any], kclass: str, result: dict[str, Any]) -> None:
        exact, loose = request_keys(params, kclass)
        summary = (params.get("messages") or [{}])[-1].get("content", "")
        entry = {"key": exact, "loose": loose, "model": params.get("model"), "tool": (params.get("tool_choice") or {}).get("name"),
                 "preview": (summary if isinstance(summary, str) else json.dumps(summary))[:120], **result}
        with self._lock:
            self.entries = [*self._read(), entry] if self.path.exists() else [*self.entries, entry]
            self._write()

    def lookup(self, params: dict[str, Any], kclass: str) -> dict[str, Any]:
        exact, loose = request_keys(params, kclass)
        with self._lock:
            for field, key in (("key", exact), ("loose", loose)):
                hits = [e for e in self.entries if e[field] == key]
                if hits:
                    n = self._counts.get(key, 0)
                    self._counts[key] = n + 1
                    return hits[min(n, len(hits) - 1)]
        miss = CassetteMiss(
            f"no recorded LLM response for this request in {self.path.as_posix().replace(REPO_ROOT.as_posix() + '/', '')} "
            f"(key {exact}, model {params.get('model')}, tool {(params.get('tool_choice') or {}).get('name')}); {RECORD_HINT}"
        )
        MISSES.append(str(miss))
        raise miss


class _Messages:
    def __init__(self, owner: CassetteSDK) -> None:
        self._owner = owner

    def create(self, **params: Any) -> Any:
        return self._owner.create(**params)


class CassetteSDK:
    """Stands in for ``anthropic.Anthropic(...)`` (only ``.messages.create`` is used by the adapter)."""

    def __init__(self, cassette: Cassette, *, api_key: str | None, real_factory: Any, kwargs: dict[str, Any]) -> None:
        self.cassette = cassette
        self.kclass = key_class(api_key)
        self._real_factory = real_factory
        self._kwargs = kwargs
        self._real: Any = None
        self.messages = _Messages(self)

    def create(self, **params: Any) -> Any:
        if self.cassette.record:
            return self._record(params)
        rec = self.cassette.lookup(params, self.kclass)
        time.sleep(0.002)  # the adapter reports latency_ms from a monotonic clock; keep it > 0
        if "error" in rec:
            raise _rebuild_error(rec["error"])
        data = dict(rec["message"])
        req_id = data.pop("_request_id", None)
        msg = _ns(data)
        if req_id:
            msg._request_id = req_id
        return msg

    def _record(self, params: dict[str, Any]) -> Any:
        if self._real is None:
            self._real = self._real_factory(**self._kwargs)
        try:
            message = self._real.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
            self.cassette.add(params, self.kclass, _error_record(exc))
            raise
        self.cassette.add(params, self.kclass, _message_record(message))
        return message


# ---------------------------------------------------------------------------- patching
def cassette_path_for(nodeid: str) -> Path:
    """One cassette per test MODULE (tests/integration/suggest/test_real_haiku.py ->
    <root>/integration/suggest/test_real_haiku.json). Tests share it (order- and xdist-independent
    lookup by request hash); the per-test replay counters make repeated identical requests replay in order."""
    rel = Path(nodeid.partition("::")[0]).with_suffix("")
    if rel.parts and rel.parts[0] == "tests":
        rel = Path(*rel.parts[1:])
    return CASSETTE_ROOT / rel.with_suffix(".json")


ACTIVE: Cassette | None = None  # the cassette installed for the running module
_RESET: set[Path] = set()  # record mode: cassettes already wiped in this process


def install(path: Path, *, record: bool, fresh: bool = True) -> Any:
    """Patch ``anthropic.Anthropic`` process-wide; returns an undo callable."""
    import anthropic

    real_cls = anthropic.Anthropic
    fresh = record and fresh and path not in _RESET
    _RESET.add(path)
    cassette = Cassette(path, record=record, fresh=fresh)

    def factory(*args: Any, **kwargs: Any) -> CassetteSDK:
        api_key = kwargs.get("api_key") if "api_key" in kwargs else (args[0] if args else None)
        return CassetteSDK(cassette, api_key=api_key, real_factory=real_cls, kwargs=dict(kwargs))

    global ACTIVE
    anthropic.Anthropic = factory  # type: ignore[misc,assignment]
    ACTIVE = cassette

    def undo() -> None:
        global ACTIVE
        anthropic.Anthropic = real_cls  # type: ignore[misc]
        ACTIVE = None

    return undo


def install_from_env() -> None:
    """Called by tests/llm_cassette_boot/sitecustomize.py inside a test's subprocess."""
    path = os.environ.get(FILE_ENV)
    if path:
        install(Path(path), record=refresh_mode(), fresh=False)


# ---------------------------------------------------------------------------- pytest plugin
def _eligible(item: pytest.Item) -> bool:
    if item.get_closest_marker("llm") is None or item.get_closest_marker("eval") is not None:
        return False
    parts = Path(str(item.path)).parts
    return "e2e" not in parts and "eval" not in parts


def _missing_cassette_skips(path: Path) -> bool:
    return not path.exists() and os.environ.get(STRICT_ENV, "") not in {"1", "true"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Transition aid: an eligible llm test whose module has no cassette file yet is skipped (reason names the
    recorder). Decided at collection because module-scoped fixtures would otherwise run (and miss) first.
    CI sets SALES0_TEST_LLM_STRICT=1 to fail instead."""
    if refresh_mode():
        return
    for item in items:
        if _eligible(item) and _missing_cassette_skips(cassette_path_for(item.nodeid)):
            item.add_marker(pytest.mark.skip(reason=f"no LLM cassette yet for this module; {RECORD_HINT}"))


@pytest.fixture(autouse=True, scope="module")
def _llm_cassette_module(request: pytest.FixtureRequest) -> Any:
    """Patches ``anthropic.Anthropic`` for the whole module (module-scoped fixtures of real-Haiku tests run
    before any function fixture) when it holds at least one eligible ``llm`` test."""
    module = request.node
    if not any(_eligible(i) for i in request.session.items if getattr(i, "module", None) is getattr(module, "obj", None)):
        yield None
        return
    record = refresh_mode()
    path = cassette_path_for(str(module.nodeid))
    mp = pytest.MonkeyPatch()
    MISSES.clear()
    if record:
        from sales0.config import get_settings, reset_settings

        reset_settings()
        if not get_settings().api_key_present:
            mp.undo()
            pytest.fail(f"{REFRESH_ENV}=1 needs a real SALES0_ANTHROPIC_API_KEY")
    elif not os.environ.get("SALES0_ANTHROPIC_API_KEY"):
        mp.setenv("SALES0_ANTHROPIC_API_KEY", "sk-replay-dummy")  # replay needs no real key
    mp.setenv(FILE_ENV, str(path))
    mp.setenv(REFRESH_ENV, "1" if record else "0")
    mp.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(BOOT_DIR), str(REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p))
    undo = install(path, record=record)
    try:
        yield path
    finally:
        undo()
        mp.undo()


@pytest.fixture(autouse=True)
def _llm_cassette(request: pytest.FixtureRequest, _llm_cassette_module: Any) -> Any:
    """Per test: fresh replay counters, a clean settings/client, and the test FAILS on any cassette miss
    (the adapter maps SDK errors, which would otherwise hide it)."""
    if _llm_cassette_module is None or not _eligible(request.node):
        yield None
        return
    if ACTIVE is not None:
        ACTIVE.reset_counters()
    from sales0.config import reset_settings
    from sales0.llm import reset_llm_client

    reset_settings()
    reset_llm_client()
    MISSES.clear()
    yield _llm_cassette_module
    if MISSES:
        pytest.fail("; ".join(MISSES), pytrace=False)
