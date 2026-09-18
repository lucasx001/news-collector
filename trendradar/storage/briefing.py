"""Durable briefing state. Remote errors must never reset the delivery ledger."""

import json
import os
from pathlib import Path


class BriefingStateStore:
    KEY = "state/briefing-v1.json"

    def __init__(self, backend):
        self.backend = backend
        self.etag = None
        self.path = Path(getattr(backend, "data_dir", "output")) / "briefing" / "state.json"

    def load(self):
        if self.backend.backend_name == "remote":
            try:
                response = self.backend.s3_client.get_object(
                    Bucket=self.backend.bucket_name, Key=self.KEY
                )
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code in ("NoSuchKey", "404", "NotFound"):
                    return None
                raise
            self.etag = response["ETag"]
            body = response["Body"]
            try:
                state = json.loads(body.read())
            finally:
                body.close()
        elif self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            return None
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("pending"), dict)
                or not isinstance(state.get("seen"), list)
                or "since" not in state):
            raise ValueError("简报状态损坏或版本不兼容，停止推送，避免重复发送")
        return state

    def save(self, state):
        data = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if self.backend.backend_name == "remote":
            # Optimistic concurrency: never overwrite another container's state.
            condition = {"IfMatch": self.etag} if self.etag else {"IfNoneMatch": "*"}
            response = self.backend.s3_client.put_object(
                Bucket=self.backend.bucket_name, Key=self.KEY, Body=data,
                ContentType="application/json", **condition,
            )
            self.etag = response["ETag"]
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
