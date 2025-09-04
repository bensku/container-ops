from dataclasses import asdict, dataclass, field
from io import StringIO
import json
import os
import subprocess
from pyinfra import host
from pyinfra.api import operation, StringCommand, FunctionCommand, FileUploadCommand
from pyinfra.operations import files, systemd, server, selinux
from pyinfra.facts.files import Sha256File, Sha1File

from containerops import podman, _ipam as ipam, _port_alloc as port_alloc


NEBULA_DOWNLOAD = 'https://github.com/slackhq/nebula/releases/download/v1.9.5/nebula-linux-amd64.tar.gz'
NEBULA_HASH = 'af57ded8f3370f0486bb24011942924b361d77fa34e3478995b196a5441dbf71'

# TODO arm64 support
NEBULA_NETNS_DOWNLOAD = 'https://github.com/bensku/nebula-netns/releases/download/v1.9.5-netns0/nebula-netns-linux-amd64'
NEBULA_NETNS_HASH = 'bdd56dfce37cc550fe0cc00c7b0232310c2ddd0d72b1f35c992b46f456f40d91'
CONTAINER_NEBULA_DOWNLOAD = 'https://github.com/bensku/nebula-netns/releases/download/v1.9.5-netns0/container-nebula.sh'
CONTAINER_NEBULA_HASH = 'd1b4f29ec04c98f58acf3c9148cfacefd65e856e36379b4ca77c01d330c94084'

FAILOVERD_DOWNLOAD = 'https://github.com/bensku/failoverd/releases/download/v0.0.1/failoverd-amd64'
FAILOVERD_HASH = '1c46cddcfa005a39897884bd63885d36668f683667fd7daf6aa41e545b70536d'

@dataclass
class Network:
    """
    Nebula network configuration. This should be shared between CA and
    all certificates/endpoints.
    
    Arguments:
        name: Name of the network.
        state_dir: Local directory where network state should be stored.
        dns_domain: DNS domain of the network.
        cidr: Network range in CIDR format.
        epoch: Epoch. Initially, this should be set to 1. This can be increased
            to re-create certificates. Note that certificate renewal is likely
            to require manual work due to insufficient testing.
        lighthouses: List of lighthouses and their addresses. These do not need
            to be set to create CA or certificates, but should be set when
            actual endpoints are created.
        underlay_port_range: Port range to use for encrypted UDP traffic of
            endpoints. Each endpoint may explicitly specify its own port,
            overriding this.
        failover_etcd: List of etcd endpoints for failover IPs. They must be
            reachable (and DNS resolveble!) from hosts that contain failover
            Nebula endpoints!
    """

    name: str
    state_dir: str
    dns_domain: str = field(repr=False)
    cidr: str
    epoch: int
    lighthouses: list[tuple[str, str]] = field(repr=False)

    underlay_port_range: tuple[int, int] = field(default=(12500, 13000), repr=False)
    failover_etcd: list[str] = field(default_factory=list, repr=False)

    def state(self):
        return f'{self.name}-{self.epoch}'
    
    @property
    def prefix_len(self) -> int:
        return int(self.cidr.split('/')[1])


@dataclass
class FirewallRule:
    port: str | int
    groups: str | list[str]
    protocol: str = field(default='any')


@dataclass
class Firewall:
    inbound: list[FirewallRule]
    outbound: list[FirewallRule]


def initialize_network(network: Network):
    # Serialize network configuration to see if it has changed
    new_state = json.dumps(asdict(network), indent=4, sort_keys=True)
    try:
        with open(f'{network.state_dir}/networks/{network.name}/state.json', 'r') as f:
            old_state = f.read()
    except FileNotFoundError:
        old_state = ''

    if new_state != old_state:
        # Check if we need to create CA certificate
        try:
            old_net = Network(**json.loads(old_state))
            if old_net.epoch != network.epoch:
                # TODO how to ensure host running this actually has nebula-cert installed?
                _make_ca(network)
        except (TypeError, json.JSONDecodeError):
            _make_ca(network)

        # Save new state
        # Truncate the file before writing new state
        with open(f'{network.state_dir}/networks/{network.name}/state.json', 'w') as f:
            f.truncate(0)
            f.write(new_state)


