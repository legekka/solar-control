from fastapi import APIRouter, Query
from typing import Any, Deque, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from pathlib import Path
import asyncio
import json
from collections import deque

from app.config import settings


router = APIRouter(prefix="/gateway", tags=["gateway"])


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _date_range_utc(start: datetime, end: datetime) -> List[datetime]:
    days = []
    cur = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    last = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    while cur <= last:
        days.append(cur)
        cur = cur + timedelta(days=1)
    return days


def _log_paths(kind: str, start: datetime, end: datetime) -> List[Path]:
    base = Path(getattr(settings, 'gateway_log_dir', 'data/gateway-logs'))
    paths: List[Path] = []
    for d in _date_range_utc(start, end):
        date_str = d.strftime('%Y-%m-%d')
        filename = f"{date_str}.events.jsonl" if kind == 'events' else f"{date_str}.requests.jsonl"
        p = base / filename
        if p.exists():
            paths.append(p)
    return paths


def _load_jsonl_filtered(paths: List[Path], predicate) -> List[dict]:
    out: List[dict] = []
    for p in paths:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        if predicate(obj):
                            out.append(obj)
                    except Exception:
                        continue
        except Exception:
            continue
    return out


@router.get('/stats')
async def get_stats(
    from_ts: Optional[str] = Query(None, alias='from'),
    to_ts: Optional[str] = Query(None, alias='to'),
):
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = _parse_iso(to_ts) or now

    def compute() -> Dict[str, Any]:
        # Requests summaries
        req_paths = _log_paths('requests', start, end)
        def in_range_req(o: dict) -> bool:
            ts = o.get('end_timestamp') or o.get('timestamp')
            dt = _parse_iso(ts)
            return bool(dt and (start <= dt <= end))

        summaries = _load_jsonl_filtered(req_paths, in_range_req)
        completed = sum(1 for s in summaries if s.get('status') == 'success')
        missed = sum(1 for s in summaries if s.get('status') == 'missed')
        error = sum(1 for s in summaries if s.get('status') == 'error')

        # Reroutes (unique requests with reroute events)
        evt_paths = _log_paths('events', start, end)
        def reroute_pred(o: dict) -> bool:
            if o.get('type') != 'request_reroute':
                return False
            ts = (o.get('data') or {}).get('timestamp') or o.get('timestamp')
            dt = _parse_iso(ts)
            return bool(dt and (start <= dt <= end))

        reroutes = _load_jsonl_filtered(evt_paths, reroute_pred)
        rerouted_unique = len({(e.get('data') or {}).get('request_id') for e in reroutes if (e.get('data') or {}).get('request_id')})

        # Token aggregates (success only)
        succ = [s for s in summaries if s.get('status') == 'success']
        p_vals: List[int] = []
        c_vals: List[int] = []
        for s in succ:
            pv = s.get('prompt_tokens')
            if isinstance(pv, (int, float)):
                p_vals.append(int(pv))
            cv = s.get('completion_tokens')
            if isinstance(cv, (int, float)):
                c_vals.append(int(cv))
        token_in_total = int(sum(p_vals)) if p_vals else 0
        token_out_total = int(sum(c_vals)) if c_vals else 0
        avg_tokens_in = (token_in_total / len(p_vals)) if p_vals else 0
        avg_tokens_out = (token_out_total / len(c_vals)) if c_vals else 0

        # Per-model breakdown
        by_model: Dict[str, Dict[str, Any]] = {}
        for s in succ:
            model_key = s.get('resolved_model') or s.get('model') or 'unknown'
            rec = by_model.setdefault(model_key, {
                'model': model_key,
                'completed': 0,
                'token_in': 0,
                'token_out': 0,
                'dur_sum': 0.0,
            })
            rec['completed'] += 1
            if isinstance(s.get('prompt_tokens'), (int, float)):
                rec['token_in'] += int(s['prompt_tokens'])
            if isinstance(s.get('completion_tokens'), (int, float)):
                rec['token_out'] += int(s['completion_tokens'])
            if isinstance(s.get('duration_s'), (int, float)):
                rec['dur_sum'] += float(s['duration_s'])
        model_rows = [
            {
                'model': k,
                'completed': v['completed'],
                'token_in': v['token_in'],
                'token_out': v['token_out'],
                'avg_duration_s': (v['dur_sum'] / v['completed']) if v['completed'] else 0.0,
            }
            for k, v in by_model.items()
        ]

        # Per-host breakdown
        by_host: Dict[str, Dict[str, Any]] = {}
        for s in succ:
            hid = s.get('host_id') or 'unknown'
            rec = by_host.setdefault(hid, {
                'host_id': hid,
                'host_name': s.get('host_name'),
                'completed': 0,
                'token_in': 0,
                'token_out': 0,
                'dur_sum': 0.0,
            })
            rec['host_name'] = s.get('host_name') or rec.get('host_name')
            rec['completed'] += 1
            if isinstance(s.get('prompt_tokens'), (int, float)):
                rec['token_in'] += int(s['prompt_tokens'])
            if isinstance(s.get('completion_tokens'), (int, float)):
                rec['token_out'] += int(s['completion_tokens'])
            if isinstance(s.get('duration_s'), (int, float)):
                rec['dur_sum'] += float(s['duration_s'])
        host_rows = [
            {
                'host_id': k,
                'host_name': v.get('host_name'),
                'completed': v['completed'],
                'token_in': v['token_in'],
                'token_out': v['token_out'],
                'avg_duration_s': (v['dur_sum'] / v['completed']) if v['completed'] else 0.0,
            }
            for k, v in by_host.items()
        ]

        return {
            'from': start.isoformat(),
            'to': end.isoformat(),
            'completed': completed,
            'missed': missed,
            'error': error,
            'rerouted_requests': rerouted_unique,
            'token_in_total': token_in_total,
            'token_out_total': token_out_total,
            'avg_tokens_in': avg_tokens_in,
            'avg_tokens_out': avg_tokens_out,
            'models': model_rows,
            'hosts': host_rows,
        }

    result = await asyncio.to_thread(compute)
    return result


