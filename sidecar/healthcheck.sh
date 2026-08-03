#!/usr/bin/env bash
# Health check for the credential sidecar. Two conditions:
#
#   1. The IMDS endpoint is listening.
#   2. The mounted certificate is not about to expire.
#
# Pass --startup to check only (1). See "which probe uses which" below.
#
# Why (2) exists: `aws_signing_helper serve` loads the certificate and key at
# startup, so when cert-manager renews them underneath us -- rewriting the CSI
# volume or the Secret -- the running process keeps presenting the old ones until
# it restarts. Nothing in the AWS blog post handles this; you would simply get
# AccessDenied the moment the original certificate lapsed, roughly a week after
# deploying.
#
# Failing liveness while the current certificate is still valid makes kubelet
# restart just this container, which re-reads the freshly renewed files. As a
# native sidecar (initContainer with restartPolicy: Always) that restart happens
# in place -- the application container is not disturbed.
#
# Why this must be an exec probe and not tcpSocket/httpGet: aws_signing_helper
# binds 127.0.0.1 only, which is correct for a credential endpoint -- nothing
# outside the pod should be able to ask it for AWS credentials. kubelet dials the
# POD IP, so a tcpSocket probe is refused every time. Only a check running inside
# the container can see the listener.
#
# Which probe uses which:
#   startupProbe    --startup   port only. The expiry check must NOT gate
#                               startup: a certificate that is legitimately near
#                               expiry would then prevent the container from ever
#                               starting, instead of starting and reloading. That
#                               deadlocks the bootstrap case, where the issuer
#                               cannot come up to renew the very certificate it
#                               is complaining about.
#   livenessProbe   (no args)   port + expiry, so renewal triggers a reload.
#
# RELOAD_BEFORE must be shorter than the certificate's renewBefore, so the new
# certificate is already on disk by the time this fires.
set -uo pipefail

CERT_DIR="${CERT_DIR:-/iamra}"
IMDS_PORT="${IMDS_PORT:-9911}"
RELOAD_BEFORE="${RELOAD_BEFORE:-86400}"   # 24h, in seconds

startup_only=0
[[ "${1:-}" == "--startup" ]] && startup_only=1

if ! timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/${IMDS_PORT}" 2>/dev/null; then
  echo "unhealthy: nothing listening on 127.0.0.1:${IMDS_PORT}"
  exit 1
fi

if [[ "${startup_only}" -eq 1 ]]; then
  exit 0
fi

if ! openssl x509 -in "${CERT_DIR}/tls.crt" -noout -checkend "${RELOAD_BEFORE}" >/dev/null 2>&1; then
  echo "unhealthy: certificate expires within ${RELOAD_BEFORE}s -- restarting to reload the renewed one"
  exit 1
fi

exit 0
