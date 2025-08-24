from dataclasses import dataclass, field
from io import StringIO
import json
from pyinfra.api import operation
from pyinfra.operations import files, server

from containerops import podman, nebula, timer


@dataclass
class PostgresConfig:
    data_checksums: bool = field(default=True)


@dataclass
class ClusterConfig:
    """
    Patroni cluster configuration.

    Arguments:
        cluster_id: Unique identifier for the cluster. DO NOT REUSE!
            Cluster id is stored in etcd, from where you need to clear it
            manually even if you destroy this cluster with present=False.
        members: List of member nodes' hostnames. You must deploy a node for
            each member listed here with the exact same hostname, otherwise
            access via proxies will not work.
        network: Nebula network to connect members to.
        etcd_endpoints: List of etcd endpoints for Patroni to use.
            They must all belong to a single etcd cluster
        etcd_client_group: Firewall group that the etcd nodes allow connections
            from.
        read_replicas: Members that will never become the primaries or
            accept writes. Do not add these members to the main members list!
        backup_replicas: If any of the members (including read_replicas) are
            listed here, read-only proxy traffic will only be routed to them
            if no other members are available. This does not apply to
            read-write (i.e. the default) connections, as they must always
            be routed to cluster primary.
        client_groups: List of firewall groups this cluster allows client
            connections from. This applies both to instances directly and
            proxies.
        patroni_api_groups: List of firewall groups that are allowed to
            access Patroni REST API. The API does not require authentication
            beyond this, so be VERY careful with this kind of access!
        postgres_config: Configuration for PostgreSQL itself.
            Currently very limited in scope, and the changes cannot be applied
            to an already existing cluster. If you need more configuration
            options, use Patroni's tooling.
        barman_backup_support: Enables support for containerops-managed Barman
            backups. If you enable this, you MUST also deploy Barman and
            schedule it to run regularly. Otherwise, the PostgreSQL cluster will
            continue to accumulate old WAL files, eventually filling the disk.
        restore_from_backup: Absolute path of physical PostgreSQL backup to restore the
            newly created cluster from. The backup must exist on at least one of
            hosts that have members in the cluster.
            
            IMPORTANT NOTES
            - Restoring to an existing Patroni cluster is not supported.
            - Remember: Use a new cluster_id, unless you have manually cleared
              the old one from etcd.
            - This is for restoring physical backups only (pg_basebackup, Barman, etc.)
              To restore a logical backup (pg_dump, pg_dumpall), create the cluster
              normally and then execute the dump's SQL against it.
            
            Your backup will be automatically chowned to postgres user of Patroni image.
    """
    cluster_id: str
    members: list[str]

    network: nebula.Network
    etcd_endpoints: list[str]
    etcd_client_group: str

    read_replicas: list[str] = field(default_factory=list)
    backup_replicas: list[str] = field(default_factory=list)

    client_groups: list[str] = field(default_factory=list)
    patroni_api_groups: list[str] = field(default_factory=list)

    postgres_config: PostgresConfig = field(default_factory=PostgresConfig)

    barman_backup_support: bool = field(default=False)
    restore_from_backup: str = field(default=None)


