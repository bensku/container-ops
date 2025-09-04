from dataclasses import dataclass, field
from io import StringIO
import json
import os
from typing import Optional
from pyinfra import host
from pyinfra.api import operation, FileUploadCommand, StringCommand, MaskString
from pyinfra.operations import files, systemd
from pyinfra.facts.server import Command
from pyinfra.facts.files import Sha1File, FindFiles

@dataclass
class Container:
    """
    Podman container specification.

    Arguments:
        name: Container name. If the container is created as part of pod,
            it will be prefixed with the pod name. Otherwise, used as-is.
        image: OCI container image to run.
        volumes: List of volume mounts for the container in the form of
            (source, container path). Optional. Supported sources are:
            * Absolute paths on host
            * Podman volume names (note: volumes are not automatically created!)
            * ConfigFile objects (for auto-uploading configuration files)
        environment: Environment variables in the form of (name, value). Optional.
        secrets: Podman secrets to add to the container as environment variables
            in form of (env variable name, secret name). Optional.
        entrypoint: Override for the container entrypoint. Optional.
        command: Override for the container command. Optional.
        reload_signal: Unix signal to send to the container when it needs to
            be reloaded. Optional, defaults to restarting with SIGTERM. You
            might want to change this if your application can reload its
            configuration files without downtime (e.g. via SIGHUP).
        linuxCapabilities: Linux capabilities to add to the container. Optional.
        linuxDevices: Linux devices to add to the container. Optional.
        dependencies: List of containers that this container needs.
            The dependencies are started before this container, and if they
            go down, this container is KILLED! Optional.
        restart_delay: When container comes down, how many seconds to wait
            before attempting to restart it. Defaults to 15.
        present: By default, container is deployed. Set this to False to
            delete it instead.
    """

    name: str
    image: str
    volumes: list[tuple[str, str]] = field(default_factory=list)
    environment: list[tuple[str, str]] = field(default_factory=list)
    secrets: list[tuple[str, str]] = field(default_factory=list)

    entrypoint: Optional[str] = field(default=None)
    command: Optional[str] = field(default=None)
    reload_signal: str = field(default='SIGTERM')

    # Advanced options
    linuxCapabilities: list[str] = field(default_factory=list)
    linuxDevices: list[str] = field(default_factory=list)
    sysctls: list[tuple[str, str]] = field(default_factory=list)

    dependencies: list[str] = field(default_factory=list)
    restart_delay: int = field(default=15)
    present: bool = field(default=True)

    def __repr__(self):
        result = ', '.join([
            f'{key}={value}' for key, value in self.__dict__.items() 
            if value not in ([], None)
        ])
        return f'{self.__class__.__name__}({result})'


@dataclass
class Network:
    """
    Container network specification.
    
    For direct Internet connectivity, use HOST_NAT. It will enable outbound
    access through the host, and allow inbound access by specifying port
    bindigs.

    For internal container networking, consider containerops.nebula module.
    """
    name: str
    handler: any
    args: dict

    dns_domain: str
    dns_servers: list[str] = field(default_factory=list)
    systemd_services: list[str] = field(default_factory=list)

    def __repr__(self):
        return f'Network({self.name})'


@dataclass
class ConfigFile:
    """
    Container configuration file. Automatically uploaded when used as volume
    source.

    Arguments:
        id: Id of the configuration file. This must be unique between ALL
        containers running on same machine.
        data: Configuration file content.
        needs_reload: By default, the container this is mounted to is
            reloaded when the file contents change. When set to False, the
            application inside must detect the change by itself.
    """
    id: str
    data: str
    needs_reload: bool = field(default=True)

    def __repr__(self):
        return f'ConfigFile({self.id})'


HOST_NAT = Network(name='host-nat', handler='podman_host_nat', args={}, dns_domain='', dns_servers=[])


def custom_dns(domain: str, servers: list[str]) -> Network:
    """
    Creates a pseudo-network that only adds DNS servers to the pods it is
    attached to.

    This may be useful if you want split DNS while still running actual data
    through one network.

    Arguments:
        domain: DNS domain that the given servers should be used for.
        servers: List of DNS servers to add to the network.
    """
    return Network(name='custom-dns', handler='custom_dns', args={}, dns_domain=domain, dns_servers=servers)


