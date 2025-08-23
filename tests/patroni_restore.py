from pyinfra import host
from pyinfra.api import deploy

from containerops import patroni, podman
from tests.nebula_common import net_config


CLUSTER_CONFIG = patroni.ClusterConfig(
    cluster_id='test2',
    members=[
        'containerops-1.patroni2.containerops.test',
        'containerops-2.patroni2.containerops.test',
    ],
    read_replicas=[
        'containerops-3.patroni2.containerops.test',
    ],
    backup_replicas=[
        'containerops-3.patroni2.containerops.test',
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
    restore_from_backup=True
)


@deploy('Patroni cluster')
def patroni_cluster():
    podman.secret('superuser-secret', source='tests/test_secret.json', json_key='pg_superuser')
    podman.secret('replication-secret', source='tests/test_secret.json', json_key='pg_replication')
    podman.secret('rewind-secret', source='tests/test_secret.json', json_key='pg_rewind')
    patroni.instance(
        cluster=CLUSTER_CONFIG,
        hostname=f'{host.name}.patroni2.containerops.test',
        superuser_secret='superuser-secret',
        replication_secret='replication-secret',
        rewind_secret='rewind-secret',
    )

    patroni.proxy(
        cluster=CLUSTER_CONFIG,
        hostname=f'{host.name}.pgproxy2.containerops.test',
    )


patroni_cluster()