def _make_ca(network: Network, duration: str = '876000h'):
    cert_dir = f'{network.state_dir}/networks/{network.name}/ca/{network.epoch}'
    os.makedirs(cert_dir, exist_ok=True)
    subprocess.run([
        '/opt/containerops/nebula/nebula-cert', 'ca',
        '-name', f'{network.name} root, epoch {network.epoch}',
        '-duration', duration,
        '-out-crt', f'{cert_dir}/ca.crt',
        '-out-key', f'{cert_dir}/ca.key',
        '-out-qr', f'{cert_dir}/ca-qrcode.png'
    ], check=True)


@operation()
def certificate(network: Network, hostname: str, ip: str, groups: list[str] = [], deploy: bool = True):
    """
    Create a Nebula certificate for an endpoint. This is useful when you do not
    wish to use Pyinfra to configure the endpoint (e.g. mobile devices).

    The certificate will be created LOCALLY, and require network CA to
    be available. Make sure that your user has write access to
    /opt/containerops/nebula directory.

    The created certificates can be found at
    /etc/containerops/nebula/networks/<network name>/endpoint/<hostname>.

    Arguments:
        network: Network definition.
        hostname: Hostname of the endpoint. This should be unique within the
            network, and must end with the network's DNS domain.
        ip: IP address to put in certificate. Must be unique within network.
        groups: Groups to put into certificate. Firewalls of network endpoints
            can refer to these to permit traffic.
        deploy: Whether to deploy the certificate on current target host.
            Default to True; if disabled, the certificate will be available
            only within network's local state directory.
    """
    # Generate the certificate locally
    local_ca_dir = f'{network.state_dir}/networks/{network.name}/ca/{network.epoch}'
    remote_ca_dir = f'/etc/containerops/nebula/networks/{network.name}/ca/{network.epoch}'
    local_cert_dir = f'{network.state_dir}/networks/{network.name}/endpoint/{hostname}'
    remote_cert_dir = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}'

    # First, check if one might've already been created with same parameters
    cert_needs_update, new_state, _ = _cert_needs_update(network, hostname, ip, groups)
    if cert_needs_update:
        # Create new certificate, possibly overwriting an old one
        yield FunctionCommand(_new_cert, args=[hostname, ip, network.prefix_len, local_ca_dir, local_cert_dir, groups], func_kwargs={})
        yield FunctionCommand(_update_state, args=[local_cert_dir, new_state], func_kwargs={})

    # Deploy on server (if certificate or e.g. the target server changed)
    if deploy:
        yield from files.put._inner(src=f'{local_ca_dir}/ca.crt', dest=f'{remote_ca_dir}/ca.crt', group='nebula', mode='640')
        yield from files.put._inner(src=f'{local_cert_dir}/host.crt', dest=f'{remote_cert_dir}/host.crt', group='nebula', mode='640')
        yield from files.put._inner(src=f'{local_cert_dir}/host.key', dest=f'{remote_cert_dir}/host.key', group='nebula', mode='640')


def _cert_needs_update(network: Network, hostname: str, ip: str, groups: list[str]) -> tuple[bool, str]:
    local_cert_dir = f'{network.state_dir}/networks/{network.name}/endpoint/{hostname}'

    new_state = f'{network.state()} {hostname} {ip}/{network.prefix_len} {groups}'
    try:
        with open(f'{local_cert_dir}/state.txt', 'r') as f:
            prev_state = f.read()
    except FileNotFoundError:
        prev_state = ''

    updated = prev_state != new_state

    # If update is needed, check whether or not it is reloadable (e.g. group change) or needs full service restart
    reloadable_update = True
    if updated:
        prev_parts = prev_state.split(' ')
        new_parts = new_state.split(' ')
        # Hostname or IP changes are not reloadable
        # TODO reloadability of epoch/network name changes?
        reloadable_update = prev_parts[1] == new_parts[1] and prev_parts[2] == new_parts[2] if len(prev_parts) > 3 else False

    return prev_state != new_state, new_state, reloadable_update