@operation()
def pod(pod_name: str, containers: list[Container], networks: list[Network], ports: list[tuple[str, str, str]] = [], present: bool = True,
        _expose_dns: bool = False):
    """
    Deploy a Podman pod.

    Arguments:
        pod_name: Name of the pod. Must be unique within same machine.
        containers: List of containers to deploy to the pod.
        networks: List of networks to attach to the pod.
        ports: List of port mappings for incoming traffic in form of
            (source port, target port, protocol). Protocol defaults to 'tcp'.
            Optional. This option REQUIRES the HOST_NAT network!
        present: By default, the pod is deployed.
            Set this to False to delete it instead.
    """

    if not present:
        # Remove ALL containers
        yield from _remove_missing_containers(containers=[], pod_name=pod_name)
        yield from _pod_dns(pod_name=pod_name, networks=[], all_networks=False, present=False)

        # Remove container-ops external networks
        for net in networks:
            if net.handler != 'podman_host_nat' and net.handler != 'custom_dns':
                yield from net.handler(**net.args, pod=pod_name, present=False)

        # Remove pod, then finally its network
        yield from _install_service(unit_name=f'{pod_name}.pod', service_name=f'{pod_name}-pod', unit='', present=False)
        yield StringCommand(f'podman pod rm -f {pod_name}') # Otherwise, it is just stopped for some reason?
        yield from _install_service(unit_name=f'{pod_name}.network', service_name=f'{pod_name}-network', unit='', present=False)
        yield StringCommand(f'podman network rm -f {pod_name}') # NetworkDeleteOnStop needs too new Podman
        return
        

    # Deploy separate network so that we can control if it is internal or not
    net_unit = f"""[Unit]
Description={pod_name} - pod network

[Network]
NetworkName={pod_name}
Driver=bridge
DisableDNS=true
Internal={'false' if HOST_NAT in networks else 'true'}
"""
    yield from _install_service(unit_name=f'{pod_name}.network', service_name=f'{pod_name}-network', unit=net_unit, present=True)

    # Deploy the actual pod
    pod_unit = f"""[Unit]
Description={pod_name} - pod

[Pod]
PodName={pod_name}
Network={pod_name}.network
DNS=127.0.0.1
{'\n'.join([f'PublishPort={p[0]}:{p[1]}/{p[2] if len(p) > 2 else "tcp"}' for p in ports])}

[Service]
Restart=always

[Install]
WantedBy=multi-user.target default.target
"""
    yield from _install_service(unit_name=f'{pod_name}.pod', service_name=f'{pod_name}-pod', unit=pod_unit, present=True)

    # Deploy DNS container for multi-network support
    yield from _pod_dns(pod_name=pod_name, networks=networks, all_networks=_expose_dns, present=True)

    # Remove containers that are no longer present
    yield from _remove_missing_containers(containers=containers, pod_name=pod_name)

    # Figure out if networks create services that our containers need to depend on
    net_svcs = [svc for net in networks for svc in net.systemd_services]

    # Deploy non-NAT networks
    for net in networks:
        if net.handler != 'podman_host_nat' and net.handler != 'custom_dns':
            yield from net.handler(**net.args, pod=pod_name, present=True)

    # Deploy this pod's containers
    for spec in containers:
        yield from container._inner(spec=spec, pod_name=pod_name, required_svcs=net_svcs)


def _pod_dns(pod_name: str, networks: list[Network], all_networks: bool, present: bool):
    if all_networks:
        serve_config = '''# Provide DNS to all networks
bind-interfaces
'''
    else:
        serve_config = '''# Provide DNS to this pod only
bind-interfaces
interface=lo
no-dhcp-interface=lo'''
    config = f'''{serve_config}

# DNS servers for pod networks
{'\n'.join([f'server=/{net.dns_domain}/{server}' for net in networks for server in net.dns_servers])}

# Fallback to public DNS
# TODO configurable
no-hosts
no-resolv
server=1.1.1.1
'''
    spec = Container(
        name='dns',
        image='ghcr.io/bensku/containerops-builds/dnsmasq:latest',
        volumes=[(ConfigFile(id=f'{pod_name}-dns-config', data=config), '/etc/dnsmasq.conf')],
        present=present
    )
    yield from container._inner(spec=spec, pod_name=pod_name)


def _remove_missing_containers(containers: list[Container], pod_name: str):
    container_names = set([container.name for container in containers])
    unit_files = host.get_fact(FindFiles, path=f'/etc/containers/systemd')
    for path in unit_files:
        unit_name = os.path.basename(path)
        if unit_name.endswith('.container') and unit_name.startswith(f'{pod_name}-') and not unit_name.endswith(f'{pod_name}-dns.container'):
            container_name = unit_name[len(f'{pod_name}-'):-len('.container')]
            if container_name not in container_names:
                yield from _install_service(unit_name=unit_name, service_name=f'{pod_name}-{container_name}', unit='', present=False)



