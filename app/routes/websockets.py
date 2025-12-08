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
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def register_host(
        self, host_id: str, websocket: WebSocket, registration: WSRegistration
    ) -> bool:
        """Register a new host connection."""
        async with self._lock:
            # Verify the host exists and API key matches
            host = host_manager.get_host(host_id)
            if not host:
                return False

            if host.api_key != registration.api_key:
                return False

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

            return True

    async def unregister_host(self, host_id: str):
        """Unregister a host connection and mark offline."""
        async with self._lock:
            if host_id in self.connected_hosts:
                del self.connected_hosts[host_id]
            if host_id in self.host_registrations:
                del self.host_registrations[host_id]

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


class WebUIConnectionManager:
    """Manages WebSocket connections from webui clients."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """Accept and register a new webui connection."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Remove a webui connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected webui clients."""
        disconnected = []
        async with self._lock:
            connections = list(self.active_connections)

        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            await self.disconnect(conn)


# Global connection managers
host_connection_manager = HostConnectionManager()
webui_manager = WebUIConnectionManager()


async def broadcast_host_status(status_update: dict):
    """Broadcast host status updates to all connected webui clients."""
    await webui_manager.broadcast(
        {"type": WSMessageType.HOST_STATUS.value, "data": status_update}
    )


async def broadcast_routing_event(event_data: dict):
    """Broadcast routing events to all connected webui clients."""
    await webui_manager.broadcast(event_data)
    # Also persist the event to gateway logs
    try:
        from app.gateway_logs import event_logger

        await event_logger.on_event(event_data)
    except Exception:
        # Never let logging failures impact routing broadcasts
        pass


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

        host_id = registration.host_id

        # Register the host
        if not await host_connection_manager.register_host(
            host_id, websocket, registration
        ):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Registration failed - invalid host or API key",
                }
            )
            await websocket.close()
            return

        # Send registration acknowledgement
        await websocket.send_json(
            {
                "type": "registration_ack",
                "host_id": host_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Get host info for broadcasts
        host = host_manager.get_host(host_id)
        host_name = host.name if host else "unknown"

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
    - Instance logs (forwarded from hosts)
    - Instance state updates (forwarded from hosts)
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

        # Keep connection alive and wait for client messages
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_text("pong")
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
