from pyinfra import host
from pyinfra.api import deploy

from containerops import patroni, podman
from tests.patroni_common import CLUSTER_CONFIG


@deploy('Patroni cluster')
def patroni_cluster():
    podman.secret('superuser-secret', source='tests/test_secret.json', json_key='pg_superuser')
    podman.secret('replication-secret', source='tests/test_secret.json', json_key='pg_replication')
    podman.secret('rewind-secret', source='tests/test_secret.json', json_key='pg_rewind')
    patroni.instance(
        cluster=CLUSTER_CONFIG,
        hostname=f'{host.name}.patroni.containerops.test',
        superuser_secret='superuser-secret',
        replication_secret='replication-secret',
        rewind_secret='rewind-secret',
    )

    patroni.proxy(
        cluster=CLUSTER_CONFIG,
        hostname=f'{host.name}.pgproxy.containerops.test',
    )


@deploy('Barman backups')
def barman_backups():
    patroni.barman_backups(
        sources=[
            patroni.BackupSource(
                cluster=CLUSTER_CONFIG,
                pgproxy_hostname='containerops-1.pgproxy.containerops.test',
                superuser_secret='superuser-secret',
                replication_secret='replication-secret',
                frequency='*/5 * * * *',
            )
        ],
        hostname='barman.containerops.test',
    )


patroni_cluster()
if host.name == 'containerops-1':
    barman_backups()