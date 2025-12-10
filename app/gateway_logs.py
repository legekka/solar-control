"""
Gateway event logging - async disk writes with buffering.

Writes events and request summaries to JSONL files:
- data/gateway-logs/YYYY-MM-DD.events.jsonl - All raw events
- data/gateway-logs/YYYY-MM-DD.requests.jsonl - Request summaries (on completion)

Uses a write queue to avoid blocking the event loop during heavy traffic.
"""

import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_str_from_iso(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def classify_request_type(endpoint: Optional[str]) -> str:
    """Classify request type based on endpoint."""
    if not endpoint:
        return "unknown"
    ep = endpoint.lower()
    if "/embeddings" in ep:
        return "embedding"
    if "/chat/completions" in ep:
        return "chat"
    if "/completions" in ep:
        return "completion"
    if "/classify" in ep:
        return "classification"
    if "/rerank" in ep:
        return "rerank"
    if "/tokenize" in ep:
        return "tokenize"
    if "/detokenize" in ep:
        return "detokenize"
    return "unknown"


@dataclass
class RequestInProgress:
    """Tracks an in-flight request for building the summary."""

    request_id: str
    request_type: str = "unknown"
    model: Optional[str] = None
    resolved_model: Optional[str] = None
    endpoint: Optional[str] = None
    client_ip: Optional[str] = None
    stream: Optional[bool] = None
    start_timestamp: Optional[str] = None
    host_id: Optional[str] = None
    host_name: Optional[str] = None
    instance_id: Optional[str] = None
    instance_url: Optional[str] = None
    attempts: int = 0


@dataclass
class RequestSummary:
    """Final summary of a completed request."""

    request_id: str
    request_type: str
    status: str  # success | error | missed
    model: Optional[str]
    resolved_model: Optional[str]
    endpoint: Optional[str]
    client_ip: Optional[str]
    stream: Optional[bool]
    attempts: int
    start_timestamp: Optional[str]
    end_timestamp: str
    duration_s: Optional[float]
    host_id: Optional[str]
    host_name: Optional[str]
    instance_id: Optional[str]
    instance_url: Optional[str]
    error_message: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    decode_tps: Optional[float] = None
    decode_ms_per_token: Optional[float] = None


class GatewayLogger:
    """Async gateway event logger with write buffering."""

    # Class-level constants for tuning
    FLUSH_INTERVAL_S = 1.0  # Flush buffer every second
    MAX_BUFFER_SIZE = 100  # Flush when buffer reaches this size

    def __init__(self) -> None:
        try:
            from app.config import settings

            self.base_dir = Path(
                getattr(settings, "gateway_log_dir", "data/gateway-logs")
            )
            self.retention_days = int(
                getattr(settings, "gateway_log_retention_days", 365)
            )
        except Exception:
            self.base_dir = Path("data/gateway-logs")
            self.retention_days = 365

        self._inflight: Dict[str, RequestInProgress] = {}
        self._lock = asyncio.Lock()  # Async lock for in-flight tracking
        
        # Write buffer: filename -> list of JSON strings
        self._write_buffer: Dict[str, List[str]] = defaultdict(list)
        self._buffer_lock = asyncio.Lock()  # Async lock for buffer access
        
        # Background flush task
        self._flush_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the background flush task."""
        if self._flush_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the background flush task and flush remaining buffer."""
        if self._stop_event:
            self._stop_event.set()
        if self._flush_task:
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        # Final flush
        await self._flush_all()

    async def _flush_loop(self) -> None:
        """Background task that periodically flushes the write buffer."""
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.FLUSH_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass
            await self._flush_all()

    async def _flush_all(self) -> None:
        """Flush all buffered writes to disk."""
        async with self._buffer_lock:
            if not self._write_buffer:
                return
            # Take ownership of buffer and clear it
            to_write = dict(self._write_buffer)
            self._write_buffer = defaultdict(list)

        # Write to disk in a thread pool to avoid blocking
        if to_write:
            await asyncio.to_thread(self._sync_write_batch, to_write)

    def _sync_write_batch(self, batch: Dict[str, List[str]]) -> None:
        """Synchronously write a batch of lines to files (runs in thread pool)."""
        for filename, lines in batch.items():
            if not lines:
                continue
            path = self.base_dir / filename
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("".join(lines))
            except Exception as e:
                print(f"[GatewayLogger] Failed to write to {path}: {e}")

    async def _queue_write(self, filename: str, data: dict) -> None:
        """Queue a JSON line to be written."""
        line = json.dumps(data, ensure_ascii=False) + "\n"
        should_flush = False
        
        async with self._buffer_lock:
            self._write_buffer[filename].append(line)
            # Check if we should trigger an early flush
            total_lines = sum(len(lines) for lines in self._write_buffer.values())
            if total_lines >= self.MAX_BUFFER_SIZE:
                should_flush = True
        
        if should_flush:
            # Flush in background, don't wait
            asyncio.create_task(self._flush_all())

    def _get_date_str(self, timestamp: Optional[str] = None) -> str:
        """Get date string for file naming."""
        if timestamp:
            return _date_str_from_iso(timestamp)
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def log_event(self, event: Dict[str, Any]) -> Optional[RequestSummary]:
        """
        Log a gateway event asynchronously.

        Returns a RequestSummary if this event completes a request (for WebSocket broadcast).
        """
        etype = event.get("type")
        data = event.get("data") or {}
        timestamp = data.get("timestamp") or event.get("timestamp") or _utc_now_iso()
        request_id = data.get("request_id")

        # Add timestamp to event if missing
        if "timestamp" not in event:
            event["timestamp"] = timestamp

        # Queue raw event write (non-blocking)
        date_str = self._get_date_str(timestamp)
        await self._queue_write(f"{date_str}.events.jsonl", event)

        if not request_id:
            return None

        # Track request state for summary building
        summary = None
        async with self._lock:
            if etype == "request_start":
                endpoint = data.get("endpoint")
                self._inflight[request_id] = RequestInProgress(
                    request_id=request_id,
                    request_type=classify_request_type(endpoint),
                    model=data.get("model"),
                    endpoint=endpoint,
                    client_ip=data.get("client_ip"),
                    stream=(
                        bool(data.get("stream"))
                        if data.get("stream") is not None
                        else None
                    ),
                    start_timestamp=timestamp,
                )

            elif etype == "request_routed":
                rip = self._inflight.get(request_id)
                if not rip:
                    # Missed the start, create minimal record
                    endpoint = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(endpoint),
                        model=data.get("model"),
                        endpoint=endpoint,
                        start_timestamp=timestamp,
                    )
                    self._inflight[request_id] = rip

                rip.attempts += 1
                rip.resolved_model = data.get("resolved_model") or rip.resolved_model
                rip.host_id = data.get("host_id") or rip.host_id
                rip.host_name = data.get("host_name") or rip.host_name
                rip.instance_id = data.get("instance_id") or rip.instance_id
                rip.instance_url = data.get("instance_url") or rip.instance_url
                rip.client_ip = data.get("client_ip") or rip.client_ip

            elif etype in ("request_success", "request_error"):
                rip = self._inflight.pop(request_id, None)
                if not rip:
                    # Missed start, create minimal
                    endpoint = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(endpoint),
                        model=data.get("model"),
                        endpoint=endpoint,
                        start_timestamp=timestamp,
                    )

                # Build summary
                status = (
                    "success"
                    if etype == "request_success"
                    else self._classify_error_status(data.get("error_message"))
                )
                duration = data.get("duration")

                # Token counts
                p_tok = (
                    data.get("prompt_tokens")
                    if isinstance(data.get("prompt_tokens"), (int, float))
                    else None
                )
                c_tok = (
                    data.get("completion_tokens")
                    if isinstance(data.get("completion_tokens"), (int, float))
                    else None
                )
                t_tok = (
                    data.get("total_tokens")
                    if isinstance(data.get("total_tokens"), (int, float))
                    else None
                )
                if t_tok is None and p_tok is not None and c_tok is not None:
                    t_tok = int(p_tok) + int(c_tok)

                decode_tps = (
                    float(data["decode_tps"])
                    if isinstance(data.get("decode_tps"), (int, float))
                    else None
                )
                decode_ms = (
                    float(data["decode_ms_per_token"])
                    if isinstance(data.get("decode_ms_per_token"), (int, float))
                    else None
                )

                summary = RequestSummary(
                    request_id=request_id,
                    request_type=rip.request_type,
                    status=status,
                    model=rip.model,
                    resolved_model=rip.resolved_model,
                    endpoint=rip.endpoint,
                    client_ip=rip.client_ip,
                    stream=rip.stream,
                    attempts=max(1, rip.attempts),
                    start_timestamp=rip.start_timestamp,
                    end_timestamp=timestamp,
                    duration_s=(
                        float(duration)
                        if duration is not None
                        else self._compute_duration(rip.start_timestamp, timestamp)
                    ),
                    host_id=rip.host_id or data.get("host_id"),
                    host_name=rip.host_name or data.get("host_name"),
                    instance_id=rip.instance_id or data.get("instance_id"),
                    instance_url=rip.instance_url,
                    error_message=data.get("error_message"),
                    prompt_tokens=int(p_tok) if p_tok is not None else None,
                    completion_tokens=int(c_tok) if c_tok is not None else None,
                    total_tokens=int(t_tok) if t_tok is not None else None,
                    decode_tps=decode_tps,
                    decode_ms_per_token=decode_ms,
                )

                # Queue summary write (non-blocking)
                await self._queue_write(f"{date_str}.requests.jsonl", asdict(summary))

        return summary

    def _compute_duration(
        self, start_iso: Optional[str], end_iso: str
    ) -> Optional[float]:
        if not start_iso:
            return None
        try:
            s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            return max(0.0, (e - s).total_seconds())
        except Exception:
            return None

    def _classify_error_status(self, message: Optional[str]) -> str:
        if not message:
            return "error"
        m = message.lower()
        if "no instances available" in m or ("model" in m and "not found" in m):
            return "missed"
        return "error"

    def cleanup_old_logs(self) -> None:
        """Remove logs older than retention_days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        try:
            for path in self.base_dir.glob("*.jsonl"):
                try:
                    date_part = path.name.split(".", 1)[0]
                    dt = datetime.strptime(date_part, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if dt < cutoff:
                        path.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception as e:
            print(f"[GatewayLogger] Cleanup error: {e}")

    def read_requests(
        self,
        start: datetime,
        end: datetime,
        status: Optional[str] = None,
        request_type: Optional[str] = None,
        model: Optional[str] = None,
        host_id: Optional[str] = None,
    ) -> List[dict]:
        """Read request summaries from disk with filtering."""
        results: List[dict] = []

        # Iterate through date range
        current = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_date = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            path = self.base_dir / f"{date_str}.requests.jsonl"

            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                                # Apply filters
                                ts = obj.get("end_timestamp") or obj.get("timestamp")
                                dt = self._parse_iso(ts)
                                if not dt or not (start <= dt <= end):
                                    continue
                                if (
                                    status
                                    and status != "all"
                                    and obj.get("status") != status
                                ):
                                    continue
                                if (
                                    request_type
                                    and request_type != "all"
                                    and obj.get("request_type") != request_type
                                ):
                                    continue
                                if (
                                    model
                                    and obj.get("model") != model
                                    and obj.get("resolved_model") != model
                                ):
                                    continue
                                if host_id and obj.get("host_id") != host_id:
                                    continue
                                results.append(obj)
                            except Exception:
                                continue
                except Exception as e:
                    print(f"[GatewayLogger] Read error for {path}: {e}")

            current += timedelta(days=1)

        return results

    def read_events(
        self,
        start: datetime,
        end: datetime,
        types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Read raw events from disk with filtering."""
        results: List[dict] = []

        current = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_date = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            path = self.base_dir / f"{date_str}.events.jsonl"

            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                                # Check type filter
                                if types and obj.get("type") not in types:
                                    continue
                                # Check time range
                                ts = (obj.get("data") or {}).get(
                                    "timestamp"
                                ) or obj.get("timestamp")
                                dt = self._parse_iso(ts)
                                if dt and start <= dt <= end:
                                    results.append(obj)
                            except Exception:
                                continue
                except Exception as e:
                    print(f"[GatewayLogger] Read error for {path}: {e}")

            current += timedelta(days=1)

        return results

    def _parse_iso(self, ts: Optional[str]) -> Optional[datetime]:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except Exception:
            return None


# Global singleton
gateway_logger = GatewayLogger()
