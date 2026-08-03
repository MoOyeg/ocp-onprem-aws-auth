# Three ways to get on-prem OpenShift into AWS without an access key

Your cluster is in a rack. Your S3 bucket is not. Sooner or later something in
that cluster needs to call an AWS API, and the path of least resistance is an
access key in a Secret.

That key does not expire. It does not rotate unless you build something to rotate
it. Anyone with `get secrets` in that namespace has it, and so does anyone who
gets a copy of an etcd backup.

There are three ways out of that, and I built all three on the same cluster so
they could be compared honestly rather than in the abstract. The code is at
[MoOyeg/ocp-onprem-aws-auth](https://github.com/MoOyeg/ocp-onprem-aws-auth); it
is Ansible in a container, and the only thing you need installed is Podman.

The demo application is byte-identical in all three deployments. Ordinary boto3,
no credential handling, no AWS-specific code. Switching between mechanisms
changes exactly one environment variable — which is the whole argument.

---

## The three

| | IAM Roles Anywhere | OIDC federation | Vault AWS secrets engine |
|---|---|---|---|
| Identity is | a short-lived X.509 certificate | the pod's own ServiceAccount token | the pod's token, presented to Vault |
| Verified by | AWS | AWS | Vault |
| Sidecar | yes | **no** | yes (injected) |
| Standing cost | **~$400/mo** for a Private CA | none | none |
| Needs AWS to reach you | no | **yes** — a JWKS document | no |
| Long-lived secret | none | none | **Vault's own AWS key** |

Every one of them still needs the cluster to reach AWS outbound. None of this is
a way to use AWS APIs without talking to AWS. What differs is whether AWS also
has to reach *back*.

---

## 1. IAM Roles Anywhere

AWS will trust an X.509 certificate as proof of identity. cert-manager mints a
six-day certificate for each pod; a sidecar trades it for STS credentials and
serves them on a loopback IMDSv2 endpoint. The application thinks it is on EC2.

This is the one the [AWS blog post][aws-post] covers. It works, but the article
leaves out enough that I would not call it a walkthrough.

[aws-post]: https://aws.amazon.com/blogs/security/connect-your-on-premises-kubernetes-cluster-to-aws-apis-using-iam-roles-anywhere/

### What you build

A Private CA, in short-lived certificate mode:

![AWS Private CA in the console, short-lived certificate mode](../docs/images/aws-acm-pca.jpg)

A trust anchor over that CA, and one Roles Anywhere profile per identity:

![IAM Roles Anywhere trust anchor and profiles](../docs/images/aws-rolesanywhere-anchors.jpg)

Then cert-manager, from the Red Hat operator:

![The Red Hat cert-manager Operator installed](../docs/images/ocp-operators.jpg)

…and an `AWSPCAClusterIssuer` that mints certificates from that CA:

![AWSPCAClusterIssuer showing Ready](../docs/images/ocp-clusterissuer.jpg)

Six commands, or one:

```bash
./ansible-runner.sh iamra
```

The pods end up with two containers — the app, and the credential sidecar:

![Pods in iamra-demo, 2/2 containers each](../docs/images/ocp-pods-iamra.jpg)

And the app sees a certificate whose CN is exactly what the IAM trust policy
pins:

![The demo app showing its certificate and assumed role](../docs/images/app-iamra.jpg)

### The chicken and egg

`aws-privateca-issuer` needs AWS credentials to issue certificates. It gets those
credentials by presenting a certificate. Which it cannot have yet.

The way out is to issue exactly one certificate by hand, straight from the CA via
the AWS CLI, drop it in a Secret, let the issuer authenticate with it — and then
have cert-manager take ownership of that same Secret and renew it forever. After
the first minute the issuer is renewing the credential it authenticates with.

It is circular and it works, but it means the system has a state it cannot
recover from on its own: if the cluster is down long enough for that certificate
to expire, nothing can renew it. You delete the Secret and re-bootstrap.

### What the article does not tell you

**`aws_signing_helper serve` loads its certificate once, at startup.** When
cert-manager renews the certificate underneath the running process, the process
carries on presenting the old one. Roughly a week after a successful deployment,
every AWS call starts failing. Nothing in the article addresses this.

The fix is a liveness probe that fails when the mounted certificate is close to
expiry *while it is still valid* — kubelet restarts that container, it re-reads
the renewed files, and because it is a native sidecar the app container is never
touched.

**The probe has to be `exec`.** The credential endpoint binds `127.0.0.1`, which
is correct — nothing outside the pod should be able to ask it for AWS
credentials. But kubelet dials the *pod IP*. A `tcpSocket` probe is refused every
single time, and the sidecar restart-loops while its own log cheerfully reports
that it is serving.

**When it fails, it returns HTTP 200.** This is the one that will cost you an
afternoon. If the certificate is rejected, the helper does not report an error.
It answers the SDK with:

```json
{"AccessKeyId":"","SecretAccessKey":"","Token":"","Code":"Success",
 "Expiration":"0001-01-01T00:00:00Z"}
```

botocore parses that zero date and raises `OverflowError: date value out of
range`. Nothing in the traceback mentions AWS, credentials, or certificates. An
application catching the obvious `ClientError` turns the most common failure of
this entire architecture into an unhandled 500.

**Inline CSI volumes need `pod-security.kubernetes.io/enforce: privileged`.**
That is the real price of pod-lifecycle-bound certificates on OpenShift. It does
not make the pods privileged — SCC still governs, and every container here is
restricted-v2 compliant — but it removes the PSA net for anything else in that
namespace. OpenShift's label-sync controller will also quietly revert the label
unless you opt the namespace out.

### The honest security note

AWS Private CA has no condition key for the *subject* of a certificate request.
You can constrain the template; you cannot constrain the CN. So anyone who
obtains the issuer's role can mint a certificate with any CN and assume any
workload role under that trust anchor.

Compromise of the issuer is equivalent to compromise of every workload identity
in that trust domain. That is inherent to the design, not a mistake in the
policies — the issuer *has* to be able to mint workload certificates. The
controls that actually bound it are in the cluster: RBAC on who can request a
certificate, and optionally `approver-policy` to validate the CN before signing.

### And the $400

A Private CA bills continuously from creation until deletion. Disabling it does
not stop the charge, and deleting it enforces a paid restoration window of at
least seven days. The blog post does not mention this once.

You can avoid it entirely: a Roles Anywhere trust anchor can be backed by a CA
bundle you manage yourself. You lose `aws-privateca-issuer` and take on running
a CA, but everything downstream is unchanged.

---

## 2. OIDC federation

Your cluster already signs identity tokens for every pod. Teach AWS to trust that
signer and the pod can hand its own token to STS directly.

This is what EKS calls IRSA. It works on any cluster, not just EKS.

### What you build

Publish the cluster's OIDC discovery document and JWKS somewhere AWS can fetch
them, register that URL as an identity provider, and point
`spec.serviceAccountIssuer` at it:

![The IAM OIDC identity provider registered for the cluster](../docs/images/aws-iam-oidc-provider.jpg)

Then a role whose trust policy pins the token's `sub` and `aud`:

```json
"Condition": { "StringEquals": {
  "cluster-oidc-….amazonaws.com:sub": "system:serviceaccount:oidc-demo:s3-demo",
  "cluster-oidc-….amazonaws.com:aud": "sts.amazonaws.com"
}}
```

```bash
./ansible-runner.sh oidc
```

Here is the part worth looking at twice:

![Pods in oidc-demo, 1/1 container each](../docs/images/ocp-pods-oidc.jpg)

**One container.** No sidecar, no certificate, no helper process. The AWS SDKs
call `AssumeRoleWithWebIdentity` natively; the entire integration is two
environment variables and a projected volume.

![The demo app showing its token claims and assumed role](../docs/images/app-oidc.jpg)

### Is this just bound ServiceAccount tokens?

Related, but not the same. Bound tokens are the *mechanism*; OIDC federation is
the *trust relationship* that lets an outsider verify one. Every pod already has
a bound token and that alone gets you nothing with AWS.

We mint a second token deliberately, with its own audience:

| | default token | AWS token |
|---|---|---|
| `aud` | the API server | `sts.amazonaws.com` |
| lifetime | 1 year | 1 hour |

A token lifted from the AWS mount cannot be replayed against the Kubernetes API,
and vice versa. That is what audiences are for.

### What bit me

**The rollout has a split-brain window.** Changing `serviceAccountIssuer` rolls
every kube-apiserver, one at a time. While that happens they disagree, and
kubelet gets a token from whichever instance served it. Two pods of the same
ReplicaSet, created in the *same second*, came back with different `iss` claims —
one federated fine, the other was rejected with `InvalidIdentityToken`.

Watching the ClusterOperator's `Progressing` condition does not close the window:
it flaps between nodes and reads `False` in the gaps. You have to check that
every master is at the latest revision. And even then, pods created before the
change keep their old token until kubelet rotates it, so restart anything that
needs AWS access rather than waiting it out.

**A stale JWKS breaks everything at once.** When the cluster rotates its
ServiceAccount signing keys, the published document no longer contains the key
that signed live tokens, and *every* workload loses AWS access simultaneously.
Republishing is one command, but it needs to be a scheduled job, not something
you remember to do.

That is the real trade against Roles Anywhere. A bad certificate takes out one
pod. A stale JWKS takes out the cluster.

---

## 3. HashiCorp Vault

Let Vault hold the AWS credential and broker access. The pod proves itself to
Vault with its ServiceAccount token; Vault calls `sts:AssumeRole` on its behalf
and its agent renders the result into a credentials file.

```bash
./ansible-runner.sh vault
```

The pod carries annotations, not containers. Vault's mutating webhook rewrites it
at admission:

![Pods in vault-demo, 2/2 containers each](../docs/images/ocp-pods-vault.jpg)

![The demo app showing the Vault-rendered STS session](../docs/images/app-vault.jpg)

### What is structurally different

With the other two, **AWS verifies the workload**. The IAM trust policy names the
certificate CN or the token subject; you can read it and see which pod it admits.

With Vault, AWS verifies *Vault*. The workload role's trust policy contains
nothing that distinguishes one pod from another — all of that lives inside Vault,
in `bound_service_account_names` and `bound_service_account_namespaces`.

That is not worse, but it is different, and it matters for where you go looking
when something is denied.

### The credential you cannot remove

Vault holds a long-lived AWS access key so your pods do not have to. That
concentrates risk in one audited place, which is a genuine improvement over a key
per namespace — but it is still a key, and it is the single thing in this design
an attacker would go for.

Its blast radius is deliberately tiny: `sts:AssumeRole` on exactly one role, no
`iam:*`, no wildcards. The upstream docs commonly show `iam:*` on `*` so the
engine can create IAM users; that credential type is not used here, so those
permissions are not needed.

You can remove even that key by giving Vault itself credentials via IAM Roles
Anywhere and pointing the AWS engine at the sidecar. Certificates authenticate
Vault; Vault brokers everything else. More moving parts, but it closes the last
gap.

### Use `assumed_role`, not `iam_user`

Vault's AWS engine can create a real IAM user and access key per lease. That is a
long-lived credential with a scheduled deletion — precisely what this exercise
exists to avoid. `credential_type=assumed_role` gives you an STS session that
expires on its own. The validation in this repo asserts the rendered key starts
with `ASIA` rather than `AKIA` for exactly that reason.

### The one that wasted an hour

The Vault Helm chart's `global.openshift: true` does two things. It gives you the
OpenShift-appropriate security contexts you want, and it silently repoints every
image at `registry.connect.redhat.com` — which does not carry the chart's default
tags. Both pods go straight to `ImagePullBackOff` with "name unknown: Image not
found". Keep the flag, override the image repositories back.

---

## All three at once

They coexist. Same cluster, same image, three namespaces:

![Three namespaces, one per method](../docs/images/ocp-projects.jpg)

![All four IAM roles in the console](../docs/images/aws-iam-roles.jpg)

Each method is completely independent — its own namespace, its own IAM role, its
own infrastructure. Running one neither creates nor requires anything belonging
to the other two.

---

## So which one

**Start with OIDC if you can publish a JWKS document.** No standing cost, no
sidecar, no certificate machinery, and the SDKs do the work. What it asks in
return is real — AWS must be able to fetch a document you host, and you have to
reconfigure the cluster's token issuer — but it is by a distance the simplest of
the three once it is running.

**Use Roles Anywhere when publishing anything AWS-reachable is off the table**,
or when you need the same mechanism for machines that are not in Kubernetes at
all. Roles Anywhere covers VMs and CI runners; OIDC federation does not. Back the
trust anchor with your own CA and the $400 goes away.

**Use Vault if you already run Vault.** Not otherwise — standing up Vault to
solve this one problem trades a credential-distribution problem for an
availability problem. If it is already there, the audit trail and per-workload
policy are genuinely valuable.

---

## The bit I did not expect

Almost everything that cost me time was not in the architecture. It was in the
seams:

- a probe type that cannot possibly work against a loopback listener
- a helper that returns `200 OK` when it has failed
- a Helm flag that fixes security contexts and breaks image pulls
- an Ansible conditional that is true for the playbook that sets it
- `oc start-build --follow` returning before the Build object is updated
- an SELinux mount flag that makes two concurrent runs impossible

None of these are in anyone's documentation, which is why they are all written
down in the repo's README, at the point in the code where they bite.

If you build one of these, budget time for the seams.

---

*Code: [github.com/MoOyeg/ocp-onprem-aws-auth](https://github.com/MoOyeg/ocp-onprem-aws-auth)*
*Every screenshot here is from a live deployment. The AWS account id is replaced
throughout with the documentation-reserved `111122223333`, and key ids, session
tokens, certificate serials and resource UUIDs are replaced with fixed
placeholders — the redaction runs in the page before the screenshot is taken.*