def _new_cert(hostname: str, ip: str, prefix_len: int, ca_dir: str, cert_dir: str, groups: list[str], duration: str = None):
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

    groups_opt = '-groups ' + ','.join(groups) if len(groups) > 0 else ''
    duration_opt = f'-duration {duration}' if duration is not None else ''
    subprocess.run(f'/opt/containerops/nebula/nebula-cert sign -name "{hostname}" -ip {ip}/{prefix_len} {groups_opt} {duration_opt} -ca-crt "{ca_dir}/ca.crt" -ca-key "{ca_dir}/ca.key" -out-crt "{cert_dir}/host.crt" -out-key "{cert_dir}/host.key" -out-qr "{cert_dir}/host-qrcode.png"', check=True, shell=True)


def _update_state(cert_dir: str, state: str):
    with open(f'{cert_dir}/state.txt', 'w') as f:
        f.truncate(0)
        f.write(state)


@operation()
def endpoint(
        network: Network,
        hostname: str,
        firewall: Firewall,
        ip: str = None,
        groups: list[str] = [],
        create_cert: bool = True,
        is_lighthouse: bool = False,
        underlay_port: int = None,
        failover: bool = False,
        pod: str = None,
        present: bool = True
    ):
    """
    Create an endpoint to attach the current host to a Nebula network.

    This does not install Nebula binaries, so the host must have
    nebula.host_setup() done before!

    Arguments:
        network: Network configuration.
        hostname: Hostname of the endpoint.
            Lighthouses will answer for DNS queries about this name.
        firewall: Firewall configuration. Empty firewall means nothing is allowed!
        ip: Endpoint IP address. Optional, but must be unique within network
            if present. When not given, a random address is assigned with IPAM.
        groups: Groups of the endpoint. Other endpoints can refer to them in
            their firewalls.
        create_cert: Whether to create a certificate for the endpoint.
            Defaults to true. If true, the CA must be available locally.
        is_lighthouse: Whether this endpoint is a lighthouse. This should
            be set only for the network's configured lighthouses!
        underlay_port: Port to use for encrypted UDP traffic. This must be
            permitted by the host machine's firewall. Defaults to an unique
            port within network's allowed underlay port range (which, in turn,
            defaults to 12500-13000). If set to 0, the port will be picked by
            kernel, which may significantly complete firewall configuration.
            Finally, if set to a non-zero number, that port will be used and
            must be available. For lighthouses, a non-zero ports must be
            explicitly set and they must match their port in network configuration!
        failover: Enable failover for this endpoint. In failover mode,
            endpoints on different machines can share the same hostname, and
            etcd is used to make sure only one of them is active at a time.
            When the currently active endpoint goes down for any reason,
            another one will automatically take its place within a few dozen
            seconds.
        pod: If set, this endpoint will be created within a Podman pod.
            If the pod has been deployed with container-ops, use
            nebula.pod_endpoint() instead to get functional DNS for free!
        present: By default, the endpoint is created.
            If set to False, it will be removed instead.
    """
    if not hostname.endswith(network.dns_domain):
        raise ValueError(f'hostname {hostname} does not belong to network DNS domain {network.dns_domain}')
    if failover and len(network.failover_etcd) == 0:
        raise ValueError('failover_key set, but network does not support failover')

    # Make sure that we have IP and (even if statically set) it is unique
    ip = ipam.allocate_ip(
        network_name=network.name,
        hostname=hostname,
        cidr=network.cidr,
        present=present,
        ip=ip,
        base_dir=f'{network.state_dir}/networks',
    )
    # If no underlay port was given, allocate one dynamically
    # If port WAS given, assume that user knows what they're doing and do not validate anything
    if underlay_port is None:
        underlay_port = port_alloc.allocate_port(
            network_name=network.name,
            machine_id=host.name, # Name of the server we're deploying to
            hostname=hostname, # Endpoint hostname (lighthouse DNS name), has no relation to Pyinfra host.name
            port_range=network.underlay_port_range,
            present=present,
            base_dir=f'{network.state_dir}/networks',
        )

    if not present:
        # Remove endpoint
        yield StringCommand(f'rm -rf /etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}')
        yield StringCommand(f'rm -f /etc/systemd/system/nebula-{hostname}.service')
        yield from systemd.service._inner(service=f'nebula-{hostname}.service', enabled=False, running=False, daemon_reload=True)
        if failover:
            yield StringCommand(f'rm -f /etc/systemd/system/nebula-{hostname}-failover.service')
            yield from systemd.service._inner(service=f'nebula-{hostname}-failover.service', enabled=False, running=False, daemon_reload=True)
        return

    if is_lighthouse:
        # Mark lighthouses, we'll permit DNS traffic towards them even if everything else is blocked
        groups += ['_lighthouse']

    # Update certificate if needed; and we're going to do that, make sure Nebula picks it up immediately
    cert_updated = False
    if create_cert:
        cert_updated, _, cert_reloadable = _cert_needs_update(network, hostname, ip, groups)
        yield from certificate._inner(network, hostname, ip, groups)

    config_path = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}/config.json'

    # Check if Nebula configuration has changed
    ca_value = f'/etc/containerops/nebula/networks/{network.name}/ca/{network.epoch}/ca.crt'
    cert_value = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}/host.crt'
    key_value = f'/etc/containerops/nebula/networks/{network.name}/endpoint/{hostname}/host.key'
    config = StringIO(json.dumps(_nebula_config(network, hostname, ip, is_lighthouse, underlay_port, firewall,
                                                ca_value, cert_value, key_value), indent=4, sort_keys=True))
    config_changed = host.get_fact(Sha1File, path=config_path) != files.get_file_sha1(config)
    
    unit_file = StringIO(_nebula_unit(network, hostname, config_path, pod, failover))
    unit_path = f'/etc/systemd/system/nebula-{hostname}.service'
    unit_changed = host.get_fact(Sha1File, path=unit_path) != files.get_file_sha1(unit_file)

    # If config file changed, update it...
    if config_changed:
        yield FileUploadCommand(src=config, dest=config_path, remote_temp_filename=host.get_temp_filename(config_path))

    # If unit file changed, upload new version and restart service
    # This will also take change of reloading Nebula config
    if unit_changed:
        yield FileUploadCommand(src=unit_file, dest=unit_path, remote_temp_filename=host.get_temp_filename(unit_path))
        if not failover: # If failover is enabled, it starts/stops the service as needed
            yield from systemd.service._inner(service=f'nebula-{hostname}', enabled=True, running=True, restarted=True, daemon_reload=True)
    elif not failover:
        if config_changed or (cert_updated and cert_reloadable):
            # Certificate or config changed, just reload service (=SIGHUP)
            yield from systemd.service._inner(service=f'nebula-{hostname}', enabled=True, running=True, reloaded=True)
        elif cert_updated and not cert_reloadable:
            # Certificate changed in a way that requires full service restart
            yield from systemd.service._inner(service=f'nebula-{hostname}', enabled=True, running=True, restarted=True)

    # If failover is enabled, deploy failoverd unit that launches Nebula on leader
    if failover:
        failoverd_unit = StringIO(_failoverd_unit(network, hostname, host.name, pod))
        failoverd_unit_path = f'/etc/systemd/system/nebula-{hostname}-failover.service'
        failoverd_unit_changed = host.get_fact(Sha1File, path=failoverd_unit_path) != files.get_file_sha1(failoverd_unit)
        if failoverd_unit_changed:
            yield FileUploadCommand(src=failoverd_unit, dest=failoverd_unit_path, remote_temp_filename=host.get_temp_filename(failoverd_unit_path))
        if failoverd_unit_changed or unit_changed or config_changed:
            yield from systemd.service._inner(service=f'nebula-{hostname}-failover.service', enabled=True, running=True, restarted=True, daemon_reload=True)



