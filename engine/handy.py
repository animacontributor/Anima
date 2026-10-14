"""Optional Handy 2 control through the authenticated Handy REST API v3.

Credentials stay in memory/handy.json (or environment variables), never in
model context. Reads are cached briefly; commands are serialized and never
retried automatically, since a timed-out write may already have happened.
"""
from __future__ import annotations

import json
import http.client
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config

API_URL = "https://www.handyfeeling.com/api/handy-rest/v3"
REVISION = "hrpp-stop-and-slot-verification-v7"
BASE_TOOL_NAMES = {"set_speed", "stop_device", "get_device_status"}
PATTERN_TOOL_NAMES = {"list_device_patterns", "play_device_pattern", "set_pattern_playback_rate"}
TOOL_NAMES = BASE_TOOL_NAMES | PATTERN_TOOL_NAMES
_lock = threading.RLock()
_cached: dict | None = None
_cache_key: tuple | None = None
_checked_at = 0.0
_issued_token: tuple | None = None  # (credential identity, client token, monotonic expiry)
_patterns_cache = None
_patterns_key = None
_patterns_at = 0.0


class HandyError(Exception):
    """A credential-free error safe to return to the model."""


def _saved_credentials() -> dict:
    try:
        saved = json.loads((config.MEMORY_DIR / "handy.json").read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    return saved


def _credentials() -> tuple[str, str]:
    saved = _saved_credentials()
    key = os.environ.get("HANDY_CONNECTION_KEY") or saved.get("connection_key", "")
    token = os.environ.get("HANDY_API_TOKEN") or saved.get("api_token", "")
    return str(key or "").strip(), str(token or "").strip()


def _application_key() -> str:
    return str(os.environ.get("HANDY_APPLICATION_KEY") or
               _saved_credentials().get("application_key", "") or "").strip()


def configured() -> bool:
    key, token = _credentials()
    return not getattr(config, "OFFLINE", False) and bool(key and (token or _application_key()))


def speed_limits() -> tuple[int, int]:
    low = getattr(config, "HANDY_MIN_SPEED", 1)
    high = getattr(config, "HANDY_MAX_SPEED", 50)
    if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 100:
        raise HandyError("speed limits must be integers with 0 <= HANDY_MIN_SPEED <= HANDY_MAX_SPEED <= 100")
    return low, high


def _seconds(name: str, default: float) -> float:
    try:
        value = float(getattr(config, name, default))
    except (ValueError, TypeError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _record_command(path: str, data: dict | None, result=None, error: str = "") -> None:
    """Only numeric control fields; never auth URLs, credentials or remote text."""
    if path not in ("/mode2", "/hamp/velocity", "/hamp/start", "/hamp/state", "/hrpp/state",
                    "/hrpp/start", "/hrpp/stop", "/hrpp/playbackrate", "/hrpp/amplitude") and not re.fullmatch(r"/hrpp/patterns/\d+/activate", path):
        return
    if data is None and path not in ("/hamp/state", "/hrpp/state"):
        return
    def fields(value):
        if not isinstance(value, dict):
            return "ok" if value == "ok" else {}
        numeric = {k: v for k, v in value.items()
                   if k in ("mode", "mode_session_id", "play_state", "velocity", "direction", "current_pattern_nr",
                            "pattern_id", "pattern_slot", "playback_rate", "playback_speed", "amplitude")
                   and type(v) in (int, float, bool) and math.isfinite(v)}
        pattern = value.get("current_pattern")
        if isinstance(pattern, dict):
            for source, target in (("id", "pattern_id"), ("slot", "pattern_slot")):
                if type(pattern.get(source)) is int:
                    numeric[target] = pattern[source]
        return numeric
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "revision": REVISION, "pid": os.getpid(),
             "method": "GET" if data is None else "PUT",
             "path": path, "request": fields(data)}
    if error:
        entry["error"] = error  # only local fixed messages supplied by _json_request
    else:
        entry["result"] = fields(result)
    line = json.dumps(entry, ensure_ascii=True)
    try:
        print("[Handy] " + line, file=sys.stdout, flush=True)
    except (OSError, ValueError):
        pass  # a closed console must not affect device control
    try:
        with (config.MEMORY_DIR / "handy-events.jsonl").open("a", encoding="utf-8") as log:
            log.write(line + "\n")
    except OSError:
        try:
            print("[Handy] command log could not be written; acknowledgement is shown above.",
                  file=sys.stdout, flush=True)
        except (OSError, ValueError):
            pass


def _json_request(path: str, data: dict | None, headers: dict):
    global _issued_token
    req = urllib.request.Request(
        API_URL + path,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="PUT" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_seconds("HANDY_TIMEOUT_S", 3)) as response:
            body = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _issued_token = None
        message = f"API request failed (HTTP {e.code}) at {path.split('?')[0]}; device state is unconfirmed"
        _record_command(path, data, error=message)
        raise HandyError(message) from None
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        message = f"API request failed or timed out at {path.split('?')[0]}; device state is unconfirmed"
        _record_command(path, data, error=message)
        raise HandyError(message) from None
    if not isinstance(body, dict) or body.get("error") or "result" not in body:
        # Never echo a remote message or response: it can include credentials.
        message = f"API returned an error or invalid response at {path.split('?')[0]}; device state is unconfirmed"
        _record_command(path, data, error=message)
        raise HandyError(message)
    _record_command(path, data, result=body["result"])
    return body["result"]


def _access_token() -> str:
    """Exchange an Application Key for a short-lived, device-scoped token."""
    global _issued_token
    key, token = _credentials()
    application_key = _application_key()
    if not application_key:
        return token  # advanced setup: a genuine issued client-token
    identity = (key, application_key)
    if (_issued_token is not None and _issued_token[0] == identity
            and time.monotonic() < _issued_token[2] - 60):
        return _issued_token[1]
    result = _json_request("/auth/token/issue?" + urllib.parse.urlencode({"to": key, "ttl": 3600}),
                           None, {"X-Api-Key": application_key})
    if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
        raise HandyError("API returned an invalid access token")
    try:
        expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError("missing timezone")
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(remaining) or remaining <= 60:
            raise ValueError("expired token")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise HandyError("API returned an invalid access token expiry") from None
    _issued_token = (identity, result["token"], time.monotonic() + remaining)
    return result["token"]


def _request(path: str, data: dict | None = None):
    if not configured():
        raise HandyError("device control is unavailable")
    key, _ = _credentials()
    return _json_request(path, data, {"X-Connection-Key": key, "Authorization": "Bearer " + _access_token()})


def invalidate() -> None:
    global _cached, _cache_key, _checked_at, _patterns_cache, _patterns_key
    with _lock:
        _cached, _cache_key, _checked_at = None, None, 0.0
        _patterns_cache, _patterns_key = None, None


def _motion_state(state) -> tuple[bool, float]:
    if (not isinstance(state, dict) or type(state.get("play_state")) is not int
            or state["play_state"] not in (0, 1)
            or type(state.get("velocity")) not in (int, float)
            or not math.isfinite(state["velocity"]) or state["velocity"] < 0):
        raise HandyError("API returned invalid motion status")
    return state["play_state"] == 1, state["velocity"]


def pattern_limits() -> tuple[float, float, int]:
    low = getattr(config, "HANDY_PATTERN_MIN_RATE", 0.1)
    high = getattr(config, "HANDY_PATTERN_MAX_RATE", 1.0)
    amplitude = getattr(config, "HANDY_PATTERN_MAX_AMPLITUDE", 100)
    if (type(low) not in (int, float) or type(high) not in (int, float)
            or not math.isfinite(low) or not math.isfinite(high) or not 0 < low <= high
            or type(amplitude) is not int or not 0 <= amplitude <= 100):
        raise HandyError("pattern limits must have finite 0 < minimum rate <= maximum rate and integer amplitude 0–100")
    return low, high, amplitude


def _pattern_settings() -> dict:
    path = config.MEMORY_DIR / "handy-patterns.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict) or not isinstance(settings.get("patterns"), dict):
            raise ValueError()
        for key, row in settings["patterns"].items():
            if (not _valid_pattern_key(key) or not isinstance(row, dict) or not isinstance(row.get("name", ""), str)
                    or not isinstance(row.get("description", ""), str) or type(row.get("enabled", True)) is not bool
                    or not _valid_max_rate(row.get("max_rate", 1.0))):
                raise ValueError()
        return settings["patterns"]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise HandyError("pattern names/settings could not be read from memory/handy-patterns.json") from None


def _valid_pattern_key(key):
    return isinstance(key, str) and (key.isdecimal() or bool(re.fullmatch(r"slot:\d+", key))
                                   or (key.startswith("name:") and 0 < len(key[5:]) <= 120))


def _valid_max_rate(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _pattern_key(pattern, catalog):
    name = pattern["device_name"].strip().casefold()
    if name and sum(p["device_name"].strip().casefold() == name for p in catalog) == 1:
        return "name:" + name
    return f"slot:{pattern['slot']}"


def _pattern_preference(settings, pattern, catalog):
    key = _pattern_key(pattern, catalog)
    if key in settings:
        return settings[key], key
    legacy = str(pattern["id"])
    if legacy in settings:
        return settings[legacy], legacy
    # Older files stored only aliases under firmware IDs. Factory names let
    # us recover those entries after the IDs change without guessing slots.
    name = pattern["device_name"].strip().casefold()
    matches = [(row, old) for old, row in settings.items() if old.isdecimal() and name
               and str(row.get("device_name") or row.get("name", "")).strip().casefold() == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HandyError("multiple saved preferences match a device pattern; give it one saved preference")
    return {}, None


def _canonical_pattern_settings(settings, catalog):
    canonical = dict(settings)
    for pattern in catalog:
        row, old = _pattern_preference(settings, pattern, catalog)
        if old is not None:
            canonical.pop(old, None)
            canonical[_pattern_key(pattern, catalog)] = dict(row, device_name=pattern["device_name"], slot=pattern["slot"])
    return canonical


def _pattern_catalog(*, force=False) -> list[dict]:
    global _patterns_cache, _patterns_key, _patterns_at
    identity = (_credentials(), _application_key(), bool(getattr(config, "OFFLINE", False)))
    if not force and _patterns_cache is not None and identity == _patterns_key and time.monotonic() - _patterns_at < 60:
        if isinstance(_patterns_cache, str):
            raise HandyError(_patterns_cache)
        return [dict(p) for p in _patterns_cache]
    try:
        result = _request("/hrpp/patterns")
        # Both shapes appear in the official v3 specification/examples.
        rows = result.get("patterns") if isinstance(result, dict) else result
        if not isinstance(rows, list) or len(rows) > 256:
            raise HandyError("API returned an invalid pattern list")
        patterns, slots, ids = [], set(), set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("slot")) is not int or row["slot"] < 0
                    or type(row.get("id")) is not int or row["id"] < 0
                    or row["slot"] in slots or row["id"] in ids or not isinstance(row.get("name"), str)
                    or type(row.get("type", 0)) is not int):
                raise HandyError("API returned invalid pattern identifiers")
            slots.add(row["slot"]); ids.add(row["id"])
            patterns.append({"id": row["id"], "slot": row["slot"], "device_name": row["name"][:120],
                             "type": {1: "script", 3: "wave"}.get(row.get("type"), "pattern")})
        _patterns_cache = patterns
    except HandyError as error:
        _patterns_cache = str(error)
        raise
    finally:
        _patterns_key, _patterns_at = identity, time.monotonic()
    return [dict(p) for p in patterns]


def list_device_patterns(*, include_disabled=False, force=True) -> dict:
    """Read the catalog in any mode, without starting or stopping movement."""
    with _lock:
        preferences = _pattern_settings()
        patterns = _pattern_catalog(force=force)
        result = []
        try:
            low, high, _ = pattern_limits()
        except HandyError:
            low = high = None  # Keeper can edit labels/maxima while fixing global settings.
        for pattern in patterns:
            saved, _ = _pattern_preference(preferences, pattern, patterns)
            if (not isinstance(saved, dict) or not isinstance(saved.get("name", ""), str)
                    or not isinstance(saved.get("description", ""), str) or type(saved.get("enabled", True)) is not bool):
                raise HandyError("invalid pattern names/settings in memory/handy-patterns.json")
            name = saved.get("name", "").strip() or pattern["device_name"].strip() or f"Pattern {pattern['slot']}"
            pattern.update(name=name[:120], description=saved.get("description", "")[:500],
                           enabled=saved.get("enabled", True), selector=f"slot:{pattern['slot']}",
                           preference_key=_pattern_key(pattern, patterns), max_rate=saved.get("max_rate", 1.0),
                           effective_max_rate=min(saved.get("max_rate", 1.0), high) if high is not None else None,
                           min_playback_rate=low / min(saved.get("max_rate", 1.0), high) if high is not None else None)
            if include_disabled or pattern["enabled"]:
                result.append(pattern)
        return {"patterns": result}


def save_pattern_names(changes) -> dict:
    """Keeper-only local names and exclusions; never a device command."""
    if not isinstance(changes, list) or len(changes) > 256:
        raise HandyError("expected a list of pattern names/settings")
    with _lock:
        settings = _pattern_settings()
        # Loading the editor populates this read-only catalog. Saving remains
        # a local operation, even if the device disconnects after loading.
        catalog = _patterns_cache if isinstance(_patterns_cache, list) else []
        settings = _canonical_pattern_settings(settings, catalog)
        changed_ids = set()
        changed_keys = set()
        for row in changes:
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 0
                    or row["id"] in changed_ids or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("description", ""), str) or type(row.get("enabled", True)) is not bool):
                raise HandyError("each pattern needs a unique numeric id, a name, a description and an enabled flag")
            changed_ids.add(row["id"])
            key = row.get("preference_key")
            pattern = next((p for p in catalog if p["id"] == row["id"]), None)
            if key is None:
                key = _pattern_key(pattern, catalog) if pattern else str(row["id"])
            if not _valid_pattern_key(key) or key in changed_keys:
                raise HandyError("each pattern needs a unique saved preference key")
            changed_keys.add(key)
            maximum = row.get("max_rate", settings.get(key, {}).get("max_rate", 1.0))
            if not _valid_max_rate(maximum):
                raise HandyError("each pattern maximum playback rate must be a finite positive number")
            name, description = row["name"].strip(), row.get("description", "").strip()
            if len(name) > 120 or len(description) > 500 or any(ord(c) < 32 for c in name + description) or re.fullmatch(r"slot:\d+", name, re.I):
                raise HandyError("use a short name (120 characters) and description (500), without control characters or slot selectors")
            settings[key] = {"name": name, "description": description, "enabled": row.get("enabled", True), "max_rate": maximum}
            if pattern:
                settings[key].update(device_name=pattern["device_name"], slot=pattern["slot"])
        names = [row.get("name", "").strip().casefold() for row in settings.values()
                 if isinstance(row, dict) and row.get("enabled", True) and row.get("name", "").strip()]
        if len(names) != len(set(names)):
            raise HandyError("give enabled patterns unique names")
        path = config.MEMORY_DIR / "handy-patterns.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps({"patterns": settings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            if _pattern_settings() != settings:
                raise HandyError("saved pattern preferences could not be verified")
        except OSError:
            raise HandyError("pattern names/settings could not be saved") from None
        return {"saved": True, "path": str(path.resolve()), "patterns": settings}


