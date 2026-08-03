#!/usr/bin/env bash
# Serve an IMDSv2-compatible credential endpoint on 127.0.0.1:${IMDS_PORT},
# backed by the X.509 certificate mounted at ${CERT_DIR}.
#
# Any AWS SDK in a sibling container picks these up by setting
#   AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:${IMDS_PORT}/
set -euo pipefail

CERT_DIR="${CERT_DIR:-/iamra}"
IMDS_PORT="${IMDS_PORT:-9911}"

fail() { echo "iamra-sidecar: $*" >&2; exit 1; }

for v in TRUST_ANCHOR_ARN PROFILE_ARN ROLE_ARN; do
  [[ -n "${!v:-}" ]] || fail "${v} must be set"
done

[[ -r "${CERT_DIR}/tls.crt" ]] || fail "${CERT_DIR}/tls.crt is missing or unreadable"
[[ -r "${CERT_DIR}/tls.key" ]] || fail "${CERT_DIR}/tls.key is missing or unreadable"

# Surface the identity we are about to present. If the CN here does not match
# the aws:PrincipalTag/x509Subject/CN condition on the role's trust policy, the
# AssumeRole call fails with AccessDenied and this line is how you find out why.
echo "iamra-sidecar: presenting certificate"
openssl x509 -in "${CERT_DIR}/tls.crt" -noout -subject -issuer -dates 2>/dev/null \
  | sed 's/^/iamra-sidecar:   /' || true

echo "iamra-sidecar: serving IMDSv2 on 127.0.0.1:${IMDS_PORT} for ${ROLE_ARN}"

exec /usr/local/bin/aws_signing_helper serve \
  --port "${IMDS_PORT}" \
  --certificate "${CERT_DIR}/tls.crt" \
  --private-key "${CERT_DIR}/tls.key" \
  --trust-anchor-arn "${TRUST_ANCHOR_ARN}" \
  --profile-arn "${PROFILE_ARN}" \
  --role-arn "${ROLE_ARN}" \
  ${AWS_REGION:+--region "${AWS_REGION}"} \
  ${SESSION_DURATION:+--session-duration "${SESSION_DURATION}"} \
  ${ROLE_SESSION_NAME:+--role-session-name "${ROLE_SESSION_NAME}"}
