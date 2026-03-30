from __future__ import annotations

import ipaddress
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any

from .config import Settings


LOGGER = logging.getLogger("tunnel_admin.docker_config")


class DockerConfigManager:
    def __init__(self, settings: Settings, database: Any) -> None:
        self.settings = settings
        self.database = database
        self.network = ipaddress.ip_network(settings.docker_network_subnet, strict=True)
        if self.network.version != 4:
            raise ValueError("Only IPv4 Docker NAT subnets are supported")

    def sync_all(self, endpoints: list[dict[str, Any]]) -> None:
        for endpoint in endpoints:
            self.sync_endpoint(endpoint)

    def sync_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        endpoint_id = int(endpoint["id"])
        docker_nat_ip = self._normalize_existing_nat_ip(str(endpoint.get("docker_nat_ip") or "").strip())
        if docker_nat_ip is None:
            docker_nat_ip = self.database.allocate_next_docker_nat_ip(self.settings.docker_network_subnet)

        endpoint_dir = self.settings.docker_configs_dir / f"endpoint-{endpoint_id}"
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        (endpoint_dir / "commands").mkdir(parents=True, exist_ok=True)

        metadata = {
            "docker_nat_ip": docker_nat_ip,
            "docker_network_name": self.settings.docker_network_name,
            "docker_service_name": f"tunnel-endpoint-{endpoint_id}",
            "docker_container_name": f"tunnel-endpoint-{endpoint_id}",
            "docker_compose_path": str((endpoint_dir / "docker-compose.yml").resolve()),
            "docker_endpoint_config_path": str((endpoint_dir / "endpoint.json").resolve()),
        }

        runtime_endpoint, compose_spec = self._build_artifacts(endpoint, metadata)
        Path(metadata["docker_endpoint_config_path"]).write_text(
            json.dumps(runtime_endpoint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        Path(metadata["docker_compose_path"]).write_text(compose_spec, encoding="utf-8")
        self.database.update_endpoint_docker_metadata(endpoint_id, metadata)
        updated = self.database.get_endpoint(endpoint_id)
        if updated is None:
            raise RuntimeError(f"Failed to reload endpoint {endpoint_id} after Docker sync")
        return updated

    def delete_endpoint_artifacts(self, endpoint: dict[str, Any]) -> None:
        endpoint_id = int(endpoint["id"])
        target_dir = (self.settings.docker_configs_dir / f"endpoint-{endpoint_id}").resolve()
        docker_root = self.settings.docker_configs_dir.resolve()
        if docker_root not in target_dir.parents:
            raise RuntimeError(f"Refusing to delete artifacts outside {docker_root}: {target_dir}")
        if target_dir.exists():
            shutil.rmtree(target_dir)

    def _build_artifacts(
        self,
        endpoint: dict[str, Any],
        metadata: dict[str, str],
    ) -> tuple[dict[str, Any], str]:
        endpoint_dir = str((self.settings.docker_configs_dir / f"endpoint-{int(endpoint['id'])}").resolve())
        runtime_config_path = metadata["docker_endpoint_config_path"]
        runtime_endpoint = {
            "id": int(endpoint["id"]),
            "name": str(endpoint["name"]),
            "tunnel_type": str(endpoint["tunnel_type"]),
            "listen_host": self._container_listen_host(str(endpoint["listen_host"])),
            "listen_port": int(endpoint["listen_port"]),
            "destination_host": str(endpoint["destination_host"]),
            "destination_port": int(endpoint["destination_port"]),
            "ssh_host": str(endpoint["ssh_host"]),
            "ssh_port": int(endpoint.get("ssh_port") or 22),
            "ssh_username": str(endpoint["ssh_username"]),
            "ssh_private_key_path": None,
            "ssh_known_hosts_path": None,
            "ssh_options": str(endpoint.get("ssh_options") or ""),
            "description": str(endpoint.get("description") or ""),
            "allowed_client_cidr": endpoint.get("allowed_client_cidr"),
            "enabled": True,
            "max_clients": int(endpoint.get("max_clients") or 0),
            "idle_timeout": int(endpoint.get("idle_timeout") or 0),
            "tags": str(endpoint.get("tags") or ""),
            "connect_timeout_seconds": float(self.settings.connect_timeout_seconds),
        }

        bind_mounts = [
            {
                "source": endpoint_dir,
                "target": "/app_data",
                "read_only": False,
            }
        ]

        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.exists() and ssh_dir.is_dir():
            bind_mounts.append(
                {
                    "source": str(ssh_dir.resolve()),
                    "target": "/root/.ssh",
                    "read_only": True,
                }
            )

        ssh_private_key_path = str(endpoint.get("ssh_private_key_path") or "").strip()
        if ssh_private_key_path:
            runtime_endpoint["ssh_private_key_path"] = "/run/tunnel-secrets/ssh_private_key"
            bind_mounts.append(
                {
                    "source": str(Path(ssh_private_key_path).expanduser().resolve()),
                    "target": "/run/tunnel-secrets/ssh_private_key",
                    "read_only": True,
                }
            )

        ssh_known_hosts_path = str(endpoint.get("ssh_known_hosts_path") or "").strip()
        if ssh_known_hosts_path:
            runtime_endpoint["ssh_known_hosts_path"] = "/run/tunnel-secrets/known_hosts"
            bind_mounts.append(
                {
                    "source": str(Path(ssh_known_hosts_path).expanduser().resolve()),
                    "target": "/run/tunnel-secrets/known_hosts",
                    "read_only": True,
                }
            )

        environment = {
            "PYTHONUNBUFFERED": "1",
            "TUNNEL_RUNTIME_STATE_FILE": "/app_data/runtime.json",
            "TUNNEL_COMMANDS_DIR": "/app_data/commands",
        }
        ssh_auth_sock = str(os.getenv("SSH_AUTH_SOCK") or "").strip()
        if ssh_auth_sock:
            ssh_auth_sock_path = Path(ssh_auth_sock)
            if ssh_auth_sock_path.exists():
                bind_mounts.append(
                    {
                        "source": str(ssh_auth_sock_path.resolve()),
                        "target": "/run/host-services/ssh-auth.sock",
                        "read_only": False,
                    }
                )
                environment["SSH_AUTH_SOCK"] = "/run/host-services/ssh-auth.sock"

        compose_spec = self._render_compose(
            endpoint=endpoint,
            metadata=metadata,
            bind_mounts=bind_mounts,
            environment=environment,
        )
        return runtime_endpoint, compose_spec

    def _render_compose(
        self,
        *,
        endpoint: dict[str, Any],
        metadata: dict[str, str],
        bind_mounts: list[dict[str, Any]],
        environment: dict[str, str],
    ) -> str:
        service_name = metadata["docker_service_name"]
        port_binding = self._build_port_binding(str(endpoint["listen_host"]), int(endpoint["listen_port"]))

        lines = [
            f"name: {self._yaml_string(service_name)}",
            "",
            "services:",
            f"  {service_name}:",
            f"    container_name: {self._yaml_string(metadata['docker_container_name'])}",
            f"    image: {self._yaml_string(self.settings.docker_runner_image)}",
            "    restart: \"no\"",
            "    command:",
            f"      - {self._yaml_string('/app_data/endpoint.json')}",
            "    environment:",
        ]

        for key, value in environment.items():
            lines.append(f"      {key}: {self._yaml_string(value)}")

        lines.extend(
            [
                "    ports:",
                f"      - {self._yaml_string(port_binding)}",
                "    volumes:",
            ]
        )

        for mount in bind_mounts:
            lines.extend(
                [
                    "      - type: bind",
                    f"        source: {self._yaml_string(mount['source'])}",
                    f"        target: {self._yaml_string(mount['target'])}",
                    f"        read_only: {str(bool(mount.get('read_only', True))).lower()}",
                ]
            )

        lines.extend(
            [
                "    networks:",
                f"      {self.settings.docker_network_name}:",
                f"        ipv4_address: {self._yaml_string(metadata['docker_nat_ip'])}",
                "",
                "networks:",
                f"  {self.settings.docker_network_name}:",
                f"    name: {self._yaml_string(self.settings.docker_network_name)}",
                "    external: true",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _container_listen_host(listen_host: str) -> str:
        host = listen_host.strip()
        if host == "::":
            return "::"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "0.0.0.0"
        return "::" if address.version == 6 else "0.0.0.0"

    @staticmethod
    def _build_port_binding(listen_host: str, listen_port: int) -> str:
        host = listen_host.strip()
        if not host or host == "*":
            return f"{listen_port}:{listen_port}/tcp"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return f"{listen_port}:{listen_port}/tcp"
        if address.version == 6:
            return f"[{host}]:{listen_port}:{listen_port}/tcp"
        return f"{host}:{listen_port}:{listen_port}/tcp"

    @staticmethod
    def _yaml_string(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _normalize_existing_nat_ip(self, candidate: str) -> str | None:
        if not candidate:
            return None
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            LOGGER.warning("Ignoring invalid stored docker_nat_ip=%s", candidate)
            return None
        if address.version != 4 or address not in self.network:
            LOGGER.warning("Ignoring out-of-range stored docker_nat_ip=%s", candidate)
            return None
        if address in {self.network.network_address, self.network.broadcast_address}:
            LOGGER.warning("Ignoring reserved stored docker_nat_ip=%s", candidate)
            return None
        return candidate