def _hrpp_state(state) -> dict:
    if not isinstance(state, dict) or type(state.get("play_state")) is not int or state["play_state"] not in range(4):
        raise HandyError("API returned invalid pattern playback status")
    rate, amplitude = state.get("playback_rate", state.get("playback_speed")), state.get("amplitude")
    if (type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0
            or type(amplitude) not in (int, float) or not math.isfinite(amplitude) or not 0 <= amplitude <= 1):
        raise HandyError("API returned invalid pattern playback settings")
    current = state.get("current_pattern") or {}
    if not isinstance(current, dict):
        raise HandyError("API returned invalid active pattern")
    pattern_id, slot = current.get("id", state.get("current_pattern_nr")), current.get("slot")
    if any(value is not None and (type(value) is not int or value < 0) for value in (pattern_id, slot)):
        raise HandyError("API returned invalid active pattern identifiers")
    name = current.get("name")
    if name is not None and not isinstance(name, str):
        raise HandyError("API returned invalid active pattern name")
    return {"running": state["play_state"] == 2, "pattern_id": pattern_id, "pattern_device_name": name,
            "pattern_slot": slot, "playback_rate": rate, "amplitude": round(amplitude * 100, 4)}


def _validate_pattern_values(rate=None, amplitude=None):
    low, high, cap = pattern_limits()
    if speed_limits()[1] == 0 or cap == 0:
        raise HandyError("movement commands are disabled")
    if rate is not None and (type(rate) not in (int, float) or not math.isfinite(rate) or not low <= rate <= high):
        raise HandyError(f"pattern playback rate must be between {low:g} and {high:g} times normal speed")
    if amplitude is not None and (type(amplitude) is not int or not 0 <= amplitude <= cap):
        raise HandyError(f"pattern amplitude must be an integer from 0 to {cap}% within the device's saved endpoints")


