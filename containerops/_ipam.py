#!/usr/bin/env python3
"""
Nebula IPAM - IP Address Management for Nebula overlay networks.
"""

import json
import os
import fcntl
import ipaddress
from pathlib import Path
from typing import Optional, Dict, List, Union
from contextlib import contextmanager
from datetime import datetime


class IPAMError(Exception):
    """Base exception for IPAM errors."""
    pass


class IPNotFoundError(IPAMError):
    """Raised when an IP allocation is not found."""
    pass


class NoAvailableIPError(IPAMError):
    """Raised when no IP addresses are available in the subnet."""
    pass


class IPConflictError(IPAMError):
    """Raised when trying to allocate an IP that's already allocated to another host."""
    pass


class NebulaIPAM:
    """
    IP Address Management for Nebula overlay networks.
    
    Stores allocation data in /etc/containerops/nebula/networks/{network_name}/ipam.json
    """
    
    def __init__(self, network_name: str, base_dir: str = "/etc/containerops/nebula/networks"):
        self.network_name = network_name
        self.base_dir = Path(base_dir)
        self.network_dir = self.base_dir / network_name
        self.ipam_file = self.network_dir / "ipam.json"
        self.lock_file = self.network_dir / ".ipam.lock"
        
        # Ensure directory exists
        self.network_dir.mkdir(parents=True, exist_ok=True)
    
    @contextmanager
    def _file_lock(self):
        """Context manager for file-based locking."""
        lock_fd = None
        try:
            # Create lock file if it doesn't exist
            self.lock_file.touch(exist_ok=True)
            
            # Open and acquire exclusive lock
            lock_fd = os.open(str(self.lock_file), os.O_RDWR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            
            yield
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
    
    def _load_data(self) -> Dict:
        """Load IPAM data from JSON file."""
        if not self.ipam_file.exists():
            return {
                "allocations": {},  # hostname -> {"ip": str, "type": "static"|"dynamic"}
                "metadata": {
                    "created": datetime.utcnow().isoformat(),
                    "modified": datetime.utcnow().isoformat(),
                    "version": "2.0"  # Version to handle migration if needed
                }
            }
        
        with open(self.ipam_file, 'r') as f:
            data = json.load(f)
            
        # Migration from v1 format (just hostname -> ip mapping)
        if "version" not in data.get("metadata", {}):
            old_allocations = data.get("allocations", {})
            new_allocations = {}
            for hostname, ip in old_allocations.items():
                new_allocations[hostname] = {"ip": ip, "type": "dynamic"}
            data["allocations"] = new_allocations
            data["metadata"]["version"] = "2.0"
        
        return data
    
    def _save_data(self, data: Dict):
        """Save IPAM data to JSON file."""
        data["metadata"]["modified"] = datetime.utcnow().isoformat()
        
        # Write to temporary file first for atomicity
        temp_file = self.ipam_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        
        # Atomic rename
        temp_file.rename(self.ipam_file)
    
    def _get_all_ips_in_subnet(self, network: ipaddress.IPv4Network) -> List[str]:
        """Get all usable host IPs in a subnet."""
        # For Nebula, we can use all IPs including network and broadcast
        # since it's an overlay network
        return [str(ip) for ip in network.hosts()]
    
    def _find_next_available_ip(self, network: ipaddress.IPv4Network, 
                               allocated_ips: set) -> Optional[str]:
        """Find the next available IP in the subnet."""
        for ip in self._get_all_ips_in_subnet(network):
            if ip not in allocated_ips:
                return ip
        return None
    
    def allocate(self, hostname: str, cidr: str, present: bool = True, 
                 ip: Optional[str] = None) -> Optional[str]:
        """
        Allocate or deallocate an IP address for a hostname.
        
        Args:
            hostname: The unique hostname (lighthouse DNS name) for the allocation
            cidr: The network CIDR (e.g., "10.42.0.0/16")
            present: True to allocate, False to deallocate
            ip: Optional specific IP to allocate (for static allocation)
        
        Returns:
            The allocated IP address (if present=True), or None (if present=False)
        
        Raises:
            IPAMError: If allocation fails
            IPConflictError: If trying to allocate an IP already assigned to another host
            NoAvailableIPError: If no IPs are available
        """
        try:
            network = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError as e:
            raise IPAMError(f"Invalid CIDR: {cidr}") from e
        
        # Validate specific IP if provided
        if ip is not None:
            try:
                ip_obj = ipaddress.ip_address(ip)
                if ip_obj not in network:
                    raise IPAMError(f"IP {ip} is not in network {cidr}")
            except ValueError as e:
                raise IPAMError(f"Invalid IP address: {ip}") from e
        
        with self._file_lock():
            data = self._load_data()
            allocations = data["allocations"]
            
            if not present:
                # Deallocate
                if hostname in allocations:
                    deallocated_ip = allocations[hostname]["ip"]
                    del allocations[hostname]
                    self._save_data(data)
                    return None
                else:
                    # Already not allocated, return None
                    return None
            
            # Check if already allocated
            if hostname in allocations:
                existing_ip = allocations[hostname]["ip"]
                existing_type = allocations[hostname]["type"]
                
                # If requesting specific IP, check if it matches existing
                if ip is not None and existing_ip != ip:
                    raise IPConflictError(
                        f"Host {hostname} already has IP {existing_ip} "
                        f"({existing_type}), cannot change to {ip}"
                    )
                
                # Verify the IP is still in the correct subnet
                if ipaddress.ip_address(existing_ip) in network:
                    return existing_ip
                else:
                    # IP is from a different subnet, need to reallocate
                    del allocations[hostname]
            
            # Build set of allocated IPs with their hostnames
            allocated_ips = {}
            for host, info in allocations.items():
                allocated_ips[info["ip"]] = host
            
            # Only consider IPs that are in the current subnet
            allocated_ips_in_subnet = {
                ip_addr: host for ip_addr, host in allocated_ips.items()
                if ipaddress.ip_address(ip_addr) in network
            }
            
            if ip is not None:
                # Static allocation requested
                if ip in allocated_ips_in_subnet:
                    conflicting_host = allocated_ips_in_subnet[ip]
                    raise IPConflictError(
                        f"IP {ip} is already allocated to host {conflicting_host}"
                    )
                
                allocations[hostname] = {"ip": ip, "type": "static"}
                self._save_data(data)
                return ip
            else:
                # Dynamic allocation
                allocated_ip_set = set(allocated_ips_in_subnet.keys())
                next_ip = self._find_next_available_ip(network, allocated_ip_set)
                
                if next_ip is None:
                    raise NoAvailableIPError(
                        f"No available IPs in {cidr} "
                        f"({len(allocated_ips_in_subnet)} allocated)"
                    )
                
                allocations[hostname] = {"ip": next_ip, "type": "dynamic"}
                self._save_data(data)
                return next_ip
    
    def get_allocation(self, hostname: str) -> Optional[Dict[str, str]]:
        """
        Get the current IP allocation for a hostname.
        
        Args:
            hostname: The hostname to look up
        
        Returns:
            Dictionary with 'ip' and 'type' keys, or None if not allocated
        """
        with self._file_lock():
            data = self._load_data()
            return data["allocations"].get(hostname)
    
    def get_ip(self, hostname: str) -> Optional[str]:
        """
        Get just the IP address for a hostname.
        
        Args:
            hostname: The hostname to look up
        
        Returns:
            The allocated IP address, or None if not allocated
        """
        allocation = self.get_allocation(hostname)
        return allocation["ip"] if allocation else None
    
    def list_allocations(self) -> Dict[str, Dict[str, str]]:
        """
        List all current allocations.
        
        Returns:
            Dictionary mapping hostnames to allocation info (ip and type)
        """
        with self._file_lock():
            data = self._load_data()
            return data["allocations"].copy()
    
    def list_ips(self) -> Dict[str, str]:
        """
        List all current allocations as a simple hostname->ip mapping.
        
        Returns:
            Dictionary mapping hostnames to IP addresses
        """
        allocations = self.list_allocations()
        return {host: info["ip"] for host, info in allocations.items()}
    
    def cleanup_subnet(self, cidr: str) -> List[str]:
        """
        Remove all allocations that are not in the specified subnet.
        Useful when changing network configuration.
        
        Args:
            cidr: The network CIDR to keep allocations for
        
        Returns:
            List of removed hostnames
        """
        try:
            network = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError as e:
            raise IPAMError(f"Invalid CIDR: {cidr}") from e
        
        with self._file_lock():
            data = self._load_data()
            allocations = data["allocations"]
            removed = []
            
            # Find allocations outside the subnet
            for hostname, info in list(allocations.items()):
                try:
                    if ipaddress.ip_address(info["ip"]) not in network:
                        del allocations[hostname]
                        removed.append(hostname)
                except ValueError:
                    # Invalid IP, remove it
                    del allocations[hostname]
                    removed.append(hostname)
            
            if removed:
                self._save_data(data)
            
            return removed


# Convenience function for single operations
def allocate_ip(network_name: str, hostname: str, cidr: str, 
                present: bool = True, ip: Optional[str] = None,
                base_dir: str = "/etc/containerops/nebula/networks") -> Optional[str]:
    """
    Convenience function to allocate/deallocate an IP address.
    
    Args:
        network_name: Name of the Nebula network
        hostname: The unique hostname for the allocation
        cidr: The network CIDR (e.g., "10.42.0.0/16")
        present: True to allocate, False to deallocate
        ip: Optional specific IP to allocate (for static allocation)
        base_dir: Base directory for network configurations
    
    Returns:
        The allocated IP address (if present=True), or None (if present=False)
    """
    ipam = NebulaIPAM(network_name, base_dir)
    return ipam.allocate(hostname, cidr, present, ip)
