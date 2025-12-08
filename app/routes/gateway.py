"""Gateway monitoring REST API endpoints.

Provides endpoints for:
- /gateway/stats - Aggregated statistics
- /gateway/requests - Request history with filtering
- /gateway/events/recent - Recent events (errors, reroutes)
"""

from fastapi import APIRouter, Query
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
import asyncio

from app.gateway_logs import gateway_logger


router = APIRouter(prefix="/gateway", tags=["gateway"])


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except Exception:
        return None


@router.get("/stats")
async def get_stats(
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
    request_type: Optional[str] = Query(
        None,
        description="Filter by request type: chat, completion, embedding, classification",
    ),
):
    """Get aggregated gateway statistics."""
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or datetime(
        now.year, now.month, now.day, tzinfo=timezone.utc
    )
    end = _parse_iso(to_ts) or now

    def compute() -> Dict[str, Any]:
        # Read all summaries in range
        summaries = gateway_logger.read_requests(
            start,
            end,
            request_type=(
                request_type if request_type and request_type != "all" else None
            ),
        )

        # Count by status
        completed = sum(1 for s in summaries if s.get("status") == "success")
        missed = sum(1 for s in summaries if s.get("status") == "missed")
        error = sum(1 for s in summaries if s.get("status") == "error")

        # Count reroutes
        events = gateway_logger.read_events(start, end, types=["request_reroute"])
        rerouted_unique = len(
            {
                e.get("data", {}).get("request_id")
                for e in events
                if e.get("data", {}).get("request_id")
            }
        )

        # Token aggregates (success only)
        succ = [s for s in summaries if s.get("status") == "success"]
        p_vals: List[int] = []
        c_vals: List[int] = []
        for s in succ:
            pv = s.get("prompt_tokens")
            if isinstance(pv, (int, float)):
                p_vals.append(int(pv))
            cv = s.get("completion_tokens")
            if isinstance(cv, (int, float)):
                c_vals.append(int(cv))
        token_in_total = int(sum(p_vals)) if p_vals else 0
        token_out_total = int(sum(c_vals)) if c_vals else 0
        avg_tokens_in = (token_in_total / len(p_vals)) if p_vals else 0
        avg_tokens_out = (token_out_total / len(c_vals)) if c_vals else 0

        # Per-model breakdown
        by_model: Dict[str, Dict[str, Any]] = {}
        for s in succ:
            model_key = s.get("resolved_model") or s.get("model") or "unknown"
            rec = by_model.setdefault(
                model_key,
                {
                    "model": model_key,
                    "completed": 0,
                    "token_in": 0,
                    "token_out": 0,
                    "dur_sum": 0.0,
                },
            )
            rec["completed"] += 1
            if isinstance(s.get("prompt_tokens"), (int, float)):
                rec["token_in"] += int(s["prompt_tokens"])
            if isinstance(s.get("completion_tokens"), (int, float)):
                rec["token_out"] += int(s["completion_tokens"])
            if isinstance(s.get("duration_s"), (int, float)):
                rec["dur_sum"] += float(s["duration_s"])

        model_rows = [
            {
                "model": k,
                "completed": v["completed"],
                "token_in": v["token_in"],
                "token_out": v["token_out"],
                "avg_duration_s": (
                    (v["dur_sum"] / v["completed"]) if v["completed"] else 0.0
                ),
            }
            for k, v in by_model.items()
        ]

        # Per-host breakdown
        by_host: Dict[str, Dict[str, Any]] = {}
        for s in succ:
            hid = s.get("host_id") or "unknown"
            rec = by_host.setdefault(
                hid,
                {
                    "host_id": hid,
                    "host_name": s.get("host_name"),
                    "completed": 0,
                    "token_in": 0,
                    "token_out": 0,
                    "dur_sum": 0.0,
                },
            )
            rec["host_name"] = s.get("host_name") or rec.get("host_name")
            rec["completed"] += 1
            if isinstance(s.get("prompt_tokens"), (int, float)):
                rec["token_in"] += int(s["prompt_tokens"])
            if isinstance(s.get("completion_tokens"), (int, float)):
                rec["token_out"] += int(s["completion_tokens"])
            if isinstance(s.get("duration_s"), (int, float)):
                rec["dur_sum"] += float(s["duration_s"])

        host_rows = [
            {
                "host_id": k,
                "host_name": v.get("host_name"),
                "completed": v["completed"],
                "token_in": v["token_in"],
                "token_out": v["token_out"],
                "avg_duration_s": (
                    (v["dur_sum"] / v["completed"]) if v["completed"] else 0.0
                ),
            }
            for k, v in by_host.items()
        ]

        # Per-request-type breakdown
        by_type: Dict[str, Dict[str, Any]] = {}
        for s in summaries:
            rt = s.get("request_type") or "unknown"
            rec = by_type.setdefault(
                rt,
                {
                    "request_type": rt,
                    "total": 0,
                    "success": 0,
                    "error": 0,
                    "missed": 0,
                },
            )
            rec["total"] += 1
            status = s.get("status")
            if status == "success":
                rec["success"] += 1
            elif status == "error":
                rec["error"] += 1
            elif status == "missed":
                rec["missed"] += 1

        type_rows = list(by_type.values())

        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "completed": completed,
            "missed": missed,
            "error": error,
            "rerouted_requests": rerouted_unique,
            "token_in_total": token_in_total,
            "token_out_total": token_out_total,
            "avg_tokens_in": avg_tokens_in,
            "avg_tokens_out": avg_tokens_out,
            "models": model_rows,
            "hosts": host_rows,
            "request_types": type_rows,
        }

    result = await asyncio.to_thread(compute)
    return result


