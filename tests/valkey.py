from pyinfra import host
from pyinfra.api import deploy

from containerops import valkey
from tests.nebula_common import net_config


@deploy('Valkey standalone node')
def one_node():
    valkey.node(
        pod_name='single-node-valkey',
        hostname='single.valkey.containerops.test',
        network=net_config,
        client_groups=['test-vm'],
    )


SENTINEL_CONFIG = valkey.SentinelConfig(
    cluster_id='test-cluster',
    master_hostname='sentinel-containerops-1.valkey.containerops.test',
    quorum=2
)

@deploy('Valkey sentinel cluster')
def sentinel_cluster():
    valkey.node(
        pod_name='valkey-sentinel',
        hostname=f'sentinel-{host.name}.valkey.containerops.test',
        network=net_config,
        client_groups=['test-vm', 'caddy'],
        sentinel_config=SENTINEL_CONFIG
    )


if host.name == 'containerops-2':
    one_node()

sentinel_cluster()