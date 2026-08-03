#!/bin/bash
# Run the ocp-onprem-aws-auth playbooks inside a Podman container.
# No local Ansible install required — everything runs in the built image and
# talks to your OpenShift cluster over the mounted KUBECONFIG and to AWS over
# your mounted ~/.aws credentials.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="localhost/ocp-onprem-aws-auth-ansible:latest"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
print_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# --- podman present? --------------------------------------------------------
if ! command -v podman &> /dev/null; then
    print_error "Podman is required but not installed"
    echo "Install podman:"
    echo "  RHEL/Fedora:   sudo dnf install -y podman"
    echo "  Ubuntu/Debian: sudo apt install -y podman"
    exit 1
fi

# --- resolve KUBECONFIG ------------------------------------------------------
# Precedence: existing $KUBECONFIG env  ->  ./kubeconfig in the repo root.
resolve_kubeconfig() {
    if [ -n "$KUBECONFIG" ] && [ -f "$KUBECONFIG" ]; then
        return 0
    fi
    if [ -f "$SCRIPT_DIR/kubeconfig" ]; then
        export KUBECONFIG="$SCRIPT_DIR/kubeconfig"
        return 0
    fi
    return 1
}

# --- resolve AWS credentials -------------------------------------------------
# Precedence: $AWS_SHARED_CREDENTIALS_FILE's directory  ->  ~/.aws.
# Mounted read-only; nothing in this project writes to it.
resolve_aws_dir() {
    if [ -n "$AWS_SHARED_CREDENTIALS_FILE" ] && [ -f "$AWS_SHARED_CREDENTIALS_FILE" ]; then
        AWS_DIR="$(cd "$(dirname "$AWS_SHARED_CREDENTIALS_FILE")" && pwd)"
        return 0
    fi
    if [ -d "$HOME/.aws" ]; then
        AWS_DIR="$HOME/.aws"
        return 0
    fi
    return 1
}

# --- build image ------------------------------------------------------------
build_image() {
    if podman image exists "$IMAGE_NAME" && [ "${FORCE_BUILD:-}" != "1" ]; then
        print_info "Ansible container image already exists ($IMAGE_NAME)"
        return 0
    fi
    print_info "Building Ansible container image..."
    cd "$SCRIPT_DIR"
    if [ ! -f "Containerfile" ]; then
        print_error "Containerfile not found in $SCRIPT_DIR"; exit 1
    fi
    local ocp_version
    ocp_version=$(grep -E '^\s*openshift_version:' "$SCRIPT_DIR/inventory/group_vars/all.yml" 2>/dev/null \
        | head -1 | sed 's/.*"\(.*\)".*/\1/' || echo "4.19")
    [ -z "$ocp_version" ] && ocp_version="4.19"
    print_info "Building with oc client version: $ocp_version"
    podman build --build-arg OPENSHIFT_VERSION="$ocp_version" -t "$IMAGE_NAME" -f Containerfile .
    print_info "Image built successfully"
}

# --- run a playbook in the container ----------------------------------------
run_ansible() {
    local playbook="$1"; shift
    local extra_args="$@"

    if [ ! -f "$SCRIPT_DIR/$playbook" ]; then
        print_error "Playbook not found: $playbook"; exit 1
    fi

    if ! resolve_kubeconfig; then
        print_error "No kubeconfig found."
        echo "Provide one of:"
        echo "  export KUBECONFIG=/path/to/kubeconfig   (recommended)"
        echo "  or copy it to $SCRIPT_DIR/kubeconfig"
        exit 1
    fi
    if ! resolve_aws_dir; then
        print_error "No AWS credentials found."
        echo "Provide one of:"
        echo "  ~/.aws/credentials  (run 'aws configure')"
        echo "  export AWS_SHARED_CREDENTIALS_FILE=/path/to/credentials"
        exit 1
    fi
    print_info "Using KUBECONFIG=$KUBECONFIG"
    print_info "Using AWS credentials from $AWS_DIR (profile: ${AWS_PROFILE:-default})"
    print_info "Running playbook: $playbook"

    local volumes=(
        # ':z' (shared), not ':Z' (private). ':Z' stamps a private SELinux
        # category on the source directory, so a SECOND container mounting the
        # same repo relabels it and the first one loses access mid-run --
        # "PermissionError: [Errno 13] Permission denied: b'/workspace'" from a
        # playbook that was working seconds earlier. Running two invocations at
        # once (a long deploy plus a quick --syntax-check, say) is normal, so the
        # mount has to tolerate it.
        "-v" "$SCRIPT_DIR:/workspace:z"
        # Mount the kubeconfig read-write at a fixed in-container path (some oc
        # flows write cache/token refreshes back to it).
        "-v" "$KUBECONFIG:/workspace/.kubeconfig:z"
        # Credentials are read-only — nothing here should ever modify them.
        #
        # ':z' (shared SELinux label), not ':Z'. ~/.aws is a directory you very
        # likely share with other containers, and ':Z' assigns a PRIVATE category
        # to it — the next container to mount it with ':Z' relabels it again and
        # silently breaks this one. ':z' is the correct choice for a shared path.
        "-v" "$AWS_DIR:/workspace/.aws:ro,z"
    )
    local env_vars=(
        "-e" "KUBECONFIG=/workspace/.kubeconfig"
        "-e" "AWS_SHARED_CREDENTIALS_FILE=/workspace/.aws/credentials"
        "-e" "AWS_CONFIG_FILE=/workspace/.aws/config"
    )
    # Let an explicitly-exported profile through; otherwise group_vars decides.
    [ -n "${AWS_PROFILE:-}" ] && env_vars+=("-e" "AWS_PROFILE=$AWS_PROFILE")

    # Use an interactive TTY only when we actually have one (real terminal).
    # In CI / automation (no TTY), -t mangles output — fall back to -i so the
    # playbook still streams cleanly.
    local tty_flags="-i"
    if [ -t 0 ] && [ -t 1 ]; then
        tty_flags="-it"
    fi

    podman run --rm $tty_flags \
        "${volumes[@]}" \
        "${env_vars[@]}" \
        --network host \
        "$IMAGE_NAME" \
        -i /workspace/inventory/hosts \
        "$playbook" \
        $extra_args
}

