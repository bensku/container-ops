from dataclasses import dataclass, field
from io import StringIO
import json
import os
import subprocess
from pyinfra import host
from pyinfra.api import operation, StringCommand, FunctionCommand, FileUploadCommand
from pyinfra.operations import files, systemd, server
from pyinfra.facts.files import Sha256File, Sha1File

from containerops import podman


NEBULA_DOWNLOAD = 'https://github.com/slackhq/nebula/releases/download/v1.9.5/nebula-linux-amd64.tar.gz'
NEBULA_HASH = 'af57ded8f3370f0486bb24011942924b361d77fa34e3478995b196a5441dbf71'


@dataclass
class Network:
    """
    Nebula network configuration. This should be shared between CA and
    all certificates/endpoints.
    
    Arguments:
        name: Name of the network.
        dns_domain: DNS domain of the network.
        prefix_len: Network prefix length.
        epoch: Epoch. Initially, this should be set to 1. This can be increased
            to re-create certificates. Note that certificate renewal is likely
            to require manual work due to insufficient testing.
        lighthouses: List of lighthouses and their addresses. These do not need
            to be set to create CA or certificates, but should be set when
            actual endpoints are created.
    """

    name: str
    dns_domain: str
    prefix_len: int
    epoch: int
    lighthouses: list[tuple[str, str]] = field(repr=False)

    def state(self):
        return f'{self.name}-{self.epoch}'


@dataclass
class FirewallRule:
    port: str | int
    groups: str | list[str]
    protocol: str = field(default='any')


@dataclass
class Firewall:
    inbound: list[FirewallRule]
    outbound: list[FirewallRule]


@operation()
def ca(network: Network, duration: str = '876000h'):
    """
    Create a Nebula certificate authority on the current host.

    The CA key and certificate will be stored at
    /opt/containerops/nebula/nebula-cert. If you used this operation on
    a remote server, you should copy them to same location on your Pyinfra
    host.

    Arguments:
        network: Network definition. This should be same that will be used for
            creating certificates and endpoints. The lighthouses do not need to
            actually exist yet.
        duration: CA validity. Defaults to practically forever, but if you can
            rotate certificates on all endpoints, setting this to lower would
            be more secure.
    """

    yield from _ensure_installed()
    cert_dir = f'/etc/containerops/nebula/networks/{network.name}/ca/{network.epoch}'
    yield StringCommand(f'mkdir -p "{cert_dir}"')
    yield StringCommand(
        f'/opt/containerops/nebula/nebula-cert ca -name "{network.name} root, epoch {network.epoch}" -duration {duration}',
        f'-out-crt "{cert_dir}/ca.crt" -out-key "{cert_dir}/ca.key"'
    )


@operation()
def certificate(network: Network, hostname: str, ip: str, groups: list[str] = []):
    """
    Create a Nebula certificate for an endpoint. This is useful when you do not
    wish to use Pyinfra to configure the endpoint (e.g. mobile devices).

    The certificate will be created LOCALLY, and require network CA to
    be available. Make sure that your user has write access to
    /opt/containerops/nebula directory.

    The created certificates can be found at
    /etc/containerops/nebula/networks/<network name>/endpoint/<hostname>.
    """

    yield from _ensure_installed()
    # Generate the certificate locally
    ca_dir = f'/etc/containerops/nebula/networks/{network.name}/ca/{network.epoch}'
    cert_dir = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}'

    # First, check if one might've already been created with same parameters
    new_state = f'{network.state()} {hostname} {ip}/{network.prefix_len} {groups}'
    try:
        with open(f'{cert_dir}/state.txt', 'r') as f:
            prev_state = f.read()
    except FileNotFoundError:
        prev_state = ''
    if prev_state != new_state:
        # Create new certificate, possibly overwriting an old one
        yield FunctionCommand(_new_cert, args=[hostname, ip, network.prefix_len, ca_dir, cert_dir, groups], func_kwargs={})
        yield FunctionCommand(_update_state, args=[cert_dir, new_state], func_kwargs={})

    # Deploy on server (if certificate or e.g. the target server changed)
    yield from files.put._inner(src=f'{ca_dir}/ca.crt', dest=f'{ca_dir}/ca.crt', group='nebula', mode='640')
    yield from files.put._inner(src=f'{cert_dir}/host.crt', dest=f'{cert_dir}/host.crt', group='nebula', mode='640')
    yield from files.put._inner(src=f'{cert_dir}/host.key', dest=f'{cert_dir}/host.key', group='nebula', mode='640')


