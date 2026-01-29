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
CALICO_VERSION="v3.31.3"

IMDS_TOKEN="$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 3600")"

export DEBIAN_FRONTEND=noninteractive
apt-get update

CIDR='10.100.0.0/16'
SERVICE_CIDR='10.101.0.0/16'

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

# configure containerd to use systemd cgroup driver

containerd config default | tee /etc/containerd/config.toml
sed -i 's/SystemdCgroup \= false/SystemdCgroup \= true/' /etc/containerd/config.toml
systemctl restart containerd

# isntall kubeadm, kubelet and kubectl
curl -fsSL https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes-archive-keyring.gpg
tee /etc/apt/sources.list.d/kubernetes.list <<EOF
deb [arch=amd64 signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/ /
EOF

apt update
apt -y install kubelet kubeadm kubectl

# Get the public IP from EC2 metadata service
PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)

# initialize the kubernetes cluster with public IP in certificate
kubeadm init --pod-network-cidr=$CIDR --service-cidr=$SERVICE_CIDR --apiserver-cert-extra-sans=$PUBLIC_IP
export KUBECONFIG=/etc/kubernetes/admin.conf

# install calico network plugin
kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VERSION/manifests/tigera-operator.yaml"

sleep 20

pushd /tmp
curl -O "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VERSION/manifests/custom-resources.yaml"
sed -i "s#cidr:.*#cidr: $CIDR#g" custom-resources.yaml

kubectl create -f custom-resources.yaml
rm -f /tmp/custom-resources.yaml