def _validate_relative_rate(rate):
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 1:
        raise HandyError("playback rate must be a number above 0 and at most 1, relative to the pattern's configured maximum")


def _device_pattern_rate(rate, pattern):
    _validate_relative_rate(rate)
    low, high, _ = pattern_limits()
    maximum = min(pattern["max_rate"], high)
    actual = rate * maximum
    if math.isclose(actual, low, rel_tol=1e-9, abs_tol=1e-12):
        actual = low
    if actual < low:
        raise HandyError(f"this pattern requires playback_rate >= {low / maximum:g} to respect the configured minimum device rate {low:g}")
    _validate_pattern_values(actual)
    return actual


def _reported_pattern_state(state, pattern):
    maximum = min(pattern["max_rate"], pattern_limits()[1])
    return dict(state, device_playback_rate=state["playback_rate"],
                playback_rate=state["playback_rate"] / maximum,
                max_rate=pattern["max_rate"], effective_max_rate=maximum, pattern=pattern["name"])


def _current_pattern(status):
    patterns = list_device_patterns(include_disabled=True, force=False)["patterns"]
    matches = [p for p in patterns if (p["slot"] == status.get("pattern_slot") if status.get("pattern_slot") is not None
                                     else p["id"] == status.get("pattern_id"))]
    if len(matches) != 1:
        raise HandyError("active pattern could not be identified; select a pattern first")
    return matches[0]