usage() {
    cat << EOF
Usage: $0 <command> [ansible-options]

ocp-onprem-aws-auth gives on-prem OpenShift workloads AWS API access with no
long-lived credentials, using IAM Roles Anywhere and short-lived X.509
certificates issued by cert-manager.

Three ways to do it. Pick one with -e auth_method=..., or run all three --
each deploys into its own namespace and they do not collide.

    iamra   (default)  IAM Roles Anywhere: short-lived X.509 certificate
                       exchanged for STS credentials by a sidecar.
    oidc               Self-managed IRSA: the pod's projected ServiceAccount
                       token, federated natively by the AWS SDK. No sidecar.
    vault              HashiCorp Vault's AWS secrets engine brokers the
                       credentials; the Vault agent renders them to a file.

Each method is self-contained: its own namespace, its own IAM role, its own
infrastructure. Running one neither creates nor requires anything belonging to
the other two, so any single command below is a complete deployment on its own.

Commands:
    build           Build the Ansible container image

  whole method, end to end (images -> infrastructure -> app -> verify):
    iamra           IAM Roles Anywhere
    oidc            OIDC federation (self-managed IRSA)
    vault           HashiCorp Vault AWS secrets engine
    install         Same, choosing with -e auth_method=...

  individual steps, if you want the granularity:
    images          Build the demo app image (+ sidecar, for iamra)
    iamra-setup     All iamra infrastructure
      aws             ... Private CA, trust anchor, IAM roles + profiles
      certmanager     ... Red Hat cert-manager Operator + the CSI driver
      issuer          ... bootstrap cert, aws-privateca-issuer, ClusterIssuer
    oidc-setup      Publish the cluster JWKS, register the IAM OIDC provider,
                    create the web-identity role
    vault-setup     Deploy Vault + agent injector, configure the AWS secrets
                    engine and Kubernetes auth

  common (these REQUIRE -e auth_method=... so they cannot act on the wrong one):
    app             Deploy the demo application
    validate        End-to-end verification from inside the pod
    destroy         Tear down (use with --destroy to confirm)

    run <playbook>  Run an arbitrary playbook in the repo
    shell           Open a shell in the Ansible container

Options are passed straight through to ansible-playbook, e.g.:
    -e key=value        Set a variable
    --syntax-check      Parse-only
    -v / -vvv           Verbose

Credentials:
    export KUBECONFIG=/path/to/kubeconfig     (or copy it to ./kubeconfig)
    ~/.aws/credentials                        (or AWS_SHARED_CREDENTIALS_FILE)

Cost warning:
    The iamra path creates an AWS Private CA, which bills at roughly
    \$400/month until deleted. Disabling it does not stop the charge, and
    deletion enforces a paid restoration window of at least 7 days. The oidc
    and vault paths have no equivalent standing cost.

Disruption warning:
    The oidc path must reconfigure spec.serviceAccountIssuer, which rolls out
    every kube-apiserver (10-30 min) and re-issues ServiceAccount tokens
    cluster-wide. That step is opt-in:
        $0 oidc -e oidc_set_service_account_issuer=true

Examples:
    $0 build
    $0 vault                                    # complete vault deployment
    $0 oidc -e oidc_set_service_account_issuer=true
    $0 iamra -e demo_bucket=my-unique-bucket
    $0 install -e auth_method=oidc              # equivalent to '$0 oidc'
    $0 app -e auth_method=vault
    $0 validate -e auth_method=oidc
    $0 destroy --destroy -e auth_method=vault
    $0 destroy --destroy -e auth_method=iamra -e destroy_aws=true
EOF
}

