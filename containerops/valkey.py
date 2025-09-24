from dataclasses import dataclass, field
from pyinfra.api import operation
from pyinfra.operations import files

from containerops import nebula, podman


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
    main_config = _valkey_config(rdb_config, use_aof, custom_config, hostname, sentinel_config is not None, sentinel_config.master_hostname if sentinel_config else None)
    containers = [podman.Container(
        name='valkey',
        image=image,
        command=f'sh -c "cp /usr/local/etc/valkey/valkey-readonly.conf /usr/local/etc/valkey/valkey.conf && exec valkey-server /usr/local/etc/valkey/valkey.conf"',
        volumes=[
            # Ask Podman to fix Selinux labels for us for the host directory
            (f'/var/containerops/data/valkey/{pod_name}', '/data:Z'),
            (podman.ConfigFile(id=f'{pod_name}-valkey-config', data=main_config), '/usr/local/etc/valkey/valkey-readonly.conf'),
        ]
    )]
    if sentinel_config is not None:
        containers.append(podman.Container(
            name='sentinel',
            image=image,
            command='sh -c "cp /usr/local/etc/valkey/sentinel-readonly.conf /usr/local/etc/valkey/sentinel.conf && exec valkey-sentinel /usr/local/etc/valkey/sentinel.conf"',
            # FIXME since sentinel edits config files, we'll trigger restart every time
            # This shouldn't normally cause Valkey outage, but with enough bad luck, that can happen!
            volumes=[(podman.ConfigFile(
                id=f'{pod_name}-sentinel-config',
                data=_sentinel_config(hostname, sentinel_config),
            ), '/usr/local/etc/valkey/sentinel-readonly.conf')]
        ))

    internal_group = f'valkey-internal-{sentinel_config.cluster_id}' if sentinel_config else None
    endpoint = nebula.pod_endpoint(
        network=network,
        hostname=hostname,
        firewall=_firewall(internal_group, client_groups),
        groups=[internal_group] if internal_group else [],
    )
    yield from files.directory._inner(path=f'/var/containerops/data/valkey/{pod_name}')
    yield from podman.pod._inner(
        pod_name=pod_name,
        containers=containers,
        networks=[endpoint],
        present=present
    )


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


def _valkey_config(rdb_config: str, use_aof: bool, custom_config: str, hostname: str, sentinel_enabled: bool, master_hostname: str):
    config = ''
    if rdb_config == '':
        config += 'save ""\n'
    else:
        config += f'save {rdb_config}\n'
    if use_aof:
        config += 'appendonly yes\n'
    if sentinel_enabled:
        config += f'replica-announce-ip {hostname}\n'
        if hostname != master_hostname:
            config += f'replicaof {master_hostname} 6379\n'
    config += custom_config
    return config
    

def _sentinel_config(hostname: str, config: SentinelConfig):
    return f"""sentinel monitor mymaster {config.master_hostname} 6379 {config.quorum}
sentinel down-after-milliseconds mymaster {config.down_after_ms}
sentinel failover-timeout mymaster {config.failover_timeout_ms}
sentinel parallel-syncs mymaster {config.parallel_syncs}

sentinel announce-ip {hostname}
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
{config.custom_config}

# PRE-GENERATED END
"""