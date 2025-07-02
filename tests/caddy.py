from pyinfra import host
from pyinfra.api import deploy


from containerops import nebula, knot, podman
from tests.nebula_common import net_config


@deploy('Install Knot for DNS-01')
def setup_dns():
    zone = knot.Zone(
        domain='acme.test',
        records=[
            knot.Record('@', 'SOA', 'dns.containerops.test. nowhere.containerops.test. (0 4H 1H 12W 1D)'),
            knot.Record('@', 'NS', 'dns.containerops.test.'),
            knot.Record('caddy', 'A', '10.2.57.42',),
        ],
        acme_config=knot.AcmeConfig(
            allowed_ip_ranges=['10.2.57.0/24'],
            tsig_key='mxDDXc1wX33ZDR0vNkGsH3nU8W7MW4g38+5TNpGtQDU=',
        ),
    )

    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'dns.containerops.test',
        ip='10.2.57.43',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
    )
    knot.install(
        svc_name='dns',
        zones=[zone],
        networks=[endpoint],
        host_port='5301'
    )


@deploy('Setup Pebble ACME test server')
def setup_pebble():
    tls_crt = """-----BEGIN CERTIFICATE-----
MIID+jCCAuKgAwIBAgIUJYF/sOZvmGWdpRAh+BtGT7bwW4EwDQYJKoZIhvcNAQEL
BQAwIDEeMBwGA1UEAxMVbWluaWNhIHJvb3QgY2EgMjRlMmRiMCAXDTI1MDcwMTIz
NTI0N1oYDzIyOTkwNDE2MjM1MjQ3WjAjMSEwHwYDVQQDDBhwZWJibGUuY29udGFp
bmVyb3BzLnRlc3QwggIiMA0GCSqGSIb3DQEBAQUAA4ICDwAwggIKAoICAQCbtyB/
211VY18O3NSz1NLDFbn1J4UYAJNyK2WhEtc8fn24j5Pw/Lgn0bR26lBikLNjvXcC
OE4jL/P/eY/R2+b+PFSiExbAZ3wBKh2Tb6SU16NyqCye5KLMufeGNPMXK35EIdfh
1bOL+x1NBB0+CErMoMsjd4QwXkT36K9KrWpFVCe7u+wS57LJCfSC7s3YEMklKgWx
KRp9sRjotQJUNhpRmlgxewGgfNxCw2KDvnYmldx6W49jiA6moMDoWrgTebx5ofTp
V9ogNe74bnWwgquNQwNlfdCK1Nqh70bn7JN/NNYGgYnVZUw5AIu3ByoedDKrYGcu
wPmqBoej1m1vaFKClmHiSvVlB+EPuLsb0cULKHzGzcZCfRXmNEUCgf8+BYTRW4ef
VALm1slvK+P/3zEfjzk/u8dLg8WNtAnbGR21fOXGOR8d/VSx6ftZ+tzB9GU/saEl
SO/eseVt/+rfRhNhi81bZ+40aTf0aogrq460UeO51PvfELsS+fOHkVNrXC3Va5yy
zjjVl35xyJ9AXWw+Z6hegyPRuRM/bqpKtN05VKhY6apRlZ2allvgDYX53UOmu5i3
YkTjGYVORdZdntew1kAIo0dCw4URaDMkEYNyKoR187aAZePTK88P5pD2jeoL4h2q
9znnvyNYD8JoMPpYZ3iYRFw2oYDsxJ1Ll8J9HQIDAQABoycwJTAjBgNVHREEHDAa
ghhwZWJibGUuY29udGFpbmVyb3BzLnRlc3QwDQYJKoZIhvcNAQELBQADggEBAJ1o
rkqeMD9ay8JKYUUek95Lil3zeNn20CFbHJVHl8v6fNe/drnO3zxF878ovdMA9uYR
Zf2rPKtaNc5ynWWzJgUFedJ2kGiFi97LR3zFQjhm4yj16+s0IzLUYkJz3DHk4g4e
PCpgXaDKNRWzY6zen9Qwm3Crf4ZylhiFHj8u5gux1nWs/3if3U13uwcOSv+aYHmW
rBuZ0b5QHOSKRqvDC47vptVpfZi4vyn6t3EWSmRV4jjHBgqfL+peC+6qKW6jzHHh
XvqT0nBoa+hxiBNW3vAMKVae2akQnnY3EVNfjUHk1FcmNo6tNZaaCUb97+N5t+6L
BpPkHnEGFVaor7Jn65k=
-----END CERTIFICATE-----
"""
    tls_key = """-----BEGIN RSA PRIVATE KEY-----
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
-----END RSA PRIVATE KEY-----
"""

    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'pebble.containerops.test',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
    )
    podman.pod(pod_name='pebble', containers=[
            podman.Container(
                name='main',
                image='ghcr.io/letsencrypt/pebble:latest',
                command='-dnsserver 10.2.57.43:5300',
                volumes=[
                    (podman.ConfigFile(id='pebble-cert', data=tls_crt), '/test/certs/localhost/cert.pem'),
                    (podman.ConfigFile(id='pebble-key', data=tls_key), '/test/certs/localhost/key.pem')
                ]
            ),
        ], networks=[endpoint], present=True)


