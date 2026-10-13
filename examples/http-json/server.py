"""Standalone prediction fixture; deliberately has no gateway or Bront imports."""

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/predict":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            features = body["features"]
            if (
                not isinstance(features, list)
                or not features
                or not all(
                    isinstance(value, (float, int))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in features
                )
            ):
                raise ValueError("Expected finite features")
            result = sum(features)
            payload = json.dumps({"prediction": result}, allow_nan=False).encode()
        except (KeyError, TypeError, ValueError, OverflowError):
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/predict", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