def _pattern_failure(error):
    try:
        stop_device()
    except HandyError:
        raise HandyError(f"{error}; stop could not be confirmed") from None
    raise HandyError(f"{error}; device was stopped") from None


def play_device_pattern(pattern: str, playback_rate: float = 1.0) -> dict:
    """the friend's playback always uses the full existing device stroke envelope."""
    return _play_device_pattern(pattern, playback_rate, 100, include_disabled=False)


def test_device_pattern(slot: int, playback_rate: float = 1.0, amplitude: int = 100) -> dict:
    """Keeper's panel test, including patterns excluded from the friend's choices."""
    if type(slot) is not int or slot < 0:
        raise HandyError("choose a numeric pattern slot from the panel's pattern list")
    return _play_device_pattern(f"slot:{slot}", playback_rate, amplitude, include_disabled=True)


def _play_device_pattern(pattern: str, playback_rate: float, amplitude: int, *, include_disabled: bool) -> dict:
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 120:
        raise HandyError("choose a pattern name or slot selector returned by list_device_patterns")
    if type(playback_rate) not in (int, float) or type(amplitude) is not int:
        raise HandyError("pattern playback rate must be a number and amplitude an integer percentage")
    _validate_relative_rate(playback_rate)
    _validate_pattern_values(amplitude=amplitude)
    with _lock:
        status = get_device_status()
        if not status["available"]:
            raise HandyError("device is unavailable; no movement command was sent")
        choices = list_device_patterns(include_disabled=include_disabled)["patterns"]
        matches = [p for p in choices if pattern.strip().casefold() in (p["name"].casefold(), p["selector"])]
        if len(matches) != 1:
            raise HandyError("pattern is unknown, disabled or ambiguous; use list_device_patterns to choose an exact name or slot selector")
        selected = matches[0]
        device_rate = _device_pattern_rate(playback_rate, selected)
        try:
            # Use the same stop as the keeper's Stop Handy button for EVERY
            # selection. Idle stops all protocols before HRPP is initialized.
            # Neither slider endpoints nor stroke boundaries are written.
            stop_device()
            _set_mode(9)
            stopped = _hrpp_state(_request("/hrpp/state"))
            if stopped["running"]:
                raise HandyError("pattern stop was not confirmed")
            state = _hrpp_state(_request(f"/hrpp/patterns/{selected['slot']}/activate", {}))
            if state["running"]:
                raise HandyError("pattern activation unexpectedly started playback")
            _hrpp_state(_request("/hrpp/playbackrate", {"playback_rate": device_rate}))
            _hrpp_state(_request("/hrpp/amplitude", {"amplitude": amplitude / 100}))
            _hrpp_state(_request("/hrpp/start", {}))
            for _ in range(3):
                state = _hrpp_state(_request("/hrpp/state"))
                if state["running"]:
                    break
            # Firmware IDs can change when HRPP is reinitialized. Slot and
            # factory name identify the activated preset across that change.
            if (not state["running"] or state["pattern_slot"] != selected["slot"]
                    or (state["pattern_device_name"] is not None and
                        state["pattern_device_name"].strip().casefold() != selected["device_name"].strip().casefold())
                    or not math.isclose(state["playback_rate"], device_rate, rel_tol=1e-5, abs_tol=1e-5)
                    or not math.isclose(state["amplitude"], amplitude, abs_tol=.01)):
                raise HandyError("requested pattern playback was not confirmed")
            return _reported_pattern_state(state, selected)
        except HandyError as error:
            _pattern_failure(error)
        finally:
            invalidate()


