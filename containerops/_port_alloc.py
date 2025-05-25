#!/usr/bin/env python3
"""
Nebula Port Allocator - Port allocation management for Nebula underlay traffic.
"""

import json
import os
import fcntl
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Union
from contextlib import contextmanager
from datetime import datetime


class PortAllocatorError(Exception):
    """Base exception for Port Allocator errors."""
    pass


class PortNotFoundError(PortAllocatorError):
    """Raised when a port allocation is not found."""
    pass


class NoAvailablePortError(PortAllocatorError):
    """Raised when no ports are available in the range."""
    pass


class PortConflictError(PortAllocatorError):
    """Raised when trying to allocate a port that's already allocated."""
    pass


class NebulaPortAllocator:
    """
    Port allocation management for Nebula underlay traffic.
    
    Stores allocation data in /etc/containerops/nebula/networks/{network_name}/ports.json
    """
    
    # Common port ranges to avoid conflicts with system services
    SYSTEM_PORTS = range(1, 1024)  # Privileged ports
    EPHEMERAL_PORTS = range(32768, 65536)  # Often used for ephemeral connections
    
    def __init__(self, network_name: str, base_dir: str = "/etc/containerops/nebula/networks"):
        self.network_name = network_name
        self.base_dir = Path(base_dir)
        self.network_dir = self.base_dir / network_name
        self.ports_file = self.network_dir / "ports.json"
        self.lock_file = self.network_dir / ".ports.lock"
        
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
        """Load port allocation data from JSON file."""
        if not self.ports_file.exists():
            return {
                "allocations": {},  # machine_id -> {hostname -> port}
                "metadata": {
                    "created": datetime.utcnow().isoformat(),
                    "modified": datetime.utcnow().isoformat(),
                    "version": "1.0"
                }
            }
        
        with open(self.ports_file, 'r') as f:
            return json.load(f)
    
    def _save_data(self, data: Dict):
        """Save port allocation data to JSON file."""
        data["metadata"]["modified"] = datetime.utcnow().isoformat()
        
        # Write to temporary file first for atomicity
        temp_file = self.ports_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        
        # Atomic rename
        temp_file.rename(self.ports_file)
    
    def _parse_port_range(self, port_range: Union[str, Tuple[int, int]]) -> Tuple[int, int]:
        """
        Parse port range specification.
        
        Args:
            port_range: Either "min-max" string or (min, max) tuple
        
        Returns:
            Tuple of (min_port, max_port)
        """
        if isinstance(port_range, str):
            try:
                parts = port_range.split('-')
                if len(parts) != 2:
                    raise ValueError
                min_port = int(parts[0])
                max_port = int(parts[1])
            except (ValueError, IndexError):
                raise PortAllocatorError(
                    f"Invalid port range format: {port_range}. "
                    "Expected 'min-max' (e.g., '4242-4442')"
                )
        elif isinstance(port_range, (tuple, list)) and len(port_range) == 2:
            min_port, max_port = int(port_range[0]), int(port_range[1])
        else:
            raise PortAllocatorError(
                f"Invalid port range type: {type(port_range)}. "
                "Expected string 'min-max' or tuple (min, max)"
            )
        
        # Validate port range
        if not (1 <= min_port <= 65535) or not (1 <= max_port <= 65535):
            raise PortAllocatorError(
                f"Port range {min_port}-{max_port} outside valid range 1-65535"
            )
        
        if min_port > max_port:
            raise PortAllocatorError(
                f"Invalid port range: min ({min_port}) > max ({max_port})"
            )
        
        return min_port, max_port
    
    def _find_next_available_port(self, min_port: int, max_port: int, 
                                  allocated_ports: set) -> Optional[int]:
        """Find the next available port in the range."""
        for port in range(min_port, max_port + 1):
            if port not in allocated_ports:
                return port
        return None
    
    def allocate(self, machine_id: str, hostname: str, port_range: Union[str, Tuple[int, int]], 
                 present: bool = True, port: Optional[int] = None) -> Optional[int]:
        """
        Allocate or deallocate a port for a machine-endpoint combination.
        
        Args:
            machine_id: Unique identifier for the machine
            hostname: The endpoint hostname
            port_range: Available port range as "min-max" string or (min, max) tuple
            present: True to allocate, False to deallocate
            port: Optional specific port to allocate
        
        Returns:
            The allocated port (if present=True), or None (if present=False)
        
        Raises:
            PortAllocatorError: If allocation fails
            PortConflictError: If trying to allocate a port already assigned
            NoAvailablePortError: If no ports are available
        """
        min_port, max_port = self._parse_port_range(port_range)
        
        # Validate specific port if provided
        if port is not None:
            if not (min_port <= port <= max_port):
                raise PortAllocatorError(
                    f"Port {port} is outside allowed range {min_port}-{max_port}"
                )
        
        with self._file_lock():
            data = self._load_data()
            allocations = data["allocations"]
            
            # Ensure machine_id exists in allocations
            if machine_id not in allocations:
                allocations[machine_id] = {}
            
            machine_allocations = allocations[machine_id]
            
            if not present:
                # Deallocate
                if hostname in machine_allocations:
                    del machine_allocations[hostname]
                    
                    # Clean up empty machine entries
                    if not machine_allocations:
                        del allocations[machine_id]
                    
                    self._save_data(data)
                    return None
                else:
                    # Already not allocated
                    return None
            
            # Check if already allocated for this machine-endpoint
            if hostname in machine_allocations:
                existing_port = machine_allocations[hostname]
                
                # If requesting specific port, check if it matches
                if port is not None and existing_port != port:
                    raise PortConflictError(
                        f"Endpoint {hostname} on machine {machine_id} "
                        f"already has port {existing_port}, cannot change to {port}"
                    )
                
                # Verify port is still in valid range
                if min_port <= existing_port <= max_port:
                    return existing_port
                else:
                    # Port is outside new range, need to reallocate
                    del machine_allocations[hostname]
            
            # Get all allocated ports for this machine
            allocated_ports = set(machine_allocations.values())
            
            if port is not None:
                # Specific port requested
                if port in allocated_ports:
                    # Find which endpoint has this port
                    conflicting_endpoint = None
                    for endpoint, allocated_port in machine_allocations.items():
                        if allocated_port == port:
                            conflicting_endpoint = endpoint
                            break
                    
                    raise PortConflictError(
                        f"Port {port} on machine {machine_id} is already "
                        f"allocated to endpoint {conflicting_endpoint}"
                    )
                
                machine_allocations[hostname] = port
                self._save_data(data)
                return port
            else:
                # Dynamic allocation
                next_port = self._find_next_available_port(
                    min_port, max_port, allocated_ports
                )
                
                if next_port is None:
                    raise NoAvailablePortError(
                        f"No available ports in range {min_port}-{max_port} "
                        f"on machine {machine_id} ({len(allocated_ports)} allocated)"
                    )
                
                machine_allocations[hostname] = next_port
                self._save_data(data)
                return next_port
    
    def get_port(self, machine_id: str, hostname: str) -> Optional[int]:
        """
        Get the allocated port for a machine-endpoint combination.
        
        Args:
            machine_id: The machine identifier
            hostname: The endpoint hostname
        
        Returns:
            The allocated port, or None if not allocated
        """
        with self._file_lock():
            data = self._load_data()
            return data["allocations"].get(machine_id, {}).get(hostname)
    
    def list_machine_allocations(self, machine_id: str) -> Dict[str, int]:
        """
        List all port allocations for a specific machine.
        
        Args:
            machine_id: The machine identifier
        
        Returns:
            Dictionary mapping hostnames to ports
        """
        with self._file_lock():
            data = self._load_data()
            return data["allocations"].get(machine_id, {}).copy()
    
    def list_all_allocations(self) -> Dict[str, Dict[str, int]]:
        """
        List all port allocations across all machines.
        
        Returns:
            Dictionary mapping machine_ids to hostname->port mappings
        """
        with self._file_lock():
            data = self._load_data()
            return data["allocations"].copy()
    
    def find_endpoint_port(self, hostname: str) -> List[Tuple[str, int]]:
        """
        Find all machines where an endpoint is allocated and their ports.
        
        Args:
            hostname: The endpoint hostname to search for
        
        Returns:
            List of (machine_id, port) tuples
        """
        results = []
        with self._file_lock():
            data = self._load_data()
            for machine_id, endpoints in data["allocations"].items():
                if hostname in endpoints:
                    results.append((machine_id, endpoints[hostname]))
        return results
    
    def cleanup_machine(self, machine_id: str) -> List[str]:
        """
        Remove all port allocations for a specific machine.
        
        Args:
            machine_id: The machine identifier
        
        Returns:
            List of removed endpoint hostnames
        """
        with self._file_lock():
            data = self._load_data()
            allocations = data["allocations"]
            
            if machine_id in allocations:
                removed = list(allocations[machine_id].keys())
                del allocations[machine_id]
                self._save_data(data)
                return removed
            
            return []
    
    def suggest_safe_range(self, size: int = 200) -> str:
        """
        Suggest a safe port range that avoids common system ports.
        
        Args:
            size: Number of ports needed in the range
        
        Returns:
            Suggested port range as "min-max" string
        """
        # Start from a commonly safe range for custom applications
        safe_start = 4242
        safe_end = safe_start + size - 1
        
        # Ensure we don't exceed valid port range
        if safe_end > 65535:
            safe_end = 65535
            safe_start = safe_end - size + 1
        
        return f"{safe_start}-{safe_end}"


# Convenience function for single operations
def allocate_port(network_name: str, machine_id: str, hostname: str, 
                  port_range: Union[str, Tuple[int, int]], present: bool = True, 
                  port: Optional[int] = None,
                  base_dir: str = "/etc/containerops/nebula/networks") -> Optional[int]:
    """
    Convenience function to allocate/deallocate a port.
    
    Args:
        network_name: Name of the Nebula network
        machine_id: Unique identifier for the machine
        hostname: The endpoint hostname
        port_range: Available port range as "min-max" string or (min, max) tuple
        present: True to allocate, False to deallocate
        port: Optional specific port to allocate
        base_dir: Base directory for network configurations
    
    Returns:
        The allocated port (if present=True), or None (if present=False)
    """
    allocator = NebulaPortAllocator(network_name, base_dir)
    return allocator.allocate(machine_id, hostname, port_range, present, port)
