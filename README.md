## Usage

To deploy the cluster:

```bash
uv run aws-k8s create --config-file cluster-config.json my-cluster
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
uv run aws-k8s delete my-cluster
```

Full usage info:

```bash
usage: aws-k8s [-h] {create,delete,list,kubeconfig} ...

Manage Kubernetes cluster on AWS

positional arguments:
  {create,delete,list,kubeconfig}
                        Available commands
    create              Create a new cluster
    delete              Delete an existing cluster
    list                List all clusters
    kubeconfig          Print path to kubeconfig file for a cluster

options:
  -h, --help            show this help message and exit
```

## GPU Support

The NVIDIA GPU drivers and container toolkit are installed on GPU worker nodes automatically by cloud-init (via `user-data-worker.sh`).

You still want to install the NVIDIA device plugin for Kubernetes:

```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.1/deployments/static/nvidia-device-plugin.yml
```

and then make sure the GPU resources are available:

```bash
kubectl get nodes "-o=custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu"
```

You can test that everything is working by running a GPU workload, for example this CUDA vector addition sample:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-pod
spec:
  restartPolicy: Never
  containers:
    - name: cuda-container
      image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0
      resources:
        limits:
          # Request 1 GPU - this tells Kubernetes to allocate a GPU from the node
          # and makes it available to the container via NVIDIA Container Runtime
          # NOTE: GPU allocation is EXCLUSIVE - once this pod gets the GPU, no other
          # pod can use it until this pod is deleted. Other pods requesting GPUs will
          # remain pending (starved) if all GPUs are allocated.
          nvidia.com/gpu: 1
  tolerations:
  # Allow scheduling on nodes with nvidia.com/gpu taint
  # GPU nodes are tainted to prevent non-GPU workloads from being scheduled on them
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

To schedule a pod with GPU resources, you need to:
1. Specify the GPU resource limit (`nvidia.com/gpu`) - this requests GPU allocation from Kubernetes
2. Add tolerations for the `nvidia.com/gpu` taint - GPU nodes are tainted to ensure only GPU-aware workloads run on them

**Important**: GPU allocation is exclusive by default. A pod requesting `nvidia.com/gpu: 1` will get exclusive access to one entire GPU. Other pods requesting GPUs will remain in Pending state until a GPU becomes available. To enable GPU sharing between multiple pods, you would need to configure additional features like NVIDIA MPS, MIG, or time-slicing in the device plugin configuration.

## Development

### Setting up pre-commit hooks

This project uses pre-commit hooks to ensure code quality and consistency. To set up the hooks for local development:

```bash
# Install development dependencies
uv sync --dev

# Install the pre-commit hooks
uv run prek install
```

The hooks will now run automatically on every commit, performing:
- Code linting and auto-fixes with Ruff
- Code formatting with Ruff
- Trailing whitespace removal
- End-of-file newline checks
- YAML syntax validation

You can also run the hooks manually on all files:

```bash
uv run prek run --all-files
```

---

Copyright 2026 Gauthier Jolly
