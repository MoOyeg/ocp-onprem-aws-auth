# Authenticating on-prem OpenShift to AWS: certificates, tokens, or Vault

<!--
Meta title:       Authenticating on-prem OpenShift to AWS without a key
Meta description: On-prem OpenShift has no instance profile. Here are three ways to reach AWS APIs without a static access key — certificates, OIDC federation, and Vault.
Slug:             authenticating-on-prem-openshift-to-aws
-->

On EC2, a workload that calls an AWS API does not carry a credential. It asks the
instance metadata service, which hands back short-lived credentials derived from
the instance profile attached to the machine. The identity comes from the
infrastructure, and nobody has to store a key anywhere. A Red Hat OpenShift
cluster running in your own data center has neither of those things — no instance
profile, no metadata service — so the usual answer is an access key in a Secret.
That key does not expire. It does not rotate unless you build something to rotate
it. Anyone with `get secrets` in that namespace has it, and so does anyone holding
a copy of an etcd backup.

There are three ways to replace it, and I built all three on the same cluster so I
could compare them against each other rather than in the abstract: IAM Roles
Anywhere, OpenID Connect (OIDC) federation, and the HashiCorp Vault AWS secrets
engine. The demo application is byte-identical across all three deployments —
ordinary boto3, no credential handling, no AWS-specific code. Switching mechanisms
changes exactly one environment variable.

