#!/usr/bin/env bash
# Regenerate docs/environment-capture.md from the live cluster.
#
#   KUBECONFIG=/path/to/kubeconfig docs/capture-environment.sh
#
# Everything written under docs/ passes through docs/redact.py first: the AWS
# account id, key ids, session tokens, JWTs, certificate serials and resource
# UUIDs are all replaced with fixed placeholders. Structure is preserved, so an
# ARN still reads as an ARN.
#
# Screenshots are taken of a LOCAL, ALREADY-REDACTED copy of each page rather
# than of the live route. That way the images are faithful renders of redacted
# HTML instead of pictures that were edited afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

R="python3 ${HERE}/redact.py"
METHODS=(iamra oidc vault)

command -v oc >/dev/null || { echo "oc not found" >&2; exit 1; }
oc whoami >/dev/null || { echo "not logged in to a cluster" >&2; exit 1; }

# --- terminal captures ------------------------------------------------------
echo "==> capturing cluster state"
{
  echo "### PODS ###"
  oc get pods -A -l app=s3-demo \
    -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name \
    --no-headers
  echo
  echo "### NAMESPACE PSA ###"
  for m in "${METHODS[@]}"; do
    printf "%-12s enforce=%s\n" "${m}-demo" \
      "$(oc get ns "${m}-demo" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)"
  done
  echo
  echo "### CLUSTER ###"
  oc get clusterversion version -o jsonpath='OpenShift {.status.desired.version}{"\n"}'
  oc get authentication.config/cluster -o jsonpath='serviceAccountIssuer {.spec.serviceAccountIssuer}{"\n"}'
} 2>&1 | ${R} > "${WORK}/cluster.txt"

for m in "${METHODS[@]}"; do
  oc get ns "${m}-demo" >/dev/null 2>&1 || { echo "  skip ${m} (not deployed)"; continue; }
  echo "==> capturing ${m}"
  oc -n "${m}-demo" exec deploy/s3-demo -c app -- token-info 2>/dev/null \
    | ${R} > "${WORK}/token-${m}.txt" || true
done

if oc get ns iamra-demo >/dev/null 2>&1; then
  oc -n iamra-demo exec deploy/s3-demo -c iamra-sidecar -- imds-probe 2>/dev/null \
    | ${R} > "${WORK}/imds.txt" || true
fi

echo "==> capturing AWS inventory"
{
  echo "### IAM ROLES ###"
  aws iam list-roles --query 'Roles[?starts_with(RoleName,`ocp-`)].RoleName' --output text \
    | tr '\t' '\n' | sed 's/^/  /'
  echo "### ROLES ANYWHERE ###"
  echo "  trust anchor: $(aws rolesanywhere list-trust-anchors --query 'trustAnchors[].name' --output text)"
  echo "  profiles:     $(aws rolesanywhere list-profiles --query 'profiles[].name' --output text)"
  echo "### PRIVATE CA ###"
  aws acm-pca list-certificate-authorities \
    --query 'CertificateAuthorities[].[Status,UsageMode]' --output text | sed 's/^/  /'
  echo "### OIDC PROVIDER ###"
  aws iam list-open-id-connect-providers \
    --query 'OpenIDConnectProviderList[].Arn' --output text | sed 's/^/  /'
} 2>&1 | ${R} > "${WORK}/aws.txt" || true

# --- screenshots ------------------------------------------------------------
CHROME="${CHROME:-}"
if [[ -z "${CHROME}" ]]; then
  CHROME="$(find "${HOME}/.cache/ms-playwright" -maxdepth 3 -name chrome -type f 2>/dev/null | sort | tail -1 || true)"
fi

if [[ -n "${CHROME}" && -x "${CHROME}" ]]; then
  mkdir -p "${HERE}/images"
  for m in "${METHODS[@]}"; do
    host="$(oc -n "${m}-demo" get route s3-demo -o jsonpath='{.spec.host}' 2>/dev/null || true)"
    [[ -z "${host}" ]] && continue
    echo "==> screenshotting ${m}"
    # Redact the HTML, then shoot the local copy.
    curl -sk --max-time 30 "https://${host}/" | ${R} > "${WORK}/${m}.html"
    "${CHROME}" --headless --disable-gpu --no-sandbox --hide-scrollbars \
      --virtual-time-budget=4000 --window-size=1180,1500 \
      --screenshot="${WORK}/${m}.png" "file://${WORK}/${m}.html" >/dev/null 2>&1 || true
    python3 - "${WORK}/${m}.png" "${HERE}/images/${m}-identity.jpg" <<'PY'
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGB")
# The two identity panels; fixed offsets so all three crops line up.
crop = im.crop((100, 162, im.width - 60, 742))
crop = crop.resize((int(crop.width * 0.82), int(crop.height * 0.82)), Image.LANCZOS)
crop.save(dst, "JPEG", quality=78, optimize=True)
PY
  done
else
  echo "==> no chromium found; skipping screenshots (set CHROME=/path/to/chrome)"
fi

# --- leak check -------------------------------------------------------------
echo "==> verifying redaction"
leaked=0
while IFS= read -r f; do
  hits="$(grep -oE '[0-9]{12}|ASIA[A-Z0-9]{16}|AKIA[A-Z0-9]{16}|AROA[A-Z0-9]{16}|AIDA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{20}' "$f" 2>/dev/null \
    | grep -vE 'EXAMPLEEXAMPLE00|111122223333' | sort -u || true)"
  [[ -n "${hits}" ]] && { echo "  LEAK in ${f}: ${hits}"; leaked=1; }
done < <(find "${WORK}" -maxdepth 1 -name '*.txt' -o -maxdepth 1 -name '*.html')
if [[ ${leaked} -ne 0 ]]; then
  echo "REFUSING to write docs -- unredacted material found above." >&2
  exit 1
fi
echo "  clean"

echo
echo "Captures are in ${WORK} (removed on exit) and images in ${HERE}/images."
echo "Paste the .txt contents into the console blocks of environment-capture.md,"
echo "or diff them against what is already there:"
for f in "${WORK}"/*.txt; do echo "  ${f}"; done