@operation()
def instance(cluster: ClusterConfig, hostname: str,
             superuser_secret: str, replication_secret: str, rewind_secret: str,
             image: str = 'ghcr.io/bensku/containerops-builds/patroni:4.0.6-postgres17',
             alias_patronictl: bool = True,
             present: bool = True):
    """
    Creates a Patroni-managed PostgreSQL instance in a container.
    You should preferably do this on at least two machines for high availability.
    
    The created cluster will have superuser named "superuser", available with
    the password in secret given to this operation. ALL secrets must have same
    passwords across the entire cluster.

    Access to PostgreSQL MUST be done through a proxy for failover support.
    Consider deploying a proxy on each machine that needs access to databases,
    and have applications refer to their local proxy. That way, if a machine
    goes down, it will not impact database access of other machines.

    Arguments:
        cluster: Cluster configuration. Must be same for all nodes.
        hostname: Unique hostname of this node.
        superuser_secret: Name of the Podman secret containing the superuser password.
        replication_secret: Name of the secret containing the replication password.
        rewind_secret: Name of the secret containing the rewind password.
        image: Container image for Patroni, if you want to override that.
        alias_patronictl: By default, a script that calls patronictl inside the cluster's
            container is created as /usr/local/bin/patronictl. Set this to False to disable.
        present: By default, the node is created or modified. If set to False,
            it is destroyed instead. Database data is NOT deleted from disk
            automatically.
    """
    if not hostname in cluster.members and not hostname in cluster.read_replicas:
        raise ValueError(f'{hostname} is not a member of Patroni cluster {cluster.cluster_id}')

    internal_group = f'patroni-internal-{cluster.cluster_id}'
    endpoint = nebula.pod_endpoint(
        network=cluster.network,
        hostname=hostname,
        firewall=nebula.Firewall(
            inbound=[
                nebula.FirewallRule(5432, internal_group), # PostgreSQL, cluster internal
                nebula.FirewallRule(5432, cluster.client_groups), # PostgreSQL, external clients
                nebula.FirewallRule(8008, internal_group), # Patroni REST API
            ],
            outbound=[
                nebula.FirewallRule(5432, internal_group), # PostgreSQL
                nebula.FirewallRule(2379, 'etcd') # Allow connecting to any etcd cluster for now
            ]
        ),
        groups=[internal_group, cluster.etcd_client_group],
    )

    yield from files.directory._inner(path=f'/var/containerops/data/patroni/{cluster.cluster_id}')
    config = _patroni_config(
        cluster_id=cluster.cluster_id,
        hostname=hostname,
        etcd_addrs=cluster.etcd_endpoints,
        readonly_replica=hostname in cluster.read_replicas,
        data_checksums=cluster.postgres_config.data_checksums,
        barman_support=cluster.barman_backup_support,
        restore_backup=cluster.restore_from_backup is not None
    )
    volumes = [
        (f'/var/containerops/data/patroni/{cluster.cluster_id}', '/data:Z'),
        (podman.ConfigFile(id=f'patroni-{cluster.cluster_id}-config', data=json.dumps(config, indent=2, sort_keys=True)), '/etc/patroni.yml')
    ]
    if cluster.restore_from_backup:
        # If we're restoring from backup, mount the backup inside container
        # The container's startup script will chown it to the postgres 
        # Backup does not need to exist on all nodes, but make sure directory exists!
        yield from files.directory._inner(path=cluster.restore_from_backup)
        volumes.append((cluster.restore_from_backup, '/incoming_restore:Z'))

    yield from podman.pod._inner(
        pod_name=f'patroni-postgres-{cluster.cluster_id}',
        containers=[
            podman.Container(
                name='main',
                image=image,
                volumes=volumes,
                secrets=[
                    # Use environment variable configuration for secrets
                    ('PATRONI_SUPERUSER_PASSWORD', superuser_secret),
                    ('PATRONI_REPLICATION_PASSWORD', replication_secret),
                    ('PATRONI_REWIND_PASSWORD', rewind_secret),
                ]
            )
        ],
        networks=[endpoint],
        present=present
    )

    if alias_patronictl:
        yield from files.put._inner(src=StringIO(PATRONICTL_SCRIPT), dest='/usr/local/bin/patronictl', mode='755')


PATRONICTL_SCRIPT = """#!/bin/sh
set -eu

cluster_id=$1
shift
podman exec -it patroni-postgres-$cluster_id-main patronictl --config-file /etc/patroni.yml $@
"""


