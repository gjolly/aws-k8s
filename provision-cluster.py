#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "boto3>=1.42.36",
#     "paramiko>=4.0.0",
# ]
# ///

import boto3
import json
import time
import paramiko
import sys
import argparse
import os
import base64
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration files
DEFAULT_CONFIG_FILE = 'cluster-config.json'
RESOURCE_FILE = 'cluster-resources.json'

def load_config(config_file):
    """Load configuration from JSON file"""
    if not os.path.exists(config_file):
        print(f"Error: {config_file} not found")
        sys.exit(1)
    
    with open(config_file, 'r') as f:
        config = json.load(f)
    
    return config

def create_vpc_resources(ec2, region, vpc_cidr_block, allowed_ingress, existing_resources=None):
    """Create VPC, subnet, internet gateway, and security group"""
    # Check if VPC resources already exist
    if existing_resources and 'vpc_id' in existing_resources and 'subnet_id' in existing_resources and 'security_group_id' in existing_resources:
        print("VPC resources already exist, skipping creation...")
        return existing_resources['vpc_id'], existing_resources['subnet_id'], existing_resources['security_group_id']
    
    print("Creating VPC resources...")
    
    # Get default VPC
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])
    vpc_id = vpcs['Vpcs'][0]['VpcId']
    
    # Create subnet
    subnet = ec2.create_subnet(
        VpcId=vpc_id,
        CidrBlock=vpc_cidr_block,
        AvailabilityZone=f'{region}a'
    )
    subnet_id = subnet['Subnet']['SubnetId']
    print(f"Created subnet: {subnet_id}")
    
    # Enable auto-assign public IP on subnet
    ec2.modify_subnet_attribute(
        SubnetId=subnet_id,
        MapPublicIpOnLaunch={'Value': True}
    )
    print("Enabled auto-assign public IP on subnet")
    
    # Create security group
    sg = ec2.create_security_group(
        GroupName=f'k8s-cluster-{int(time.time())}',
        Description='Security group for Kubernetes cluster',
        VpcId=vpc_id
    )
    sg_id = sg['GroupId']
    print(f"Created security group: {sg_id}")
    
    # Add security group rules
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {'IpProtocol': '-1', 'FromPort': -1, 'ToPort': -1, 
             'UserIdGroupPairs': [{'GroupId': sg_id}]},  # All traffic within SG
            {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 
             'IpRanges': [{'CidrIp': allowed_ingress}]},  # SSH
            {'IpProtocol': 'tcp', 'FromPort': 6443, 'ToPort': 6443, 
             'IpRanges': [{'CidrIp': allowed_ingress}]},  # K8s API
        ]
    )
    
    return vpc_id, subnet_id, sg_id

def read_user_data(filename):
    """Read user data script"""
    try:
        with open(filename, 'r') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: {filename} not found, using empty user data")
        return ""

def get_ami_id(ssm, ami_ssm_parameter):
    """Get AMI ID from SSM parameter"""
    print(f"Fetching AMI ID from SSM...")
    response = ssm.get_parameter(Name=ami_ssm_parameter)
    ami_id = response['Parameter']['Value']
    print(f"Using AMI: {ami_id}")
    return ami_id

