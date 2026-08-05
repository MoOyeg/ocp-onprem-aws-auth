# Authenticating on-prem OpenShift to AWS: certificates, tokens, or Vault

<!--
Meta title:       Authenticating on-prem OpenShift to AWS without a key
Meta description: On-prem OpenShift has no instance profile. Three ways to reach AWS APIs without a static key: certificates, OIDC federation and Vault, compared on one cluster.
Slug:             authenticating-on-prem-openshift-to-aws
-->

On EC2, nobody stores an AWS key. A workload asks the instance metadata service,
which hands back short-lived credentials for the IAM role in the instance profile
attached to the machine. The identity comes from the infrastructure. A Red Hat
OpenShift cluster running in your own data center has neither of those things — no
instance profile, no metadata service — so the usual answer is an access key in a
Secret. That key does not expire. It does not rotate unless you build something to
rotate it. Anyone with `get secrets` in that namespace has it, and so does anyone
holding a copy of an etcd backup.

Several mechanisms can replace that key. This post covers three that take it out
of the workload entirely — IAM Roles Anywhere, OpenID Connect (OIDC) federation,
and the HashiCorp Vault AWS secrets engine — and how each one behaves in practice:
what proves the identity, what scopes the permission, and where it breaks. All
three are running on one cluster as I write this, so none of it is theoretical.

The demo application is byte-identical across all three deployments —
ordinary boto3, no credential handling, no AWS-specific code. Switching mechanisms
changes exactly one environment variable.