@operation()
def container(spec: Container, pod_name: str = None, required_svcs: list[str] = []):
    pod_prefix = f'{pod_name}-' if pod_name else ''
    service_name = f'{pod_prefix}{spec.name}'

    # Upload container configuration files (or remove them)
    config_needs_reload = False
    dir_created = False
    for v in spec.volumes:
        if type(v[0]) == ConfigFile:
            if spec.present:
                new_config = StringIO(v[0].data)
                local_hash = files.get_file_sha1(new_config)
                remote_path = f'/etc/containerops/configs/{v[0].id}'
                remote_hash = host.get_fact(Sha1File, path=remote_path)
                if local_hash != remote_hash:
                    if not dir_created:
                        yield StringCommand('mkdir -p "/etc/containerops/configs"')
                        dir_created = True
                    yield FileUploadCommand(src=new_config, dest=remote_path, remote_temp_filename=host.get_temp_filename(remote_path))
                    if v[0].needs_reload:
                        config_needs_reload = True
            else:
                yield StringCommand(f'rm -f "/etc/containerops/configs/{v[0].id}"')

    # Get secret ids - we'll refer to them by these, so that changes force service restart
    secret_ids = []
    for (_, secret_name) in spec.secrets:
        secret_ids.append(host.get_fact(Command, f'podman secret inspect {secret_name} --format "{{{{.ID}}}}"').strip())

    unit = f"""[Unit]
Description={f'{pod_name} - {spec.name}' if pod_name else spec.name}
{'\n'.join([f'Requires={pod_prefix}{c}.service\nAfter={pod_prefix}{c}.service' for c in spec.dependencies])}
{'\n'.join([f'Requires={svc}.service\nAfter={svc}.service' for svc in required_svcs])}
StartLimitIntervalSec=0

[Container]
ContainerName={service_name}
Image={spec.image}
ReloadSignal={spec.reload_signal}
{f'Pod={pod_name}.pod' if pod_name else ''}
{'\n'.join([f'Volume={f'/etc/containerops/configs/{v[0].id}' if type(v[0]) == ConfigFile else v[0]}:{v[1]}' for v in spec.volumes])}
{'\n'.join([f'Environment={e[0]}={e[1]}' for e in spec.environment])}
{'\n'.join([f'Secret={secret_ids[i]},type=env,target={secret[0]}' for i, secret in enumerate(spec.secrets)])}

{f'Entrypoint={spec.entrypoint}' if spec.entrypoint else ''}
{f'Exec={spec.command}' if spec.command else ''}

{'\n'.join([f'AddCapability={c}' for c in spec.linuxCapabilities])}
{'\n'.join([f'AddDevice={d}' for d in spec.linuxDevices])}
{'\n'.join([f'Sysctl={ctl[0]}={ctl[1]}' for ctl in spec.sysctls])}

[Service]
Restart=always
RestartSec={spec.restart_delay}s

[Install]
WantedBy=multi-user.target default.target
"""
    yield from _install_service(unit_name=f'{service_name}.container', service_name=service_name, unit=unit, present=spec.present, reload=config_needs_reload)


def _install_service(unit_name: str, service_name: str, unit: str, present: bool, reload: bool = False):
    remote_path = f'/etc/containers/systemd/{unit_name}'

    if present:
        # Update and restart the unit if it has changed from server's version
        # TODO restart if ConfigFiles change (unless the files opt out of that)
        local_unit = StringIO(unit)
        local_hash = files.get_file_sha1(local_unit)
        remote_hash = host.get_fact(Sha1File, path=remote_path)

        if local_hash != remote_hash:
            yield FileUploadCommand(src=local_unit, dest=remote_path, remote_temp_filename=host.get_temp_filename(remote_path))
            yield from systemd.service._inner(service=service_name, running=True, restarted=True, daemon_reload=True)
        elif reload:
            # Caller has requested at least reload, and we don't need to restart
            yield from systemd.service._inner(service=service_name, running=True, reloaded=True)
    else:
        # Uninstall the systemd service
        yield StringCommand(f'rm -f "{remote_path}"')
        yield from systemd.daemon_reload._inner()
        yield from systemd.service._inner(service=service_name, running=False, daemon_reload=True)


@operation()
def secret(secret_name: str, source: str, json_key: str = None, present: bool = True):
    """
    Create a Podman secret based on a local file.

    Arguments:
        secret_name: Name of the secret. Must be unique within host.
        source: Path of local file to read the secret from.
        json_key: JSON key in the local file. If this is set, the source
            file must be valid JSON and contain this top-level key. Value
            of the key will be set as secret value.
        present: By default, the secret is created. If False, it is deleted.
    """
    if present:
        with open(source, 'r') as f:
            data = f.read()
        value = json.loads(data)[json_key] if json_key else data
        old_value = host.get_fact(Command, f'podman secret inspect {secret_name} --format "{{{{.SecretData}}}}" --showsecret || true')
        if old_value:
            old_value = old_value.strip()
        if value != old_value:
            yield StringCommand('echo -n', MaskString(f'"{value}"'), '|', 'podman secret create --replace', secret_name, '-')
    else:
        yield StringCommand('podman secret rm --ignore', secret_name)