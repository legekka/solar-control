"""WebSocket 2.0 - Unified WebSocket architecture for solar ecosystem.

This module provides:
- /ws/host-channel: Endpoint for solar-hosts to connect and stream events
- /ws/events: Endpoint for webui clients to receive all events
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, List, Optional
import asyncio
import json
from datetime import datetime, timezone

from app.config import host_manager
from app.models import (
    WSMessageType,
    WSRegistration,
    HostStatus,
)


router = APIRouter(tags=["websockets"])


class HostConnectionManager:
    """Manages WebSocket connections from solar-hosts."""

    def __init__(self):
        # host_id -> WebSocket connection
        self.connected_hosts: Dict[str, WebSocket] = {}
        # host_id -> registration data
        self.host_registrations: Dict[str, WSRegistration] = {}
        # host_id -> list of instances (pushed by host)
        self.host_instances: Dict[str, List[Dict]] = {}
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def register_host(
        self, websocket: WebSocket, registration: WSRegistration
    ) -> Optional[str]:
        """Register a new host connection.

        Looks up the host by API key and returns the host_id if found.
        Returns None if no host with this API key exists.
        """
        async with self._lock:
            # Find host by API key
            host = None
            for h in host_manager.get_all_hosts():
                if h.api_key == registration.api_key:
                    host = h
                    break

            if not host:
                return None

            host_id = host.id

            # Close existing connection if any
            if host_id in self.connected_hosts:
                try:
                    await self.connected_hosts[host_id].close()
                except Exception:
                    pass

            self.connected_hosts[host_id] = websocket
            self.host_registrations[host_id] = registration

            # Update host status to online
            host_manager.update_host_status(host_id, HostStatus.ONLINE)

            return host_id

    async def unregister_host(self, host_id: str):
        """Unregister a host connection and mark offline."""
        async with self._lock:
            if host_id in self.connected_hosts:
                del self.connected_hosts[host_id]
            if host_id in self.host_registrations:
                del self.host_registrations[host_id]
            if host_id in self.host_instances:
                del self.host_instances[host_id]

            # Immediately mark host as offline
            host_manager.update_host_status(host_id, HostStatus.OFFLINE)

            # Broadcast status change to webui clients
            await webui_manager.broadcast(
                {
                    "type": WSMessageType.HOST_STATUS.value,
                    "data": {
                        "host_id": host_id,
                        "status": "offline",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )

    def is_host_connected(self, host_id: str) -> bool:
        """Check if a host is currently connected."""
        return host_id in self.connected_hosts

    def get_connected_host_ids(self) -> List[str]:
        """Get list of connected host IDs."""
        return list(self.connected_hosts.keys())

    async def send_to_host(self, host_id: str, message: dict) -> bool:
        """Send a message to a specific host."""
        if host_id not in self.connected_hosts:
            return False
        try:
            await self.connected_hosts[host_id].send_json(message)
            return True
        except Exception:
            await self.unregister_host(host_id)
            return False

    def update_host_instances(self, host_id: str, instances: List[Dict]):
        """Update the cached instance list for a host."""
        self.host_instances[host_id] = instances

    def get_host_instances(self, host_id: str) -> List[Dict]:
        """Get cached instances for a host."""
        return self.host_instances.get(host_id, [])

    def get_all_connected_instances(self) -> Dict[str, List[Dict]]:
        """Get all instances from all connected hosts."""
        return dict(self.host_instances)


class GatewayFilter:
    """Filter settings for gateway events."""

    def __init__(
        self,
        status: str = "all",  # all, success, error, missed
        request_type: str = "all",  # all, chat, completion, embedding, classification, etc.
        model: Optional[str] = None,
        host_id: Optional[str] = None,
    ):
        self.status = status
        self.request_type = request_type
        self.model = model
        self.host_id = host_id

    def matches(self, event: dict) -> bool:
        """Check if an event matches this filter."""
        event_type = event.get("type", "")
        data = event.get("data", {})

        # Only filter gateway_request events
        if event_type != "gateway_request":
            return True  # Pass through all other events

        # Filter by status
        if self.status != "all" and data.get("status") != self.status:
            return False

        # Filter by request_type
        if self.request_type != "all" and data.get("request_type") != self.request_type:
            return False

        # Filter by model
        if self.model:
            model = data.get("model") or data.get("resolved_model")
            if model != self.model:
                return False

        # Filter by host_id
        if self.host_id and data.get("host_id") != self.host_id:
            return False

        return True

    @classmethod
    def from_dict(cls, d: dict) -> "GatewayFilter":
        return cls(
            status=d.get("status", "all"),
            request_type=d.get("request_type", "all"),
            model=d.get("model"),
            host_id=d.get("host_id"),
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "request_type": self.request_type,
            "model": self.model,
            "host_id": self.host_id,
        }


class WebUIConnectionManager:
    """Manages WebSocket connections from webui clients with filtering support."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_filters: Dict[WebSocket, GatewayFilter] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """Accept and register a new webui connection."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
            self.connection_filters[websocket] = GatewayFilter()  # Default: no filter

    async def disconnect(self, websocket: WebSocket):
        """Remove a webui connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
            if websocket in self.connection_filters:
                del self.connection_filters[websocket]

    def set_filter(self, websocket: WebSocket, filter_config: GatewayFilter):
        """Set the gateway event filter for a connection."""
        self.connection_filters[websocket] = filter_config

    def get_filter(self, websocket: WebSocket) -> GatewayFilter:
        """Get the current filter for a connection."""
        return self.connection_filters.get(websocket, GatewayFilter())

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected webui clients, respecting filters."""
        disconnected = []
        async with self._lock:
            connections = list(self.active_connections)
            filters = dict(self.connection_filters)

        for connection in connections:
            try:
                # Check if message passes the client's filter
                client_filter = filters.get(connection, GatewayFilter())
                if client_filter.matches(message):
                    await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            await self.disconnect(conn)

    async def send_to(self, websocket: WebSocket, message: dict):
        """Send a message to a specific client."""
        try:
            await websocket.send_json(message)
        except Exception:
            await self.disconnect(websocket)


