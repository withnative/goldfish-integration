# Reused from goldfish_sync.py; the hourly mirror is not included or invoked.
# Standard-library Python 3.9+. Native auth paths are set by the kit caller.
import base64
import fcntl
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
VERSION = "0.2.0"
USER_AGENT = "goldfish-review/" + VERSION
ISSUER = "https://plucky-discovery-38.authkit.app"
RESOURCE = "https://plugin.withnative.ai/mcp"
LOGIN_PORT = 53691
REDIRECT = "http://127.0.0.1:%d/callback" % LOGIN_PORT
PROTOCOL = "2026-07-28"
AUTH = Path.home() / ".config/goldfish-review/native-auth.json"
WRITE_TOOLS = {"records_write", "records_lifecycle", "records_delete", "coordination_write"}
TRANSIENT = re.compile(r"pool timed out|deadline exceeded|HTTP 50[234]|could not reach Native", re.I)
class SyncError(Exception):
    pass

def private_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(tmp), str(path))
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

class Native:
    def __init__(self, config):
        self.url = config["native_mcp_url"]
        self._auth = None
        self._next_id = 1

    # --- OAuth (browser PKCE once, then refresh tokens)
    def _token_post(self, body):
        request = urllib.request.Request(ISSUER + "/oauth2/token",
                                         data=urllib.parse.urlencode(body).encode(),
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("error", "")
            except Exception:
                detail = ""
            if detail == "invalid_grant":
                raise SyncError("Native sign-in has expired or been revoked: run `login` again")
            raise SyncError("Native sign-in server returned HTTP %d %s" % (error.code, detail))

    def _store(self, token, client_id):
        previous = self._auth or {}
        self._auth = {
            "client_id": client_id,
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token") or previous.get("refresh_token"),
            "expires_at": time.time() + int(token.get("expires_in", 300)),
        }
        private_write(AUTH, json.dumps(self._auth))

    def access_token(self):
        if self._auth is None:
            self._auth = load_json(AUTH, None)
            if not self._auth:
                raise SyncError("not signed in to Native: run `python3 %s login`" % sys.argv[0])
        if self._auth["expires_at"] - 60 > time.time():
            return self._auth["access_token"]
        token = self._token_post({"grant_type": "refresh_token",
                                  "refresh_token": self._auth["refresh_token"],
                                  "client_id": self._auth["client_id"], "resource": RESOURCE})
        self._store(token, self._auth["client_id"])
        return self._auth["access_token"]

    def login(self):
        request = urllib.request.Request(ISSUER + "/oauth2/register", data=json.dumps({
            "client_name": "goldfish-review (" + socket.gethostname() + ")",
            "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
        }).encode(), headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            client_id = json.load(response)["client_id"]
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        url = ISSUER + "/oauth2/authorize?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "resource": RESOURCE})
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                received.update({k: v[0] for k, v in query.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"goldfish-review: signed in. You can close this tab.")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", LOGIN_PORT), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        print("Opening your browser to sign in to Native. If it does not open, visit:\n" + url)
        webbrowser.open(url)
        thread.join(timeout=300)
        server.server_close()
        if received.get("state") != state:
            raise SyncError("sign-in did not complete (no matching browser redirect within 5 minutes)")
        if "code" not in received:
            raise SyncError("sign-in refused: " + received.get("error", "unknown"))
        token = self._token_post({"grant_type": "authorization_code", "code": received["code"],
                                  "redirect_uri": REDIRECT, "client_id": client_id,
                                  "code_verifier": verifier, "resource": RESOURCE})
        if not token.get("refresh_token"):
            raise SyncError("sign-in server returned no refresh token; hourly runs would stop working")
        self._store(token, client_id)
        print("Signed in. Credentials saved to %s (readable only by you)." % AUTH)

    # --- MCP (stateless Streamable HTTP, 2026-07-28)
    def tool(self, name, arguments):
        # Reads retry briefly on transient pool/deadline/gateway failures; writes never retry here.
        delays = [] if name in WRITE_TOOLS else [2, 5]
        while True:
            try:
                return self._tool(name, arguments)
            except SyncError as error:
                if not delays or not TRANSIENT.search(str(error)):
                    raise
                time.sleep(delays.pop(0))

    def _tool(self, name, arguments):
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments, "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                    "io.modelcontextprotocol/clientInfo": {"name": "goldfish-review", "version": VERSION},
                    "io.modelcontextprotocol/clientCapabilities": {}}}}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.access_token(),
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/call", "Mcp-Name": name,
            "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise SyncError("Native refused the sign-in (HTTP 401): run `login` again")
            raise SyncError("Native returned HTTP %d: %s" % (error.code, error.read()[:300].decode("utf-8", "replace")))
        except (urllib.error.URLError, socket.timeout, ConnectionError) as error:
            raise SyncError("could not reach Native (%s)" % getattr(error, "reason", error))
        message = None
        if "text/event-stream" in kind:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    candidate = json.loads(line[5:].strip())
                    if candidate.get("id") == self._next_id:
                        message = candidate
        else:
            message = json.loads(raw)
        if not message:
            raise SyncError("Native sent no reply for " + name)
        if "error" in message:
            raise SyncError("Native %s error: %s" % (name, message["error"].get("message")))
        result = message["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise NativeRefusal(text.split("\n")[0][:300])
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    def call(self, executor, operation, arguments):
        return self.tool(executor, {"operation": operation, "arguments": arguments, "format": "json"})

    def existing(self, collection_id):
        rows = self.call("sql_read", "query_sql", {
            "sql": "SELECT r.id, f.value AS gkey, w.value AS written FROM records r "
                   "JOIN facet_values f ON f.record_id = r.id AND f.key = 'goldfish_key' "
                   "LEFT JOIN facet_values w ON w.record_id = r.id AND w.key = 'goldfish_body_sha256' "
                   "WHERE r.home_id = ?1 AND r.archived = 0 ORDER BY r.id LIMIT 1000",
            "parameters": [collection_id]}).get("rows", [])
        return {row["gkey"]: {"record_id": row["id"], "written": row.get("written")} for row in rows}

    def body_digest(self, record_id):
        data = self.call("records_read", "get_record", {"id": record_id})
        record = data.get("record") if isinstance(data.get("record"), dict) else data
        if isinstance(data.get("records"), list) and data["records"]:
            record = data["records"][0]
        return record.get("body_digest")

class NativeRefusal(SyncError):
    pass
