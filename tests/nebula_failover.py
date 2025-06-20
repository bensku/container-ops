from pyinfra import host
from pyinfra.api import deploy


from containerops import podman, nebula
from tests.nebula_common import net_config


@deploy('Failover IP target')
def failover_ip():
    config = f'''server {{
    listen 80;
    location / {{
        add_header Content-Type text/plain;
        return 200 'leader {host.name}';
    }}
}}'''
    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'failover.containerops.test',
        ip='10.2.57.100',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
        failover=True
    )
    podman.pod(pod_name='failover', containers=[
            podman.Container(
                name='main',
                image='docker.io/nginx:latest',
                volumes=[(podman.ConfigFile(id='nginx-failover', data=config), '/etc/nginx/conf.d/default.conf')]
            ),
        ], networks=[endpoint], present=True)


failover_ip()