def _new_cert(hostname: str, ip: str, prefix_len: int, ca_dir: str, cert_dir: str, groups: list[str]):
    # Make sure we have empty directory where to generate certificate
    os.makedirs(cert_dir, exist_ok=True)
    try:
        os.remove(f'{cert_dir}/host.key')
    except OSError:
        pass
    try:
        os.remove(f'{cert_dir}/host.crt')
    except OSError:
        pass
    try:
        os.remove(f'{cert_dir}/host-qrcode.png')
    except OSError:
        pass

    groups_opt = ','.join(groups) if len(groups) > 0 else ''
    subprocess.run(f'/opt/containerops/nebula/nebula-cert sign -name "{hostname}" -ip {ip}/{prefix_len} {groups_opt} -ca-crt "{ca_dir}/ca.crt" -ca-key "{ca_dir}/ca.key" -out-crt "{cert_dir}/host.crt" -out-key "{cert_dir}/host.key" -out-qr "{cert_dir}/host-qrcode.png"', check=True, shell=True)


def _update_state(cert_dir: str, state: str):
    with open(f'{cert_dir}/state.txt', 'w') as f:
        f.write(state)


@operation()
def endpoint(
        network: Network,
        hostname: str,
        ip: str,
        firewall: Firewall,
        create_cert: bool = True,
        is_lighthouse: bool = False,
        underlay_port: int = 0,
        pod: str = None,
        present: bool = True
    ):
    """
    Create an endpoint to attach the current host to a Nebula network.

    Arguments:
        network: Network configuration.
        hostname: Hostname of the endpoint.
            Lighthouses will answer for DNS queries about this name.
        ip: Endpoint IP address. Must be unique within Nebula network!
        firewall: Firewall configuration. Empty firewall means nothing is allowed!
        create_cert: Whether to create a certificate for the endpoint.
            Defaults to true. If true, the CA must be available locally.
        is_lighthouse: Whether this endpoint is a lighthouse. This should
            be set only for the network's configured lighthouses!
        underlay_port: Port to use for encrypted UDP traffic. This must be
            permitted by the host machine's firewall. Defaults to a random
            port picked by the host kernel, which is usually fine.
            For lighthouses, this must be set to the port listed for them in
            the network configuration!
        pod: If set, this endpoint will be created within a Podman pod.
            If the pod has been deployed with container-ops, use
            nebula.pod_endpoint() instead to get functional DNS for free!
        present: By default, the endpoint is created.
            If set to False, it will be removed instead.
    """
    if not present:
        # Remove endpoint
        yield StringCommand(f'rm -rf /etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}')
        yield StringCommand(f'rm -f /etc/systemd/system/nebula-{hostname}.service')
        yield from systemd.service._inner(service=f'nebula-{hostname}.service', enabled=False, running=False, daemon_reload=True)
        return

    if create_cert:
        yield from certificate._inner(network, hostname, ip)

    config = StringIO(json.dumps(_nebula_config(network, hostname, ip, is_lighthouse, underlay_port, firewall), indent=4, sort_keys=True))
    config_path = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}/config.json'
    config_changed = host.get_fact(Sha1File, path=config_path) != files.get_file_sha1(config)
    
    unit_file = StringIO(_nebula_unit(network, hostname, config_path, pod))
    unit_path = f'/etc/systemd/system/nebula-{hostname}.service'
    unit_changed = host.get_fact(Sha1File, path=unit_path) != files.get_file_sha1(unit_file)

    # If config file changed, update it...
    if config_changed:
        yield FileUploadCommand(src=config, dest=config_path, remote_temp_filename=host.get_temp_filename(config_path))

    # If unit file changed, upload new version and restart service
    # This will also take change of reloading Nebula config
    if unit_changed:
        yield FileUploadCommand(src=unit_file, dest=unit_path, remote_temp_filename=host.get_temp_filename(unit_path))
        yield from systemd.service._inner(service=f'nebula-{hostname}', enabled=True, running=True, restarted=True, daemon_reload=True)
    elif config_changed:
        # If only config changed, just reload (=send SIGHUP) the service
        yield from systemd.service._inner(service=f'nebula-{hostname}', enabled=True, running=True, reloaded=True)