@operation()
def proxy(cluster: ClusterConfig, hostname: str,
          image: str = 'docker.io/haproxy:3.2-alpine',
          present: bool = True):
    """
    Creates a proxy for accessing Patroni-managed PostgreSQL instances
    with failover support. It is recommended that you deploy one proxy per
    cluster on each server that needs access to the cluster. The proxies should
    be accessed locally, even when using Nebula networking; otherwise, they
    provide little fault tolerance.

    Each proxy listens to PostgreSQL connections on:
    - Port 5432: Read-write connections, router to cluster primary.
    - Port 5433: Read-only connections, routed to read replicas. This requires
      the cluster to have at least one read replica, which is not the default!
    - Port 5434: Read-only connections, routed to all nodes in cluster.
      You are responsible for ensuring that your application really doesn't
      perform writes; sometimes, the connection might also be routed to primary.

    Arguments:
        cluster: Cluster configuration. Must be same for all nodes.
        hostname: Unique hostname of this node. Note, DO NOT reuse a hostname
            from a Patroni node.
        image: Container image for HAProxy, if you want to override that.
        present: By default, the proxy is created or reconfigured. If set to False,
            it is destroyed instead.
    """
    if hostname in cluster.members:
        raise ValueError(f'hostname {hostname} is already in use by Patroni in cluster {cluster.cluster_id}')

    internal_group = f'patroni-internal-{cluster.cluster_id}'
    endpoint = nebula.pod_endpoint(
        network=cluster.network,
        hostname=hostname,
        firewall=nebula.Firewall(
            inbound=[
                nebula.FirewallRule(5432, cluster.client_groups), # Proxied PostgreSQL
                nebula.FirewallRule(5433, cluster.client_groups), # Proxied PostgreSQL read replicas
                nebula.FirewallRule(5434, cluster.client_groups), # Proxied PostgreSQL all nodes
                nebula.FirewallRule(5432, 'barman-internal'),
                nebula.FirewallRule(7000, cluster.client_groups), # HAProxy stats server
            ],
            outbound=[
                nebula.FirewallRule(5432, internal_group), # PostgreSQL
                nebula.FirewallRule(8008, internal_group) # Patroni REST API
            ]
        ),
        groups=[internal_group, 'pgproxy-all', f'pgproxy-{cluster.cluster_id}'],
    )

    config = _haproxy_config(cluster.members, cluster.read_replicas, cluster.backup_replicas)
    yield from podman.pod._inner(
        pod_name=f'patroni-proxy-{cluster.cluster_id}',
        containers=[
            podman.Container(
                name='main',
                image=image,
                volumes=[
                    (podman.ConfigFile(id=f'patroni-proxy-{cluster.cluster_id}-config', data=config), '/usr/local/etc/haproxy/haproxy.cfg')
                ],
                reload_signal='SIGHUP'
            )
        ],
        networks=[endpoint],
        present=present
    )


def _patroni_config(cluster_id: str, hostname: str, etcd_addrs: list[str],
                    readonly_replica: bool, data_checksums: bool, barman_support: bool,
                    restore_backup: bool):
    # If this should be read-only replica, prevent it from becoming primary
    tags = {}
    if readonly_replica:
        tags['nofailover'] = True

    # If Barman backups are used, make Patroni manage a physical replication slot for it
    # This way, Patroni replicates the slot from primary to replicas, which ensures
    # backups don't get corrupted during failover
    slots = {}
    if barman_support:
        slots['barman'] = { 'type': 'physical' }

    # If we're bootstrapping a new cluster from physical backup, try to copy local incoming_restore on each node
    # It will eventually succeed on the node it exists on
    bootstrap_method = 'restorebackup' if restore_backup else 'initdb'
    restore_config = None
    if bootstrap_method == 'restorebackup':
        restore_config = {
            'command': 'cp -R /incoming_restore /data/postgres',
            'no_params': True,
            'keep_existing_recovery_conf': True
        }

    return {
        'namespace': '/containerops/patroni/',
        'scope': cluster_id,
        'name': hostname,
        'restapi': {
            'listen': '0.0.0.0:8008',
            'connect_address': f'{hostname}:8008',
        },
        'etcd3': {
            'hosts': etcd_addrs
        },
        'bootstrap': {
            'dcs': {
                'postgresql': {
                    'use_pg_rewind': True,
                    'pg_hba': [
                        'host replication replicator 0.0.0.0/0 md5',
                        'host all all 0.0.0.0/0 md5',
                    ]
                },
                'slots': slots
            },
            'initdb': [
                { 'encoding': 'utf-8' },
                *(['data-checksums'] if data_checksums else [])
            ],
            'method': bootstrap_method,
            'restorebackup': restore_config
        },
        'postgresql': {
            'listen': '0.0.0.0:5432',
            'connect_address': f'{hostname}:5432',
            'data_dir': '/data/postgres',
            'pgpass': '/data/pgpass0',
            'authentication': {
                'superuser': {
                    'username': 'superuser',
                },
                'replication': {
                    'username': 'replicator',
                },
                'rewind': {
                    'username': 'rewind_user',
                }
            },
            'callbacks': {},
        },
        'tags': tags
    }


