import asyncio
import json
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_str_from_iso(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%d')


def _safe_get(dct: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = dct
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


@dataclass
class RequestInProgress:
    request_id: str
    model: Optional[str] = None
    resolved_model: Optional[str] = None
    endpoint: Optional[str] = None
    client_ip: Optional[str] = None
    stream: Optional[bool] = None
    start_timestamp: Optional[str] = None
    last_route_host_id: Optional[str] = None
    last_route_host_name: Optional[str] = None
    last_route_instance_id: Optional[str] = None
    last_route_instance_url: Optional[str] = None
    attempts: int = 0


@dataclass
class RequestSummary:
    request_id: str
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
    # Token usage and decode metrics (optional)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    decode_tps: Optional[float] = None
    decode_ms_per_token: Optional[float] = None


class GatewayEventLogger:
    def __init__(self) -> None:
        # Fallback defaults; real values may come from settings at runtime
        try:
            from app.config import settings  # local import to avoid import cycle at module load
            self.base_dir = Path(getattr(settings, 'gateway_log_dir', 'data/gateway-logs'))
            self.retention_days = int(getattr(settings, 'gateway_log_retention_days', 365))
        except Exception:
            self.base_dir = Path('data/gateway-logs')
            self.retention_days = 365

        self._queue: 'asyncio.Queue[Dict[str, Any]]' = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._inflight_by_id: Dict[str, RequestInProgress] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop(), name='gateway_event_writer')
        self._cleanup_task = asyncio.create_task(self._retention_loop(), name='gateway_log_retention')

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        # Drain queue gracefully
        try:
            await asyncio.sleep(0)  # allow other tasks to proceed
        except Exception:
            pass
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
            self._writer_task = None
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    # ---------------
    # Public API
    # ---------------
    async def on_event(self, event: Dict[str, Any]) -> None:
        """Accept a routing event. Non-blocking and safe if logger not started yet."""
        # Enqueue raw event for JSONL persistence
        # Ensure event has timestamp as top-level for convenience
        ts = _safe_get(event, 'data', 'timestamp', default=_utc_now_iso())
        if 'timestamp' not in event:
            event['timestamp'] = ts
        try:
            self._queue.put_nowait({'kind': 'event', 'ts': ts, 'data': event})
        except Exception:
            # If queue is full or not available, drop rather than block routing
            pass

        # Update in-memory state for summaries
        await self._handle_for_summary(event)

    # ---------------
    # Internal helpers
    # ---------------
    async def _handle_for_summary(self, event: Dict[str, Any]) -> None:
        etype = event.get('type')
        data = event.get('data') or {}
        request_id = data.get('request_id')
        if not request_id:
            return
        async with self._lock:
            if etype == 'request_start':
                self._inflight_by_id[request_id] = RequestInProgress(
                    request_id=request_id,
                    model=data.get('model'),
                    endpoint=data.get('endpoint'),
                    client_ip=data.get('client_ip'),
                    stream=bool(data.get('stream')) if data.get('stream') is not None else None,
                    start_timestamp=data.get('timestamp') or event.get('timestamp') or _utc_now_iso(),
                )
                return

            rip = self._inflight_by_id.get(request_id)
            if not rip:
                # If we missed the start (e.g., logger started later), create a minimal record
                rip = RequestInProgress(
                    request_id=request_id,
                    model=data.get('model'),
                    start_timestamp=data.get('timestamp') or event.get('timestamp') or _utc_now_iso(),
                )
                self._inflight_by_id[request_id] = rip

            if etype == 'request_routed':
                rip.attempts += 1
                rip.resolved_model = data.get('resolved_model') or rip.resolved_model
                rip.last_route_host_id = data.get('host_id') or rip.last_route_host_id
                rip.last_route_host_name = data.get('host_name') or rip.last_route_host_name
                rip.last_route_instance_id = data.get('instance_id') or rip.last_route_instance_id
                rip.last_route_instance_url = data.get('instance_url') or rip.last_route_instance_url
                rip.client_ip = data.get('client_ip') or rip.client_ip
                return

            if etype in ('request_success', 'request_error'):
                # Finalize summary
                end_ts = data.get('timestamp') or event.get('timestamp') or _utc_now_iso()
                duration = data.get('duration')
                status = 'success' if etype == 'request_success' else self._classify_error_status(data.get('error_message'))
                # Optional usage fields from success payload
                p_tok = data.get('prompt_tokens') if isinstance(data.get('prompt_tokens'), (int, float)) else None
                c_tok = data.get('completion_tokens') if isinstance(data.get('completion_tokens'), (int, float)) else None
                t_tok = data.get('total_tokens') if isinstance(data.get('total_tokens'), (int, float)) else None
                if t_tok is None and p_tok is not None and c_tok is not None:
                    try:
                        t_tok = int(p_tok) + int(c_tok)
                    except Exception:
                        t_tok = None
                _dt = data.get('decode_tps')
                decode_tps = float(_dt) if isinstance(_dt, (int, float)) else None
                _dmpt = data.get('decode_ms_per_token')
                decode_ms_per_token = float(_dmpt) if isinstance(_dmpt, (int, float)) else None
                summary = RequestSummary(
                    request_id=request_id,
                    status=status,
                    model=rip.model,
                    resolved_model=rip.resolved_model,
                    endpoint=rip.endpoint,
                    client_ip=rip.client_ip,
                    stream=rip.stream,
                    attempts=max(1, rip.attempts),
                    start_timestamp=rip.start_timestamp,
                    end_timestamp=end_ts,
                    duration_s=float(duration) if duration is not None else self._compute_duration(rip.start_timestamp, end_ts),
                    host_id=rip.last_route_host_id,
                    host_name=rip.last_route_host_name,
                    instance_id=rip.last_route_instance_id,
                    instance_url=rip.last_route_instance_url,
                    error_message=data.get('error_message'),
                    prompt_tokens=int(p_tok) if p_tok is not None else None,
                    completion_tokens=int(c_tok) if c_tok is not None else None,
                    total_tokens=int(t_tok) if t_tok is not None else None,
                    decode_tps=decode_tps,
                    decode_ms_per_token=decode_ms_per_token,
                )
                try:
                    self._queue.put_nowait({'kind': 'summary', 'ts': end_ts, 'data': asdict(summary)})
                except Exception:
                    pass
                # Remove from inflight
                self._inflight_by_id.pop(request_id, None)

    def _compute_duration(self, start_iso: Optional[str], end_iso: str) -> Optional[float]:
        if not start_iso:
            return None
        try:
            s = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
            e = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))
            return max(0.0, (e - s).total_seconds())
        except Exception:
            return None

    def _classify_error_status(self, message: Optional[str]) -> str:
        if not message:
            return 'error'
        m = message.lower()
        if 'no instances available' in m or 'model' in m and 'not found' in m:
            return 'missed'
        return 'error'

    async def _writer_loop(self) -> None:
        # Simple writer loop that appends to daily JSONL files
        # Keep small cache of open file handles per day to reduce open/close churn
        open_files: Dict[str, Dict[str, Any]] = {}  # date_str -> { 'events': file, 'requests': file }

        def ensure_open(date_str: str, kind: str):
            day_dir = self.base_dir
            day_dir.mkdir(parents=True, exist_ok=True)
            if date_str not in open_files:
                open_files[date_str] = {}
            if kind not in open_files[date_str]:
                filename = f"{date_str}.events.jsonl" if kind == 'event' else f"{date_str}.requests.jsonl"
                path = day_dir / filename
                # newline='\n' ensures consistent line endings
                open_files[date_str][kind] = open(path, 'a', encoding='utf-8')

        try:
            while True:
                item = await self._queue.get()
                kind_val = item.get('kind')
                ts = item.get('ts') or _utc_now_iso()
                date_str = _date_str_from_iso(ts)
                try:
                    kind: str = 'event' if kind_val == 'event' else 'summary'
                    ensure_open(date_str, kind)
                    f = open_files[date_str][kind]
                    line = json.dumps(item['data'], ensure_ascii=False)
                    f.write(line + '\n')
                    f.flush()
                except Exception:
                    # Drop on disk error; continue
                    pass
        except asyncio.CancelledError:
            # Close all files on shutdown
            for by_kind in open_files.values():
                for f in by_kind.values():
                    try:
                        f.close()
                    except Exception:
                        pass
            raise

    async def _retention_loop(self) -> None:
        try:
            while True:
                await self._cleanup_old_logs()
                # Sleep until next day boundary (~24h)
                await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            raise

    async def _cleanup_old_logs(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(self.retention_days))
        try:
            for path in self.base_dir.glob('*.jsonl'):
                # Expect filename format YYYY-MM-DD.*.jsonl
                try:
                    date_part = path.name.split('.', 1)[0]
                    dt = datetime.strptime(date_part, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        try:
                            path.unlink(missing_ok=True)  # type: ignore[arg-type]
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass


# Global singleton
event_logger = GatewayEventLogger()