def _nebula_config(network: Network, hostname: str, ip: str, is_lighthouse: bool, underlay_port: int, firewall: Firewall,
                   ca_value: str, cert_value: str, key_value: str) -> dict:
    # Make sure the firewall permits essential things like our internal DNS!
    firewall = _patch_firewall(firewall)

    lighthouse_map = {}
    for lh in network.lighthouses:
        lighthouse_map[lh[0]] = lh[1]
    return {
        # Point to CA cert and host key material that should've been already uploaded
        'pki': {
            'ca': ca_value,
            'cert': cert_value,
            'key': key_value,
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
            'punch': True,
            'respond': True,
        },
        'tun': {
            'disabled': False, # Lighthouses need TUN for DNS
            'dev': f'nebula{hostname[:8]}', # Truncate to max device name length
        },
        # Convert our firewall rule definitions to Nebula format
        'firewall': {
            'inbound': list([item for rule in firewall.inbound for item in _convert_fw_rule(rule)]),
            'outbound': list([item for rule in firewall.outbound for item in _convert_fw_rule(rule)]),
        },
        'logging': {
            'level': 'info', # TODO debug logging support
        },
        '_ip': ip,
    }


def _patch_firewall(firewall: Firewall) -> Firewall:
    permit_dns = [FirewallRule(53, '_lighthouse')]
    return Firewall(
        inbound=firewall.inbound,
        outbound=firewall.outbound + permit_dns,
    )


