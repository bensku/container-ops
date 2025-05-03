from dataclasses import dataclass, field
from io import StringIO
import os
from typing import Optional
from pyinfra import host
from pyinfra.api import operation, FileUploadCommand, StringCommand
from pyinfra.operations import files, systemd
from pyinfra.facts.files import Sha1File, FindFiles

@dataclass
class Container:
    name: str
    image: str
    volumes: list[tuple[str, str]] = field(default_factory=list)
    environment: list[tuple[str, str]] = field(default_factory=list)

    entrypoint: Optional[str] = field(default=None)
    command: Optional[str] = field(default=None)

    # Advanced options
    linuxCapabilities: list[str] = field(default_factory=list)
    linuxDevices: list[str] = field(default_factory=list)

    dependencies: list[str] = field(default_factory=list)
    present: bool = field(default=True)

    def __repr__(self):
        result = ', '.join([
            f'{key}={value}' for key, value in self.__dict__.items() 
            if value not in ([], None)
        ])
        return f'{self.__class__.__name__}({result})'


@dataclass
class Network:    
    handler: any
    args: dict

    def __repr__(self):
        return 'HOST_NAT' if self == HOST_NAT else f'Network({self.handler})'


@dataclass
class ConfigFile:
    id: str
    data: str

    def __repr__(self):
        return f'ConfigFile({self.id})'


HOST_NAT = Network(handler='podman_host_nat', args={})


@operation()
def pod(pod_name: str, containers: list[Container], networks: list[Network], ports: list[tuple[str, str, str]] = [], present: bool = True):
    if not present:
        # TODO Remove containers and network containers before the pod or network!
        return

    net_unit = f"""[Unit]
Description={pod_name} - pod network

[Network]
NetworkName={pod_name}
Driver=bridge
Internal={'false' if HOST_NAT in networks else 'true'}
"""
    yield from _install_service(unit_name=f'{pod_name}.network', service_name=f'{pod_name}-network', unit=net_unit, present=present)

    pod_unit = f"""[Unit]
Description={pod_name} - pod

[Pod]
PodName={pod_name}
Network={pod_name}.network
{'\n'.join([f'PublishPort={p[0]}:{p[1]}/{p[2] if len(p) > 2 else "tcp"}' for p in ports])}
"""
    yield from _install_service(unit_name=f'{pod_name}.pod', service_name=f'{pod_name}-pod', unit=pod_unit, present=present)

    # Remove containers that are no longer present
    container_names = set([container.name for container in containers])
    unit_files = host.get_fact(FindFiles, path=f'/etc/containers/systemd')
    for path in unit_files:
        unit_name = os.path.basename(path)
        if unit_name.endswith('.container') and unit_name.startswith(f'{pod_name}-'):
            container_name = unit_name[len(f'{pod_name}-'):-len('.container')]
            if container_name not in container_names:
                yield from _install_service(unit_name=unit_name, service_name=f'{pod_name}-{container_name}', unit='', present=False)

    # Deploy this pod's containers
    for spec in containers:
        yield from container._inner(spec=spec, pod_name=pod_name)

    # Deploy non-NAT networks
    for net in networks:
        if net.handler != 'podman_host_nat':
            # TODO network deletion separately immediately after container deletion
            yield from net.handler(**net.args, present=True)


@operation()
def container(spec: Container, pod_name: str = None):
    pod_prefix = f'{pod_name}-' if pod_name else ''
    service_name = f'{pod_prefix}{spec.name}'

    # Upload container configuration files
    for v in spec.volumes:
        if type(v[0]) == ConfigFile:
            yield from files.put._inner(src=StringIO(v[0].data), dest=f'/etc/containerops/configs/{v[0].id}')

    unit = f"""[Unit]
Description={f'{pod_name} - {spec.name}' if pod_name else spec.name}
{'\n'.join([f'Requires={pod_prefix}{c}.service\nAfter={pod_prefix}{c}.service' for c in spec.dependencies])}

[Container]
ContainerName={service_name}
Image={spec.image}
{f'Pod={pod_name}.pod' if pod_name else ''}
{'\n'.join([f'Volume={f'/etc/containerops/configs/{v[0].id}' if type(v[0]) == ConfigFile else v[0]}:{v[1]}' for v in spec.volumes])}
{'\n'.join([f'Environment={e[0]}={e[1]}' for e in spec.environment])}

{f'Entrypoint={spec.entrypoint}' if spec.entrypoint else ''}
{f'Command={spec.command}' if spec.command else ''}

{'\n'.join([f'AddCapability={c}' for c in spec.linuxCapabilities])}
{'\n'.join([f'AddDevice={d}' for d in spec.linuxDevices])}
"""
    yield from _install_service(unit_name=f'{service_name}.container', service_name=service_name, unit=unit, present=spec.present)


def _install_service(unit_name: str, service_name: str, unit: str, present: bool):
    remote_path = f'/etc/containers/systemd/{unit_name}'

    if present:
        # Update and restart the unit if it has changed from server's version
        local_unit = StringIO(unit)
        local_hash = files.get_file_sha1(local_unit)
        remote_hash = host.get_fact(Sha1File, path=remote_path)

        if local_hash != remote_hash:
            yield FileUploadCommand(src=local_unit, dest=remote_path, remote_temp_filename=host.get_temp_filename(remote_path))
            yield from systemd.service._inner(service=service_name, running=True, restarted=True, daemon_reload=True)
    else:
        # Uninstall the systemd service
        yield StringCommand(f'rm -f "{remote_path}"')
        yield from systemd.daemon_reload._inner()
        yield from systemd.service._inner(service=service_name, running=False)