# app / validate / destroy act on ONE method's resources. Defaulting them would
# mean 'validate' quietly reporting on iamra right after you deployed vault, or
# 'destroy' removing the wrong namespace. Make the caller say which.
require_method() {
    local cmd="$1"; shift
    for arg in "$@"; do
        case "$arg" in *auth_method=*) return 0 ;; esac
    done
    print_error "'$cmd' needs to know which method to act on."
    echo ""
    echo "  $0 $cmd -e auth_method=iamra"
    echo "  $0 $cmd -e auth_method=oidc"
    echo "  $0 $cmd -e auth_method=vault"
    echo ""
    echo "Deploying a whole method instead? Use: $0 iamra|oidc|vault"
    exit 1
}

case "${1:-}" in
    build)       build_image ;;

    # Whole method, end to end. Each pins its own auth_method, so these are
    # complete and independent deployments.
    iamra)       build_image; shift; run_ansible "site.yml" -e auth_method=iamra "$@" ;;
    oidc)        build_image; shift; run_ansible "site.yml" -e auth_method=oidc  "$@" ;;
    vault)       build_image; shift; run_ansible "site.yml" -e auth_method=vault "$@" ;;
    install)     build_image; shift; run_ansible "site.yml" "$@" ;;

    # Individual steps. The *-setup commands pin their method for the same
    # reason the whole-method commands do.
    images)      build_image; shift; run_ansible "images.yml" "$@" ;;
    iamra-setup) build_image; shift; run_ansible "iamra.yml" -e auth_method=iamra "$@" ;;
    oidc-setup)  build_image; shift; run_ansible "oidc.yml"  -e auth_method=oidc  "$@" ;;
    vault-setup) build_image; shift; run_ansible "vault.yml" -e auth_method=vault "$@" ;;
    aws)         build_image; shift; run_ansible "aws.yml" -e auth_method=iamra "$@" ;;
    certmanager) build_image; shift; run_ansible "certmanager.yml" -e auth_method=iamra "$@" ;;
    issuer)      build_image; shift; run_ansible "issuer.yml" -e auth_method=iamra "$@" ;;

    app)         shift; require_method app "$@";      build_image; run_ansible "app.yml" "$@" ;;
    validate)    shift; require_method validate "$@"; build_image; run_ansible "validate.yml" "$@" ;;
    destroy)
        shift
        confirmed="no"; filtered=()
        for arg in "$@"; do
            if [ "$arg" = "--destroy" ] || [ "$arg" = "--yes" ] || [ "$arg" = "-y" ]; then
                confirmed="yes"
            else
                filtered+=("$arg")
            fi
        done
        # Ask which method before asking for confirmation -- "are you sure?" is
        # meaningless if the caller and the playbook disagree about the target.
        if [ "${#filtered[@]}" -gt 0 ]; then
            require_method destroy "${filtered[@]}"
        else
            require_method destroy
        fi
        if [ "$confirmed" != "yes" ]; then
            print_warn "destroy removes that method's namespace and its in-cluster resources."
            print_warn "Add -e destroy_aws=true to also remove IAM roles, the trust anchor and the Private CA."
            print_warn "Add -e destroy_all_methods=true to remove all three namespaces."
            print_warn "Re-run with --destroy to confirm."
            exit 1
        fi
        build_image
        run_ansible "destroy.yml" -e force_destroy=true "${filtered[@]}"
        ;;
    run)
        if [ -z "$2" ]; then print_error "Specify a playbook to run"; usage; exit 1; fi
        build_image; shift; playbook="$1"; shift
        run_ansible "$playbook" "$@"
        ;;
    shell)
        build_image
        resolve_kubeconfig || true
        resolve_aws_dir || true
        print_info "Opening shell in Ansible container..."
        podman run --rm -it \
            -v "$SCRIPT_DIR:/workspace:z" \
            ${KUBECONFIG:+-v "$KUBECONFIG:/workspace/.kubeconfig:z" -e "KUBECONFIG=/workspace/.kubeconfig"} \
            ${AWS_DIR:+-v "$AWS_DIR:/workspace/.aws:ro,z" -e "AWS_SHARED_CREDENTIALS_FILE=/workspace/.aws/credentials" -e "AWS_CONFIG_FILE=/workspace/.aws/config"} \
            --network host --entrypoint /bin/bash "$IMAGE_NAME"
        ;;
    -h|--help|help|"") usage ;;
    *) print_error "Unknown command: ${1:-}"; echo ""; usage; exit 1 ;;
esac
