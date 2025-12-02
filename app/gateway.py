import aiohttp
import asyncio
import re
import uuid
import time
from typing import Dict, List, Optional, Any, Set, Tuple
from collections import defaultdict
from itertools import cycle
from datetime import datetime, timezone

from app.config import host_manager, settings
from app.models import HostStatus


class OpenAIGateway:
    """OpenAI-compatible API gateway with routing and load balancing"""

    def __init__(self):
        # Map of model alias to list of instance info dictionaries
        self.model_to_hosts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        # Round-robin iterators for each model (used as tiebreaker)
        self.model_iterators: Dict[str, Any] = (
            {}
        )  # cycle objects don't have good type hints
        # Track active requests per instance (instance_id -> count)
        self.active_requests: Dict[str, int] = defaultdict(int)
        # Track active requests per host (host_id -> count)
        self.host_active_counts: Dict[str, int] = defaultdict(int)
        # Track active parameter weight per host (host_id -> total "B" units)
        self.host_active_weight: Dict[str, float] = defaultdict(float)
        self.session: Optional[aiohttp.ClientSession] = None
        # Instance health state keyed by host-instance composite key
        # values: { 'last_ok': float|None, 'cooldown_until': float|None }
        self.instance_health: Dict[str, Dict[str, Optional[float]]] = {}
        # Background tasks
        self._bg_tasks: List[asyncio.Task] = []
        self._stop_event: Optional[asyncio.Event] = None

    async def _ensure_session(self):
        """Ensure aiohttp session exists"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        """Close aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def refresh_model_registry(self):
        """Refresh the model registry from all hosts"""
        await self._ensure_session()

        # Guard for type-checkers; ensure we have a session
        if not self.session:
            return

        # Preserve previous entries grouped by host so we can retain on failure
        previous_map = self.model_to_hosts
        previous_entries_by_host: Dict[str, List[Tuple[str, Dict[str, Any]]]] = (
            defaultdict(list)
        )
        for alias, inst_list in previous_map.items():
            for inst in inst_list:
                previous_entries_by_host[inst["host_id"]].append((alias, inst))

        new_model_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        seen_instance_keys: Set[str] = set()

        # Query each host for its instances
        for host in host_manager.get_all_hosts():
            previous_entries = previous_entries_by_host.get(host.id, [])
            try:
                # Get instances from host
                url = f"{host.url}/instances"
                headers = {"X-API-Key": host.api_key}

                session = self.session
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        instances = await response.json()

                        # Update host status
                        host_manager.update_host_status(host.id, HostStatus.ONLINE)

                        # Add running instances to registry
                        for instance in instances:
                            if instance.get("status") == "running":
                                alias = instance["config"]["alias"]
                                port = instance.get("port")

                                # Build instance URL
                                host_base = host.url.rsplit(":", 1)[0]  # Remove port
                                instance_url = f"{host_base}:{port}"
                                instance_api_key = instance["config"]["api_key"]

                                # Get supported endpoints (default to standard OpenAI endpoints)
                                supported_endpoints = instance.get(
                                    "supported_endpoints",
                                    [
                                        "/v1/chat/completions",
                                        "/v1/completions",
                                        "/v1/models",
                                    ],
                                )
                                # Get backend type if available
                                backend_type = instance.get("config", {}).get(
                                    "backend_type", "llamacpp"
                                )

                                entry = {
                                    "host_id": host.id,
                                    "instance_id": instance["id"],
                                    "url": instance_url,
                                    "api_key": instance_api_key,
                                    "model_alias": alias,
                                    "supported_endpoints": supported_endpoints,
                                    "backend_type": backend_type,
                                }
                                new_model_map[alias].append(entry)
                                key = f"{host.id}-{instance['id']}"
                                seen_instance_keys.add(key)
                                # Ensure health record exists
                                if key not in self.instance_health:
                                    self.instance_health[key] = {
                                        "last_ok": None,
                                        "cooldown_until": None,
                                    }
                    else:
                        host_manager.update_host_status(host.id, HostStatus.ERROR)
                        # Retain previous entries for this host
                        for alias, inst in previous_entries:
                            new_model_map[alias].append(inst)
                            key = f"{inst['host_id']}-{inst['instance_id']}"
                            seen_instance_keys.add(key)
                            if key not in self.instance_health:
                                self.instance_health[key] = {
                                    "last_ok": None,
                                    "cooldown_until": None,
                                }

            except Exception:
                host_manager.update_host_status(host.id, HostStatus.OFFLINE)
                # Retain previous entries for this host
                for alias, inst in previous_entries:
                    new_model_map[alias].append(inst)
                    key = f"{inst['host_id']}-{inst['instance_id']}"
                    seen_instance_keys.add(key)
                    if key not in self.instance_health:
                        self.instance_health[key] = {
                            "last_ok": None,
                            "cooldown_until": None,
                        }

        # Atomically replace the registry with the newly built map
        self.model_to_hosts = defaultdict(list)
        for alias, entries in new_model_map.items():
            if entries:
                self.model_to_hosts[alias].extend(entries)

        # Create round-robin iterators for each model
        self.model_iterators.clear()
        for alias, instances in self.model_to_hosts.items():
            if instances:
                self.model_iterators[alias] = cycle(instances)

        # Prune instance health records that are no longer referenced
        valid_keys = seen_instance_keys
        for key in list(self.instance_health.keys()):
            if key not in valid_keys:
                del self.instance_health[key]

    # -------------------------
    # Background tasks lifecycle
    # -------------------------
    async def start_background_tasks(self):
        if self._stop_event is not None:
            return
        self._stop_event = asyncio.Event()
        # Initial refresh
        await self.refresh_model_registry()
        # Start periodic tasks
        self._bg_tasks = [
            asyncio.create_task(
                self._registry_refresh_loop(), name="registry_refresh_loop"
            ),
            asyncio.create_task(self._health_probe_loop(), name="health_probe_loop"),
        ]

    async def stop_background_tasks(self):
        if self._stop_event is None:
            return
        self._stop_event.set()
        for t in self._bg_tasks:
            t.cancel()
        for t in self._bg_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._bg_tasks = []
        self._stop_event = None

    async def _registry_refresh_loop(self):
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.refresh_model_registry()
            except Exception:
                pass
            try:
                assert self._stop_event is not None
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.registry_refresh_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    async def _health_probe_loop(self):
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._probe_all_instances_once()
            except Exception:
                pass
            try:
                assert self._stop_event is not None
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.health_check_interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def _probe_all_instances_once(self):
        # Build a unique list of current instances
        instances: List[Tuple[str, Dict[str, Any]]] = []
        for alias, inst_list in self.model_to_hosts.items():
            for inst in inst_list:
                key = f"{inst['host_id']}-{inst['instance_id']}"
                instances.append((key, inst))

        # Limit concurrency
        sem = asyncio.Semaphore(20)

        async def _probe_one(key: str, inst: Dict[str, Any]):
            async with sem:
                ok = await self._tcp_connect_ok(inst.get("url", ""))
                now = time.time()
                rec = self.instance_health.setdefault(
                    key, {"last_ok": None, "cooldown_until": None}
                )
                if ok:
                    rec["last_ok"] = now
                # Do not set cooldown here on probe failure; cooldown is set on active request failures

        await asyncio.gather(
            *[_probe_one(k, i) for (k, i) in instances], return_exceptions=True
        )

    async def _tcp_connect_ok(self, url: str) -> bool:
        try:
            # Parse host and port from http(s)://host:port
            from urllib.parse import urlparse

            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
            if not hostname or not port:
                return False
            await asyncio.wait_for(
                asyncio.open_connection(hostname, port),
                timeout=settings.health_check_interval_s / 2,
            )
            return True
        except Exception:
            return False

    # -------------------------
    # Usage enrichment helpers
    # -------------------------
    def _extract_usage_from_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        usage = result.get("usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict):
            return {}
        out: Dict[str, Any] = {}
        if isinstance(usage.get("prompt_tokens"), (int, float)):
            out["prompt_tokens"] = int(usage["prompt_tokens"])
        if isinstance(usage.get("completion_tokens"), (int, float)):
            out["completion_tokens"] = int(usage["completion_tokens"])
        if isinstance(usage.get("total_tokens"), (int, float)):
            out["total_tokens"] = int(usage["total_tokens"])
        return out

    async def _fetch_last_generation_metrics(
        self, host_id: str, instance_id: str
    ) -> Dict[str, Any]:
        try:
            await self._ensure_session()
            if not self.session:
                return {}
            host = host_manager.get_host(host_id)
            if not host:
                return {}
            url = f"{host.url}/instances/{instance_id}/last-generation"
            headers = {"X-API-Key": host.api_key}
            timeout = aiohttp.ClientTimeout(total=3)
            async with self.session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                out: Dict[str, Any] = {}
                if isinstance(data.get("prompt_tokens"), (int, float)):
                    out["prompt_tokens"] = int(data["prompt_tokens"])
                if isinstance(data.get("generated_tokens"), (int, float)):
                    out["completion_tokens"] = int(data["generated_tokens"])
                if "prompt_tokens" in out and "completion_tokens" in out:
                    out["total_tokens"] = int(out["prompt_tokens"]) + int(
                        out["completion_tokens"]
                    )
                if isinstance(data.get("decode_tps"), (int, float)):
                    out["decode_tps"] = float(data["decode_tps"])
                if isinstance(data.get("decode_ms_per_token"), (int, float)):
                    out["decode_ms_per_token"] = float(data["decode_ms_per_token"])
                return out
        except Exception:
            return {}

    async def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of all available models with full metadata from llama.cpp servers"""
        await self.refresh_model_registry()
        await self._ensure_session()

        if not self.session:
            return []

        models_dict = {}  # Use dict to deduplicate by model ID

        # Query each instance directly to get full model info
        for alias, instances in self.model_to_hosts.items():
            if not instances:
                continue

            # Get model info from the first available instance
            instance = instances[0]
            try:
                url = f"{instance['url']}/v1/models"
                headers = {"Authorization": f"Bearer {instance['api_key']}"}

                async with self.session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        # llama.cpp returns both "models" and "data" arrays
                        # Use the "data" array which has the OpenAI-compatible format
                        if "data" in data and data["data"]:
                            for model in data["data"]:
                                # Use the model ID as key to avoid duplicates
                                model_id = model.get("id", alias)
                                if model_id not in models_dict:
                                    models_dict[model_id] = model
            except Exception:
                # Fallback: create minimal model info
                if alias not in models_dict:
                    models_dict[alias] = {
                        "id": alias,
                        "object": "model",
                        "created": int(datetime.now(timezone.utc).timestamp()),
                        "owned_by": "solar",
                    }

        return list(models_dict.values())

    def _resolve_model_name(self, model: str) -> Optional[str]:
        """Resolve partial model name to full model name

        Strategy:
        1. First try exact match
        2. If no exact match, try prefix match (e.g., "thinker-v3" matches "thinker-v3:30b")
        3. If multiple prefix matches, return the first one (sorted alphabetically)

        Returns the resolved model name, or None if no match found
        """
        # Try exact match first
        if model in self.model_to_hosts and self.model_to_hosts[model]:
            return model

        # Try prefix match
        matching_models = [
            m
            for m in self.model_to_hosts.keys()
            if m.startswith(model) and self.model_to_hosts[m]
        ]

        if matching_models:
            # Return first match (sorted alphabetically for consistency)
            return sorted(matching_models)[0]

        return None

    def _parse_model_size(self, alias: str) -> Optional[float]:
        """Parse model size from alias suffix into B units (billions of params).

        Examples:
        - "gpt-oss:20b" -> 20.0
        - "minimax-m2:230b" -> 230.0
        - "mixtral:8x7b" -> 56.0 (8 * 7)
        - "tiny:13m" -> 0.013

        Returns None if size cannot be parsed.
        """
        try:
            # Use the part after the last ':' as the size token, if present
            size_token = alias.rsplit(":", 1)[-1] if ":" in alias else alias
            # Pattern: optional multiplier (e.g., 8x), numeric value (int/float), and unit (b/m)
            match = re.fullmatch(
                r"(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*([bBmM])", size_token
            )
            if not match:
                return None
            multiplier_str, value_str, unit = match.groups()
            multiplier = int(multiplier_str) if multiplier_str else 1
            value = float(value_str)
            unit_lower = unit.lower()
            if unit_lower == "b":
                return multiplier * value
            if unit_lower == "m":
                return multiplier * (value / 1000.0)
            return None
        except Exception:
            return None

    def _get_next_instance(
        self,
        model: str,
        *,
        exclude_keys: Optional[Set[str]] = None,
        required_endpoint: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get next instance for a model using host-aware load balancing.

        Strategy:
        1. Prefer hosts with no active requests (any model). Choose an instance on a free host
           using per-model round-robin as a tiebreaker; fallback to alphabetical host name.
        2. If all candidate hosts are busy, choose the host with the smallest current active
           parameter weight (sum of B units of in-flight models on that host). Ties break by
           alphabetical host name, then per-model round-robin among instances on that host.

        Args:
            model: Model name or alias to route to.
            exclude_keys: Set of instance keys to exclude from selection.
            required_endpoint: If specified, only consider instances that support this endpoint.
        """
        # Resolve partial model name to full model name
        resolved_model = self._resolve_model_name(model)
        if not resolved_model:
            return None

        if (
            resolved_model not in self.model_to_hosts
            or not self.model_to_hosts[resolved_model]
        ):
            return None

        available_instances = self.model_to_hosts[resolved_model]

        # Filter by required endpoint if specified
        if required_endpoint:
            available_instances = [
                inst
                for inst in available_instances
                if required_endpoint in inst.get("supported_endpoints", [])
            ]
            if not available_instances:
                return None

        # Health filtering
        def healthy_now(inst: Dict[str, Any]) -> bool:
            ikey = f"{inst['host_id']}-{inst['instance_id']}"
            if exclude_keys and ikey in exclude_keys:
                return False
            rec = self.instance_health.get(ikey)
            now = time.time()
            # Honor cooldown
            if rec:
                cooldown_until = rec.get("cooldown_until")
                if cooldown_until is not None and now < float(cooldown_until):
                    return False
            # Only accept recent OK within TTL
            if rec:
                last_ok = rec.get("last_ok")
                if last_ok is not None:
                    return (now - float(last_ok)) <= settings.health_ttl_s
            # Unknown health considered not recently-healthy
            return False

        recent_healthy = [inst for inst in available_instances if healthy_now(inst)]
        if recent_healthy:
            available_instances = recent_healthy
        else:
            # Fallback to all excluding cooldown and excluded
            def not_in_cooldown(inst: Dict[str, Any]) -> bool:
                ikey = f"{inst['host_id']}-{inst['instance_id']}"
                if exclude_keys and ikey in exclude_keys:
                    return False
                rec = self.instance_health.get(ikey)
                if rec:
                    cooldown_until = rec.get("cooldown_until")
                    if cooldown_until is not None:
                        return time.time() >= float(cooldown_until)
                return True

            available_instances = [
                inst for inst in available_instances if not_in_cooldown(inst)
            ]

        # Group candidate instances by host
        host_to_instances: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for inst in available_instances:
            host_to_instances[inst["host_id"]].append(inst)

        candidate_host_ids = list(host_to_instances.keys())
        if not candidate_host_ids:
            return None

        # Step 1: Prefer hosts with zero active requests
        free_hosts = [
            h for h in candidate_host_ids if self.host_active_counts.get(h, 0) == 0
        ]
        if free_hosts:
            # Try per-model round-robin filtered to instances on free hosts
            if resolved_model in self.model_iterators:
                for _ in range(len(available_instances)):
                    candidate = next(self.model_iterators[resolved_model])
                    if (
                        candidate["host_id"] in free_hosts
                        and candidate in available_instances
                    ):
                        return candidate

            # Fallback: alphabetical host name
            def host_name(hid: str) -> str:
                h = host_manager.get_host(hid)
                return h.name if h and h.name else hid

            chosen_host = sorted(free_hosts, key=host_name)[0]
            # Within chosen host, pick least instance-active; rr as final tiebreaker
            host_insts = host_to_instances[chosen_host]
            min_inst_active = float("inf")
            best_insts: List[Dict[str, Any]] = []
            for inst in host_insts:
                ikey = f"{inst['host_id']}-{inst['instance_id']}"
                iactive = self.active_requests.get(ikey, 0)
                if iactive < min_inst_active:
                    min_inst_active = iactive
                    best_insts = [inst]
                elif iactive == min_inst_active:
                    best_insts.append(inst)
            if len(best_insts) == 1:
                return best_insts[0]
            if resolved_model in self.model_iterators:
                for _ in range(len(available_instances)):
                    candidate = next(self.model_iterators[resolved_model])
                    if candidate in best_insts:
                        return candidate
            return best_insts[0]

        # Step 2: All hosts are busy - choose host with smallest active weight
        # Build weight map for candidate hosts
        host_weights = {
            hid: float(self.host_active_weight.get(hid, 0.0))
            for hid in candidate_host_ids
        }

        # Determine minimal weight hosts
        min_weight = min(host_weights.values()) if host_weights else 0.0
        min_weight_hosts = [hid for hid, w in host_weights.items() if w == min_weight]

        # Tie-break by alphabetical host name
        def host_name(hid: str) -> str:
            h = host_manager.get_host(hid)
            return h.name if h and h.name else hid

        chosen_host = sorted(min_weight_hosts, key=host_name)[0]

        # Within chosen host, pick the least instance-active; rr as final tiebreaker
        host_insts = host_to_instances[chosen_host]
        min_inst_active = float("inf")
        best_insts_busy: List[Dict[str, Any]] = []
        for inst in host_insts:
            ikey = f"{inst['host_id']}-{inst['instance_id']}"
            iactive = self.active_requests.get(ikey, 0)
            if iactive < min_inst_active:
                min_inst_active = iactive
                best_insts_busy = [inst]
            elif iactive == min_inst_active:
                best_insts_busy.append(inst)
        if len(best_insts_busy) == 1:
            return best_insts_busy[0]
        if resolved_model in self.model_iterators:
            for _ in range(len(available_instances)):
                candidate = next(self.model_iterators[resolved_model])
                if candidate in best_insts_busy:
                    return candidate
        return best_insts_busy[0]

    async def route_request(
        self,
        model: str,
        endpoint: str,
        data: Dict[str, Any],
        client_ip: str = "unknown",
        required_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Route a request to the appropriate instance.

        Args:
            model: Model name or alias to route to.
            endpoint: API endpoint path (e.g., "/v1/chat/completions").
            data: Request data to forward.
            client_ip: Client IP for logging.
            required_endpoint: If specified, only route to instances supporting this endpoint.
        """
        # Generate unique request ID and start time
        request_id = str(uuid.uuid4())
        start_time = time.time()

        # Import here to avoid circular dependency
        from app.routes.websockets import broadcast_routing_event
        from app.config import host_manager

        # Emit request start event
        await broadcast_routing_event(
            {
                "type": "request_start",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "endpoint": endpoint,
                    "client_ip": client_ip,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        )

        await self._ensure_session()

        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")

        # Retry loop on connect errors/timeouts
        attempted: Set[str] = set()
        last_error: Optional[Exception] = None
        retried_once = False
        # Use endpoint as required_endpoint filter if not explicitly provided
        filter_endpoint = required_endpoint or endpoint

        for attempt in range(max(1, int(settings.route_max_attempts))):
            # Get instance for this model
            instance = self._get_next_instance(
                model,
                exclude_keys=attempted,
                required_endpoint=filter_endpoint,
            )
            if not instance:
                if not retried_once:
                    retried_once = True
                    attempted.clear()
                    try:
                        await self.refresh_model_registry()
                    except Exception:
                        pass
                    delay = max(
                        0.0, float(getattr(settings, "route_retry_delay_s", 0.0))
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break

            # Track this request as active (instance + host)
            instance_key = f"{instance['host_id']}-{instance['instance_id']}"
            attempted.add(instance_key)
            self.active_requests[instance_key] += 1
            host_id_for_request = instance["host_id"]
            weight_for_request = self._parse_model_size(instance["model_alias"])
            self.host_active_counts[host_id_for_request] += 1
            if weight_for_request is not None:
                self.host_active_weight[host_id_for_request] = (
                    self.host_active_weight.get(host_id_for_request, 0.0)
                    + float(weight_for_request)
                )

            # Get host name
            host = host_manager.get_host(instance["host_id"])
            host_name = host.name if host else "unknown"

            try:
                # Emit routing event (instance selected)
                await broadcast_routing_event(
                    {
                        "type": "request_routed",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "resolved_model": instance["model_alias"],
                            "host_id": instance["host_id"],
                            "host_name": host_name,
                            "instance_id": instance["instance_id"],
                            "instance_url": instance["url"],
                            "client_ip": client_ip,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "attempt": attempt + 1,
                        },
                    }
                )

                # Forward request to instance
                url = f"{instance['url']}{endpoint}"
                headers = {
                    "Authorization": f"Bearer {instance['api_key']}",
                    "Content-Type": "application/json",
                }

                timeout = aiohttp.ClientTimeout(
                    total=None, connect=settings.route_connect_timeout_s
                )
                async with self.session.post(
                    url, json=data, headers=headers, timeout=timeout
                ) as response:
                    if response.status == 200:
                        # Mark success in health state
                        self._mark_instance_success(instance_key)
                        result = await response.json()
                        duration = time.time() - start_time

                        # Try to enrich with token usage
                        usage_fields = self._extract_usage_from_result(result)
                        if (
                            "prompt_tokens" not in usage_fields
                            or "completion_tokens" not in usage_fields
                        ):
                            # fallback to host metrics
                            host_metrics = await self._fetch_last_generation_metrics(
                                instance["host_id"], instance["instance_id"]
                            )
                            # host metrics keys already mapped partially
                            usage_fields = {**usage_fields, **host_metrics}

                        # Emit success event (with optional usage fields)
                        base_data = {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance["host_id"],
                            "instance_id": instance["instance_id"],
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        base_data.update(usage_fields)
                        event_payload = {
                            "type": "request_success",
                            "data": base_data,
                        }
                        await broadcast_routing_event(event_payload)

                        return result
                    else:
                        # Do not reroute on HTTP error (only connect errors/timeouts)
                        error_text = await response.text()
                        duration = time.time() - start_time

                        # Emit error event
                        await broadcast_routing_event(
                            {
                                "type": "request_error",
                                "data": {
                                    "request_id": request_id,
                                    "model": model,
                                    "host_id": instance["host_id"],
                                    "instance_id": instance["instance_id"],
                                    "error_message": f"Request failed: {response.status} - {error_text}",
                                    "duration": duration,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                            }
                        )
                        raise Exception(
                            f"Request failed: {response.status} - {error_text}"
                        )
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                # Mark cooldown and try next instance
                self._mark_instance_failure(instance_key)
                last_error = e
                await broadcast_routing_event(
                    {
                        "type": "request_reroute",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance.get("host_id"),
                            "instance_id": instance.get("instance_id"),
                            "reason": "connect_error",
                            "attempt": attempt + 1,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                # continue to next attempt
            except Exception as e:
                # Non-connect error: bubble up
                duration = time.time() - start_time
                await broadcast_routing_event(
                    {
                        "type": "request_error",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance.get("host_id") if instance else None,
                            "instance_id": (
                                instance.get("instance_id") if instance else None
                            ),
                            "error_message": str(e),
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                raise
            finally:
                # Decrement active counts for this attempt
                try:
                    self.active_requests[instance_key] = max(
                        0, self.active_requests[instance_key] - 1
                    )
                    self.host_active_counts[host_id_for_request] = max(
                        0, self.host_active_counts[host_id_for_request] - 1
                    )
                    if weight_for_request is not None:
                        self.host_active_weight[host_id_for_request] = max(
                            0.0,
                            self.host_active_weight[host_id_for_request]
                            - float(weight_for_request),
                        )
                except Exception:
                    pass

        # Out of attempts or no instance
        error_msg = (
            f"Model '{model}' not found or no instances available"
            if not attempted
            else f"Failed to connect to model '{model}' after {len(attempted)} attempts: {last_error}"
        )
        await broadcast_routing_event(
            {
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "error_message": error_msg,
                    "client_ip": client_ip,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        if attempted:
            raise Exception(error_msg)
        raise ValueError(error_msg)

    async def stream_request(
        self,
        model: str,
        endpoint: str,
        data: Dict[str, Any],
        client_ip: str = "unknown",
        required_endpoint: Optional[str] = None,
    ):
        """Stream a request to the appropriate instance.

        Args:
            model: Model name or alias to route to.
            endpoint: API endpoint path (e.g., "/v1/chat/completions").
            data: Request data to forward.
            client_ip: Client IP for logging.
            required_endpoint: If specified, only route to instances supporting this endpoint.
        """
        # Generate unique request ID and start time
        request_id = str(uuid.uuid4())
        start_time = time.time()

        # Import here to avoid circular dependency
        from app.routes.websockets import broadcast_routing_event
        from app.config import host_manager

        # Emit request start event
        await broadcast_routing_event(
            {
                "type": "request_start",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "endpoint": endpoint,
                    "stream": True,
                    "client_ip": client_ip,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        )

        await self._ensure_session()

        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")

        # Use endpoint as required_endpoint filter if not explicitly provided
        filter_endpoint = required_endpoint or endpoint

        # Retry loop for streaming on connect errors/timeouts
        attempted: Set[str] = set()
        last_error: Optional[Exception] = None
        retried_once = False
        for attempt in range(max(1, int(settings.route_max_attempts))):
            instance = self._get_next_instance(
                model,
                exclude_keys=attempted,
                required_endpoint=filter_endpoint,
            )
            if not instance:
                if not retried_once:
                    retried_once = True
                    attempted.clear()
                    try:
                        await self.refresh_model_registry()
                    except Exception:
                        pass
                    delay = max(
                        0.0, float(getattr(settings, "route_retry_delay_s", 0.0))
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                break

            instance_key = f"{instance['host_id']}-{instance['instance_id']}"
            attempted.add(instance_key)
            self.active_requests[instance_key] += 1
            host_id_for_request = instance["host_id"]
            weight_for_request = self._parse_model_size(instance["model_alias"])
            self.host_active_counts[host_id_for_request] += 1
            if weight_for_request is not None:
                self.host_active_weight[host_id_for_request] = (
                    self.host_active_weight.get(host_id_for_request, 0.0)
                    + float(weight_for_request)
                )

            host = host_manager.get_host(instance["host_id"])
            host_name = host.name if host else "unknown"

            try:
                await broadcast_routing_event(
                    {
                        "type": "request_routed",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "resolved_model": instance["model_alias"],
                            "host_id": instance["host_id"],
                            "host_name": host_name,
                            "instance_id": instance["instance_id"],
                            "instance_url": instance["url"],
                            "client_ip": client_ip,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "attempt": attempt + 1,
                        },
                    }
                )

                url = f"{instance['url']}{endpoint}"
                headers = {
                    "Authorization": f"Bearer {instance['api_key']}",
                    "Content-Type": "application/json",
                }

                timeout = aiohttp.ClientTimeout(
                    total=None, connect=settings.route_connect_timeout_s
                )
                async with self.session.post(
                    url, json=data, headers=headers, timeout=timeout
                ) as response:
                    if response.status == 200:
                        self._mark_instance_success(instance_key)
                        async for line in response.content:
                            yield line

                        # Emit success event after stream completes (try to enrich with host metrics)
                        duration = time.time() - start_time
                        usage_fields = await self._fetch_last_generation_metrics(
                            instance["host_id"], instance["instance_id"]
                        )
                        base_data = {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance["host_id"],
                            "instance_id": instance["instance_id"],
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        base_data.update(usage_fields)
                        await broadcast_routing_event(
                            {
                                "type": "request_success",
                                "data": base_data,
                            }
                        )
                        return
                    else:
                        error_text = await response.text()
                        duration = time.time() - start_time
                        await broadcast_routing_event(
                            {
                                "type": "request_error",
                                "data": {
                                    "request_id": request_id,
                                    "model": model,
                                    "host_id": instance["host_id"],
                                    "instance_id": instance["instance_id"],
                                    "error_message": f"Request failed: {response.status} - {error_text}",
                                    "duration": duration,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                            }
                        )
                        raise Exception(
                            f"Request failed: {response.status} - {error_text}"
                        )
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                self._mark_instance_failure(instance_key)
                last_error = e
                await broadcast_routing_event(
                    {
                        "type": "request_reroute",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance.get("host_id"),
                            "instance_id": instance.get("instance_id"),
                            "reason": "connect_error",
                            "attempt": attempt + 1,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                # continue to next attempt
            except Exception as e:
                duration = time.time() - start_time
                await broadcast_routing_event(
                    {
                        "type": "request_error",
                        "data": {
                            "request_id": request_id,
                            "model": model,
                            "host_id": instance.get("host_id") if instance else None,
                            "instance_id": (
                                instance.get("instance_id") if instance else None
                            ),
                            "error_message": str(e),
                            "duration": duration,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                raise
            finally:
                try:
                    self.active_requests[instance_key] = max(
                        0, self.active_requests[instance_key] - 1
                    )
                    self.host_active_counts[host_id_for_request] = max(
                        0, self.host_active_counts[host_id_for_request] - 1
                    )
                    if weight_for_request is not None:
                        self.host_active_weight[host_id_for_request] = max(
                            0.0,
                            self.host_active_weight[host_id_for_request]
                            - float(weight_for_request),
                        )
                except Exception:
                    pass

        error_msg = (
            f"Model '{model}' not found or no instances available"
            if not attempted
            else f"Failed to connect to model '{model}' after {len(attempted)} attempts: {last_error}"
        )
        await broadcast_routing_event(
            {
                "type": "request_error",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "error_message": error_msg,
                    "client_ip": client_ip,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        if attempted:
            raise Exception(error_msg)
        raise ValueError(error_msg)

    def _mark_instance_failure(self, instance_key: str) -> None:
        now = time.time()
        rec = self.instance_health.setdefault(
            instance_key, {"last_ok": None, "cooldown_until": None}
        )
        rec["cooldown_until"] = now + float(settings.health_cooldown_s)

    def _mark_instance_success(self, instance_key: str) -> None:
        now = time.time()
        rec = self.instance_health.setdefault(
            instance_key, {"last_ok": None, "cooldown_until": None}
        )
        rec["last_ok"] = now
        rec["cooldown_until"] = None


# Global gateway instance
gateway = OpenAIGateway()