@deploy('Setup Caddy')
def setup_caddy():
    config = f"""
{{
    acme_ca https://pebble.containerops.test:14000/dir
    acme_ca_root /etc/caddy/pebble-ca.pem
    storage redis failover {{
        address {{
            sentinel-containerops-1.valkey.containerops.test:26379
            sentinel-containerops-2.valkey.containerops.test:26379
            sentinel-containerops-3.valkey.containerops.test:26379
        }}
        master_name mymaster
    }}
}}

*.acme.test {{
    tls {{
        dns rfc2136 {{
            server "dns.containerops.test:5300"
            key_alg "hmac-sha256"
            key_name "acme.test-acme-key"
            key "mxDDXc1wX33ZDR0vNkGsH3nU8W7MW4g38+5TNpGtQDU="
        }}
        propagation_timeout -1
    }}
    respond "caddy at {host.name}"
}}
"""
    
    pebble_ca = """-----BEGIN CERTIFICATE-----
MIIDCTCCAfGgAwIBAgIIJOLbes8sTr4wDQYJKoZIhvcNAQELBQAwIDEeMBwGA1UE
AxMVbWluaWNhIHJvb3QgY2EgMjRlMmRiMCAXDTE3MTIwNjE5NDIxMFoYDzIxMTcx
MjA2MTk0MjEwWjAgMR4wHAYDVQQDExVtaW5pY2Egcm9vdCBjYSAyNGUyZGIwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC5WgZNoVJandj43kkLyU50vzCZ
alozvdRo3OFiKoDtmqKPNWRNO2hC9AUNxTDJco51Yc42u/WV3fPbbhSznTiOOVtn
Ajm6iq4I5nZYltGGZetGDOQWr78y2gWY+SG078MuOO2hyDIiKtVc3xiXYA+8Hluu
9F8KbqSS1h55yxZ9b87eKR+B0zu2ahzBCIHKmKWgc6N13l7aDxxY3D6uq8gtJRU0
toumyLbdzGcupVvjbjDP11nl07RESDWBLG1/g3ktJvqIa4BWgU2HMh4rND6y8OD3
Hy3H8MY6CElL+MOCbFJjWqhtOxeFyZZV9q3kYnk9CAuQJKMEGuN4GU6tzhW1AgMB
AAGjRTBDMA4GA1UdDwEB/wQEAwIChDAdBgNVHSUEFjAUBggrBgEFBQcDAQYIKwYB
BQUHAwIwEgYDVR0TAQH/BAgwBgEB/wIBADANBgkqhkiG9w0BAQsFAAOCAQEAF85v
d40HK1ouDAtWeO1PbnWfGEmC5Xa478s9ddOd9Clvp2McYzNlAFfM7kdcj6xeiNhF
WPIfaGAi/QdURSL/6C1KsVDqlFBlTs9zYfh2g0UXGvJtj1maeih7zxFLvet+fqll
xseM4P9EVJaQxwuK/F78YBt0tCNfivC6JNZMgxKF59h0FBpH70ytUSHXdz7FKwix
Mfn3qEb9BXSk0Q3prNV5sOV3vgjEtB4THfDxSz9z3+DepVnW3vbbqwEbkXdk3j82
2muVldgOUgTwK8eT+XdofVdntzU/kzygSAtAQwLJfn51fS1GvEcYGBc1bDryIqmF
p9BI7gVKtWSZYegicA==
-----END CERTIFICATE-----"""

    endpoint = nebula.pod_endpoint(
        network=net_config,
        hostname=f'{host.name}.caddy.containerops.test',
        firewall=nebula.Firewall(
            inbound=[nebula.FirewallRule('any', 'any')],
            outbound=[nebula.FirewallRule('any', 'any')]
        ),
        groups=['caddy']
    )
    podman.pod(pod_name='caddy', containers=[
            podman.Container(
                name='main',
                image='ghcr.io/bensku/containerops-builds/caddy:2.10.0',
                volumes=[
                    (podman.ConfigFile(id='caddy-config', data=config), '/etc/caddy/Caddyfile'),
                    (podman.ConfigFile(id='pebble-ca', data=pebble_ca), '/etc/caddy/pebble-ca.pem')
                ]
            ),
        ], networks=[endpoint, podman.custom_dns('acme.test', ['10.2.57.43#5300'])])


if host.name == 'containerops-1':
    setup_dns()
    setup_pebble()
setup_caddy()