def _adjust_pattern(path, value, *, rate=None, amplitude=None, fixed_amplitude=False, expected_slot=None):
    _validate_pattern_values(rate, 100 if fixed_amplitude else amplitude)
    with _lock:
        status = get_device_status()
        if not status["available"] or status["mode"] != 9:
            raise HandyError("select a device pattern first; no movement command was sent")
        if expected_slot is not None and status.get("pattern_slot") != expected_slot:
            raise HandyError("active pattern changed; no movement command was sent")
        try:
            _hrpp_state(_request(path, value))
            if fixed_amplitude and status.get("amplitude") != 100:
                _hrpp_state(_request("/hrpp/amplitude", {"amplitude": 1.0}))
            state = _hrpp_state(_request("/hrpp/state"))
            if expected_slot is not None and state.get("pattern_slot") != expected_slot:
                raise HandyError("active pattern changed during the rate command")
            target, actual = (rate, state["playback_rate"]) if rate is not None else (amplitude, state["amplitude"])
            if not math.isclose(target, actual, rel_tol=1e-5, abs_tol=.01):
                raise HandyError("requested pattern setting was not confirmed")
            if fixed_amplitude and not math.isclose(state["amplitude"], 100, abs_tol=.01):
                raise HandyError("fixed 100% pattern amplitude was not confirmed")
            return state
        except HandyError as error:
            _pattern_failure(error)
        finally:
            invalidate()


