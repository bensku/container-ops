from pyinfra import host

from containerops import nebula


ip_addresses = host.data.ip_addresses
net_config = nebula.Network(
    'containerops-test',
    dns_domain='containerops.test',
    prefix_len=24,
    epoch=1,
    lighthouses=[
        ('10.2.57.1', f'{ip_addresses['containerops-1']}:4242'),
        ('10.2.57.3', f'{ip_addresses['containerops-3']}:4242'),
    ]
)
