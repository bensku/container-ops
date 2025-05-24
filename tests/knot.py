from pyinfra import host
from pyinfra.api import deploy


from containerops import nebula, knot
from tests.nebula_common import net_config


@deploy('Install Knot DNS server')
def install_knot():
    zone1 = knot.Zone(
        domain='a.knot.test',
        records=[
            knot.Record('@', 'SOA', 'ns1.a.knot.test. nowhere.knot.test. (0 4H 1H 12W 1D)'),
            knot.Record('@', 'A', '127.0.0.1'),
        ],
    )
    zone2 = knot.Zone(
        domain='b.knot.test',
        records=[
            knot.Record('@', 'SOA', 'ns1.b.knot.test. nowhere.knot.test. (0 4H 1H 12W 1D)'),
            knot.Record('@', 'A', '127.0.0.1',),
            knot.Record('test', 'TXT', 'hello world'),
        ],
        acme_config=knot.AcmeConfig(
            allowed_ip_ranges=['10.2.57.0/24'],
            tsig_key='mxDDXc1wX33ZDR0vNkGsH3nU8W7MW4g38+5TNpGtQDU=',
        ),
    )

    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'knot-{host.name}.containerops.test',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
    )
    knot.install(
        svc_name='knot',
        zones=[zone1, zone2],
        networks=[endpoint],
        host_port='5300'
    )


install_knot()