#!/bin/bash -eux
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

KUBE_VERSION="v1.35"
NVIDIA_DRIVER_VERSION="580"

export DEBIAN_FRONTEND=noninteractive
apt-get update

swapoff -a

modprobe overlay
modprobe br_netfilter

tee /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF

tee /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sysctl --system

apt -y install curl gnupg apt-transport-https ca-certificates software-properties-common

# install containerd
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
tee /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable
EOF

apt update
apt -y install containerd.io

# Detect and setup NVMe instance store if present
NVME_DEVICE=""
for device in /dev/nvme*n1; do
    if [ -b "$device" ] && [ "$device" != "/dev/nvme0n1" ]; then
        # Found a non-root NVMe device (instance store)
        NVME_DEVICE="$device"
        break
    fi
done

if [ -n "$NVME_DEVICE" ]; then
    echo "Found NVMe instance store: $NVME_DEVICE"

    # Format the device
    mkfs.ext4 -F "$NVME_DEVICE"

    # Create mount point and mount
    mkdir -p /mnt/instance-store
    mount "$NVME_DEVICE" /mnt/instance-store

    # Add to fstab for persistence across reboots (though instance store is ephemeral)
    echo "$NVME_DEVICE /mnt/instance-store ext4 defaults,nofail 0 2" >> /etc/fstab

    # Setup containerd to use instance store
    mkdir -p /mnt/instance-store/containerd
    systemctl stop containerd

    # Move existing containerd data if any
    if [ -d /var/lib/containerd ]; then
        mv /var/lib/containerd/* /mnt/instance-store/containerd/ 2>/dev/null || true
        rm -rf /var/lib/containerd
    fi

    # Create symlink
    ln -s /mnt/instance-store/containerd /var/lib/containerd

    echo "Configured containerd to use instance store at /mnt/instance-store/containerd"
fi

# configure containerd to use systemd cgroup driver

containerd config default | tee /etc/containerd/config.toml
sed -i 's/SystemdCgroup \= false/SystemdCgroup \= true/' /etc/containerd/config.toml
systemctl restart containerd

# install nvidia drivers and container toolkit if NVIDIA GPU is present
if lspci | grep -i nvidia; then
    # We pin to the specific kernel version to avoid
    # installing a newer kernel and having to reboot
    apt install -y \
        "linux-headers-$(uname -r)" \
        "linux-modules-nvidia-$NVIDIA_DRIVER_VERSION-server-$(uname -r)" \
        nvidia-utils-$NVIDIA_DRIVER_VERSION-server \
        curl \
        gnupg

    mkdir -p /etc/apt/keyrings

    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor > /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed "s#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" > /etc/apt/sources.list.d/nvidia-container-toolkit.list

    apt update
    apt -y install nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=containerd --nvidia-set-as-default
    systemctl restart containerd
fi

# install kubeadm, kubelet and kubectl
curl -fsSL https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes-archive-keyring.gpg
tee /etc/apt/sources.list.d/kubernetes.list <<EOF
deb [arch=amd64 signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/ /
EOF

apt update
apt -y install kubelet kubeadm kubectl
