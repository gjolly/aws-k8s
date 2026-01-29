#!/usr/bin/env python3
# Copyright 2026 Gauthier Jolly
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib.resources import files
from pathlib import Path

import boto3
import paramiko

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# Configuration files
DEFAULT_CONFIG_FILE = "cluster-config.json"
RESOURCE_FILE = "cluster-resources.json"
KUBECONFIG_FILE = "kubeconfig"


def get_data_dir():
    """Get the data directory following XDG Base Directory specification"""
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "aws-k8s"
    return Path.home() / ".local" / "share" / "aws-k8s"


def get_cluster_dir(cluster_name):
    """Get the directory for a specific cluster"""
    cluster_dir = get_data_dir() / cluster_name
    return cluster_dir


def ensure_cluster_dir(cluster_name):
    """Ensure the cluster directory exists"""
    cluster_dir = get_cluster_dir(cluster_name)
    cluster_dir.mkdir(parents=True, exist_ok=True)
    return cluster_dir


def list_clusters():
    """List all available clusters"""
    data_dir = get_data_dir()
    if not data_dir.exists():
        return []

    clusters = []
    for item in data_dir.iterdir():
        if item.is_dir():
            resource_file = item / RESOURCE_FILE
            if resource_file.exists():
                clusters.append(item.name)
    return clusters


def load_config(config_file):
    """Load configuration from JSON file"""
    if not os.path.exists(config_file):
        logger.error(f"Config file {config_file} not found")
        sys.exit(1)

    with open(config_file) as f:
        config = json.load(f)

    return config


def create_vpc_resources(ec2, region, vpc_cidr_block, allowed_ingress, existing_resources=None):
    """Create VPC, subnet, internet gateway, and security group"""
    # Check if VPC resources already exist
    if (
        existing_resources
        and "vpc_id" in existing_resources
        and "subnet_id" in existing_resources
        and "security_group_id" in existing_resources
    ):
        logger.info("VPC resources already exist, skipping creation")
        return existing_resources["vpc_id"], existing_resources["subnet_id"], existing_resources["security_group_id"]

    logger.info("Creating VPC resources")

    # Get default VPC
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    # Create subnet
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=vpc_cidr_block, AvailabilityZone=f"{region}a")
    subnet_id = subnet["Subnet"]["SubnetId"]
    logger.info(f"Created subnet: {subnet_id}")

    # Enable auto-assign public IP on subnet
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    logger.info("Enabled auto-assign public IP on subnet")

    # Create security group
    sg = ec2.create_security_group(
        GroupName=f"k8s-cluster-{int(time.time())}", Description="Security group for Kubernetes cluster", VpcId=vpc_id
    )
    sg_id = sg["GroupId"]
    logger.info(f"Created security group: {sg_id}")

    # Add security group rules
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "-1",
                "FromPort": -1,
                "ToPort": -1,
                "UserIdGroupPairs": [{"GroupId": sg_id}],
            },  # All traffic within SG
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": allowed_ingress}]},  # SSH
            {
                "IpProtocol": "tcp",
                "FromPort": 6443,
                "ToPort": 6443,
                "IpRanges": [{"CidrIp": allowed_ingress}],
            },  # K8s API
        ],
    )

    return vpc_id, subnet_id, sg_id


def read_user_data(filename):
    """Read user data script from package resources"""
    try:
        # Try to read from package resources first
        user_data_files = files("aws_k8s").joinpath("user_data")
        script_path = user_data_files.joinpath(filename)
        return script_path.read_text()
    except (FileNotFoundError, AttributeError):
        # Fall back to reading from current directory for development
        logger.warning(f"Reading {filename} from current directory")
        try:
            with open(filename) as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"{filename} not found, using empty user data")
            return ""


def get_ami_id(ssm, ami_ssm_parameter):
    """Get AMI ID from SSM parameter"""
    logger.info("Fetching AMI ID from SSM")
    response = ssm.get_parameter(Name=ami_ssm_parameter)
    ami_id = response["Parameter"]["Value"]
    logger.info(f"Using AMI: {ami_id}")
    return ami_id


