from dataclasses import dataclass
import json
import shutil
from containerops import nebula, _ipam as ipam
import os


@dataclass
class Endpoint:
    network: nebula.Network
    hostname: str
    ip: str
    firewall: nebula.Firewall

    ca_data: str
    cert_data: str
    key_data: str


def nebula_config_file(endpoint: Endpoint, out_dir: str):
    """
    Just creates Nebula configuration file with inline keys.
    """
    with open(f'{out_dir}/config.json', 'w') as f:
        f.write(_bundled_config(endpoint))


def systemd_svc_installer(endpoint: Endpoint, out_dir: str):
    """
    Creates a shell script that downloads Nebula and installs the endpoint as
    systemd service. Same script can also uninstall the service.
    """
    client_id = f'{endpoint.network.name}-{endpoint.hostname}'

    # Create configurations
    config = _bundled_config(endpoint)
    config_path = f'/etc/containerops-vpn/{client_id}/config.json'
    unit = nebula._nebula_unit(endpoint.network, endpoint.hostname, config_path)

    # Create script that installs (or uninstalls) everything
    script = f"""#!/bin/sh
op=$1
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Root access required to install systemd service" >&2
    exit 1
fi

config=$(cat <<"EOF"
{config}
EOF
)

unit=$(cat <<"EOF"{unit}
EOF
)

if [ "$op" = "install" ]; then
    echo "Downloading Nebula client..."
    mkdir -p /opt/containerops-vpn/{client_id}
    wget -q -O /opt/containerops-vpn/{client_id}/nebula {nebula.NEBULA_NETNS_DOWNLOAD}
    chmod +x /opt/containerops-vpn/{client_id}/nebula

    echo "Installing configuration..."
    mkdir -p /etc/containerops-vpn/{client_id}
    printf '%s' "$config" > /etc/containerops-vpn/{client_id}/config.json

    echo "Setting up systemd service..."
    printf '%s' "$unit" > /etc/systemd/system/containerops-vpn-{client_id}.service
    systemctl daemon-reload
    systemctl enable containerops-vpn-{client_id}.service
    systemctl restart containerops-vpn-{client_id}.service
    
    echo "Endpoint {client_id} installed and started successfully."
elif [ "$op" = "uninstall" ]; then
    echo "Stopping Nebula client..."
    systemctl stop containerops-vpn-{client_id}.service || true
    
    echo "Disabling and removing systemd service..."
    systemctl disable containerops-vpn-{client_id}.service || true
    rm /etc/systemd/system/containerops-vpn-{client_id}.service
    systemctl daemon-reload
    
    echo "Cleaning up installation..."
    rm /etc/containerops-vpn/{client_id}/config.json
    rmdir /etc/containerops-vpn/{client_id} || true
    rm /opt/containerops-vpn/{client_id}/nebula
    rmdir /opt/containerops-vpn/{client_id} || true

    echo "Endpoint {client_id} uninstalled successfully."
else
    echo "Usage: $0 <install|uninstall>" >&2
    exit 2
fi
"""
    with open(f'{out_dir}/install_service.sh', 'w') as f:
        f.write(script)
    os.chmod(f'{out_dir}/install_service.sh', 0o755)


def _bundled_config(endpoint: Endpoint) -> str:
    config = nebula._nebula_config(
        network=endpoint.network,
        hostname=endpoint.hostname,
        ip=endpoint.ip,
        is_lighthouse=False,
        underlay_port=0,
        firewall=endpoint.firewall,
        # Embed key material directly in config
        ca_value=endpoint.ca_data,
        cert_value=endpoint.cert_data,
        key_value=endpoint.key_data
    )
    return json.dumps(config, indent=4, sort_keys=True)


def _new_client(state_dir: str, net_name: str, hostname: str, groups: list[str], duration: str):
    with open(f'{state_dir}/networks/{net_name}/state.json', 'r') as f:
        network = nebula.Network(**json.loads(f.read()))
    ip = ipam.allocate_ip(
        network_name=network.name,
        hostname=hostname,
        cidr=network.cidr,
        base_dir=f'{network.state_dir}/networks',
    )
    # Allow VPN clients to connect to anywhere that permits them
    # TODO make this configurable
    firewall = nebula.Firewall(
        inbound=[],
        outbound=[nebula.FirewallRule('any', 'any')]
    )

    ca_dir = f'{network.state_dir}/networks/{network.name}/ca/{network.epoch}'
    cert_dir = f'{network.state_dir}/networks/{network.name}/endpoint/{hostname}'
    with open(f'{ca_dir}/ca.crt', 'r') as f:
        ca_data = f.read()

    nebula._new_cert(hostname, ip, network.prefix_len, ca_dir, cert_dir, groups, duration)
    with open(f'{cert_dir}/host.crt', 'r') as f:
        cert_data = f.read()
    with open(f'{cert_dir}/host.key', 'r') as f:
        key_data = f.read()

    endpoint = Endpoint(
        network=network,
        hostname=hostname,
        ip=ip,
        firewall=firewall,
        ca_data=ca_data,
        cert_data=cert_data,
        key_data=key_data
    )

    out_dir = f'{network.state_dir}/client-configs/{hostname}'
    os.makedirs(out_dir, exist_ok=True)

    systemd_svc_installer(endpoint, out_dir)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Nebula VPN client configuration generator')
    parser.add_argument('--state', required=True, help='Directory where Nebula state is stored')
    subparsers = parser.add_subparsers(dest='command')

    client_parser = subparsers.add_parser('new', help='Generate Nebula client configurations and join scripts')
    client_parser.add_argument('network_name', help='Nebula network name')
    client_parser.add_argument('hostname', help='Hostname of client')
    client_parser.add_argument('--duration', required=True, help='Certificate duration. Valid time units are s (seconds), m (minutes), h (hours).')
    client_parser.add_argument('--groups', nargs='*', default=[], help='Endpoint groups')

    args = parser.parse_args()

    if args.command == 'new':
        _new_client(args.state, args.network_name, args.hostname, args.groups, args.duration)
    else:
        parser.print_help()
        exit(1)
