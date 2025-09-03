from dataclasses import dataclass, field
import textwrap
from pyinfra.api import operation

from containerops import podman


@dataclass
class Zone:
    """
    Zone (i.e. DNS records for a domain) for Knot authoritative DNS server.

    Arguments:
        domain: Domain name.
        records: List of DNS records.
        default_ttl: Default TTL in seconds for records in this zone.
        acme_config: Configuration for ACME DNS-01 challenges via RFC 2136
            dynamic zone updates. Optional, if absent, no dynamic zone updates
            will be supported.
        transfer_to: List of secondaries to transfer this zone to.
        transfer_from: Primary that this zone on this server accepts zone
            transfers from.
    """
    domain: str
    records: list['Record'] = field(repr=False)

    default_ttl: int = field(default=300)
    acme_config: 'AcmeConfig' = field(default=None)

    transfer_to: list['Remote'] = field(default_factory=list)
    transfer_from: 'Remote' = field(default=None)


@dataclass(eq=True, frozen=True)
class Record:
    name: str
    type: str
    content: str

    def __str__(self):
        # Automatically split long TXT records
        if self.type == 'TXT' and len(self.content) > 252:
            text = self.content[1:len(self.content)-1]
            content = ' '.join([f'"{part}"' for part in textwrap.wrap(text, 255)])
        else:
            content = self.content
        return f'{self.name} {self.type} {content}'

    def __lt__(self, other: 'Record'):
        return str(self) < str(other)


@dataclass
class AcmeConfig:
    """
    RFC 2136-based ACME DNS-01 challenge configuration.

    Arguments:
        allowed_ip_ranges: List of IP ranges allowed for updates.
            This should preferably be limited to your internal network.
        tsig_key: TSIG key for ACME DNS-01 challenge updates.
            Must be for hmac-sha256 format.
    """

    allowed_ip_ranges: list[str]
    tsig_key: str = field(repr=False)


@dataclass
class Remote:
    """
    Remote for DNS zone transfers.

    Arguments:
        name: Unique name of remote, used within Knot configuration.
        address: Address of remote. If transfering between Knot instances,
            note that they run on port 5300 in internal network. Remember
            to specify this (e.g. '1.2.3.4@5300') and permit traffic in
            endpoint firewalls!
    """
    name: str
    address: str


@operation()
def install(svc_name: str, zones: list[Zone], present: bool = True,
            image: str = 'docker.io/cznic/knot:v3.4.6',
            host_port: str = '53', networks: list[podman.Network] = []):
    """
    Installs Knot authorative DNS server and configures it to serve the given zones.

    Arguments:
        svc_name: Name of this service. Must be unique if you're running
            multiple instances of Knot on same machine.
        zones: List of zones to serve.
        present: By default, Knot is installed. If False, it is removed instead.
        image: Docker image to use for Knot, in case you want to override that.
        host_bind: Port to expose the DNS server on host. Optionally, you can
            bind only to some interface by specifying both IP address and port:
            '1.2.3.4:53'. This may be useful for authorative DNS server hosts
            that also have a local DNS resolver running at loopback 53.
        networks: List of additional networks to attach to the pod running the
            DNS server. This can be useful if you want to e.g. ACME updates
            over a private network.
    """
    main_config = f"""server:
    listen: 0.0.0.0@5300
    listen: ::@5300
    automatic-acl: on

log:
  - target: stdout
    any: debug
"""
    
    # List remotes we'll refer to later
    remotes = []
    for zone in zones:
        if zone.transfer_from:
            remotes.append(zone.transfer_from)
        else:
            remotes += zone.transfer_to
    main_config += 'remote:\n'
    for remote in remotes:
        main_config += f'''  - id: {remote.name}
    address: {remote.address}
'''
    
    # Create keys for zones that allow dynamic updates
    main_config += 'key:\n'
    for zone in zones:
        if zone.acme_config:
            main_config += f'''  - id: {zone.domain}-acme-key
    algorithm: hmac-sha256
    secret: {zone.acme_config.tsig_key}
'''
    # Create ACLs for zones that allow dynamic updates
    main_config += 'acl:\n'
    for zone in zones:
        if zone.acme_config:
            main_config += f"""  - id: {zone.domain}-acme-acl
    address: [{','.join(zone.acme_config.allowed_ip_ranges)}]
    key: {zone.domain}-acme-key
    action: update
    update-type: [TXT]
"""
    
    # Declare the zones in main config
    main_config += 'zone:\n'
    for zone in zones:
        main_config += f"""  - domain: {zone.domain}.
    zonefile-sync: -1
    file: /config/{zone.domain}.zone
    {f'acl: {zone.domain}-acme-acl' if zone.acme_config else ''}
    {f'master: {zone.transfer_from.name}' if zone.transfer_from else ''}
    {f'notify: [{','.join([remote.name for remote in zone.transfer_to])}]' if len(zone.transfer_to) > 0 else ''}
"""

    # Create volumes for main config file and the individual zone files
    volumes = [
        (podman.ConfigFile(id=f'knot-{svc_name}-config', data=main_config), '/config/knot.conf')
    ]
    for zone in zones:
        content = _format_zone_file(zone)
        volumes.append((podman.ConfigFile(id=f'knot-{svc_name}-zone-{zone.domain}', data=content), f'/config/{zone.domain}.zone'))

    # Create the pod
    yield from podman.pod._inner(
        pod_name=f'knot-dns-{svc_name}',
        containers=[
            podman.Container(
                name='main',
                image=image,
                command='knotd',
                volumes=volumes,
            )
        ],
        ports=[
            (host_port, '5300', 'tcp'),
            (host_port, '5300', 'udp')
        ],
        networks=[podman.HOST_NAT, *networks],
        present=present
    )


def _format_zone_file(zone: Zone) -> str:
    zone_file = ''

    # Origin and default TTL
    zone_file += f'$ORIGIN {zone.domain}.\n'
    zone_file += f'$TTL {zone.default_ttl}\n'

    record_list = list(zone.records)
    record_list.sort()
    soa_found = False
    for record in record_list:
        if record.type == 'SOA':
            soa_found = True
        zone_file += str(record) + '\n'
    if not soa_found:
        raise ValueError(f'No SOA record found in zone {zone.domain}')
    return zone_file