def launch_spot_instance(ec2, name, instance_type, subnet_id, sg_id, user_data, ami_id, key_name):
    """Launch a spot instance"""
    print(f"Launching {name} ({instance_type})...")
    
    # Base64 encode user data
    user_data_encoded = base64.b64encode(user_data.encode('utf-8')).decode('utf-8')
    
    response = ec2.request_spot_instances(
        SpotPrice='1.0',  # Max price per hour
        InstanceCount=1,
        Type='one-time',
        LaunchSpecification={
            'ImageId': ami_id,
            'InstanceType': instance_type,
            'KeyName': key_name,
            'SubnetId': subnet_id,
            'SecurityGroupIds': [sg_id],
            'UserData': user_data_encoded,
        }
    )
    
    spot_request_id = response['SpotInstanceRequests'][0]['SpotInstanceRequestId']
    print(f"Spot request created: {spot_request_id}")
    
    # Wait for spot request to be fulfilled
    print("Waiting for spot request fulfillment...")
    while True:
        requests = ec2.describe_spot_instance_requests(
            SpotInstanceRequestIds=[spot_request_id]
        )
        status = requests['SpotInstanceRequests'][0]['Status']['Code']
        
        if status == 'fulfilled':
            instance_id = requests['SpotInstanceRequests'][0]['InstanceId']
            print(f"Instance {instance_id} launched")
            break
        elif status in ['price-too-low', 'canceled-before-fulfillment', 'bad-parameters']:
            print(f"Spot request failed: {status}")
            sys.exit(1)
        
        time.sleep(5)
    
    # Wait for instance to be running
    print("Waiting for instance to be running...")
    waiter = ec2.get_waiter('instance_running')
    waiter.wait(InstanceIds=[instance_id])
    
    # Tag the instance
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[{'Key': 'Name', 'Value': name}]
    )
    print(f"Tagged instance {instance_id} with name {name}")
    
    # Get instance details and wait for public IP assignment
    print("Waiting for public IP assignment...")
    public_ip = None
    for attempt in range(30):  # Try for up to 30 seconds
        instances = ec2.describe_instances(InstanceIds=[instance_id])
        instance = instances['Reservations'][0]['Instances'][0]
        public_ip = instance.get('PublicIpAddress')
        
        if public_ip:
            print(f"Public IP assigned: {public_ip}")
            break
        
        time.sleep(1)
    
    if not public_ip:
        print(f"Warning: No public IP assigned to {instance_id}")
    
    return {
        'spot_request_id': spot_request_id,
        'instance_id': instance_id,
        'public_ip': public_ip,
        'private_ip': instance.get('PrivateIpAddress')
    }

def wait_for_ssh(host, key_path, timeout=300):
    """Wait for SSH to become available"""
    print(f"Waiting for SSH on {host}...")
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username='ubuntu', key_filename=key_path, timeout=5)
            ssh.close()
            print(f"SSH available on {host}")
            return True
        except Exception:
            time.sleep(5)
    
    return False

