from containerops import patroni
from tests.nebula_common import net_config


CLUSTER_CONFIG = patroni.ClusterConfig(
    cluster_id='test',
    members=[
        'containerops-1.patroni.containerops.test',
        'containerops-2.patroni.containerops.test',
    ],
    read_replicas=[
        'containerops-3.patroni.containerops.test',
    ],
    backup_replicas=[
        'containerops-3.patroni.containerops.test',
    ],
    client_groups=['test-vm'],
    network=net_config,
    etcd_endpoints=[
        'containerops-1.etcd.containerops.test:2379',
        'containerops-2.etcd.containerops.test:2379',
        'containerops-3.etcd.containerops.test:2379',
    ],
    etcd_client_group='patroni-test',
    barman_backup_support=True,
)