@router.get('/requests')
async def list_requests(
    from_ts: Optional[str] = Query(None, alias='from'),
    to_ts: Optional[str] = Query(None, alias='to'),
    status: str = Query('all', pattern='^(all|success|error|missed)$'),
    model: Optional[str] = None,
    host_id: Optional[str] = None,
    page: int = 1,
    limit: int = 200,
):
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or (now - timedelta(days=1))
    end = _parse_iso(to_ts) or now

    def load() -> Dict[str, Any]:
        req_paths = _log_paths('requests', start, end)
        def pred(o: dict) -> bool:
            ts = o.get('end_timestamp') or o.get('timestamp')
            dt = _parse_iso(ts)
            if not (dt and (start <= dt <= end)):
                return False
            if status != 'all' and o.get('status') != status:
                return False
            if model and o.get('model') != model and o.get('resolved_model') != model:
                return False
            if host_id and o.get('host_id') != host_id:
                return False
            return True

        items = _load_jsonl_filtered(req_paths, pred)
        # Sort by end_timestamp desc
        def sort_key(o: dict) -> Tuple[float, str]:
            ts = o.get('end_timestamp') or o.get('timestamp') or ''
            dt = _parse_iso(ts) or datetime.fromtimestamp(0, tz=timezone.utc)
            return (dt.timestamp(), o.get('request_id', ''))

        items.sort(key=sort_key, reverse=True)
        total = len(items)
        start_idx = max(0, (page - 1) * max(1, limit))
        end_idx = start_idx + max(1, limit)
        page_items = items[start_idx:end_idx]
        return {
            'from': start.isoformat(),
            'to': end.isoformat(),
            'page': page,
            'limit': limit,
            'total': total,
            'items': page_items,
        }

    result = await asyncio.to_thread(load)
    return result


@router.get('/events/recent')
async def recent_events(
    from_ts: Optional[str] = Query(None, alias='from'),
    to_ts: Optional[str] = Query(None, alias='to'),
    types: str = 'request_error,request_reroute',
    limit: int = 1000,
):
    now = datetime.now(timezone.utc)
    start = _parse_iso(from_ts) or (now - timedelta(days=1))
    end = _parse_iso(to_ts) or now
    wanted = {t.strip() for t in types.split(',') if t.strip()}

    def load() -> Dict[str, Any]:
        evt_paths = _log_paths('events', start, end)
        buf: Deque[dict] = deque(maxlen=max(1, limit))
        for p in evt_paths:
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                            et = obj.get('type')
                            ts = (obj.get('data') or {}).get('timestamp') or obj.get('timestamp')
                            dt = _parse_iso(ts)
                            if et in wanted and dt and (start <= dt <= end):
                                buf.append(obj)
                        except Exception:
                            continue
            except Exception:
                continue
        # Return newest last (consistent with streaming); sort by timestamp
        def sort_key(o: dict) -> float:
            ts = (o.get('data') or {}).get('timestamp') or o.get('timestamp') or ''
            dt = _parse_iso(ts) or datetime.fromtimestamp(0, tz=timezone.utc)
            return dt.timestamp()
        items = list(buf)
        items.sort(key=sort_key)
        return {
            'from': start.isoformat(),
            'to': end.isoformat(),
            'types': list(wanted),
            'items': items[-limit:],
        }

    result = await asyncio.to_thread(load)
    return result