def wait_for_cloud_init(host, key_path):
    """Wait for cloud-init to complete"""
    print(f"Waiting for cloud-init on {host}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username='ubuntu', key_filename=key_path)
    
    # Use --wait flag to block until cloud-init is done
    stdin, stdout, stderr = ssh.exec_command('cloud-init status --wait')
    stdout.channel.recv_exit_status()  # Wait for command to complete
    
    print(f"Cloud-init completed on {host}")
    ssh.close()

def get_join_command(main_ip, key_path):
    """Get kubeadm join command from main node"""
    print("Getting kubeadm join command...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(main_ip, username='ubuntu', key_filename=key_path)
    
    stdin, stdout, stderr = ssh.exec_command('sudo kubeadm token create --print-join-command')
    join_command = stdout.read().decode().strip()
    
    ssh.close()
    return join_command

def join_worker_to_cluster(worker_ip, key_path, join_command):
    """Join worker node to the cluster"""
    print(f"Joining worker {worker_ip} to cluster...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(worker_ip, username='ubuntu', key_filename=key_path)
    
    stdin, stdout, stderr = ssh.exec_command(f'sudo {join_command}')
    stdout.channel.recv_exit_status()  # Wait for command to complete
    
    print(f"Worker {worker_ip} joined successfully")
    ssh.close()

def load_resources():
    """Load existing resources from JSON file if it exists"""
    if os.path.exists(RESOURCE_FILE):
        with open(RESOURCE_FILE, 'r') as f:
            resources = json.load(f)
        print(f"Loaded existing resources from {RESOURCE_FILE}")
        return resources
    return None

def save_resources(resources):
    """Save resource IDs to JSON file"""
    with open(RESOURCE_FILE, 'w') as f:
        json.dump(resources, f, indent=2)
    print(f"Resources saved to {RESOURCE_FILE}")

def download_kubeconfig(main_ip, key_path, output_file='kubeconfig'):
    """Download and configure kubeconfig from main node"""
    print("Downloading kubeconfig...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(main_ip, username='ubuntu', key_filename=key_path)
    
    # Get kubeconfig from main node
    stdin, stdout, stderr = ssh.exec_command('sudo cat /etc/kubernetes/admin.conf')
    kubeconfig = stdout.read().decode()
    
    ssh.close()
    
    # Replace internal IP with public IP using regex
    kubeconfig = re.sub(r'https://[0-9.]+:6443', f'https://{main_ip}:6443', kubeconfig)
    
    # Save to file
    with open(output_file, 'w') as f:
        f.write(kubeconfig)
    
    print(f"Kubeconfig saved to {output_file}")
    print(f"You can now use: export KUBECONFIG={output_file}")
    
    return output_file

def create_cluster(config_file):
    """Create a new Kubernetes cluster"""
    # Load configuration
    config = load_config(config_file)
    region = config['region']
    ami_ssm_parameter = config['ami_ssm_parameter']
    allowed_ingress = config['allowed_ingress']
    key_name = config['key_name']
    key_path = config['key_path']
    vpc_cidr_block = config['vpc_cidr_block']
    main_instance_type = config['main_instance_type']
    worker_instance_type = config['worker_instance_type']
    gpu_instance_type = config['gpu_instance_type']
    num_gpu_workers = config['num_gpu_workers']
    num_cpu_workers = config['num_cpu_workers']
    
    ec2 = boto3.client('ec2', region_name=region)
    ssm = boto3.client('ssm', region_name=region)
    
    # Load existing resources if available
    resources = load_resources()
    if resources is None:
        resources = {
            'created_at': datetime.now().isoformat(),
            'region': region
        }
    
    # Get AMI ID from SSM
    ami_id = get_ami_id(ssm, ami_ssm_parameter)
    
    # Create VPC resources (skip if already exist)
    vpc_id, subnet_id, sg_id = create_vpc_resources(ec2, region, vpc_cidr_block, allowed_ingress, resources)
    resources['vpc_id'] = vpc_id
    resources['subnet_id'] = subnet_id
    resources['security_group_id'] = sg_id
    save_resources(resources)  # Save after VPC creation
    
    # Read user data scripts
    main_user_data = read_user_data('user-data-main.sh')
    worker_user_data = read_user_data('user-data-worker.sh')
    
    # Launch instances in parallel (skip already created ones)
    print("Launching instances...")
    total_workers = num_gpu_workers + num_cpu_workers
    max_workers = total_workers + 1  # +1 for main node
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        
        # Launch main node if not exists
        if 'main_node' not in resources:
            futures[executor.submit(launch_spot_instance, ec2, 'k8s-main', main_instance_type, subnet_id, sg_id, main_user_data, ami_id, key_name)] = 'main_node'
        else:
            print("Main node already exists, skipping...")
        
        # Launch GPU workers
        for i in range(num_gpu_workers):
            node_key = f'gpu_worker_{i}'
            if node_key not in resources:
                name = f'k8s-gpu-worker-{i+1}' if num_gpu_workers > 1 else 'k8s-gpu-worker'
                futures[executor.submit(launch_spot_instance, ec2, name, gpu_instance_type, subnet_id, sg_id, worker_user_data, ami_id, key_name)] = node_key
            else:
                print(f"GPU worker {i+1} already exists, skipping...")
        
        # Launch CPU workers
        for i in range(num_cpu_workers):
            node_key = f'cpu_worker_{i}'
            if node_key not in resources:
                name = f'k8s-cpu-worker-{i+1}' if num_cpu_workers > 1 else 'k8s-cpu-worker'
                futures[executor.submit(launch_spot_instance, ec2, name, worker_instance_type, subnet_id, sg_id, worker_user_data, ami_id, key_name)] = node_key
            else:
                print(f"CPU worker {i+1} already exists, skipping...")
        
        for future in as_completed(futures):
            node_name = futures[future]
            try:
                resources[node_name] = future.result()
                save_resources(resources)  # Save after each instance is created
            except Exception as e:
                print(f"Failed to launch {node_name}: {e}")
                sys.exit(1)
    
    main_node = resources['main_node']
    
    # Wait for main node to be ready
    wait_for_ssh(main_node['public_ip'], key_path)
    wait_for_cloud_init(main_node['public_ip'], key_path)
    
    # Get join command
    join_command = get_join_command(main_node['public_ip'], key_path)
    
    # Wait for workers and join them
    workers = []
    for i in range(num_gpu_workers):
        workers.append((f'GPU worker {i+1}', resources[f'gpu_worker_{i}'], f'gpu_worker_{i}_joined'))
    for i in range(num_cpu_workers):
        workers.append((f'CPU worker {i+1}', resources[f'cpu_worker_{i}'], f'cpu_worker_{i}_joined'))
    
    for worker_name, worker, joined_key in workers:
        if not resources.get(joined_key, False):
            wait_for_ssh(worker['public_ip'], key_path)
            wait_for_cloud_init(worker['public_ip'], key_path)
            join_worker_to_cluster(worker['public_ip'], key_path, join_command)
            resources[joined_key] = True
            save_resources(resources)  # Save after each worker joins
        else:
            print(f"{worker_name} already joined, skipping...")
    
    # Download kubeconfig if not already done
    if 'kubeconfig_file' not in resources:
        kubeconfig_file = download_kubeconfig(main_node['public_ip'], key_path)
        resources['kubeconfig_file'] = kubeconfig_file
        save_resources(resources)
    else:
        print("Kubeconfig already downloaded, skipping...")
    
    print("\nCluster provisioned successfully!")
    print(f"Main node: {main_node['public_ip']}")
    for i in range(num_gpu_workers):
        print(f"GPU worker {i+1}: {resources[f'gpu_worker_{i}']['public_ip']}")
    for i in range(num_cpu_workers):
        print(f"CPU worker {i+1}: {resources[f'cpu_worker_{i}']['public_ip']}")

def delete_cluster():
    """Delete the Kubernetes cluster and all associated resources"""
    if not os.path.exists(RESOURCE_FILE):
        print(f"Error: {RESOURCE_FILE} not found. Nothing to delete.")
        sys.exit(1)
    
    # Load resources
    with open(RESOURCE_FILE, 'r') as f:
        resources = json.load(f)
    
    # Get region from resources or config
    region = resources.get('region')
    if not region:
        print("Error: Region not found in resources. Using region from config.")
        sys.exit(1)
    
    ec2 = boto3.client('ec2', region_name=region)
    
    print("Deleting cluster resources...")
    
    # Terminate all instances without clean shutdown
    instance_ids = []
    spot_request_ids = []
    
    # Collect instance and spot request IDs
    for key, value in resources.items():
        if isinstance(value, dict) and 'instance_id' in value:
            instance_ids.append(value['instance_id'])
            if 'spot_request_id' in value:
                spot_request_ids.append(value['spot_request_id'])
    
    # Terminate instances
    if instance_ids:
        print(f"Terminating instances: {', '.join(instance_ids)}")
        ec2.terminate_instances(InstanceIds=instance_ids)
        print("Waiting for instances to terminate...")
        waiter = ec2.get_waiter('instance_terminated')
        waiter.wait(InstanceIds=instance_ids)
        print("Instances terminated")
    
    # Cancel spot requests
    if spot_request_ids:
        print(f"Canceling spot requests: {', '.join(spot_request_ids)}")
        ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=spot_request_ids)
        print("Spot requests canceled")
    
    # Delete security group
    if 'security_group_id' in resources:
        sg_id = resources['security_group_id']
        print(f"Deleting security group: {sg_id}")
        try:
            ec2.delete_security_group(GroupId=sg_id)
            print("Security group deleted")
        except Exception as e:
            print(f"Warning: Could not delete security group: {e}")
    
    # Delete subnet
    if 'subnet_id' in resources:
        subnet_id = resources['subnet_id']
        print(f"Deleting subnet: {subnet_id}")
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
            print("Subnet deleted")
        except Exception as e:
            print(f"Warning: Could not delete subnet: {e}")
    
    # Delete kubeconfig file if it exists
    if 'kubeconfig_file' in resources:
        kubeconfig = resources['kubeconfig_file']
        if os.path.exists(kubeconfig):
            os.remove(kubeconfig)
            print(f"Deleted {kubeconfig}")
    
    # Delete resource file
    if os.path.exists(RESOURCE_FILE):
        os.remove(RESOURCE_FILE)
        print(f"Deleted {RESOURCE_FILE}")
    
    print("\nCluster deleted successfully!")

def main():
    parser = argparse.ArgumentParser(
        description='Manage Kubernetes cluster on AWS',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        'action',
        choices=['create', 'delete'],
        help='Action to perform: create a new cluster or delete existing cluster'
    )

    parser.add_argument(
        '--config',
        default=DEFAULT_CONFIG_FILE,
        help=f'Path to configuration file (default: {DEFAULT_CONFIG_FILE})'
    )    
    args = parser.parse_args()
    
    if args.action == 'create':
        create_cluster(args.config)
    elif args.action == 'delete':
        delete_cluster()

if __name__ == '__main__':
    main()