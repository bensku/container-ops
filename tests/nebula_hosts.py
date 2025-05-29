from pyinfra import host
from pyinfra.api import deploy

from containerops import nebula
from tests.nebula_common import net_config

@deploy('Nebula management network')
def management_net():
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


management_net()