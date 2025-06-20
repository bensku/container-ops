from pyinfra import host
from pyinfra.api import deploy
from pyinfra.operations import server

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
    # FIXME terrible hack
    server.shell(f'echo "options rotate\nnameserver 10.2.57.1\nnameserver 8.8.8.8" >/etc/resolv.conf')


management_net()