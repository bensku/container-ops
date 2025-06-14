from pyinfra import host
from pyinfra.api import deploy

from containerops import etcd
from tests.nebula_common import net_config


CLUSTER_CONFIG = etcd.ClusterConfig(
    cluster_id='test-cluster',
    members=[
        'containerops-1.etcd.containerops.test',
        'containerops-2.etcd.containerops.test',
        'containerops-3.etcd.containerops.test',
    ]
)

@deploy('etcd cluster')
def etcd_cluster():
    etcd.node(
        cluster=CLUSTER_CONFIG,
        hostname=f'{host.name}.etcd.containerops.test',
        network=net_config,
        client_groups=['test-vm', 'patroni-test'],
    )


etcd_cluster()