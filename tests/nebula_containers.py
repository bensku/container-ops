from pyinfra import host
from pyinfra.api import deploy


from containerops import podman, nebula
from tests.nebula_common import net_config


@deploy('Backend nginx')
def backend_nginx():
    back_config = '''server {
    listen 81;
    location /test {
        add_header Content-Type text/plain;
        return 200 'success';
    }
}'''
    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'back-nginx.containerops.test',
        ip='10.2.57.11',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
    )
    podman.pod(pod_name='nginx-back', containers=[
            podman.Container(
                name='main',
                image='docker.io/nginx:latest',
                volumes=[(podman.ConfigFile(id='nginx-back', data=back_config), '/etc/nginx/conf.d/default.conf')]
            ),
        ], networks=[endpoint], present=True)


@deploy('Frontend nginx')
def frontend_nginx():
    front_config = '''server {
    location /test {
        resolver 127.0.0.1;
        set $backend_server back-nginx.containerops.test;
        proxy_pass http://$backend_server:81/test;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}'''
    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'front-nginx.containerops.test',
        ip='10.2.57.12',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
    )
    podman.pod(pod_name='nginx-front', containers=[
            podman.Container(
                name='main',
                image='docker.io/nginx:latest',
                volumes=[(podman.ConfigFile(id='nginx-front', data=front_config), '/etc/nginx/conf.d/default.conf')]
            )
        ], networks=[podman.HOST_NAT, endpoint], ports=[('8082', '80')], present=True)


if host.name == 'containerops-1':
    backend_nginx()
elif host.name == 'containerops-2':
    frontend_nginx()