def _haproxy_config(hostnames: list[str], readonly_hostnames: list[str], backup_hostnames: list[str]):
    backups = set(backup_hostnames)
    rw_lines = [f'    server {hostname} {hostname}:5432 resolvers poddns maxconn 100 check port 8008 init-addr none' for hostname in hostnames]
    ro_lines = [f'    server {hostname} {hostname}:5432 resolvers poddns maxconn 100 check port 8008 init-addr none{' backup' if hostname in backups else ''}' for hostname in readonly_hostnames]
    return f"""global
    maxconn 100

defaults
    log global
    mode tcp
    retries 2
    timeout client 30m
    timeout connect 4s
    timeout server 30m
    timeout check 5s

resolvers poddns
    parse-resolv-conf
    hold valid 10s

listen stats
    mode http
    bind *:7000
    stats enable
    stats uri /

listen postgres-primary
    bind *:5432
    option httpchk OPTIONS /primary
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
{'\n'.join(rw_lines)}

listen postgres-replica
    bind *:5433
    option httpchk OPTIONS /replica
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
{'\n'.join(ro_lines)}

listen postgres-read-only
    bind *:5434
    option httpchk OPTIONS /read-only
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
{'\n'.join(rw_lines)}
{'\n'.join(ro_lines)}
"""


@dataclass
class BackupSource:
    """
    Backup source for Barman.

    Arguments:
        cluster: Patroni cluster configuration.
        pgproxy_hostname: Hostname of one of pgproxy instances of the cluster.
            Preferably, you should deploy one in the machine where Barman will run.
        superuser_secret: Name of the Podman secret containing the superuser password.
        replication_secret: Name of the secret containing the replication password.
        frequency: Automatic full backup frequency in cron format.
        minimum_redundancy: Minimum number of backups to keep. This can avoid
            accidental deletion of all backups. Defaults to 1.
        recovery_window: Recovery window in days. Barman will keep backups this long
            and then automatically delete them. Defaults to 30 days.
    """
    cluster: ClusterConfig
    pgproxy_hostname: str
    superuser_secret: str
    replication_secret: str

    frequency: str = field(default='30 3 * * *') # Every day at 03:30
    minimum_redundancy: int = field(default=1)
    recovery_window: int = field(default=30)


