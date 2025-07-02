from io import StringIO
from pyinfra.api import operation, StringCommand
from pyinfra.operations import files, systemd

from containerops import podman


@operation()
def install(networks: list[podman.Network], write_resolvconf=True, disable_nm_dns=True, fallback_dns='1.1.1.1', present=True):
    """
    Configures the host this runs on to use DNS resolvers of given container networks.
    This is done by deploying a copy of dnsmasq and, by default, overwriting host's
    current DNS configuration to point to it.

    Note that this does NOT grant the host network connectivity! For Nebula networks,
    this can be achieved through non-pod endpoints.

    Arguments:
        networks: List of networks to grab DNS configuration from. Include
            podman.HOST_NAT if you want public Internet DNS!
        write_resolvconf: Overwrite /etc/resolv.conf to point to our dnsmasq
            server (and a fallback).
        disable_nm_dns: Make NetworkManager disable its own DNS. This is
            required if you are using NetworkManager, but would otherwise
            lead to errors.
        fallback_dns: DNS server to use if our dnsmasq is unreachable. This
            will happen when e.g. pulling the dnsmasq container itself!
            Defaults to Cloudflare's public DNS resolver.
        present: Whether to deploy this or remove it. Note that overwritten
            /etc/resolv.conf cannot be recovered!
    """

    if disable_nm_dns:
        if present:
            yield from files.put._inner(src=StringIO('[main]\ndns=none'), dest='/etc/NetworkManager/conf.d/containerops-hostdns.conf')
        else:
            yield StringCommand('rm -f /etc/NetworkManager/conf.d/containerops-hostdns.conf')
        yield from systemd.service._inner('NetworkManager', running=True, restarted=True)

    if write_resolvconf and present:
        config = f"""# Managed by containerops hostdns module
# Primary DNS served by dnsmasq
nameserver 127.0.0.1
# Emergency DNS if primary unavailable
nameserver {fallback_dns}
"""
        yield from files.put._inner(src=StringIO(config), dest='/etc/resolv.conf')

    # The Podman pod will get dnsmasq automatically
    # Use internal _expose_dns to bind it to all interfaces, then expose ports with Podman
    yield from podman.pod._inner(pod_name='hostdns', containers=[], networks=networks, ports=[
        # For security and to avoid conflicts with authorative DNS servers, expose ONLY on localhost
        ('127.0.0.1:53', '53', 'udp'),
        ('127.0.0.1:53', '53', 'tcp')
    ], _expose_dns=True, present=present)