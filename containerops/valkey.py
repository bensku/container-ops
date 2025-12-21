from dataclasses import dataclass, field
from io import StringIO
from pyinfra import host
from pyinfra.api import operation
from pyinfra.operations import files, systemd
from pyinfra.facts.files import Sha1File

from containerops import nebula, podman, _ipam as ipam


@dataclass
class SentinelConfig:
    cluster_id: str

    master_hostname: str
    quorum: int
    down_after_ms: int = field(default=5_000)
    failover_timeout_ms: int = field(default=180_000)
    parallel_syncs: int = field(default=1)

    custom_config: str = field(default='')


@operation()
def node(pod_name: str, hostname: str,
         network: nebula.Network, client_groups: list[str],
         rdb_config: str = '', use_aof: bool = True,
         sentinel_config: SentinelConfig = None,
         custom_config: str = '',
         image: str = 'ghcr.io/valkey-io/valkey:8.1-alpine3.21',
         present: bool = True):
    """
    Creates a containerized Valkey node that is reachable over Nebula overlay.
    Optionally, the node can be a part of a group of Valkey sentinels,
    providing high availability.

    This is a rather opinioned setup. If you wish to use a different networking
    configuration, it is best to deploy Valkey on your own with podman module.

    Arguments:
        pod_name: Valkey pod name. Must be unique within all Podman pod names
            within the same machine.
        hostname: Unique hostname of this node.
        network: Nebula network to connect to.
        client_groups: List of firewall groups to allow clients connect from.
        rdb_config: Valkey RDB configuration, as it would appear in valkey.conf.
            Optional, by default RDB saving is disabled.
        use_aof: Whether to use AOF saving or not. Enabled by default.
        sentinel_config: Sentinel configuration. Optional, by default this
            node is standalone and no sentinel will be run.
        custom_config: Custom config to append valkey.conf.
        image: Container image for Valkey.
        present: By default, the node is created or modified. If set to False,
            it is destroyed instead. Data stored in RDB/AOF files is NOT deleted
            automatically.
    """
    config_dir = f'/etc/containerops/configs/{pod_name}-valkey'
    yield from files.directory._inner(config_dir)

    # Check server's read-only config copy against local config for changes
    main_config = _valkey_config(network, rdb_config, use_aof, custom_config, hostname, sentinel_config is not None, sentinel_config.master_hostname if sentinel_config else None)
    main_config_file = f'{config_dir}/valkey.conf'
    restart_pod = False
    if files.get_file_sha1(StringIO(main_config)) != host.get_fact(Sha1File, path=f'{main_config_file}-readonly'):
        # Configuration updated, update also the read-write config (overwriting changes made by Valkey)
        yield from files.put._inner(src=StringIO(main_config), dest=f'{main_config_file}-readonly')
        yield from files.put._inner(src=StringIO(main_config), dest=main_config_file)
        restart_pod = True
    containers = [podman.Container(
        name='valkey',
        image=image,
        command='valkey-server /usr/local/etc/valkey/valkey.conf',
        volumes=[
            # Ask Podman to fix Selinux labels for us for the host directory
            (f'/var/containerops/data/valkey/{pod_name}', '/data:Z'),
            (config_dir, '/usr/local/etc/valkey:Z')
        ]
    )]

    if sentinel_config is not None:
        # Same update handling as above for sentinel config
        sentinel_config_content = _sentinel_config(network, hostname, sentinel_config)
        sentinel_config_file = f'{config_dir}/sentinel.conf'
        if files.get_file_sha1(StringIO(sentinel_config_content)) != host.get_fact(Sha1File, path=f'{sentinel_config_file}-readonly'):
            yield from files.put._inner(src=StringIO(sentinel_config_content), dest=f'{sentinel_config_file}-readonly')
            yield from files.put._inner(src=StringIO(sentinel_config_content), dest=sentinel_config_file)
            restart_pod = True

        containers.append(podman.Container(
            name='sentinel',
            image=image,
            command='valkey-sentinel /usr/local/etc/valkey/sentinel.conf',
            volumes=[(config_dir, '/usr/local/etc/valkey:Z')]
        ))

    internal_group = f'valkey-internal-{sentinel_config.cluster_id}' if sentinel_config else None
    endpoint = nebula.pod_endpoint(
        network=network,
        hostname=hostname,
        firewall=_firewall(internal_group, client_groups),
        groups=[internal_group] if internal_group else [],
    )
    yield from files.directory._inner(path=f'/var/containerops/data/valkey/{pod_name}')

    if restart_pod:
        yield from systemd.service._inner(service=f'{pod_name}-pod', running=False)
    yield from podman.pod._inner(
        pod_name=pod_name,
        containers=containers,
        networks=[endpoint],
        present=present
    )

    if restart_pod:
        yield from systemd.service._inner(service=f'{pod_name}-pod', running=True, restarted=True)


def _firewall(internal_group: str, allow_groups: list[str]) -> nebula.Firewall:
    """
    Creates a firewall that can be attached to Valkey nodes to permit clients
    connect to them. When sentinels is used, the firewall also permits them to
    talk to each other.

    Arguments:
        internal_group: Group that Valkey nodes have. None if not using sentinel.
        allow_groups: Clients with these groups can connect to Valkey nodes.
    """
    all_groups = allow_groups.copy()
    if internal_group:
        all_groups.append(internal_group)
    return nebula.Firewall(
        inbound=[
            nebula.FirewallRule(port=6379, groups=all_groups),
            nebula.FirewallRule(port=26379, groups=all_groups),
        ],
        outbound=[
            nebula.FirewallRule(port=6379, groups=[internal_group]),
            nebula.FirewallRule(port=26379, groups=[internal_group]),
        ] if internal_group else []
    )


def _valkey_config(network: nebula.Network, rdb_config: str, use_aof: bool, custom_config: str, hostname: str, sentinel_enabled: bool, master_hostname: str):
    config = ''
    if rdb_config == '':
        config += 'save ""\n'
    else:
        config += f'save {rdb_config}\n'
    if use_aof:
        config += 'appendonly yes\n'
    if sentinel_enabled:
        ip = ipam.allocate_ip(
            network_name=network.name,
            hostname=hostname,
            cidr=network.cidr,
            base_dir=f'{network.state_dir}/networks',
        )
        config += f'replica-announce-ip {ip}\n'
        if hostname != master_hostname:
            master_ip = ipam.allocate_ip(
                network_name=network.name,
                hostname=master_hostname,
                cidr=network.cidr,
                base_dir=f'{network.state_dir}/networks',
            )
            config += f'replicaof {master_ip} 6379\n'
    config += custom_config
    return config
    

def _sentinel_config(network: nebula.Network, hostname: str, config: SentinelConfig):
    ip = ipam.allocate_ip(
        network_name=network.name,
        hostname=hostname,
        cidr=network.cidr,
        base_dir=f'{network.state_dir}/networks',
    )
    master_ip = ipam.allocate_ip(
        network_name=network.name,
        hostname=config.master_hostname,
        cidr=network.cidr,
        base_dir=f'{network.state_dir}/networks',
)
    return f"""sentinel monitor mymaster {master_ip} 6379 {config.quorum}
sentinel down-after-milliseconds mymaster {config.down_after_ms}
sentinel failover-timeout mymaster {config.failover_timeout_ms}
sentinel parallel-syncs mymaster {config.parallel_syncs}

sentinel announce-ip {ip}
sentinel resolve-hostnames no
sentinel announce-hostnames no
{config.custom_config}

# PRE-GENERATED END
"""