def set_pattern_playback_rate(playback_rate: float) -> dict:
    _validate_relative_rate(playback_rate)
    _validate_pattern_values(amplitude=100)
    with _lock:
        status = get_device_status()
        if not status["available"] or status["mode"] != 9:
            raise HandyError("select a device pattern first; no movement command was sent")
        pattern = _current_pattern(status)
        device_rate = _device_pattern_rate(playback_rate, pattern)
        state = _adjust_pattern("/hrpp/playbackrate", {"playback_rate": device_rate}, rate=device_rate,
                                fixed_amplitude=True, expected_slot=pattern["slot"])
        return _reported_pattern_state(state, pattern)


def set_pattern_amplitude(amplitude: int) -> dict:
    if type(amplitude) is not int:
        raise HandyError("pattern amplitude must be an integer percentage")
    return _adjust_pattern("/hrpp/amplitude", {"amplitude": amplitude / 100}, amplitude=amplitude)


def get_device_status(*, force: bool = True) -> dict:
    """Read connectivity and mode, and HAMP speed when that mode is active."""
    global _cached, _cache_key, _checked_at
    with _lock:
        creds = _credentials()
        low, high = getattr(config, "HANDY_MIN_SPEED", 1), getattr(config, "HANDY_MAX_SPEED", 50)
        key = (creds, _application_key(), bool(getattr(config, "OFFLINE", False)), low, high,
               getattr(config, "HANDY_TIMEOUT_S", 3), getattr(config, "HANDY_PATTERN_MIN_RATE", 0.1),
               getattr(config, "HANDY_PATTERN_MAX_RATE", 1.0), getattr(config, "HANDY_PATTERN_MAX_AMPLITUDE", 100))
        if (not force and _cached is not None and key == _cache_key
                and time.monotonic() - _checked_at < _seconds("HANDY_STATUS_CACHE_S", 10)):
            return dict(_cached)
        status = {"connected": False, "available": False, "mode": None,
                  "running": None, "speed": None, "reported_velocity": None,
                  "min_speed": low, "max_speed": high}
        if configured():
            try:
                connected = _request("/connected")
                if not isinstance(connected, dict) or type(connected.get("connected")) is not bool:
                    raise HandyError("API returned invalid connectivity")
                status["connected"] = connected["connected"]
                if status["connected"]:
                    mode = _request("/mode")
                    if not isinstance(mode, dict) or type(mode.get("mode")) is not int or mode["mode"] not in range(10):
                        raise HandyError("API returned an invalid device mode")
                    status["mode"] = mode["mode"]
                    # Maintenance and firmware updates cannot be controlled.
                    status["available"] = mode["mode"] not in (3, 5)
                    if mode["mode"] == 0:
                        running, velocity = _motion_state(_request("/hamp/state"))
                        status["running"] = running
                        status["reported_velocity"] = velocity
                        # The v3 state schema does not constrain velocity to
                        # [0, 1] and even contains a 100-valued example. Only
                        # convert fractional readings; keep other values raw
                        # rather than guessing whether they are percent/mm/s.
                        status["speed"] = (round(velocity * 100) if velocity <= 1 else None) if running else 0
                    elif mode["mode"] == 7:
                        status.update(running=False, speed=0)
                    elif mode["mode"] == 9:
                        status.update(_hrpp_state(_request("/hrpp/state")))
                        status["device_playback_rate"] = status["playback_rate"]
                        try:
                            status.update(_reported_pattern_state(status, _current_pattern(status)))
                        except HandyError:
                            # Status remains read-only and useful if the catalog
                            # is unavailable; label the raw device rate explicitly.
                            status["relative_playback_rate_unavailable"] = True
            except HandyError as e:
                status.update(available=False, error=str(e))
        _cached, _cache_key, _checked_at = dict(status), key, time.monotonic()
        return status