def _nebula_config(network: Network, hostname: str, ip: str, is_lighthouse: bool, underlay_port: int, firewall: Firewall):
    ca_dir = f'/etc/containerops/nebula/networks/{network.name}/ca/{network.epoch}'
    cert_dir = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}'

    lighthouse_map = {}
    for lh in network.lighthouses:
        lighthouse_map[lh[0]] = lh[1]
    return {
        # Point to CA cert and host key material that should've been already uploaded
        'pki': {
            'ca': f'{ca_dir}/ca.crt',
            'cert': f'{cert_dir}/host.crt',
            'key': f'{cert_dir}/host.key',
        },
        # Configure how to reach lighthouses, even if we are lighthouse
        'static_host_map': lighthouse_map,
        # If we are not a lighthouse, enable the aforementioned lighthouses
        # If we ARE a lighthouse, enable DNS over overlay (but not underlay!) network
        'lighthouse': {
            'am_lighthouse': is_lighthouse,
            'serve_dns': is_lighthouse,
            'dns': { 'host': ip, 'port': 53, } if is_lighthouse else None,
            'hosts': list([l[0] for l in network.lighthouses] if not is_lighthouse else []),
        },
        'listen': {
            'host': '::', # All interfaces, both IPv4 and IPv6
            'port': underlay_port,
        },
        # NAT hole punching in case some endpoints are behind NATs
        # TODO make this configurable if user wants to reduce "unnecessary" network chatter
        'punchy': {
            'enabled': True,
            'respond': True,
        },
        'tun': {
            'disabled': False, # Lighthouses need TUN for DNS
            'dev': f'nebula{hostname[:8]}', # Truncate to max device name length
        },
        # Convert our firewall rule definitions to Nebula format
        'firewall': {
            'inbound': list([_convert_fw_rule(rule) for rule in firewall.inbound]),
            'outbound': list([_convert_fw_rule(rule) for rule in firewall.outbound]),
        },
        'logging': {
            'level': 'info', # TODO debug logging support
        },
        '_ip': ip,
    }


def _convert_fw_rule(rule: FirewallRule):
    if rule.groups == 'any' or rule.groups == ['any']:
        return {
            'port': rule.port,
            'host': 'any',
            'proto': rule.protocol,
        }
    else:
        return {
            'port': rule.port,
            'groups': [rule.groups] if isinstance(rule.groups, str) else rule.groups,
            'proto': rule.protocol,
        }


def _nebula_unit(network: Network, hostname: str, config_path: str, target_pod: str = None):
    config_path = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}/config.json'
    return f'''
[Unit]
Description=Nebula overlay - {hostname} ({network.name})
Wants=network-online.target
After=network-online.target
{f'Requires={target_pod}-pod.service\nAfter={target_pod}-pod.service' if target_pod else ''}

[Service]
ExecStartPre=/opt/containerops/nebula/nebula-container -test -config {config_path}
ExecStart={f'/opt/containerops/nebula/container-launch.sh {target_pod}-infra' if target_pod else '/opt/containerops/nebula/nebula-container'} -config {config_path}
ExecReload=/bin/kill -HUP $MAINPID

# RuntimeDirectory=nebula
# ConfigurationDirectory=nebula
# CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
# AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
# ProtectControlGroups=true
# ProtectHome=true
# ProtectKernelTunables=true
# ProtectSystem=full
User=root
Group=root

SyslogIdentifier=nebula

Restart=always
RestartSec=2
TimeoutStopSec=5
StartLimitInterval=0
LimitNOFILE=131072

Nice=-1

[Install]
WantedBy=multi-user.target
'''