def _convert_fw_rule(rule: FirewallRule):
    if rule.groups == 'any' or rule.groups == ['any']:
        return [{
            'port': rule.port,
            'host': 'any',
            'proto': rule.protocol,
        }]
    else:
        # With Nebula firewall, if a rule specifies multiple groups, ALL of them must be present for rule to match
        # In author's opinion, this is an annoying footgun, so we make one rule per group specified
        groups = [rule.groups] if isinstance(rule.groups, str) else rule.groups
        return [{
            'port': rule.port,
            'group': group,
            'proto': rule.protocol,
        } for group in groups]


def _nebula_unit(network: Network, hostname: str, config_path: str, target_pod: str = None, failover: bool = False):
    return f'''
[Unit]
Description=Nebula overlay - {hostname} ({network.name})
Wants=network-online.target
After=network-online.target
{f'Requires={target_pod}-pod.service\nAfter={target_pod}-pod.service' if target_pod else ''}
{f'Requires=nebula-{hostname}-failover.service' if failover else ''}

[Service]
ExecStartPre=/opt/containerops/nebula/nebula-netns -test -config {config_path}
ExecStart={f'/opt/containerops/nebula/container-nebula.sh {target_pod}-infra' if target_pod else '/opt/containerops/nebula/nebula-netns'} -config {config_path}
ExecReload=/bin/kill -HUP $MAINPID
Environment="NEBULA_NETNS_BINARY=/opt/containerops/nebula/nebula-netns"

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


def _failoverd_unit(network: Network, hostname: str, failover_key: str, target_pod: str = None):
    return f'''
[Unit]
Description=Nebula overlay failoverd - {hostname} ({network.name})
Wants=network-online.target
After=network-online.target
{f'Requires={target_pod}-pod.service\nAfter={target_pod}-pod.service' if target_pod else ''}

[Service]
ExecStart=/opt/containerops/failoverd \\
    -etcd-endpoints {','.join(network.failover_etcd)} -session-ttl 10 \\
    -election-prefix /failoverd/nebula/{network.name}/{hostname} -node-id {failover_key} \\
    -start-command "/bin/systemctl start nebula-{hostname}" -startup-delay 10s \\
    -stop-command "/bin/systemctl stop nebula-{hostname}"

User=root
Group=root

Restart=always
RestartSec=2
TimeoutStopSec=5
StartLimitInterval=0

Nice=-1

