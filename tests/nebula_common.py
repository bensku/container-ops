from pyinfra import host

from containerops import nebula


ip_addresses = host.data.ip_addresses
net_config = nebula.Network(
    'containerops-test',
    state_dir='nebula_state',
    dns_domain='containerops.test',
    cidr='10.2.57.0/24',
    epoch=1,
    lighthouses=[
        ('10.2.57.1', f'{ip_addresses['containerops-1']}:4242'),
        ('10.2.57.3', f'{ip_addresses['containerops-3']}:4242'),
    ],
    failover_etcd=[
        'containerops-1.etcd.containerops.test:2379',
        'containerops-2.etcd.containerops.test:2379',
        'containerops-3.etcd.containerops.test:2379',
    ]
)

nebula.initialize_network(net_config)