def launch_spot_instance(ec2, name, instance_type, subnet_id, sg_id, user_data, ami_id, key_name):
    """Launch a spot instance"""
    logger.info(f"Launching {name} ({instance_type})")

    # Base64 encode user data
    user_data_encoded = base64.b64encode(user_data.encode("utf-8")).decode("utf-8")

    response = ec2.request_spot_instances(
        SpotPrice="1.0",  # Max price per hour
        InstanceCount=1,
        Type="one-time",
        LaunchSpecification={
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "KeyName": key_name,
            "SubnetId": subnet_id,
            "SecurityGroupIds": [sg_id],
            "UserData": user_data_encoded,
        },
    )

    spot_request_id = response["SpotInstanceRequests"][0]["SpotInstanceRequestId"]
    logger.info(f"Spot request created: {spot_request_id}")

    # Wait for spot request to be fulfilled
    logger.info("Waiting for spot request fulfillment")
    while True:
        requests = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
        status = requests["SpotInstanceRequests"][0]["Status"]["Code"]

        if status == "fulfilled":
            instance_id = requests["SpotInstanceRequests"][0]["InstanceId"]
            logger.info(f"Instance {instance_id} launched")
            break
        elif status in ["price-too-low", "canceled-before-fulfillment", "bad-parameters"]:
            logger.error(f"Spot request failed: {status}")
            sys.exit(1)

        time.sleep(5)

    # Wait for instance to be running
    logger.info("Waiting for instance to be running")
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    # Tag the instance
    ec2.create_tags(Resources=[instance_id], Tags=[{"Key": "Name", "Value": name}])
    logger.info(f"Tagged instance {instance_id} with name {name}")

    # Get instance details and wait for public IP assignment
    logger.info("Waiting for public IP assignment")
    public_ip = None
    for attempt in range(30):  # Try for up to 30 seconds
        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")

        if public_ip:
            logger.info(f"Public IP assigned: {public_ip}")
            break

        time.sleep(1)

    if not public_ip:
        logger.warning(f"No public IP assigned to {instance_id}")

    return {
        "spot_request_id": spot_request_id,
        "instance_id": instance_id,
        "public_ip": public_ip,
        "private_ip": instance.get("PrivateIpAddress"),
    }


def wait_for_ssh(host, key_path, timeout=300):
    """Wait for SSH to become available"""
    logger.info(f"Waiting for SSH on {host}")
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username="ubuntu", key_filename=key_path, timeout=5)
            ssh.close()
            logger.info(f"SSH available on {host}")
            return True
        except Exception:
            time.sleep(5)

    return False


