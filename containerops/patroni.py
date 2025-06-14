from dataclasses import dataclass
import json
from pyinfra.api import operation
from pyinfra.operations import files

from containerops import podman, nebula


@dataclass
class ClusterConfig:
    """
    Patroni cluster configuration.

    Arguments:
        cluster_id: Unique identifier for the cluster.
        members: List of member nodes' hostnames. You must deploy a node for
            each member listed here with the exact same hostname, otherwise
            access via proxies will not work.
        network: Nebula network to connect to.
        etcd_endpoints: List of etcd endpoints for Patroni to use.
            They must all belong to a single etcd cluster
        etcd_client_group: Firewall group that the etcd nodes allow connections
            from.
    """
    cluster_id: str
    members: list[str]

    network: nebula.Network
    etcd_endpoints: list[str]
    etcd_client_group: str


@operation()
def instance(cluster: ClusterConfig, hostname: str,
             superuser_secret: str, replication_secret: str, rewind_secret: str,
             image: str = 'ghcr.io/bensku/pigeon/patroni',
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
        present: By default, the node is created or modified. If set to False,
            it is destroyed instead. Database data is NOT deleted from disk
            automatically.
    """
    if not hostname in cluster.members:
        raise ValueError(f'{hostname} is not a member of Patroni cluster {cluster.cluster_id}')

    internal_group = f'patroni-internal-{cluster.cluster_id}'
    endpoint = nebula.pod_endpoint(
        network=cluster.network,
        hostname=hostname,
        firewall=nebula.Firewall(
            inbound=[
                nebula.FirewallRule(5432, internal_group), # PostgreSQL
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
    config = _patroni_config(cluster.cluster_id, hostname, cluster.etcd_endpoints)
    yield from podman.pod._inner(
        pod_name=f'patroni-postgres-{cluster.cluster_id}',
        containers=[
            podman.Container(
                name='main',
                image=image,
                volumes=[
                    (f'/var/containerops/data/patroni/{cluster.cluster_id}', '/data:Z'),
                    (podman.ConfigFile(id=f'patroni-{cluster.cluster_id}-config', data=json.dumps(config, indent=2, sort_keys=True)), '/etc/patroni.yml')
                ],
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


@operation()
def proxy(cluster: ClusterConfig, hostname: str,
          client_groups: list[str],
          image: str = 'docker.io/haproxy:3.2-alpine',
          present: bool = True):
    """
    Creates a proxy for accessing Patroni-managed PostgreSQL instances
    with failover support.
    You should deploy one one each machine that needs access to the databases.

    Arguments:
        cluster: Cluster configuration. Must be same for all nodes.
        hostname: Unique hostname of this node. Note, DO NOT reuse a hostname
            from a Patroni node.
        client_groups: List of firewall groups to allow client connections from.
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
                nebula.FirewallRule(5000, client_groups), # Proxied PostgreSQL
                nebula.FirewallRule(7000, client_groups), # HAProxy stats server
            ],
            outbound=[
                nebula.FirewallRule(5432, internal_group), # PostgreSQL
                nebula.FirewallRule(8008, internal_group) # Patroni REST API
            ]
        ),
        groups=[internal_group, 'postgres'],
    )

    config = _haproxy_config(cluster.members)
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


def _patroni_config(cluster_id: str, hostname: str, etcd_addrs: list[str]):
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
                }
            },
            'initdb': [
                { 'encoding': 'utf-8' },
                'data-checksums'
            ]
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
        'tags': {}
    }


def _haproxy_config(hostnames: str):
    server_lines = [f'    server {hostname} {hostname}:5432 resolvers poddns maxconn 100 check port 8008 init-addr none' for hostname in hostnames]
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

listen postgres
    bind *:5000
    option httpchk
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
{'\n'.join(server_lines)}
"""