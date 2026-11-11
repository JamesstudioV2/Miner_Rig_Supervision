"""Low level LAN transports: CGMiner TCP API (port 4028) and HTTP (digest auth)."""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request


class MinerError(Exception):
    pass


class MinerConnectionError(MinerError):
    pass


class MinerAuthError(MinerError):
    pass


_INF_RE = re.compile(r"(?<=[:\[,])\s*-?(?:inf|nan)\b", re.IGNORECASE)
_NO_PROXY = urllib.request.ProxyHandler({})  # LAN traffic must never go through a proxy


def _reason(exc) -> str:
    if isinstance(exc, socket.timeout):
        return "időtúllépés"
    if isinstance(exc, ConnectionRefusedError):
        return "a kapcsolat elutasítva"
    if isinstance(exc, urllib.error.URLError) and getattr(exc, "reason", None) is not None:
        return _reason(exc.reason) if isinstance(exc.reason, Exception) else str(exc.reason)
    text = str(exc)
    if "timed out" in text:
        return "időtúllépés"
    if "No route to host" in text or "Host is unreachable" in text:
        return "a hoszt nem érhető el"
    return text or exc.__class__.__name__


def parse_cgminer_json(data: bytes) -> dict:
    """Decode a CGMiner/BMMiner API reply, repairing the usual firmware bugs."""
    text = data.decode("utf-8", errors="replace").replace("\x00", "").strip()
    if not text:
        raise MinerError("üres válasz")
    try:
        obj = json.loads(text, strict=False)
    except ValueError:
        fixed = (text.replace("\n", "").replace(",}", "}").replace("}{", "},{").replace("[,{", "[{")
                 .replace('"inf"', "0").replace('"nan"', "0"))
        fixed = _INF_RE.sub("0", fixed)
        if fixed.startswith(","):
            fixed = "{" + fixed[1:]
        try:
            obj = json.loads(fixed, strict=False)
        except ValueError as exc:
            raise MinerError("érvénytelen JSON válasz: %s" % exc)
    if not isinstance(obj, dict):
        raise MinerError("váratlan válasz formátum")
    status = obj.get("STATUS")
    if isinstance(status, list) and status and isinstance(status[0], dict):
        if status[0].get("STATUS") in ("E", "F"):
            raise MinerError(str(status[0].get("Msg") or "API hiba"))
    return obj


def cgminer_command(host: str, port: int, command: str, parameter=None, timeout: float = 4.0) -> dict:
    payload = {"command": command}
    if parameter is not None:
        payload["parameter"] = parameter
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as exc:
        raise MinerConnectionError("API kapcsolódás sikertelen (%s:%s): %s" % (host, port, _reason(exc)))
    chunks = []
    try:
        sock.settimeout(timeout)
        sock.sendall(json.dumps(payload).encode("utf-8"))
        total = 0
        deadline = time.monotonic() + timeout * 2
        while True:
            try:
                buf = sock.recv(65536)
            except socket.timeout:
                if chunks:
                    break
                raise MinerConnectionError("API időtúllépés (%s:%s)" % (host, port))
            if not buf:
                break
            chunks.append(buf)
            total += len(buf)
            if buf.endswith(b"\x00") or total > 8000000 or time.monotonic() > deadline:
                break
    except OSError as exc:
        if not chunks:
            raise MinerConnectionError("API hiba (%s:%s): %s" % (host, port, _reason(exc)))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return parse_cgminer_json(b"".join(chunks))


def http_request(url: str, method: str = "GET", data=None, headers=None, username=None, password=None,
                 timeout: float = 5.0):
    """Perform an HTTP request (digest or basic auth when credentials given).

    Returns ``(status, body_bytes)``.  Raises MinerAuthError on 401 and
    MinerConnectionError on network failures.
    """
    handlers = [_NO_PROXY]
    if username is not None:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, username, password or "")
        handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
    opener = urllib.request.build_opener(*handlers)
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            content = exc.read()
        except Exception:  # noqa: BLE001
            content = b""
        if exc.code == 401:
            raise MinerAuthError("hibás felhasználónév vagy jelszó (HTTP 401)")
        return exc.code, content
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise MinerConnectionError("HTTP hiba (%s): %s" % (url.split("/")[2], _reason(exc)))


def http_json(url: str, method: str = "GET", data=None, headers=None, username=None, password=None,
              timeout: float = 5.0, allow_empty: bool = False):
    status, body = http_request(url, method, data, headers, username, password, timeout)
    if status != 200:
        raise MinerError("HTTP %d válasz (%s)" % (status, url.split("/", 3)[-1]))
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        if allow_empty:
            return {}
        raise MinerError("üres HTTP válasz")
    try:
        return json.loads(text, strict=False)
    except ValueError:
        try:
            return parse_cgminer_json(body)
        except MinerError:
            raise MinerError("érvénytelen JSON a webes válaszban")
