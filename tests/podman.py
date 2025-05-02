from pyinfra.api import deploy
import containerops.podman as podman


@deploy('Container with inbound ports and outbound NAT')
def deploy_with_net():
    back_config = '''server {
    listen 81;
    location /test {
        add_header Content-Type text/plain;
        return 200 'success';
    }
}'''
    front_config = '''server {
    location /test {
        proxy_pass http://localhost:81/test;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}'''
    podman.pod(pod_name='reachable', containers=[
            podman.Container(
                name='back',
                image='docker.io/nginx:latest',
                volumes=[(podman.ConfigFile(id='nginx-back', data=back_config), '/etc/nginx/conf.d/default.conf')]
            ),
            podman.Container(
                name='front',
                image='docker.io/nginx:latest',
                volumes=[(podman.ConfigFile(id='nginx-front', data=front_config), '/etc/nginx/conf.d/default.conf')]
            )
        ], networks=[podman.HOST_NAT], ports=[('8081', '80')], present=True)
    

deploy_with_net()
# podman.pod(pod_name='no-network', containers=[], networks=[], present=True)