[Install]
WantedBy=multi-user.target
'''


def _pod_handler(network: Network, hostname: str, ip: str, groups: list[str], firewall: Firewall, failover: bool,
                 pod: str, present: bool):
    yield from endpoint._inner(network, hostname=hostname, ip=ip, groups=groups, firewall=firewall, failover=failover, pod=pod, present=present)


def pod_endpoint(network: Network, hostname: str, firewall: Firewall, ip: str = None, groups: list[str] = [], failover: bool = False):
    """
    Create an endpoint that can be used to attach a pod to a Nebula network.

    When added to a pod's networks list, this allows it to access the Nebula
    network according to the given firewall rules. Pod DNS is also configured
    to resolve names under network's DNS domain to other endpoints.

    Note that host the pod runs on must have nebula.host_setup() done!

    Arguments:
        network: Network configuration.
        hostname: Hostname of the endpoint.
            Lighthouses will answer for DNS queries about this name.
        firewall: Firewall configuration. Empty firewall means nothing is allowed!s
        ip: Endpoint IP address. Optional, but must be unique within network
            if present. When not given, a random address is assigned with IPAM.
        groups: Groups of the endpoint. Other endpoints can refer to them in
            their firewalls.
        failover: Enable failover for this endpoint. In failover mode,
            endpoints on different machines can share the same hostname, and
            etcd is used to make sure only one of them is active at a time.
            When the currently active endpoint goes down for any reason,
            another one will automatically take its place within a few dozen
            seconds.
    """
    return podman.Network(
        name=f'nebula:{hostname}',
        handler=_pod_handler,
        args={'network': network, 'hostname': hostname, 'ip': ip, 'groups': groups, 'firewall': firewall, 'failover': failover},
        dns_domain=network.dns_domain,
        dns_servers=[lh[0] for lh in network.lighthouses],
        # If failover is enabled, make pod depend on it instead of forcefully starting the Nebula service itself
        systemd_services=[f'nebula-{hostname}-failover' if failover else f'nebula-{hostname}']
    )


@operation()
def setup_host(install_tools: bool = False, failover_support: bool = True, selinux_label: bool = False):
    """
    Installs Nebula on current host, allowing it and pods running it to have
    endpoints.

    Arguments:
        install_tools: Install Nebula tools, e.g. nebula-cert. Not needed for
            endpoints themself, defaults to False.
        failover_support: Enable failover endpoint support for this host.
            Defaults to True, but can be safely disabled if you do not
            intend to use Nebula failover.
        selinux_label: Adjust Selinux labels of executable files automatically.
            Defaults to False, enable if your host uses Selinux.
    """

    yield from server.user._inner(user='nebula', system=True, create_home=False)
    yield from files.directory._inner(path='/opt/containerops/nebula')

    # If desired, install vanilla Nebula for nebula-cert
    if install_tools and host.get_fact(Sha256File, path='/opt/containerops/nebula.tar.gz') != NEBULA_HASH:
        yield from files.download._inner(src=NEBULA_DOWNLOAD, dest='/opt/containerops/nebula.tar.gz', sha256sum=NEBULA_HASH)
        yield StringCommand('tar xzf /opt/containerops/nebula.tar.gz -C /opt/containerops/nebula')

    # Install nebula-netns for container networking support
    yield from files.download._inner(src=NEBULA_NETNS_DOWNLOAD, sha256sum=NEBULA_NETNS_HASH, dest='/opt/containerops/nebula/nebula-netns', mode='755')
    if selinux_label:
        yield from selinux.file_context._inner(path='/opt/containerops/nebula/nebula-netns', se_type='bin_t')
    yield from files.download._inner(src=CONTAINER_NEBULA_DOWNLOAD, sha256sum=CONTAINER_NEBULA_HASH, dest='/opt/containerops/nebula/container-nebula.sh', mode='755')
    if selinux_label:
        yield from selinux.file_context._inner(path='/opt/containerops/nebula/container-nebula.sh', se_type='bin_t')

    # If failover is used, install failoverd
    if failover_support:
        yield from files.download._inner(src=FAILOVERD_DOWNLOAD, sha256sum=FAILOVERD_HASH, dest='/opt/containerops/failoverd', mode='755')
        if selinux_label:
            yield from selinux.file_context._inner(path='/opt/containerops/failoverd', se_type='bin_t')
