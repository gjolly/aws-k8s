## Usage

To deploy the cluster:

```bash
uv run provision-cluster.py create --config cluster-config.json
```

the cluster config file is like this:

```json
{
  "region": "eu-south-2",
  "ami_ssm_parameter": "/aws/service/canonical/ubuntu/server/noble/stable/current/amd64/hvm/ebs-gp3/ami-id",
  "allowed_ingress": "YOU_IP/32",
  "key_name": "YOUR_KEY_NAME",
  "key_path": "PATH_TO_YOU_PRIVATE_KEY",
  "vpc_cidr_block": "172.31.128.0/20",
  "main_instance_type": "t3.medium",
  "worker_instance_type": "t3.small",
  "gpu_instance_type": "g6.xlarge",
  "num_gpu_workers": 0,
  "num_cpu_workers": 2
}
```

Cluster info is stored in `cluster-resources.json` after creation.

To delete the cluster:

```bash
uv run provision-cluster.py delete
```