@operation()
def barman_backups(sources: list[BackupSource], hostname: str,
                   image: str = 'ghcr.io/bensku/containerops-builds/barman:latest',
                   present: bool = True):
    """
    Sets up Barman backup server on the current host, backing up one or more
    Patroni clusters.

    TEST YOUR BACKUPS! container-ops can and will have bugs. It is not enough
    that Barman places some files on disk; how do you know Postgres can actually
    restore them without testing?

    Arguments:
        sources: Backup sources.
        hostname: Unique hostname of this Barman server. Nothing can connect to it,
            but it needs this to connect to Postgres servers over overlay network.
        image: Container image for Barman, if you want to override that.
        present: By default, the Barman server is created or modified. If set to False,
            it is destroyed instead.
    """
    config_mounts = []
    secrets = []
    for source in sources:
        source_id = source.cluster.cluster_id
        config = _barman_config(source)
        config_mounts.append((podman.ConfigFile(id=f'barman-source-{source_id}', data=config), f'/etc/barman-sources/{source_id}.conf'))

        # Environment variables with
        secrets += [
            (f'REPLICATION_PASSWORD_{source_id}', source.replication_secret),
            (f'SUPERUSER_PASSWORD_{source_id}', source.superuser_secret)
        ]

    crontab = _barman_crontab(sources)
    config_mounts.append((podman.ConfigFile(id='barman-crontab', data=crontab), '/etc/cron.d/barman-backups'))

    endpoint = nebula.pod_endpoint(
        network=sources[0].cluster.network, # TODO what if clusters are in different networks?
        hostname=hostname,
        firewall=nebula.Firewall(
            inbound=[],
            outbound=[
                nebula.FirewallRule(5432, 'pgproxy-all'), # PostgreSQL
            ]
        ),
        groups=['barman-internal'],
    )

    yield from files.directory._inner(path=f'/var/containerops/data/barman')
    yield from files.directory._inner(path=f'/var/containerops/data/barman_restored')
    yield from podman.pod._inner(
        pod_name=f'barman',
        containers=[
            podman.Container(
                name='main',
                image=image,
                volumes=[
                    ('/var/containerops/data/barman', '/var/lib/barman:Z'),
                    ('/var/containerops/data/barman_restored', '/var/barman_restored:Z'),
                    *config_mounts
                ],
                secrets=secrets,
            )
        ],
        networks=[endpoint],
        present=present
    )


@operation()
def backup_now(cluster: ClusterConfig):
    """
    Takes immediate backup with current host's Barman.

    Arguments:
        cluster: Cluster to back up.
    """
    yield from server.shell._inner(f'podman exec barman-main barman backup -q "{cluster.cluster_id}"')


@operation()
def restore_backup(cluster_id: str, restore_name: str, backup_id: str = 'auto', target_time: str = None):
    """
    Restores a backup to `/var/containerops/data/barman_restored/<restore_name>` from current host's Barman.
    From there, you can copy it to wherever you want to restore it.

    To view a list of available backups, use:
    `podman exec barman-main barman list-backups <cluster_id>`

    Arguments:
        cluster: Cluster to restore backup for.
        restore_name: Name for this restored backup. This affects only the storage
            location, which is `/var/containerops/data/barman_restored/<restore_name>`.
        backup_id: ID of the backup to restore. Defaults to `auto`, in which case
            Barman will automatically pick the backup based on target_time.
        target_time: If given, point-in-time recovery is done to this exact time,
            even if it is between two backups.
            The time must be in unambiguous format, such as what `list-backups` shows.
    """
    target_time_arg = f'--target-time "{target_time}"' if target_time else ''
    # Note: path inside container is different from path on host
    yield from server.shell._inner(f'podman exec barman-main barman restore {cluster_id} {backup_id} /var/barman_restored/{restore_name} {target_time_arg}')


def _barman_config(source: BackupSource):
    return f"""[{source.cluster.cluster_id}]
description = "Streaming replication backup for cluster {source.cluster.cluster_id}"
streaming_archiver = on
backup_method = postgres
streaming_conninfo = host={source.pgproxy_hostname} user=replicator dbname=postgres password=$REPLICATION_PASSWORD_{source.cluster.cluster_id}
slot_name = barman
create_slot = manual
conninfo = host={source.pgproxy_hostname} user=superuser dbname=postgres password=$SUPERUSER_PASSWORD_{source.cluster.cluster_id}

minimum_redundancy = {source.minimum_redundancy}
retention_policy = RECOVERY WINDOW OF {source.recovery_window} DAYS
"""


def _barman_crontab(sources: list[BackupSource]):
    lines = []
    for source in sources:
        lines.append(f'{source.frequency} barman /usr/bin/barman -q backup "{source.cluster.cluster_id}"')
    return '\n'.join(lines) + '\n'