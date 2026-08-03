ARG OPENSHIFT_VERSION=4.19

FROM registry.access.redhat.com/ubi9/ubi:latest

RUN dnf install -y \
    python3 \
    python3-pip \
    git \
    wget \
    tar \
    jq \
    openssl \
    unzip \
    && dnf clean all

# Ansible plus the Python libraries kubernetes.core needs (kubernetes client for
# the k8s/k8s_info modules, jsonpatch for strategic-merge patches).
RUN pip3 install --no-cache-dir \
    'ansible>=2.15' \
    'kubernetes>=27.2.0' \
    'boto3>=1.34.0' \
    'botocore>=1.34.0' \
    jsonpatch \
    jinja2 \
    pyyaml

COPY requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml

# OpenShift CLI — used for jsonpath queries, binary builds, and `oc exec` checks.
ARG OPENSHIFT_VERSION
RUN wget -q https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-${OPENSHIFT_VERSION}/openshift-client-linux.tar.gz && \
    tar -xzf openshift-client-linux.tar.gz -C /usr/local/bin/ oc kubectl && \
    rm -f openshift-client-linux.tar.gz && \
    chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# Helm — aws-privateca-issuer and cert-manager-csi-driver ship only as charts.
# Neither has a Red Hat operator equivalent, so they are installed with the
# kubernetes.core.helm module rather than an OLM Subscription.
RUN wget -q https://get.helm.sh/helm-v3.16.3-linux-amd64.tar.gz && \
    tar -xzf helm-v3.16.3-linux-amd64.tar.gz -C /tmp linux-amd64/helm && \
    mv /tmp/linux-amd64/helm /usr/local/bin/helm && \
    rm -rf helm-v3.16.3-linux-amd64.tar.gz /tmp/linux-amd64 && \
    chmod +x /usr/local/bin/helm

# AWS CLI v2 — AWS Private CA and IAM Roles Anywhere have no Ansible modules, so
# those two are driven through the CLI. IAM and S3 use amazon.aws modules.
RUN wget -q "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -O /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

RUN mkdir -p /workspace && chmod 777 /workspace
WORKDIR /workspace

ENTRYPOINT ["ansible-playbook"]
CMD ["--version"]
