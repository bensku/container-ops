from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import server

from containerops import hostdns, nebula, podman
from tests.nebula_common import net_config

@deploy('Nebula management network')
def management_net():
    nebula.setup_host(selinux=True)
    nebula.endpoint(
        network=net_config, 
        hostname=f'{host.name}.containerops.test',
        ip=host.data.nebula_ip,
        groups=['test-vm'],
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
        is_lighthouse=host.data.lighthouse,
        underlay_port=4242,
        present=True
    )
    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'{host.name}.hostdns.containerops.test',
        firewall=nebula.Firewall(
            inbound=[],
            outbound=[]
        ),
    )
    hostdns.install(networks=[endpoint, podman.HOST_NAT], present=True)


management_net()