@router.get("/requests")
async def list_requests(
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
    status: str = Query("all", pattern="^(all|success|error|missed)$"),
    request_type: Optional[str] = Query(
        None,
        description="Filter by request type: chat, completion, embedding, classification",
    ),
    model: Optional[str] = None,
    host_id: Optional[str] = None,
    page: int = 1,
    limit: int = 200,
):
    """List gateway requests with filtering and pagination."""
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or (now - timedelta(days=1))
    end = _parse_iso(to_ts) or now

    def load() -> Dict[str, Any]:
        # Use gateway_logger to read with filtering
        items = gateway_logger.read_requests(
            start,
            end,
            status=status if status != "all" else None,
            request_type=(
                request_type if request_type and request_type != "all" else None
            ),
            model=model,
            host_id=host_id,
        )

        # Sort by end_timestamp desc
        def sort_key(o: dict) -> Tuple[float, str]:
            ts = o.get("end_timestamp") or o.get("timestamp") or ""
            dt = _parse_iso(ts) or datetime.fromtimestamp(0, tz=timezone.utc)
            return (dt.timestamp(), o.get("request_id", ""))

        items.sort(key=sort_key, reverse=True)
        total = len(items)
        start_idx = max(0, (page - 1) * max(1, limit))
        end_idx = start_idx + max(1, limit)
        page_items = items[start_idx:end_idx]

        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "page": page,
            "limit": limit,
            "total": total,
            "items": page_items,
        }

    result = await asyncio.to_thread(load)
    return result


@router.get("/events/recent")
async def recent_events(
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
    types: str = "request_error,request_reroute",
    limit: int = 1000,
):
    """Get recent gateway events (errors, reroutes)."""
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or (now - timedelta(days=1))
    end = _parse_iso(to_ts) or now
    wanted = [t.strip() for t in types.split(",") if t.strip()]

    def load() -> Dict[str, Any]:
        events = gateway_logger.read_events(start, end, types=wanted)

        # Sort by timestamp
        def sort_key(o: dict) -> float:
            ts = (o.get("data") or {}).get("timestamp") or o.get("timestamp") or ""
            dt = _parse_iso(ts) or datetime.fromtimestamp(0, tz=timezone.utc)
            return dt.timestamp()

        events.sort(key=sort_key)

        # Return last N items
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "types": wanted,
            "items": events[-limit:] if len(events) > limit else events,
        }

    result = await asyncio.to_thread(load)
    return result
