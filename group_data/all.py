import os

_sudo = True

# Generate SSH config for Pyinfra
os.system('vagrant ssh-config >tests/vagrant_ssh_config')
ssh_config_file = 'tests/vagrant_ssh_config'

# Parse IP addresses from SSH config
# Vagrant static IPs are all kinds of broken on MicroOS
ip_addresses = {}
with open(ssh_config_file, 'r') as f:
    lines = f.readlines()
    current_host = None
    for line in lines:
        if line.startswith('Host'):
            current_host = line.split(' ')[1]
        elif line.startswith('HostName'):
            ip_addresses[current_host] = line.split(' ')[1]