def _pod_handler(network: Network, hostname: str, ip: str, firewall: Firewall,
                 pod: str, present: bool):
    yield from endpoint._inner(network, hostname=hostname, ip=ip, firewall=firewall, pod=pod, present=present)


def pod_endpoint(network: Network, hostname: str, ip: str, firewall: Firewall):
    """
    Create an endpoint that can be used to attach a pod to a Nebula network.

    When added to a pod's networks list, this allows it to access the Nebula
    network according to the given firewall rules. Pod DNS is also configured
    to resolve names under network's DNS domain to other endpoints.

    Arguments:
        network: Network configuration.
        hostname: Hostname of the endpoint.
            Lighthouses will answer for DNS queries about this name.
        ip: Endpoint IP address. Must be unique within Nebula network!
        firewall: Firewall configuration. Empty firewall means nothing is allowed!s
    """
    return podman.Network(
        name=f'nebula:{hostname}',
        handler=_pod_handler,
        args={'network': network, 'hostname': hostname, 'ip': ip, 'firewall': firewall},
        dns_domain=network.dns_domain,
        dns_servers=[lh[0] for lh in network.lighthouses]
    )

NEBULA_CONTAINER_SCRIPT = '''#!/bin/bash

CONTAINER_NAME="$1"

# Wait for the container to be up and retrieve its PID.
MAX_CONTAINER_WAIT=15  # Maximum seconds to wait for container startup
WAITED_CONTAINER=0
echo "Waiting for container '$CONTAINER_NAME' to be up..."
while true; do
  CONTAINER_PID=$(podman inspect --format '{{.State.Pid}}' "$CONTAINER_NAME" 2>/dev/null)
  if [ -n "$CONTAINER_PID" ] && [ "$CONTAINER_PID" -gt 0 ]; then
    break
  fi
  sleep 1
  WAITED_CONTAINER=$((WAITED_CONTAINER+1))
  if [ $WAITED_CONTAINER -ge $MAX_CONTAINER_WAIT ]; then
    echo "Error: Container '$CONTAINER_NAME' did not start within $MAX_CONTAINER_WAIT seconds."
    exit 1
  fi
done

echo "Container '$CONTAINER_NAME' is running with PID $CONTAINER_PID."

# Launch Nebula with TUN inside the container's network namespace
shift # Pass rest of arguments to Nebula
echo "Executing Nebula..."
exec /opt/containerops/nebula/nebula-container -netns /proc/"$CONTAINER_PID"/ns/net $@
'''


def _ensure_installed():
    yield from server.user._inner(user='nebula', system=True, create_home=False)
    if host.get_fact(Sha256File, path='/opt/containerops/nebula.tar.gz') != NEBULA_HASH:
        yield StringCommand('mkdir -p /opt/containerops/nebula')
        yield from files.download._inner(src=NEBULA_DOWNLOAD, dest='/opt/containerops/nebula.tar.gz', sha256sum=NEBULA_HASH)
        yield StringCommand('tar xzf /opt/containerops/nebula.tar.gz -C /opt/containerops/nebula')
    # TODO download this from server, this won't work except for testing!
    yield from files.put._inner(src='nebula-container/nebula-container', dest='/opt/containerops/nebula/nebula-container', mode='755')
    yield from files.put._inner(src=StringIO(NEBULA_CONTAINER_SCRIPT), dest='/opt/containerops/nebula/container-launch.sh', mode='755')