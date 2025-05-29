from dataclasses import dataclass
from pyinfra.api import operation
from pyinfra.operations import files

from containerops import nebula, podman


@dataclass
class ClusterConfig:
    """
    etcd cluster configuration.

    Arguments:
        cluster_id: Unique identifier for the cluster.
        members: List of member nodes' hostnames. You must deploy a node for
            each member listed here with the exact same hostname, otherwise
            etcd will fail to start (or worse).
    """

    cluster_id: str
    members: list[str]


@operation()
def node(cluster: ClusterConfig, hostname: str,
         network: nebula.Network, client_groups: list[str],
         image: str = 'gcr.io/etcd-development/etcd:v3.6.0',
         present: bool = True):
    """
    Creates a node for a containerized, highly available etcd cluster.
    You must always deploy 3 nodes. When the cluster is healthy, each node
    serves both reads and writes at port 2379 to clients in allowed firewall
    groups.
    
    This is a rather opinioned setup. If you wish to use a different networking
    configuration or more nodes, it is best to deploy etcd on your own with the
    podman module.

    Arguments:
        cluster: Cluster configuration. Must be same for all nodes.
        hostname: Unique hostname of this node.
        network: Nebula network to connect to.
        client_groups: List of firewall groups to allow client connections from.
        image: Container image for etcd, if you want to override that.
        present: By default, the node is created or modified. If set to False,
            it is destroyed instead. Database data is NOT deleted from disk
            automatically.
    """
    if len(cluster.members) != 3:
        raise ValueError('only 3-node clusters are currently supported')

    internal_group = f'etcd-internal-{cluster.cluster_id}'
    endpoint = nebula.pod_endpoint(
        network=network,
        hostname=hostname,
        firewall=_firewall(internal_group, client_groups),
        groups=[internal_group] if internal_group else [],
    )

    initial_cluster = ','.join([f'{member}=http://{member}:2380' for member in cluster.members])
    yield from files.directory._inner(path=f'/var/containerops/data/etcd/{cluster.cluster_id}')
    yield from podman.pod._inner(
        pod_name=f'etcd-{cluster.cluster_id}',
        containers=[
            podman.Container(
                name='main',
                image=image,
                command=f'/usr/local/bin/etcd --data-dir /etcd-data --name {hostname} \
--initial-advertise-peer-urls http://{hostname}:2380 --listen-peer-urls http://0.0.0.0:2380 \
--advertise-client-urls http://{hostname}:2379 --listen-client-urls http://0.0.0.0:2379 \
--initial-cluster {initial_cluster} \
--initial-cluster-state new --initial-cluster-token {cluster.cluster_id}',
                volumes=[(f'/var/containerops/data/etcd/{cluster.cluster_id}', '/etcd-data:Z')],
            )
        ],
        networks=[endpoint],
        present=present
    )


def _firewall(internal_group: str, client_groups: list[str]) -> nebula.Firewall:
    all_groups = client_groups + [internal_group]
    return nebula.Firewall(
        inbound=[
            nebula.FirewallRule(port=2379, groups=all_groups),
            nebula.FirewallRule(port=2380, groups=[internal_group]),
        ],
        outbound=[
            nebula.FirewallRule(port=2379, groups=[internal_group]),
            nebula.FirewallRule(port=2380, groups=[internal_group]),
        ]
    )