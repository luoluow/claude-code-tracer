#!/usr/bin/env python3
"""Claude Code Tracer — base-URL forwarder (Phase 4).

A streaming reverse proxy. Claude Code is pointed here via
ANTHROPIC_BASE_URL=http://127.0.0.1:7356. We forward each request over real
HTTPS to api.anthropic.com, stream the response straight back to the client
chunk-by-chunk (no buffering), and *tap* the traffic in passing: we keep the
request body and reassemble the streaming SSE response into the final assistant
turn, then POST one ApiCall event to the tracer (CONTRACT §1).

Stdlib only — no extra dependencies.
"""

import http.client
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# One logical session per forwarder run, so a run's API turns group together.
# (Claude Code sends no session key on API requests, so we can't correlate to
# the hook session_id; an explicit x-session-id header still wins if present.)
RUN_ID = time.strftime("api-%Y%m%d_%H%M%S")

PORT = int(os.environ.get("FORWARDER_PORT", "7356"))
UPSTREAM_HOST = os.environ.get("FORWARDER_UPSTREAM_HOST", "api.anthropic.com")
UPSTREAM_PORT = int(os.environ.get("FORWARDER_UPSTREAM_PORT", "443"))
UPSTREAM_TLS = os.environ.get("FORWARDER_UPSTREAM_TLS", "1") != "0"
TRACER_HOST = os.environ.get("TRACER_HOST", "127.0.0.1")
TRACER_PORT = int(os.environ.get("TRACER_PORT", "7355"))

# Hop-by-hop headers must not be forwarded.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

CHUNK = 8192


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")

    # --- core --------------------------------------------------------------

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "accept-encoding")
        }
        headers["Host"] = UPSTREAM_HOST
        # Force identity so the SSE we tap is plaintext, not gzip/br. The client
        # gets an uncompressed response (localhost hop — bandwidth is irrelevant).
        headers["Accept-Encoding"] = "identity"

        if UPSTREAM_TLS:
            conn = http.client.HTTPSConnection(
                UPSTREAM_HOST, UPSTREAM_PORT,
                context=ssl.create_default_context(), timeout=600,
            )
        else:
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=600,
            )

        try:
            try:
                conn.request(method, self.path, body=body, headers=headers)
                upstream = conn.getresponse()
            except OSError as e:
                self.send_error(502, "Upstream connection failed")
                print(f"[forwarder] upstream error: {e}", file=sys.stderr)
                return

            # Mirror status + headers to the client (drop hop-by-hop;
            # let our own connection handling set length/encoding).
            self.send_response(upstream.status)
            for k, v in upstream.getheaders():
                if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()

            # Stream through, tapping a copy of the SSE bytes.
            tap = bytearray()
            is_sse = "text/event-stream" in (
                upstream.getheader("Content-Type") or ""
            )
            while True:
                chunk = upstream.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if is_sse:
                    tap.extend(chunk)
        finally:
            conn.close()

        if method == "POST" and is_sse and tap:
            self._emit_apicall(body, bytes(tap))

    # --- tap / reassembly --------------------------------------------------

    def _emit_apicall(self, request_body, sse_bytes):
        try:
            req = json.loads(request_body or b"{}")
        except ValueError:
            req = {}
        response, usage = _reassemble_sse(sse_bytes)
        if not response.get("content") and not usage:
            print(f"[forwarder] WARNING: empty reassembly; first bytes: {sse_bytes[:80]!r}",
                  file=sys.stderr)
        session_id = self.headers.get("x-session-id") or RUN_ID
        event = {
            "session_id": session_id,
            "hook_event_name": "ApiCall",
            "request": req,
            "response": response,
            "usage": usage,
        }
        _post_event(event)


def _reassemble_sse(sse_bytes):
    """Rebuild the final assistant turn from an Anthropic SSE stream.

    Returns (response, usage) where response mirrors a Messages API result
    (role, model, stop_reason, content blocks) and usage holds token counts.
    """
    text = sse_bytes.decode("utf-8", "replace").replace("\r\n", "\n")
    response = {"role": "assistant", "content": []}
    usage = {}
    blocks = {}  # index -> {"type": ..., accumulator}

    for raw in text.split("\n\n"):
        data = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            evt = json.loads(data)
        except ValueError:
            continue

        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message", {})
            response["id"] = msg.get("id")
            response["model"] = msg.get("model")
            response["role"] = msg.get("role", "assistant")
            usage.update(msg.get("usage", {}))
        elif etype == "content_block_start":
            blocks[evt["index"]] = dict(evt.get("content_block", {}))
            blocks[evt["index"]].setdefault("_text", "")
            blocks[evt["index"]].setdefault("_json", "")
        elif etype == "content_block_delta":
            blk = blocks.setdefault(evt["index"], {"_text": "", "_json": ""})
            d = evt.get("delta", {})
            if d.get("type") == "text_delta":
                blk["_text"] += d.get("text", "")
            elif d.get("type") == "thinking_delta":
                blk["_text"] += d.get("thinking", "")
            elif d.get("type") == "input_json_delta":
                blk["_json"] += d.get("partial_json", "")
        elif etype == "message_delta":
            delta = evt.get("delta", {})
            if "stop_reason" in delta:
                response["stop_reason"] = delta["stop_reason"]
            if "stop_sequence" in delta:
                response["stop_sequence"] = delta["stop_sequence"]
            usage.update(evt.get("usage", {}))

    for idx in sorted(blocks):
        response["content"].append(_finalize_block(blocks[idx]))
    return response, usage


def _finalize_block(blk):
    btype = blk.get("type")
    if btype in ("text", "thinking"):
        out = {"type": btype}
        out["thinking" if btype == "thinking" else "text"] = blk["_text"]
        return out
    if btype == "tool_use":
        out = {"type": "tool_use", "id": blk.get("id"), "name": blk.get("name")}
        try:
            out["input"] = json.loads(blk["_json"]) if blk["_json"] else {}
        except ValueError:
            out["input"] = {"_raw": blk["_json"]}
        return out
    # Fallback: hand back what we have, minus internal accumulators.
    return {k: v for k, v in blk.items() if not k.startswith("_")}


def _post_event(event):
    try:
        conn = http.client.HTTPConnection(TRACER_HOST, TRACER_PORT, timeout=5)
        conn.request(
            "POST", "/event",
            body=json.dumps(event),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
        conn.close()
    except OSError as e:
        print(f"[forwarder] could not POST ApiCall to tracer: {e}",
              file=sys.stderr)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    scheme = "https" if UPSTREAM_TLS else "http"
    print(f"[forwarder] listening on http://127.0.0.1:{PORT}")
    print(f"[forwarder] upstream  -> {scheme}://{UPSTREAM_HOST}:{UPSTREAM_PORT}")
    print(f"[forwarder] ApiCall   -> http://{TRACER_HOST}:{TRACER_PORT}/event")
    server.serve_forever()


if __name__ == "__main__":
    main()