# Global connection managers
host_connection_manager = HostConnectionManager()
webui_manager = WebUIConnectionManager()


async def broadcast_host_status(status_update: dict):
    """Broadcast host status updates to all connected webui clients."""
    await webui_manager.broadcast(
        {"type": WSMessageType.HOST_STATUS.value, "data": status_update}
    )


async def broadcast_routing_event(event_data: dict):
    """Broadcast routing events to all connected webui clients and log to disk."""
    from dataclasses import asdict
    from app.gateway_logs import gateway_logger

    # Log event to disk (synchronous, returns summary if request completed)
    try:
        summary = gateway_logger.log_event(event_data)

        # If request completed, broadcast summary with request_type
        if summary:
            await webui_manager.broadcast(
                {
                    "type": "gateway_request",
                    "data": asdict(summary),
                }
            )
    except Exception as e:
        # Never let logging failures impact routing broadcasts
        print(f"[broadcast_routing_event] Logging error: {e}")

    # Also broadcast the raw event (for real-time tracking)
    await webui_manager.broadcast(event_data)


@router.websocket("/host-channel")
async def host_channel(websocket: WebSocket):
    """WebSocket endpoint for solar-hosts to connect and stream events.

    Protocol:
    1. Host connects and sends registration message
    2. Solar-control validates and acknowledges
    3. Host streams events (logs, instance_state, health)
    4. Solar-control forwards relevant events to webui clients
    """
    await websocket.accept()

    host_id: Optional[str] = None

    try:
        # Wait for registration message (with timeout)
        try:
            raw_message = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            message = json.loads(raw_message)
        except asyncio.TimeoutError:
            await websocket.send_json(
                {"type": "error", "message": "Registration timeout"}
            )
            await websocket.close()
            return
        except json.JSONDecodeError:
            await websocket.send_json({"type": "error", "message": "Invalid JSON"})
            await websocket.close()
            return

        # Validate registration message
        if message.get("type") != WSMessageType.REGISTRATION.value:
            await websocket.send_json(
                {"type": "error", "message": "Expected registration message"}
            )
            await websocket.close()
            return

        try:
            registration = WSRegistration(**message.get("data", {}))
        except Exception as e:
            await websocket.send_json(
                {"type": "error", "message": f"Invalid registration: {e}"}
            )
            await websocket.close()
            return

        # Register the host (looks up by API key)
        host_id = await host_connection_manager.register_host(websocket, registration)
        if not host_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Registration failed - no host found with this API key",
                }
            )
            await websocket.close()
            return

        # Get host info for broadcasts
        host = host_manager.get_host(host_id)
        host_name = registration.host_name or (host.name if host else "unknown")

        # Store initial instances from registration
        if registration.instances:
            host_connection_manager.update_host_instances(
                host_id, registration.instances
            )

        # Send registration acknowledgement with the assigned host_id
        await websocket.send_json(
            {
                "type": "registration_ack",
                "host_id": host_id,
                "host_name": host_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Broadcast host online status to webui
        await webui_manager.broadcast(
            {
                "type": WSMessageType.HOST_STATUS.value,
                "data": {
                    "host_id": host_id,
                    "name": host_name,
                    "status": "online",
                    "url": host.url if host else None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
        )

        # Main message loop
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=60.0
                )

                # Handle ping/pong
                if raw_message == "ping":
                    await websocket.send_text("pong")
                    continue

                message = json.loads(raw_message)
                msg_type = message.get("type")

                # Forward events to webui clients with host context
                if msg_type in (
                    WSMessageType.LOG.value,
                    WSMessageType.INSTANCE_STATE.value,
                    WSMessageType.HOST_HEALTH.value,
                ):
                    # Enrich message with host info
                    enriched_message = {
                        **message,
                        "host_id": host_id,
                        "host_name": host_name,
                    }
                    await webui_manager.broadcast(enriched_message)

                    # Handle health updates - update host memory info
                    if msg_type == WSMessageType.HOST_HEALTH.value:
                        data = message.get("data", {})
                        if "memory" in data and host:
                            from app.models import MemoryInfo

                            try:
                                host.memory = MemoryInfo(**data["memory"])
                                host_manager.save()
                            except Exception:
                                pass

                # Handle instance list updates
                elif msg_type == WSMessageType.INSTANCES_UPDATE.value:
                    data = message.get("data", {})
                    instances = data.get("instances", [])
                    host_connection_manager.update_host_instances(host_id, instances)
                    # Trigger gateway registry refresh to pick up new instances
                    try:
                        from app.gateway import gateway

                        asyncio.create_task(gateway.refresh_model_registry())
                    except Exception:
                        pass

            except asyncio.TimeoutError:
                # Send keepalive
                try:
                    await websocket.send_json({"type": WSMessageType.KEEPALIVE.value})
                except Exception:
                    break
            except json.JSONDecodeError:
                # Invalid JSON, log but continue
                continue

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Host channel error for {host_id}: {e}")
    finally:
        if host_id:
            await host_connection_manager.unregister_host(host_id)


@router.websocket("/events")
async def events_stream(websocket: WebSocket):
    """WebSocket endpoint for webui clients to receive all events.

    This endpoint streams:
    - Host status updates (online/offline)
    - Routing events (request_start, request_routed, request_success, request_error)
    - Gateway request summaries (gateway_request) - filterable
    - Instance logs (forwarded from hosts)
    - Instance state updates (forwarded from hosts)

    Clients can send filter configuration:
    {
        "type": "set_filter",
        "filter": {
            "status": "all",           // all, success, error, missed
            "request_type": "all",     // all, chat, completion, embedding, classification
            "model": null,             // specific model or null for all
            "host_id": null            // specific host or null for all
        }
    }
    """
    await webui_manager.connect(websocket)

    try:
        # Send initial status of all hosts
        hosts = host_manager.get_all_hosts()
        await websocket.send_json(
            {
                "type": WSMessageType.INITIAL_STATUS.value,
                "data": [
                    {
                        "host_id": host.id,
                        "name": host.name,
                        "status": host.status.value,
                        "url": host.url,
                        "last_seen": (
                            host.last_seen.isoformat() if host.last_seen else None
                        ),
                        "memory": host.memory.model_dump() if host.memory else None,
                        "connected": host_connection_manager.is_host_connected(host.id),
                    }
                    for host in hosts
                ],
            }
        )

        # Send current filter to client
        current_filter = webui_manager.get_filter(websocket)
        await websocket.send_json(
            {
                "type": "filter_status",
                "filter": current_filter.to_dict(),
            }
        )

        # Keep connection alive and handle client messages
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=30
                )

                if raw_message == "ping":
                    await websocket.send_text("pong")
                    continue

                # Try to parse as JSON for filter updates
                try:
                    message = json.loads(raw_message)
                    msg_type = message.get("type")

                    if msg_type == "set_filter":
                        # Update client's gateway filter
                        filter_config = message.get("filter", {})
                        new_filter = GatewayFilter.from_dict(filter_config)
                        webui_manager.set_filter(websocket, new_filter)

                        # Acknowledge filter update
                        await websocket.send_json(
                            {
                                "type": "filter_status",
                                "filter": new_filter.to_dict(),
                            }
                        )

                except json.JSONDecodeError:
                    # Not JSON, ignore
                    pass

            except asyncio.TimeoutError:
                # Send keepalive
                await websocket.send_json({"type": WSMessageType.KEEPALIVE.value})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebUI events error: {e}")
    finally:
        await webui_manager.disconnect(websocket)


# Legacy endpoints for backward compatibility during migration
# These will be removed after migration is complete


@router.websocket("/status")
async def status_updates(websocket: WebSocket):
    """Legacy WebSocket endpoint for real-time host status updates.

    DEPRECATED: Use /ws/events instead.
    """
    await webui_manager.connect(websocket)

    try:
        # Send current status of all hosts immediately
        hosts = host_manager.get_all_hosts()
        await websocket.send_json(
            {
                "type": "initial_status",
                "data": [
                    {
                        "host_id": host.id,
                        "name": host.name,
                        "status": host.status.value,
                        "url": host.url,
                        "last_seen": (
                            host.last_seen.isoformat() if host.last_seen else None
                        ),
                    }
                    for host in hosts
                ],
            }
        )

        # Keep connection alive and wait for disconnect
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "keepalive"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        await webui_manager.disconnect(websocket)


@router.websocket("/routing")
async def routing_updates(websocket: WebSocket):
    """Legacy WebSocket endpoint for real-time routing events.

    DEPRECATED: Use /ws/events instead.
    """
    await webui_manager.connect(websocket)

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "keepalive"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Routing WebSocket error: {e}")
    finally:
        await webui_manager.disconnect(websocket)