def available() -> bool:
    return get_device_status(force=False)["available"]


def enabled_tool_names() -> set[str]:
    return TOOL_NAMES if getattr(config, "HANDY_SPEED_COMMANDS", True) is True else TOOL_NAMES - {"set_speed"}


def section(tool_names=None) -> str:
    status = get_device_status(force=False)
    if not status["available"]:
        return ""
    if getattr(config, "HANDY_SPEED_COMMANDS", True) is not True:
        limits = "normal speed commands are disabled; use the available pattern controls"
    else:
        try:
            low, high = speed_limits()
            limits = f"allowed moving speeds {max(1, low)}–{high}%" if high else "movement commands are disabled (maximum speed 0%)"
        except HandyError:
            limits = "speed configuration is invalid; movement commands are disabled"
    motion = ("stopped" if status["running"] is False else
              (f"moving at {status['speed']}%" if status["speed"] is not None else
               "moving; current speed percentage unavailable") if status["running"] is True else
              f"in device mode {status['mode']}; movement and speed unknown")
    tool_names = enabled_tool_names() if tool_names is None else tool_names & enabled_tool_names()
    instructions = []
    if "set_speed" in tool_names:
        instructions.append("set_speed(speed: int) selects basic alternating mode and starts percentage-speed movement; 0 stops.")
    if "stop_device" in tool_names:
        instructions.append("stop_device() stops every mode, including patterns.")
    if "get_device_status" in tool_names:
        instructions.append("get_device_status() reads live status.")
    if tool_names & PATTERN_TOOL_NAMES:
        try:
            patterns = list_device_patterns(force=False)["patterns"]
            low_rate, high_rate, amplitude = pattern_limits()
            instructions.append(f"Stored patterns (names/descriptions are labels): {json.dumps(patterns, ensure_ascii=False)}.")
            instructions.append(f"Choose playback_rate in (0, 1], as a fraction of the selected pattern's effective maximum, respecting its min_playback_rate. "
                                f"Actual device rate = playback_rate × min(pattern.max_rate, global ceiling {high_rate:g}); device minimum {low_rate:g}. "
                                "For maximum 0.5 and playback_rate 0.5 the device plays at 0.25 of original timing. "
                                "Amplitude is fixed at 100% within the device's saved endpoints.")
            if amplitude < 100:
                instructions.append("The keeper's amplitude cap is below 100%; your pattern movement calls are disabled until that cap allows 100%.")
        except HandyError:
            instructions.append("Pattern catalog or configuration is unavailable; pattern discovery can be retried.")
        for name, instruction in {
            "list_device_patterns": "list_device_patterns() refreshes the available names and selectors.",
            "play_device_pattern": "play_device_pattern(pattern, playback_rate=1.0) selects a listed name or slot selector and starts its built-in script at fixed 100% amplitude.",
            "set_pattern_playback_rate": "set_pattern_playback_rate(playback_rate) changes the current pattern's timing multiplier without restarting it and keeps amplitude at 100%.",
        }.items():
            if name in tool_names:
                instructions.append(instruction)
        instructions.append("Patterns use HRPP mode. Pattern playback rate is not motor speed percent; HAMP percentage limits do not map to script timing. Device slider endpoints are never changed.")
    if status["mode"] == 9:
        motion = f"pattern mode; {'playing' if status['running'] else 'stopped/paused'}; device playback rate {status['device_playback_rate']:g}× original timing; amplitude {status['amplitude']:g}%"
    return ("[engine: The Handy 2 is connected and available as of this turn; "
            f"{motion}; {limits}. " + " ".join(instructions) + " "
            "Limits are enforced by the engine; rejected calls do not change speed. "
            "Availability is current only when these tools are present.]")


