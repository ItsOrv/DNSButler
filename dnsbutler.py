#!/usr/bin/env python3
import os
import sys
import argparse
import yaml
import docker
import requests
import subprocess
from pathlib import Path
from typing import Dict, List

class DNSServerManager:
    def __init__(self):
        self.client = None
        self.config = {
            'dns_records': {},
            'docker_image': 'ubuntu/bind9:latest',
            'volume_path': '/etc/bind'
        }
        self.public_ip = None

    class DNSConfigError(Exception):
        """Base class for DNS configuration errors"""
        pass

    class DockerError(Exception):
        """Docker-related errors"""
        pass

    def _run_command(self, cmd: str):
        """Helper method to run shell commands with error handling"""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                check=True,
                capture_output=True,
                text=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise self.DNSConfigError(
                f"Command failed: {e.cmd}\nError: {e.stderr}"
            ) from e

    def _get_public_ip(self) -> str:
        """Get public IP with multiple fallback methods"""
        try:
            return requests.get('https://api.ipify.org', timeout=3).text
        except requests.exceptions.RequestException:
            try:
                return self._run_command(
                    "curl -4 ifconfig.co 2>/dev/null || hostname -I | awk '{print $1}'"
                )
            except self.DNSConfigError:
                raise self.DNSConfigError("Failed to determine public IP address")

    def _setup_docker(self):
        """Install and configure Docker"""
        try:
            self._run_command("docker ps")  # Test Docker connection
        except self.DNSConfigError:
            try:
                print("Installing Docker...")
                self._run_command("curl -fsSL https://get.docker.com | sh")
                self._run_command("systemctl enable --now docker")
            except self.DNSConfigError as e:
                raise self.DockerError(f"Docker installation failed: {str(e)}")

    def _pull_bind_image(self):
        """Pull Docker image with retries"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.client.images.pull(self.config['docker_image'])
                return
            except docker.errors.ImageNotFound:
                raise self.DockerError(f"Image {self.config['docker_image']} not found")
            except docker.errors.APIError as e:
                if attempt == max_retries - 1:
                    raise self.DockerError(f"Failed to pull image: {str(e)}")
                print(f"Retrying image pull ({attempt+1}/{max_retries})...")

    def _generate_zone_files(self):
        """Generate BIND configuration files"""
        try:
            config_dir = Path(self.config['volume_path'])
            config_dir.mkdir(parents=True, exist_ok=True)

            # Generate named.conf
            named_conf = f"""
options {{
    directory "/etc/bind";
    listen-on port 53 {{ any; }};
    allow-query {{ any; }};
    recursion no;
}};

{self._generate_zone_entries()}
            """
            (config_dir / "named.conf").write_text(named_conf)

            # Generate zone files
            for domain in self.config['dns_records']:
                zone_content = self._generate_zone_file(domain)
                (config_dir / f"db.{domain}").write_text(zone_content)

        except PermissionError as e:
            raise self.DNSConfigError(f"Permission denied: {str(e)}")
        except OSError as e:
            raise self.DNSConfigError(f"File system error: {str(e)}")

    def _generate_zone_entries(self) -> str:
        """Generate zone entries for named.conf"""
        return "\n".join(
            f'zone "{domain}" {{ type master; file "/etc/bind/db.{domain}"; }};'
            for domain in self.config['dns_records']
        )

    def _generate_zone_file(self, domain: str) -> str:
        """Generate individual zone file"""
        records = "\n".join(
            f"{record.ljust(30)} IN A {ip}"
            for record, ip in self.config['dns_records'][domain].items()
        )
        return f"""; Zone file for {domain}
$TTL 86400
@ IN SOA ns.{domain}. admin.{domain}. (
    {self._get_serial()}
    3600
    900
    604800
    86400 )

@       IN NS  ns.{domain}.
ns      IN A   {self.public_ip}

{records}
"""

    def _get_serial(self) -> int:
        """Generate zone serial from timestamp"""
        from datetime import datetime
        now = datetime.utcnow()
        return int(now.strftime("%Y%m%d%H"))

    def start(self, records: Dict[str, str]):
        """Main entry point to start DNS server"""
        try:
            self.public_ip = self._get_public_ip()
            print(f"Public IP detected: {self.public_ip}")
            
            self._setup_docker()
            self.client = docker.from_env()
            self._pull_bind_image()
            
            # Organize records by domain
            for record, ip in records.items():
                domain = ".".join(record.split(".")[-2:])
                self.config['dns_records'].setdefault(domain, {})[record] = ip
            
            self._generate_zone_files()
            self._start_container()
            
            return self.public_ip

        except (self.DNSConfigError, self.DockerError) as e:
            print(f"\n❌ Error: {str(e)}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nOperation cancelled by user", file=sys.stderr)
            sys.exit(130)

    def _start_container(self):
        """Start and configure Docker container"""
        try:
            # Cleanup existing container if exists
            try:
                old_container = self.client.containers.get("dns-server")
                old_container.remove(force=True)
            except docker.errors.NotFound:
                pass

            # Start new container
            self.client.containers.run(
                self.config['docker_image'],
                ports={'53/tcp': 53, '53/udp': 53},
                volumes={self.config['volume_path']: {'bind': '/etc/bind', 'mode': 'rw'}},
                detach=True,
                name="dns-server",
                restart_policy={"Name": "always"},
                network_mode="host"
            )
        except docker.errors.APIError as e:
            raise self.DockerError(f"Docker API error: {str(e)}")

def main():
    parser = argparse.ArgumentParser(
        description="AutoDNS - Automated DNS Server Setup",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        'records',
        nargs='+',
        metavar='RECORD=IP',
        help="DNS records in format 'hostname=ip'\nExample: chat.mistral.ai=198.255.15.6"
    )
    
    try:
        args = parser.parse_args()
        records = {}
        for entry in args.records:
            if '=' not in entry:
                raise ValueError(f"Invalid record format: {entry}")
            host, ip = entry.split('=', 1)
            records[host.strip()] = ip.strip()
        
        manager = DNSServerManager()
        dns_ip = manager.start(records)
        
        print("\n✅ DNS Server Successfully Configured!")
        print(f"DNS Server Address: {dns_ip}")
        print("Test Command:")
        print(f"dig @{dns_ip} {list(records.keys())[0]} +short")

    except ValueError as e:
        print(f"❌ Invalid input: {str(e)}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