What follows is the trade-offs, not the installation. The working code lives in
[github.com/MoOyeg/ocp-onprem-aws-auth](https://github.com/MoOyeg/ocp-onprem-aws-auth).

## The three options

All three replace the static key with something that expires. They differ in who
does the verifying:

- **IAM Roles Anywhere** — the pod presents a short-lived X.509 certificate signed
  by a certificate authority (CA) that AWS trusts. The AWS Security Token Service
  (STS) trades it for temporary credentials. A sidecar does the exchange and
  serves the result on a loopback endpoint that mimics EC2's metadata service.
- **OIDC federation** — the pod presents the ServiceAccount token the cluster
  already issues, and STS verifies it against the cluster's published JSON Web Key
  Set (JWKS). Amazon Elastic Kubernetes Service (EKS) calls this IAM Roles for
  Service Accounts (IRSA); the same mechanism works on any cluster that can
  publish a discovery document AWS can fetch.
- **Vault AWS secrets engine** — the pod proves itself to Vault with its
  ServiceAccount token. Vault holds an AWS credential, calls `sts:AssumeRole` on
  the pod's behalf, and its injected agent writes the result into a credentials
  file.

| | IAM Roles Anywhere | OIDC federation | Vault AWS secrets engine |
|---|---|---|---|
| Identity is | a short-lived X.509 certificate | the pod's own ServiceAccount token | the pod's token, presented to Vault |
| Verified by | AWS | AWS | Vault |
| Sidecar | yes | **no** | yes (injected) |
| Standing cost | **~$400/mo** for a Private CA | none | running Vault |
| Needs AWS to reach you | no | **yes** — a JWKS document | no |
| Long-lived secret | none | none | **Vault's own AWS key** |

Every one of them still needs the cluster to reach AWS outbound. None of this is a
way to use AWS APIs without talking to AWS. What differs is whether AWS also has
to reach *back*.

## IAM Roles Anywhere

cert-manager mints a six-day certificate for each pod, and the sidecar serves the
resulting credentials on a loopback Instance Metadata Service Version 2 (IMDSv2)
endpoint, so the application thinks it is on EC2. AWS documents this pattern in a
[security blog post][aws-post], and that is where I started. The architecture
works as described; building it on OpenShift surfaced things the article does not
cover.

[aws-post]: https://aws.amazon.com/blogs/security/connect-your-on-premises-kubernetes-cluster-to-aws-apis-using-iam-roles-anywhere/

![Pods in iamra-demo, 2/2 containers each](../docs/images/ocp-pods-iamra.jpg)

**Benefits**

- **Nothing you own has to be reachable from outside.** The trust material is
  uploaded to AWS once, when you create the trust anchor.
- **Failures are scoped to one workload.** A bad or expired certificate takes out
  the pod holding it, not the cluster.
- **It covers machines that are not in Kubernetes.** Virtual machines, build
  agents and continuous integration runners can use the same trust anchor. OIDC
  federation cannot.
- **The certificate dies with the pod**, so no credential is sitting in etcd to
  leak, and the sidecar names each session after its pod, which gives you per-pod
  attribution in CloudTrail.

**Issues**

- **The Private CA costs roughly $400 a month**, continuously, from creation until
  deletion ([AWS Private CA pricing][pca-price]). Disabling it does not stop the
  charge, and deleting it enforces a paid restoration window of at least seven
  days. Backing the trust anchor with your own CA avoids this entirely.
- **When the exchange fails, the helper returns HTTP 200.** This one cost me an
  afternoon. If the certificate is rejected it answers the AWS software
  development kit (SDK) with `{"AccessKeyId":"", "Code":"Success",
  "Expiration":"0001-01-01T00:00:00Z"}`. botocore parses that zero date and raises
  `OverflowError: date value out of range`. Nothing in the traceback mentions AWS,
  credentials, or certificates, so an application catching only the obvious
  `ClientError` turns a routine rejection into an unhandled 500.
- **Renewal is not handled for you.** `aws_signing_helper serve` loads its
  certificate once at startup, so when cert-manager renews it underneath, the
  process keeps presenting the old one and every AWS call fails about a week
  later. You have to add a liveness probe that fails near expiry, and it has to be
  an `exec` probe: the endpoint binds `127.0.0.1` and kubelet dials the pod IP, so
  a `tcpSocket` probe is refused every time and the sidecar restart-loops while
  its own log reports that it is serving normally.
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
signer and the pod can hand its own token to STS directly, with a role's trust
policy pinning the token's `sub` and `aud` claims to one ServiceAccount in one
namespace.

![Pods in oidc-demo, 1/1 container each](../docs/images/ocp-pods-oidc.jpg)

**One container.** The whole integration is two environment variables and a
projected volume.

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
  disagree about the issuer, and two pods of the same ReplicaSet, created in the
  *same second*, came back with different `iss` claims: one federated fine, the
  other was rejected with `InvalidIdentityToken`. The cluster operator's
  `Progressing` condition does not close that window; it flaps between nodes and
  reads `False` in the gaps.
- **Write access to the published bucket is equivalent to the signing key.** It is
  public-read by design, which makes it easy to forget that anyone who can write
  to it can publish their own key and mint tokens AWS will trust.

## HashiCorp Vault

You write annotations rather than container specs: Vault's mutating webhook
rewrites the pod at admission and injects the agent that keeps the credential
fresh.

![Pods in vault-demo, 2/2 containers each](../docs/images/ocp-pods-vault.jpg)

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
  namespace, but it is still a key and it is the one thing an attacker would go
  for. Its blast radius is deliberately tiny — `sts:AssumeRole` on exactly one
  role, no wildcards.
- **AWS verifies Vault, not the pod.** The workload role's trust policy contains
  nothing that distinguishes one pod from another. All of that lives inside Vault,
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

## Choosing between the three

The three coexist happily. Same image, three namespaces, three IAM roles, and
running one requires nothing from the other two.

![Three namespaces, one per method](../docs/images/ocp-projects.jpg)

**Start with OIDC federation if you can publish a JWKS document.** What it asks in
return is real — AWS must fetch a document you host, and you have to reconfigure
the cluster's token issuer — but it is the simplest of the three to operate once
it is running.

**Use IAM Roles Anywhere when publishing anything AWS-reachable is off the
table**, or when you need the same mechanism for machines outside Kubernetes. Back
the trust anchor with your own CA and the $400 goes away.

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
It runs in a container, so all you need locally is Podman,
plus an OpenShift cluster and an AWS account. Each method deploys with one
command. If you hit a seam I missed, open an issue.

*Every screenshot here is from a live deployment. The AWS account id reads
`111122223333` throughout, and key ids, session tokens, certificate serials and
resource UUIDs are fixed placeholders — the redaction runs in the page before the
screenshot is taken.*