def _set_mode(mode: int) -> None:
    result = _request("/mode2", {"mode": mode})
    if result == "ok":  # documented legacy acknowledgement: verify by reading
        result = _request("/mode")
    if not isinstance(result, dict) or type(result.get("mode")) is not int or result["mode"] != mode:
        raise HandyError("device mode change was not confirmed")


def stop_device() -> dict:
    """Idle mode stops playback regardless of the current motion protocol."""
    with _lock:
        try:
            _set_mode(7)
            return {"stopped": True, "speed": 0}
        finally:
            invalidate()


def set_speed(speed: int) -> dict:
    if type(speed) is not int or not 0 <= speed <= 100:
        raise HandyError("speed must be an integer percentage from 0 to 100")
    if speed == 0:
        return stop_device()
    if getattr(config, "HANDY_SPEED_COMMANDS", True) is not True:
        raise HandyError("normal speed commands are disabled in Handy settings; use a device pattern or stop_device")
    low, high = speed_limits()
    if not low <= speed <= high:
        raise HandyError(f"speed must be between {max(1, low)} and {high}%; 0 always stops")
    with _lock:
        status = get_device_status(force=True)
        if not status["available"]:
            raise HandyError("device is unavailable; no movement command was sent")
        try:
            if status["mode"] != 0:
                _set_mode(0)
            # A successful write acknowledges the requested setpoint. The
            # returned velocity is current state, not a promise that the motor
            # has already reached that setpoint (it can still be zero).
            running, _ = _motion_state(_request("/hamp/velocity", {"velocity": speed / 100}))
            if not running:
                _motion_state(_request("/hamp/start", {}))
                # Handy 2 firmware can acknowledge start with velocity zero,
                # discarding the value set while stopped. Set the bounded
                # target again AFTER that acknowledged start. This is an
                # intentional setup step, never a retry of a timed-out write.
                _motion_state(_request("/hamp/velocity", {"velocity": speed / 100}))
            running, _ = _motion_state(_request("/hamp/state"))
            if not running:
                # Some firmware acknowledges start before the playback flag
                # catches up. Read back a bounded number of times; never resend
                # a movement command in response to an ambiguous result.
                for _ in range(2):
                    if running:
                        break
                    running, _ = _motion_state(_request("/hamp/state"))
            if not running:
                raise HandyError("requested movement was not confirmed")
            return {"running": True, "speed": speed}
        except HandyError as e:
            # A write timeout is ambiguous. Try to stop, never retry movement.
            try:
                stop_device()
            except HandyError:
                raise HandyError(f"{e}; stop could not be confirmed") from None
            raise HandyError(f"{e}; device was stopped") from None
        finally:
            invalidate()