def wait_for_cloud_init(host, key_path):
    """Wait for cloud-init to complete"""
    logger.info(f"Waiting for cloud-init on {host}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username="ubuntu", key_filename=key_path)

    # Use --wait flag to block until cloud-init is done
    stdin, stdout, stderr = ssh.exec_command("cloud-init status --wait")
    exit_status = stdout.channel.recv_exit_status()  # Wait for command to complete

    if exit_status != 0:
        error_output = stderr.read().decode().strip()
        logger.error(f"cloud-init failed on {host} with exit code {exit_status}: {error_output}")
        ssh.close()
        raise RuntimeError(f"cloud-init failed on {host}")

    # Check the actual status
    stdin, stdout, stderr = ssh.exec_command("cloud-init status")
    status_output = stdout.read().decode().strip()

    if "status: done" in status_output:
        logger.info(f"Cloud-init completed successfully on {host}")
    elif "status: error" in status_output:
        logger.error(f"Cloud-init failed on {host}: {status_output}")
        ssh.close()
        raise RuntimeError(f"Cloud-init failed on {host}")
    else:
        logger.info(f"Cloud-init finished on {host} with status: {status_output}")

    ssh.close()


def get_join_command(main_ip, key_path):
    """Get kubeadm join command from main node"""
    logger.info("Getting kubeadm join command")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(main_ip, username="ubuntu", key_filename=key_path)

    stdin, stdout, stderr = ssh.exec_command("sudo kubeadm token create --print-join-command")
    join_command = stdout.read().decode().strip()

    ssh.close()
    return join_command


def join_worker_to_cluster(worker_ip, key_path, join_command):
    """Join worker node to the cluster"""
    logger.info(f"Joining worker {worker_ip} to cluster")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(worker_ip, username="ubuntu", key_filename=key_path)

    stdin, stdout, stderr = ssh.exec_command(f"sudo {join_command}")
    stdout.channel.recv_exit_status()  # Wait for command to complete

    logger.info(f"Worker {worker_ip} joined successfully")
    ssh.close()


def load_resources(cluster_name):
    """Load existing resources from JSON file if it exists"""
    cluster_dir = get_cluster_dir(cluster_name)
    resource_file = cluster_dir / RESOURCE_FILE

    if resource_file.exists():
        with open(resource_file) as f:
            resources = json.load(f)
        logger.info(f"Loaded existing resources from {resource_file}")
        return resources
    return None


def save_resources(cluster_name, resources):
    """Save resource IDs to JSON file"""
    cluster_dir = ensure_cluster_dir(cluster_name)
    resource_file = cluster_dir / RESOURCE_FILE

    with open(resource_file, "w") as f:
        json.dump(resources, f, indent=2)
    logger.debug(f"Resources saved to {resource_file}")


def download_kubeconfig(cluster_name, main_ip, key_path):
    """Download and configure kubeconfig from main node"""
    logger.info("Downloading kubeconfig")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(main_ip, username="ubuntu", key_filename=key_path)

    # Get kubeconfig from main node
    stdin, stdout, stderr = ssh.exec_command("sudo cat /etc/kubernetes/admin.conf")
    kubeconfig = stdout.read().decode()

    ssh.close()

    # Replace internal IP with public IP using regex
    kubeconfig = re.sub(r"https://[0-9.]+:6443", f"https://{main_ip}:6443", kubeconfig)

    # Save to cluster directory
    cluster_dir = ensure_cluster_dir(cluster_name)
    output_file = cluster_dir / KUBECONFIG_FILE

    with open(output_file, "w") as f:
        f.write(kubeconfig)

    logger.info(f"Kubeconfig saved to {output_file}")
    logger.info(f"You can now use: export KUBECONFIG={output_file}")

    return str(output_file)


def create_cluster(cluster_name, config_file):
    """Create a new Kubernetes cluster"""
    # Check if cluster already exists
    existing_clusters = list_clusters()
    if cluster_name in existing_clusters:
        logger.error(f"Cluster '{cluster_name}' already exists")
        sys.exit(1)

    # Load configuration
    config = load_config(config_file)
    region = config["region"]
    ami_ssm_parameter = config["ami_ssm_parameter"]
    allowed_ingress = config["allowed_ingress"]
    key_name = config["key_name"]
    key_path = config["key_path"]
    vpc_cidr_block = config["vpc_cidr_block"]
    main_instance_type = config["main_instance_type"]
    worker_instance_type = config["worker_instance_type"]
    gpu_instance_type = config["gpu_instance_type"]
    num_gpu_workers = config["num_gpu_workers"]
    num_cpu_workers = config["num_cpu_workers"]

    ec2 = boto3.client("ec2", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    # Load existing resources if available
    resources = load_resources(cluster_name)
    if resources is None:
        resources = {"created_at": datetime.now().isoformat(), "region": region, "cluster_name": cluster_name}

    # Get AMI ID from SSM
    ami_id = get_ami_id(ssm, ami_ssm_parameter)

    # Create VPC resources (skip if already exist)
    vpc_id, subnet_id, sg_id = create_vpc_resources(ec2, region, vpc_cidr_block, allowed_ingress, resources)
    resources["vpc_id"] = vpc_id
    resources["subnet_id"] = subnet_id
    resources["security_group_id"] = sg_id
    save_resources(cluster_name, resources)  # Save after VPC creation

    # Read user data scripts
    main_user_data = read_user_data("user-data-main.sh")
    worker_user_data = read_user_data("user-data-worker.sh")

    # Launch instances in parallel (skip already created ones)
    logger.info("Launching instances")
    total_workers = num_gpu_workers + num_cpu_workers
    max_workers = total_workers + 1  # +1 for main node

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}

        # Launch main node if not exists
        if "main_node" not in resources:
            futures[
                executor.submit(
                    launch_spot_instance,
                    ec2,
                    "k8s-main",
                    main_instance_type,
                    subnet_id,
                    sg_id,
                    main_user_data,
                    ami_id,
                    key_name,
                )
            ] = "main_node"
        else:
            logger.info("Main node already exists, skipping")

        # Launch GPU workers
        for i in range(num_gpu_workers):
            node_key = f"gpu_worker_{i}"
            if node_key not in resources:
                name = f"k8s-gpu-worker-{i + 1}" if num_gpu_workers > 1 else "k8s-gpu-worker"
                futures[
                    executor.submit(
                        launch_spot_instance,
                        ec2,
                        name,
                        gpu_instance_type,
                        subnet_id,
                        sg_id,
                        worker_user_data,
                        ami_id,
                        key_name,
                    )
                ] = node_key
            else:
                logger.info(f"GPU worker {i + 1} already exists, skipping")

        # Launch CPU workers
        for i in range(num_cpu_workers):
            node_key = f"cpu_worker_{i}"
            if node_key not in resources:
                name = f"k8s-cpu-worker-{i + 1}" if num_cpu_workers > 1 else "k8s-cpu-worker"
                futures[
                    executor.submit(
                        launch_spot_instance,
                        ec2,
                        name,
                        worker_instance_type,
                        subnet_id,
                        sg_id,
                        worker_user_data,
                        ami_id,
                        key_name,
                    )
                ] = node_key
            else:
                logger.info(f"CPU worker {i + 1} already exists, skipping")

        for future in as_completed(futures):
            node_name = futures[future]
            try:
                resources[node_name] = future.result()
                save_resources(cluster_name, resources)  # Save after each instance is created
            except Exception as e:
                logger.error(f"Failed to launch {node_name}: {e}")
                sys.exit(1)

    main_node = resources["main_node"]

    # Wait for main node to be ready
    wait_for_ssh(main_node["public_ip"], key_path)
    wait_for_cloud_init(main_node["public_ip"], key_path)

    # Get join command
    join_command = get_join_command(main_node["public_ip"], key_path)

    # Wait for workers and join them
    workers = []
    for i in range(num_gpu_workers):
        workers.append((f"GPU worker {i + 1}", resources[f"gpu_worker_{i}"], f"gpu_worker_{i}_joined"))
    for i in range(num_cpu_workers):
        workers.append((f"CPU worker {i + 1}", resources[f"cpu_worker_{i}"], f"cpu_worker_{i}_joined"))

    for worker_name, worker, joined_key in workers:
        if not resources.get(joined_key, False):
            wait_for_ssh(worker["public_ip"], key_path)
            wait_for_cloud_init(worker["public_ip"], key_path)
            join_worker_to_cluster(worker["public_ip"], key_path, join_command)
            resources[joined_key] = True
            save_resources(cluster_name, resources)  # Save after each worker joins
        else:
            logger.info(f"{worker_name} already joined, skipping")

    # Download kubeconfig if not already done
    if "kubeconfig_file" not in resources:
        kubeconfig_file = download_kubeconfig(cluster_name, main_node["public_ip"], key_path)
        resources["kubeconfig_file"] = kubeconfig_file
        save_resources(cluster_name, resources)
    else:
        logger.info("Kubeconfig already downloaded, skipping")

    logger.info("Cluster provisioned successfully!")
    logger.info(f"Main node: {main_node['public_ip']}")
    for i in range(num_gpu_workers):
        logger.info(f"GPU worker {i + 1}: {resources[f'gpu_worker_{i}']['public_ip']}")
    for i in range(num_cpu_workers):
        logger.info(f"CPU worker {i + 1}: {resources[f'cpu_worker_{i}']['public_ip']}")


def delete_cluster(cluster_name):
    """Delete the Kubernetes cluster and all associated resources"""
    cluster_dir = get_cluster_dir(cluster_name)
    resource_file = cluster_dir / RESOURCE_FILE

    if not resource_file.exists():
        logger.error(f"Cluster '{cluster_name}' not found")
        sys.exit(1)

    # Load resources
    with open(resource_file) as f:
        resources = json.load(f)

    # Get region from resources or config
    region = resources.get("region")
    if not region:
        logger.error("Region not found in resources")
        sys.exit(1)

    ec2 = boto3.client("ec2", region_name=region)

    logger.info(f"Deleting cluster '{cluster_name}' resources")

    # Terminate all instances without clean shutdown
    instance_ids = []
    spot_request_ids = []

    # Collect instance and spot request IDs
    for key, value in resources.items():
        if isinstance(value, dict) and "instance_id" in value:
            instance_ids.append(value["instance_id"])
            if "spot_request_id" in value:
                spot_request_ids.append(value["spot_request_id"])

    # Terminate instances
    if instance_ids:
        logger.info(f"Terminating instances: {', '.join(instance_ids)}")
        ec2.terminate_instances(InstanceIds=instance_ids)
        logger.info("Waiting for instances to terminate")
        waiter = ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids)
        logger.info("Instances terminated")

    # Cancel spot requests
    if spot_request_ids:
        logger.info(f"Canceling spot requests: {', '.join(spot_request_ids)}")
        ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=spot_request_ids)
        logger.info("Spot requests canceled")

    # Delete security group
    if "security_group_id" in resources:
        sg_id = resources["security_group_id"]
        logger.info(f"Deleting security group: {sg_id}")
        try:
            ec2.delete_security_group(GroupId=sg_id)
            logger.info("Security group deleted")
        except Exception as e:
            logger.warning(f"Could not delete security group: {e}")

    # Delete subnet
    if "subnet_id" in resources:
        subnet_id = resources["subnet_id"]
        logger.info(f"Deleting subnet: {subnet_id}")
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
            logger.info("Subnet deleted")
        except Exception as e:
            logger.warning(f"Could not delete subnet: {e}")

    # Delete entire cluster directory (includes kubeconfig and resource file)
    import shutil

    if cluster_dir.exists():
        shutil.rmtree(cluster_dir)
        logger.info(f"Deleted cluster directory: {cluster_dir}")

    logger.info(f"Cluster '{cluster_name}' deleted successfully!")


