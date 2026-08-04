# Optional: cert-manager approver-policy

Applies to the [`iamra`](../README.md#iamra) method only.

## The gap it closes

The trust anchor accepts **any** certificate chaining to the Private CA. The only
thing separating one workload's AWS permissions from another's is the CN
condition on the role's trust policy:

```json
"aws:PrincipalTag/x509Subject/CN": "s3-demo.iamra-demo"
```

So the security question reduces to: *who in this cluster can obtain a
certificate with `CN=s3-demo.iamra-demo`?*

The default answer in this repo is ordinary RBAC — see
[rbac.yaml.j2](../roles/demo_app/templates/rbac.yaml.j2). Because the CSI driver
runs with `useTokenRequest: true`, each `CertificateRequest` is created as the
*mounting pod's* ServiceAccount, so only ServiceAccounts you explicitly grant
`create` on `certificaterequests` can get a certificate at all.

What RBAC does **not** give you is content control. A ServiceAccount permitted to
create `CertificateRequest` objects in its own namespace can request *any* CN,
including one belonging to a more privileged role. approver-policy validates the
contents of every request before it is signed.

AWS cannot help here: `acm-pca:IssueCertificate` supports exactly one condition
key, `acm-pca:TemplateArn`. There is no condition key for the requested subject,
so this has to be enforced in the cluster.

## Why it is not automated

Installing approver-policy requires disabling cert-manager's built-in approver
cluster-wide:

```
--controllers=*,-certificaterequests-approver
```

Two consequences:

1. **Blast radius.** Every `CertificateRequest` in the cluster — including ones
   you did not create, such as Let's Encrypt ingress certificates — then needs a
   matching `CertificateRequestPolicy` or it sits unapproved forever. The AWS
   blog post makes this change without mentioning it. The catch-all below exists
   specifically to preserve the previous behaviour for every other issuer.

2. **The Red Hat cert-manager Operator may not accept that argument.** It takes a
   curated set via `spec.controllerConfig.overrideArgs`. Check before relying on
   it:

   ```bash
   oc explain certmanager.spec.controllerConfig.overrideArgs
   oc -n cert-manager get deploy cert-manager -o jsonpath='{.spec.template.spec.containers[0].args}'
   ```

   If `--controllers` is rejected, you have two options: stay on the RBAC-only
   path, or replace the operator with upstream Jetstack cert-manager, which
   accepts arbitrary `extraArgs` but is not Red Hat supported.

## Installing

Order matters. Applying the strict policy before the catch-all leaves every other
issuer in the cluster without one.

```bash
# 1. Disable the built-in approver (verify the arg is accepted first)
oc patch certmanager cluster --type=merge -p '{
  "spec": {"controllerConfig": {"overrideArgs": ["--controllers=*\\,-certificaterequests-approver"]}}
}'

# 2. Install approver-policy, told to own the signers we care about
helm upgrade -i cert-manager-approver-policy jetstack/cert-manager-approver-policy \
  --namespace cert-manager --wait --version v0.27.0 \
  --set app.approveSignerNames="{issuers.cert-manager.io/*,clusterissuers.cert-manager.io/*,awspcaclusterissuers.awspca.cert-manager.io/*,awspcaissuers.awspca.cert-manager.io/*}"

# 3. Catch-all FIRST, or unrelated certificates start hanging
oc apply -f catch-all-policy.yaml

# 4. Then the strict policies for the IAMRA issuer
oc apply -f iamra-policy.yaml
```

## catch-all-policy.yaml

Preserves the previous behaviour for every issuer that is **not** ours: anything
goes, as before. Without this, disabling the built-in approver silently stalls
certificate issuance across the cluster.

```yaml
apiVersion: policy.cert-manager.io/v1alpha1
kind: CertificateRequestPolicy
metadata:
  name: allow-all-other-issuers
spec:
  allowed:
    commonName: {value: "*"}
    dnsNames: {values: ["*"]}
    ipAddresses: {values: ["*"]}
    uris: {values: ["*"]}
    emailAddresses: {values: ["*"]}
    isCA: true
    usages: ["any"]
    subject:
      organizations: {values: ["*"]}
      countries: {values: ["*"]}
      organizationalUnits: {values: ["*"]}
      localities: {values: ["*"]}
      provinces: {values: ["*"]}
      streetAddresses: {values: ["*"]}
      postalCodes: {values: ["*"]}
      serialNumber: {value: "*"}
  selector:
    # Everything EXCEPT the AWS PCA issuers, which get the strict policies below.
    issuerRef:
      group: "cert-manager.io"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cert-manager-policy:allow-all-other-issuers
rules:
  - apiGroups: ["policy.cert-manager.io"]
    resources: ["certificaterequestpolicies"]
    verbs: ["use"]
    resourceNames: ["allow-all-other-issuers"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cert-manager-policy:allow-all-other-issuers
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cert-manager-policy:allow-all-other-issuers
subjects:
  # Every requester. This policy is a compatibility shim, not a control.
  - kind: Group
    apiGroup: rbac.authorization.k8s.io
    name: system:authenticated
```

## iamra-policy.yaml

Substitute `ISSUER_CN`, `CERT_ORG`, `CERT_OU`, `APP_NAMESPACE` and `APP_SA` for
your values (they default to `iamra-issuer`, `Example Corp.`, `Platform`,
`iamra-demo`, `s3-demo`).

```yaml
# The issuer's own credential. Exactly one CN, with O and OU pinned to match
# aws_iam/templates/issuer-trust-policy.json.j2.
apiVersion: policy.cert-manager.io/v1alpha1
kind: CertificateRequestPolicy
metadata:
  name: iamra-issuer-identity
spec:
  allowed:
    commonName: {value: "iamra-issuer", required: true}
    isCA: false
    usages: ["client auth", "server auth"]
    subject:
      organizations: {values: ["Example Corp."], required: true}
      organizationalUnits: {values: ["Platform"], required: true}
  selector:
    issuerRef:
      group: awspca.cert-manager.io
      kind: AWSPCAClusterIssuer
      name: iamra-pca
---
# Workload certificates. The CN is constrained to the requesting pod's OWN
# namespace, so a ServiceAccount in namespace A cannot obtain a certificate that
# impersonates namespace B and assume its IAM role.
#
# No `subject:` block here: the cert-manager CSI driver cannot set O or OU, so
# requiring them would deny every request. Same reason the workload IAM trust
# policy pins CN only.
apiVersion: policy.cert-manager.io/v1alpha1
kind: CertificateRequestPolicy
metadata:
  name: iamra-workload-identity
spec:
  allowed:
    commonName:
      # .Request.Namespace is substituted by approver-policy at evaluation time.
      value: "*.{{ .Request.Namespace }}"
      required: true
    isCA: false
    usages: ["client auth", "server auth"]
  constraints:
    # Nobody gets a certificate outliving the CA's short-lived cap. This is the
    # only policy-based validity ceiling available -- AWS has no IAM condition
    # key for validity.
    maxDuration: 168h
    minDuration: 1h
    privateKey:
      algorithm: RSA
      minSize: 2048
      maxSize: 4096
  selector:
    issuerRef:
      group: awspca.cert-manager.io
      kind: AWSPCAClusterIssuer
      name: iamra-pca
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cert-manager-policy:iamra-issuer-identity
rules:
  - apiGroups: ["policy.cert-manager.io"]
    resources: ["certificaterequestpolicies"]
    verbs: ["use"]
    resourceNames: ["iamra-issuer-identity"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cert-manager-policy:iamra-workload-identity
rules:
  - apiGroups: ["policy.cert-manager.io"]
    resources: ["certificaterequestpolicies"]
    verbs: ["use"]
    resourceNames: ["iamra-workload-identity"]
---
# The issuer's Certificate is an ordinary cert-manager Certificate, so the
# request is created by cert-manager's own controller ServiceAccount.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cert-manager-policy:iamra-issuer-identity
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cert-manager-policy:iamra-issuer-identity
subjects:
  - kind: ServiceAccount
    name: cert-manager
    namespace: cert-manager
---
# Workload requests arrive as the application's OWN ServiceAccount, because the
# CSI driver runs with useTokenRequest: true. Without that setting every request
# would come from the csi-driver SA and this binding would be meaningless.
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cert-manager-policy:iamra-workload-identity
  namespace: iamra-demo
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cert-manager-policy:iamra-workload-identity
subjects:
  - kind: ServiceAccount
    name: s3-demo
    namespace: iamra-demo
```

## Verifying

A request for a CN the policy does not allow should be **denied**, not signed:

```bash
oc -n iamra-demo get certificaterequest -o wide
oc -n iamra-demo describe certificaterequest <name> | grep -A5 Conditions
```

Look for `Denied` with a message naming the offending field. A request that sits
with no conditions at all means no policy matched it — usually the catch-all is
missing.