What follows is the trade-offs, not the installation. The working code lives in
[github.com/MoOyeg/ocp-onprem-aws-auth](https://github.com/MoOyeg/ocp-onprem-aws-auth).

## The three options

All three replace the static key with something that expires. They differ in who
does the verifying:

- **IAM Roles Anywhere** — a sidecar presents a short-lived X.509 certificate
  signed by a certificate authority (CA) that AWS trusts, and the AWS Security
  Token Service (STS) trades it for temporary credentials.
- **OIDC federation** — the pod presents the ServiceAccount token the cluster
  already issues, and STS verifies it against the cluster's published JSON Web Key
  Set (JWKS). Amazon Elastic Kubernetes Service (EKS) calls this IAM Roles for
  Service Accounts (IRSA); the same mechanism works on any cluster that can
  publish a discovery document AWS can fetch.
- **Vault AWS secrets engine** — the pod proves itself to Vault with its
  ServiceAccount token, and Vault calls `sts:AssumeRole` on its behalf.

| | IAM Roles Anywhere | OIDC federation | Vault AWS secrets engine |
|---|---|---|---|
| Identity is | a short-lived X.509 certificate | the pod's own ServiceAccount token | the pod's token, presented to Vault |
| Verified by | AWS | AWS | Vault |
| Sidecar | yes | **no** | yes (injected) |
| Standing cost | **a Private CA**, billed monthly | none | running Vault |
| Needs AWS to reach you | no | **yes** — a JWKS document | no |
| Long-lived secret | none | none | **Vault's own AWS key** |

Every one of them still needs the cluster to reach AWS outbound. None of this is a
way to use AWS APIs without talking to AWS. What differs is whether AWS also has
to reach *back*.

## IAM Roles Anywhere

The certificate lasts six days. A sidecar exchanges it for STS credentials and
serves them back through the credential chain the AWS SDKs already probe, so the
application calls S3 with unmodified boto3 and never learns a certificate was
involved. That is the capability worth the machinery: the workload keeps its
ordinary code and gains an identity AWS can verify. AWS documents this pattern in
a [security blog post][aws-post], and that is where I started. The architecture
works as described; building it on OpenShift surfaced things the article does not
cover.

[aws-post]: https://aws.amazon.com/blogs/security/connect-your-on-premises-kubernetes-cluster-to-aws-apis-using-iam-roles-anywhere/

In AWS there is a Private CA in short-lived certificate mode, which caps every
certificate it issues at seven days:

![AWS Private CA in the console, short-lived certificate mode](../docs/images/aws-acm-pca.jpg)

A trust anchor over that CA, and one Roles Anywhere profile per identity:

![IAM Roles Anywhere trust anchor and profiles](../docs/images/aws-rolesanywhere-anchors.jpg)

In the cluster, every pod runs two containers — the application and the credential
sidecar:

![Pods in iamra-demo, 2/2 containers each](../docs/images/ocp-pods-iamra.jpg)

The cert-manager side is one issuer pointing at the CA. It has to be the
cluster-scoped `AWSPCAClusterIssuer`, not the namespaced `AWSPCAIssuer`, because a
namespaced issuer is invisible outside its own namespace:

```yaml
apiVersion: awspca.cert-manager.io/v1beta1
kind: AWSPCAClusterIssuer
metadata:
  name: iamra-pca
spec:
  arn: arn:aws:acm-pca:us-east-2:111122223333:certificate-authority/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  region: us-east-2
```

Each pod then asks for its own certificate through an inline CSI volume, minted at
admission and written only to the pod's tmpfs:

```yaml
volumes:
  - name: iamra-cert
    csi:
      driver: csi.cert-manager.io
      readOnly: true
      volumeAttributes:
        csi.cert-manager.io/issuer-name: iamra-pca
        csi.cert-manager.io/issuer-kind: AWSPCAClusterIssuer
        csi.cert-manager.io/issuer-group: awspca.cert-manager.io
        # expanded by the CSI driver at admission, not by a shell.
        # This is the value the IAM trust policy pins.
        csi.cert-manager.io/common-name: "${SERVICE_ACCOUNT_NAME}.${POD_NAMESPACE}"
        csi.cert-manager.io/duration: 144h
        csi.cert-manager.io/renew-before: 24h
        csi.cert-manager.io/key-encoding: PKCS8
```

### How permission is scoped

The gate is ordinary Kubernetes RBAC, and that is a deliberate property of the
CSI driver: it runs with `useTokenRequest: true`, so each `CertificateRequest`
arrives as the *mounting pod's* ServiceAccount rather than the driver's. Delete
one RoleBinding and the pod cannot obtain a certificate, so it cannot reach AWS:

```yaml
kind: Role                       # namespace: iamra-demo
rules:
  - apiGroups: ["cert-manager.io"]
    resources: ["certificaterequests"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: ["awspca.cert-manager.io"]
    resources: ["awspcaclusterissuers"]
    verbs: ["get", "list", "watch"]
    resourceNames: ["iamra-pca"]   # this issuer, not every issuer
```

You can watch that happen. The driver turns the mount into a
`CertificateRequest`, and the request arrives under the pod's own identity rather
than the driver's:

```yaml
# oc -n iamra-demo get certificaterequest -o yaml   (trimmed)
apiVersion: cert-manager.io/v1
kind: CertificateRequest
metadata:
  ownerReferences:
    - kind: Pod                  # so it is garbage collected with the pod
      name: s3-demo-69d9f4f8cc-zzgwb
spec:
  duration: 144h0m0s
  issuerRef:
    group: awspca.cert-manager.io
    kind: AWSPCAClusterIssuer
    name: iamra-pca
  request: LS0tLS1CRUdJTiBDRVJU…   # PKCS#10 — the common name is in here
  username: system:serviceaccount:iamra-demo:s3-demo
  extra:
    authentication.kubernetes.io/pod-name:
      - s3-demo-69d9f4f8cc-zzgwb
```

`username` is the whole point of `useTokenRequest: true` — RBAC applies to the
workload, not to the driver. The common name is inside the base64 PKCS#10 blob,
so decode it to see what was actually asked for:

```bash
$ oc -n iamra-demo get certificaterequest <name> \
    -o jsonpath='{.spec.request}' | base64 -d | openssl req -noout -subject
subject=CN=s3-demo.iamra-demo

$ oc -n iamra-demo get certificaterequest <name> \
    -o jsonpath='{.status.certificate}' | base64 -d | openssl x509 -noout -subject -dates
subject=CN=s3-demo.iamra-demo
notBefore=Aug  4 01:53:47 2026 GMT
notAfter=Aug 10 02:53:46 2026 GMT
```

`${SERVICE_ACCOUNT_NAME}.${POD_NAMESPACE}` resolved to `s3-demo.iamra-demo`, and
the six days are the `144h` from the volume attributes. That string is the
identity.

In AWS, the trust policy is where it becomes one. Roles Anywhere surfaces the
certificate subject as a principal tag, and the policy pins it:

![The iamra workload role trust policy, pinning the certificate common name](../docs/images/aws-iam-role-iamra-trust.jpg)

`aws:PrincipalTag/x509Subject/CN` has to equal `s3-demo.iamra-demo` — the exact
common name the CSI volume requested — and `aws:SourceArn` pins the trust anchor,
so a certificate presented through a different Roles Anywhere trust anchor in the
same account cannot assume this role.

There is a second identity: the issuer role, trusted for `CN=iamra-issuer` and
allowed `acm-pca:IssueCertificate` on one CA, conditioned to the end-entity
template. That condition is doing real work — without it, the issuer could mint
itself a subordinate CA and sign certificates offline, outside any audit trail.

![The demo app showing its certificate and assumed role](../docs/images/app-iamra.jpg)

**Benefits**

- **Nothing you own has to be reachable from outside.** The trust material is
  uploaded to AWS once, when you create the trust anchor.
- **Failures are scoped to one workload.** A bad or expired certificate takes out
  the pod holding it, not the cluster.
- **It covers machines that are not in Kubernetes.** Virtual machines, build
  agents and continuous integration runners can use the same trust anchor. OIDC
  federation cannot.
- **No Secret is ever created**, so there is nothing in etcd to leak, and the
  sidecar names each session after its pod, giving you per-pod attribution in
  CloudTrail.

**Issues**

- **AWS Private CA carries a standing monthly charge.** It is by far the largest
  line item of the three, and it bills continuously from creation until deletion —
  disabling the CA does not stop it, and deleting it enforces a paid restoration
  window of at least seven days. Check the current figure on the
  [AWS Private CA pricing page][pca-price] before you create one; backing the
  trust anchor with your own CA avoids the charge entirely.
- **When the exchange fails, the helper returns HTTP 200.** This one cost me an
  afternoon. If the certificate is rejected it answers the AWS software
  development kit (SDK) with `{"AccessKeyId":"", "Code":"Success",
  "Expiration":"0001-01-01T00:00:00Z"}`. botocore parses that zero date and raises
  `OverflowError: date value out of range`. Nothing in the traceback mentions AWS,
  credentials, or certificates, so an application catching only the obvious
  `ClientError` turns a routine rejection into an unhandled 500.
- **Renewal is not handled for you.** `aws_signing_helper serve` loads its
  certificate once at startup, so when cert-manager renews it underneath the
  process keeps presenting the old one and every AWS call fails about a week
  later. You add a liveness probe that fails near expiry — and it has to be an
  `exec` probe, because the endpoint binds `127.0.0.1` while kubelet dials the pod
  IP, so a `tcpSocket` probe is refused every time and the sidecar restart-loops
  while its own log reports that it is serving normally.
- **It needs a Pod Security Admission relaxation.** Inline Container Storage
  Interface volumes require `pod-security.kubernetes.io/enforce: privileged` on the
  namespace. Security context constraints still govern, so nothing runs
  privileged, but the admission net is gone for the rest of that namespace.
- **AWS cannot constrain the certificate subject.** You can pin the template but
  not the common name, so anyone holding the issuer's role can mint a certificate
  with any name and assume any workload role under that trust anchor. The cluster
  is where you bound it, with role-based access control on who may request one.
- **There is a bootstrap loop.** The issuer needs AWS credentials to issue
  certificates, and gets them by presenting a certificate it cannot have yet. You
  break it by hand-issuing one and letting cert-manager renew it from there — but
  if the cluster is down long enough for that certificate to expire, you
  re-bootstrap by hand.

[pca-price]: https://aws.amazon.com/private-ca/pricing/

## OIDC federation

Your cluster already signs identity tokens for every pod. Teach AWS to trust that
signer, and a role's trust policy can pin the token's `sub` and `aud` claims to
one ServiceAccount in one namespace.

In AWS, the published issuer is registered as an identity provider:

![The IAM OIDC identity provider registered for the cluster](../docs/images/aws-iam-oidc-provider.jpg)

In the cluster, the payoff:

![Pods in oidc-demo, 1/1 container each](../docs/images/ocp-pods-oidc.jpg)

**One container.** No sidecar, no certificate, no helper process.

On the OpenShift side there is exactly one change, and it is cluster-wide: tell
the cluster to sign tokens claiming that same issuer.

```yaml
apiVersion: config.openshift.io/v1
kind: Authentication
metadata:
  name: cluster
spec:
  serviceAccountIssuer: https://cluster-oidc-111122223333.s3.us-east-2.amazonaws.com
```

That URL has to match, byte for byte, the `iss` claim in the tokens the cluster
mints, the `issuer` field in the discovery document AWS fetches, and the provider
registered in IAM. STS compares them as strings.

The whole pod-side integration is a projected token and two environment
variables:

```yaml
volumes:
  - name: aws-token
    projected:
      sources:
        - serviceAccountToken:
            path: token
            # must match the aud condition in the role's trust policy
            audience: sts.amazonaws.com
            expirationSeconds: 3600
env:
  - name: AWS_ROLE_ARN
    value: arn:aws:iam::111122223333:role/ocp-oidc-s3-demo
  - name: AWS_WEB_IDENTITY_TOKEN_FILE
    value: /var/run/secrets/aws/token
```

### How permission is scoped

There is no `Role`, no `RoleBinding` and nothing to grant. kubelet mints the
projected token unconditionally — a workload does not need permission to have an
identity. The entire boundary is the IAM trust policy:

![The oidc workload role trust policy, pinning the ServiceAccount subject and audience](../docs/images/aws-iam-role-oidc-trust.jpg)

The principal is the federated issuer, the action is
`sts:AssumeRoleWithWebIdentity`, and the two conditions name the exact
ServiceAccount (`system:serviceaccount:oidc-demo:s3-demo`) and the exact audience
(`sts.amazonaws.com`).

That is the tightest scoping of the three and the easiest to get wrong. `sub` has
to be an exact match: a `StringLike` with `system:serviceaccount:*:s3-demo` admits
that ServiceAccount name in *every* namespace, including one an attacker can
create. `aud` has to be pinned as well, or a token minted for some other audience
is accepted here. And it is one role per ServiceAccount — a role that trusts the
issuer with no `sub` condition trusts every pod you will ever run.

![The demo app showing its token claims and assumed role](../docs/images/app-oidc.jpg)

**Benefits**

- **No sidecar and no certificate machinery.** The AWS SDKs call
  `AssumeRoleWithWebIdentity` natively: one fewer container, one fewer image to
  patch, no credential process to supervise.
- **No standing cost.** There is no CA to pay for and nothing to run.
- **The scoping is the tightest of the three.** The trust policy names an exact
  ServiceAccount and an exact audience, so you can read it and know precisely
  which pod it admits.
- **Rotation is kubelet's problem.** It refreshes the token file, the SDK re-reads
  it, and nothing restarts.
- **Audience scoping is a real boundary.** The AWS token is minted for
  `sts.amazonaws.com` and lives an hour; the default token is for the API server
  and lives a year. Neither can be replayed in place of the other.

**Issues**

- **AWS has to reach a document you publish.** The discovery document and JWKS
  must be fetchable by STS. They contain only public keys, so nothing secret
  leaves the cluster — but if publishing anything internet-reachable needs a
  review board, this option is unavailable.
- **A stale JWKS breaks every workload at once.** When the cluster rotates its
  ServiceAccount signing keys, the published document no longer contains the key
  that signed live tokens. Republishing is one command, but it has to be a
  scheduled job rather than something you remember. *A bad certificate takes out
  one pod. A stale JWKS takes out the cluster.*
- **Turning it on reconfigures the cluster.** Setting `serviceAccountIssuer` rolls
  every kube-apiserver and re-issues ServiceAccount tokens cluster-wide — the only
  one of the three that touches the control plane. While the API servers roll they
  disagree about the issuer: two pods of the same ReplicaSet, created in the *same
  second*, came back with different `iss` claims, and one was rejected with
  `InvalidIdentityToken`. Watching the cluster operator's `Progressing` condition
  does not close that window; it flaps between nodes and reads `False` in the gaps.
- **Write access to the published bucket is equivalent to the signing key.** It is
  public-read by design, which makes it easy to forget that anyone who can write
  to it can publish their own key and mint tokens AWS will trust.

## HashiCorp Vault

You write annotations rather than container specs. Vault's mutating webhook reads
them at admission and injects the agent that keeps the credential fresh.

In AWS, Vault authenticates as an IAM user whose entire permission set is one
action on one resource. That is the whole blast radius of the long-lived key
Vault holds:

![Vault's IAM user policy: sts:AssumeRole on exactly one role ARN](../docs/images/aws-iam-user-vault.jpg)

In the cluster, the injected agent makes it two containers:

![Pods in vault-demo, 2/2 containers each](../docs/images/ocp-pods-vault.jpg)

The template renders the lease into the exact INI shape boto3 already looks for,
so the application needs no Vault awareness at all — it reads
`AWS_SHARED_CREDENTIALS_FILE=/vault/secrets/aws` and behaves as if someone had
left a credentials file there:

```yaml
template:
  metadata:
    annotations:
      vault.hashicorp.com/agent-inject: "true"
      vault.hashicorp.com/role: s3-demo
      vault.hashicorp.com/agent-inject-secret-aws: aws/creds/s3-demo
      vault.hashicorp.com/agent-inject-template-aws: |
        {{- with secret "aws/creds/s3-demo" -}}
        [default]
        aws_access_key_id={{ .Data.access_key }}
        aws_secret_access_key={{ .Data.secret_key }}
        aws_session_token={{ .Data.security_token }}
        {{- end }}
      # without this the injector pins runAsUser=100, which is outside the
      # namespace's openshift.io/sa.scc.uid-range and the pod is rejected
      vault.hashicorp.com/agent-set-security-context: "false"
      # we want the AWS credentials file, not a copy of the Vault token
      vault.hashicorp.com/agent-inject-token: "false"
```

### How permission is scoped

The pod needs no special Kubernetes RBAC here either. What gates it is the Vault
Kubernetes-auth role, which binds the ServiceAccount name *and* its namespace,
paired with a policy granting read on exactly one credential path:

```bash
vault write auth/kubernetes/role/s3-demo \
    bound_service_account_names=s3-demo \
    bound_service_account_namespaces=vault-demo \
    policies=s3-demo ttl=1h
```

```hcl
path "aws/creds/s3-demo" { capabilities = ["read"] }
```

Vault validates the presented token with a `TokenReview` call, which is why
Vault's own ServiceAccount holds `system:auth-delegator`. That is the one elevated
cluster permission this method needs, and it belongs to Vault rather than to your
workloads.

The workload role on the AWS side names Vault's IAM user and nothing else:

![The Vault workload role's trust policy, trusting only Vault's IAM user](../docs/images/aws-iam-role-vault-trust.jpg)

Read that policy and you cannot tell which pod it serves, because nothing in AWS
knows. Compare it with the other two trust policies above: those name a
certificate common name and a ServiceAccount subject, and you can tell exactly
which workload they admit. This one names a broker.

![The demo app showing the Vault-rendered STS session](../docs/images/app-vault.jpg)

**Benefits**

- **One broker for AWS and everything else.** If Vault is already in your estate,
  this adds a credential path rather than a new system — with per-workload policy
  and a full audit trail your security team probably already reviews.
- **The smallest egress footprint of the three.** Application pods never talk to
  AWS at all; only the Vault server does.
- **No cluster reconfiguration and no public endpoint.** Nothing about the control
  plane changes, and nothing of yours needs to be reachable.
- **Credentials are genuinely temporary.** With `credential_type=assumed_role`,
  Vault returns an STS session that expires on its own rather than creating a real
  IAM user per lease.

**Issues**

- **Vault holds a long-lived AWS access key.** This moves the problem rather than
  removing it. Risk is concentrated in one audited place, which beats a key per
  namespace, but it is still the one thing an attacker would go for. Its blast
  radius is deliberately tiny — `sts:AssumeRole` on exactly one role, no wildcards.
- **AWS verifies Vault, not the pod.** The workload role's trust policy contains
  nothing that distinguishes one pod from another; all of that lives inside Vault,
  in `bound_service_account_names` and `bound_service_account_namespaces`. Not
  worse, but it changes where you look when something is denied.
- **Vault becomes an availability dependency**, and the injector is a webhook, so
  it fails quietly: if it is not running when a pod is admitted, the pod comes up
  perfectly healthy with no agent and no credentials file. The only symptom is a
  `NoCredentialsError` deep in the application.
- **The Helm chart's `global.openshift: true` flag broke image pulls.** It gives
  you the OpenShift-appropriate security contexts you want, and silently repoints
  every image at a registry that does not carry the chart's default tags. Both
  pods went straight to `ImagePullBackOff`. Keep the flag and override the image
  repositories back.

## What the credential can do is identical in all three

Scoping has two halves, and only the first one differs. *Who may obtain a
credential* is answered in three completely different places — a certificate
common name, a ServiceAccount subject, a Vault role binding. *What that credential
may then do* is answered the same way every time, by an IAM permission policy on
the workload role.

I did not vary that half, and the three policies are byte-identical. So here it is
once rather than three times:

![The workload role's permission policy: S3 on one bucket, no wildcards](../docs/images/aws-iam-role-permissions.jpg)

Two statements, one bucket, five actions, no wildcards. Whichever mechanism proved
the identity, this is the ceiling on what the resulting session can touch — which
is worth remembering when comparing the three, because none of them makes a
badly-scoped permission policy any safer.

## Choosing between the three

The three coexist happily. Same image, three namespaces, three IAM roles, and
running one requires nothing from the other two.

**Start with OIDC federation if you can publish a JWKS document.** What it asks in
return is real, but it is the simplest of the three to operate once it is running.

**Use IAM Roles Anywhere when publishing anything AWS-reachable is off the
table**, or when you need the same mechanism for machines outside Kubernetes. Back
the trust anchor with your own CA and the standing charge goes away.

**Use Vault if you already run Vault.** Standing it up for this one problem trades
a credential-distribution problem for an availability problem.

## The seams cost more than the architecture

Almost everything that cost me time was not in the architecture. It was in the
seams:

- a probe type that cannot possibly work against a loopback listener
- a helper that returns `200 OK` when it has failed
- a Helm flag that fixes security contexts and breaks image pulls
- an Ansible conditional that is true for the playbook that sets it
- `oc start-build --follow` returning before the Build object is updated
- an SELinux mount flag that makes two concurrent runs impossible

I could not find any of these documented, so they are all written down in the
repo's README at the point in the code where they bite. Budget time for the seams.

## Build it yourself

Everything above is in
[github.com/MoOyeg/ocp-onprem-aws-auth](https://github.com/MoOyeg/ocp-onprem-aws-auth):
the Ansible, the demo application, the IAM policies, and notes on every failure.
It runs in a container, so all you need locally is Podman, plus an OpenShift
cluster and an AWS account. Each method deploys with one command. If you hit a
seam I missed, open an issue.

*Every screenshot and snippet here is from a live deployment. The AWS account id
reads `111122223333` throughout, and key ids, certificate serials and resource
UUIDs are fixed placeholders — the redaction runs in the page before the
screenshot is taken.*