def show_clusters():
    """Show all available clusters"""
    clusters = list_clusters()

    if not clusters:
        print("No clusters found")
        return

    print("Available clusters:")
    for cluster_name in clusters:
        cluster_dir = get_cluster_dir(cluster_name)
        resource_file = cluster_dir / RESOURCE_FILE

        with open(resource_file) as f:
            resources = json.load(f)

        created_at = resources.get("created_at", "Unknown")
        region = resources.get("region", "Unknown")
        main_ip = resources.get("main_node", {}).get("public_ip", "N/A")

        print(f"  • {cluster_name}")
        print(f"    Created: {created_at}")
        print(f"    Region: {region}")
        print(f"    Main node: {main_ip}")
        print(f"    Kubeconfig: {cluster_dir / KUBECONFIG_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Manage Kubernetes cluster on AWS", formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new cluster")
    create_parser.add_argument("name", help="Name of the cluster")
    create_parser.add_argument(
        "--config", default=DEFAULT_CONFIG_FILE, help=f"Path to configuration file (default: {DEFAULT_CONFIG_FILE})"
    )

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete an existing cluster")
    delete_parser.add_argument("name", help="Name of the cluster to delete")

    # List command
    subparsers.add_parser("list", help="List all clusters")

    # Kubeconfig printer
    kubeconfig_parser = subparsers.add_parser("kubeconfig", help="Print path to kubeconfig file for a cluster")
    kubeconfig_parser.add_argument("name", help="Name of the cluster")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "create":
        create_cluster(args.name, args.config)
    elif args.command == "delete":
        delete_cluster(args.name)
    elif args.command == "list":
        show_clusters()
    elif args.command == "kubeconfig":
        cluster_dir = get_cluster_dir(args.name)
        kubeconfig_file = cluster_dir / KUBECONFIG_FILE
        if kubeconfig_file.exists():
            print(kubeconfig_file)
        else:
            logger.error(f"Kubeconfig for cluster '{args.name}' not found")
            sys.exit(1)


if __name__ == "__main__":
    main()
