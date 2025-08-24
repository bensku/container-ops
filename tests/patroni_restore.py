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
    barman_backup_support=False,
    restore_from_backup='/var/containerops/data/barman_restored/restore1'
)

@deploy('Restore Patroni cluster from backup')
def restore_backup():
    patroni.restore_backup(
        cluster_id='test',
        restore_name='restore1',
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


if host.name == 'containerops-1':
    restore_backup()
patroni_cluster()
