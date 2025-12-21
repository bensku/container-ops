from dataclasses import dataclass
import base64
import json
from containerops import nebula, _ipam as ipam
import os
import segno
import re


_QR_HTML_TEMPLATE = '''
<!DOCTYPE html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Nebula</title>
<style>
body{{font:14px monospace;padding:8px}}
h2{{font-size:16px;margin:8px 0}}
pre{{background:#eee;padding:8px;white-space:pre-wrap;word-break:break-all}}
button{{margin-left:8px;padding:2px 8px}}
</style>
<script>
function c(id){{var t=document.getElementById(id).innerText,a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');document.body.removeChild(a)}}
</script>
</head><body>
<h2>CA <button onclick="c('ca')">Copy</button></h2>
<pre id=ca>{ca}</pre>
<h2>Cert <button onclick="c('cert')">Copy</button></h2>
<pre id=cert>{cert}</pre>
<h2>Key <button onclick="c('key')">Copy</button></h2>
<pre id=key>{key}</pre>
<h2>Hosts</h2>
{hosts}
</body></html>
'''

_QR_MAX_BYTES = 2900


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
    nebula_path = f'/opt/containerops-vpn/{client_id}/nebula'
    unit = nebula._nebula_unit(
        endpoint.network,
        endpoint.hostname,
        config_path,
        nebula_path='')

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
    mkdir -p /opt/containerops-vpn/{client_id}
    echo "Downloading Nebula client..."
    nebula_path={nebula_path}
    wget -q -O $nebula_path {nebula.NEBULA_NETNS_DOWNLOAD}
    echo "Making client service executable"
    chmod +x $nebula_path
    if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
        semanage fcontext -a -t bin_t $nebula_path
        restorecon -v $nebula_path
    fi

    echo "Installing configuration..."
    mkdir -p /etc/containerops-vpn/{client_id}
    chmod 700 /etc/containerops-vpn/{client_id}
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


def mobile_nebula_qrcode(endpoint: Endpoint, out_dir: str):
    """Generate QR code with data URL containing Nebula credentials."""
    # Generate per-host HTML with individual copy buttons for overlay IP and public endpoint
    hosts_html = ''.join(
        f'<div><code id=o{i}>{lh[0]}</code><button onclick="c(\'o{i}\')">Copy</button> '
        f'<code id=h{i}>{lh[1]}</code><button onclick="c(\'h{i}\')">Copy</button></div>'
        for i, lh in enumerate(endpoint.network.lighthouses)
    )

    # Strip template newlines first, then format with data (preserving cert newlines)
    template = re.sub(r'\n\s*', '', _QR_HTML_TEMPLATE)
    html = template.format(
        ca=endpoint.ca_data.strip(),
        cert=endpoint.cert_data.strip(),
        key=endpoint.key_data.strip(),
        hosts=hosts_html
    )

    html_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
    data_url = f'data:text/html;base64,{html_b64}'

    if len(data_url) > _QR_MAX_BYTES:
        raise ValueError(f'QR data ({len(data_url)} bytes) exceeds max ({_QR_MAX_BYTES})')

    qr = segno.make(data_url, error='L')
    qr_path = f'{out_dir}/mobile-qrcode.png'
    qr.save(qr_path, scale=8, border=4)
    return qr_path


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
        key_value=endpoint.key_data,
        local_allow_list=nebula._LOCAL_ALLOW_LIST_DEFAULT
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
    mobile_nebula_qrcode(endpoint, out_dir)


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
