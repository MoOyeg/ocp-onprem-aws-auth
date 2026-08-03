# On-prem OpenShift → AWS APIs, with no long-lived credentials

**Three** ways to give on-prem OpenShift workloads AWS API access without an
access key, implemented side by side so you can compare them on one cluster. All
driven by Ansible in a Podman container — no local Ansible install, nothing to
install but Podman.

The demo application is **byte-identical** across all three. That is the point:
[`app/app.py`](app/app.py) is ordinary boto3 with no credential handling at all,
and switching mechanisms changes one environment variable.

| | `iamra` | `oidc` | `vault` |
|---|---|---|---|
| What proves identity | short-lived X.509 certificate | projected ServiceAccount token | ServiceAccount token, to Vault |
| Who verifies it | AWS | AWS | Vault |
| Sidecar in the pod | yes | **no** | yes (injected) |
| Recurring AWS cost | **~$400/mo** (Private CA) | none | none |
| Outbound reachability | `rolesanywhere` + `acm-pca` | `sts` | `sts` (from Vault only) |
| Inbound reachability | none | **JWKS document AWS can fetch** | none |
| Long-lived secret anywhere | **none** | **none** | Vault's AWS key |
| Cluster reconfiguration | none | **kube-apiserver rollout** | none |
| PSA relaxation needed | **`privileged`** | none | none |
| Blast radius of a failure | one workload | **whole cluster** | Vault outage |
| Works for non-Kubernetes hosts | **yes** | no | yes |

Started from
[Connect your on-premises Kubernetes cluster to AWS APIs using IAM Roles Anywhere](https://aws.amazon.com/blogs/security/connect-your-on-premises-kubernetes-cluster-to-aws-apis-using-iam-roles-anywhere/),
with the gaps in that article closed and the OpenShift-specific parts handled —
then extended with the two alternatives.

```
iamra                             oidc                        vault
─────                             ────                        ─────
AWS Private CA                    cluster JWKS ──► S3         Vault (k8s auth)
   │  (≤7 day certs)                 │ (public)                  │
IAM Roles Anywhere                IAM OIDC provider           AWS secrets engine
trust anchor                         │                           │ assumed_role
   │                              role trusts sub+aud         role trusts Vault's
role trusts cert CN                  │                        IAM user
   │                                 │                           │
cert-manager + CSI                projected SA token          vault-agent injector
   │                                 │                           │
sidecar → IMDSv2 :9911            AWS SDK, natively           renders creds file
   │                                 │                           │
   └──────────────────── the same unmodified boto3 app ──────────┘
```

Choose with `-e auth_method=`; each deploys to its own namespace
(`iamra-demo`, `oidc-demo`, `vault-demo`) and they coexist.

📄 **[docs/environment-capture.md](docs/environment-capture.md)** — a capture of
all three running side by side, with screenshots and real terminal output
(account ids and secrets redacted). Regenerate it with
[`docs/capture-environment.sh`](docs/capture-environment.sh).

---

## Choosing a method

First, the thing that is true of all three: **the workload side needs a network
path to AWS.** `iamra` pods reach `rolesanywhere` on every `CreateSession`,
`oidc` pods reach `sts` on every `AssumeRoleWithWebIdentity`, and under `vault`
the Vault server reaches `sts` on your pods' behalf. None of this is a way to use
AWS APIs without talking to AWS. See [Network](#network) for the exact endpoints.

What differs is whether AWS also has to reach back.

**Start with `oidc` if you can publish a JWKS document.** It has no standing
cost, no sidecar, no certificate machinery, and the AWS SDKs do the federation
natively. What it asks in return is real: AWS STS must be able to *fetch* your
cluster's OIDC discovery document and JWKS, and you must reconfigure
`spec.serviceAccountIssuer`, which rolls every kube-apiserver. Live with the
consequence that a stale JWKS — after the cluster rotates its ServiceAccount
signing keys — breaks *every* workload at once, where a bad certificate breaks
one.

**Use `iamra` when publishing anything AWS-reachable is off the table**, when you
need the same mechanism for non-Kubernetes on-prem hosts (VMs, CI runners — Roles
Anywhere covers those, OIDC does not), or when you want failures scoped per
workload. Its trust material is uploaded to AWS once, at trust-anchor creation,
so nothing of yours ever has to be reachable from outside — but the sidecar does
still call out to `rolesanywhere` for every session. If you go this way, consider
backing the trust anchor with
[your own CA](#using-your-own-ca-instead-of-private-ca) rather than AWS Private
CA and the $400/month disappears.

**Use `vault` if you already run Vault**, or if you need one broker for AWS *and*
everything else, with per-workload policy and a full audit trail. Be clear-eyed
that it moves the long-lived credential rather than removing it: Vault holds an
AWS key so your pods do not have to. That concentrates risk in one well-audited
place, which is a genuine improvement — but it is still a key, and it is the one
thing in this repo that an attacker would go for. See
[Bootstrapping Vault without a static key](#bootstrapping-vault-without-a-static-key).

There is also a structural difference worth noticing. With `iamra` and `oidc`,
**AWS itself verifies the workload's identity** and the IAM trust policy can
condition on it. With `vault`, AWS only verifies Vault — the role's trust policy
contains nothing that distinguishes one pod from another, and all of that
authorization lives inside Vault instead.

## Read this before you start

Applies to the **`iamra`** path only.

**The Private CA costs roughly $400/month**, from creation until deletion.
Disabling it does not stop the charge, and deletion enforces a 7–30 day
restoration window during which you are still billed. `destroy -e destroy_aws=true`
uses the 7-day minimum. This is the single largest practical consideration in the
design and the blog post does not mention it.

**Short-lived certificate mode has no revocation.** AWS Private CA in
`SHORT_LIVED_CERTIFICATE` mode publishes no CRL and supports no OCSP. Your
revocation story is expiry — 6 days for the certificates issued here, or
immediately if you disable the Roles Anywhere profile or trust anchor. Set
`pca_usage_mode: GENERAL_PURPOSE` if you need real revocation, and expect to pay
more per certificate.

**A cheaper variant exists.** An IAM Roles Anywhere trust anchor can be backed by
an external CA bundle you manage yourself, with no Private CA at all. See
[Using your own CA instead of Private CA](#using-your-own-ca-instead-of-private-ca).

---

## Requirements

| | |
|---|---|
| Local | `podman`. That is all — Ansible, `oc`, `helm` and the AWS CLI live in the runner image |
| OpenShift | 4.16+ — native sidecars (`initContainers` with `restartPolicy: Always`) |
| OpenShift | the internal image registry enabled, for the default in-cluster builds |
| AWS | permissions for `acm-pca`, `rolesanywhere`, `iam`, `s3` |

Verified against OpenShift 4.22, `aws_signing_helper` 1.7.0,
`aws-privateca-issuer` v1.9.1, `cert-manager-csi-driver` v0.15.0.

### Network

**Every method needs outbound reachability to AWS.** None of them work in an
environment with no path to AWS at all — the pod (or Vault) has to reach an AWS
endpoint to obtain credentials in the first place.

| Method | Who connects out | To what | How often |
|---|---|---|---|
| `iamra` | every pod's sidecar | `rolesanywhere.<region>.amazonaws.com` | on every `CreateSession`, i.e. at startup and each credential refresh |
| `iamra` | the `aws-privateca-issuer` pod | `acm-pca.<region>.amazonaws.com` | whenever a certificate is issued or renewed |
| `oidc` | every pod (via the AWS SDK) | `sts.<region>.amazonaws.com` | on `AssumeRoleWithWebIdentity` and each refresh |
| `vault` | the Vault server only | `sts.<region>.amazonaws.com` | when a lease is created or renewed |

All of that is **outbound**, and all of it can go over PrivateLink / VPC
endpoints reached via Direct Connect or a VPN — none of it requires the public
internet. (The AWS blog post says this pattern needs public internet access; it
does not.) Note the `vault` row: application pods never talk to AWS at all, only
Vault does, which is the smallest egress footprint of the three.

**Only `oidc` additionally needs inbound reachability** — AWS STS must fetch your
OIDC discovery document and JWKS before it can verify a token. That is a
connection *to* something you publish, and it is the one requirement `iamra` and
`vault` do not have. In this implementation the document is served from an S3
bucket, so your cluster is still never exposed; what you take on is the
obligation to publish it somewhere AWS can reach and keep it in step with the
cluster's signing keys.

## Quick start

**Each method is a complete, independent deployment.** Pick one and run it — it
creates only its own namespace, its own IAM role and its own infrastructure, and
neither creates nor requires anything belonging to the other two.

```bash
export KUBECONFIG=/path/to/kubeconfig      # or copy it to ./kubeconfig
# AWS credentials are read from ~/.aws (or $AWS_SHARED_CREDENTIALS_FILE)

./ansible-runner.sh build

./ansible-runner.sh iamra      # or
./ansible-runner.sh oidc       # or
./ansible-runner.sh vault
```

Each of those runs [site.yml](site.yml) end to end for that method: build the app
image → stand up that method's infrastructure → deploy the demo app → verify it
from inside the pod. Run one and you get one; run all three and you get three
live comparisons on one cluster, in `iamra-demo`, `oidc-demo` and `vault-demo`.

`./ansible-runner.sh install -e auth_method=oidc` is the same thing spelled out.

### Individual steps

Every step is separately runnable and rediscovers whatever it needs, so you can
re-run any one on its own:

| Command | Playbook | Method | What it does |
|---|---|---|---|
| `images` | [images.yml](images.yml) | all | builds the demo app **in the cluster** (plus the sidecar, for iamra) |
| `iamra-setup` | [iamra.yml](iamra.yml) | iamra | all three steps below |
| `aws` | [aws.yml](aws.yml) | iamra | Private CA, trust anchor, both IAM roles + Roles Anywhere profiles, S3 bucket |
| `certmanager` | [certmanager.yml](certmanager.yml) | iamra | Red Hat cert-manager Operator + the CSI driver |
| `issuer` | [issuer.yml](issuer.yml) | iamra | bootstrap certificate → `aws-privateca-issuer` → `AWSPCAClusterIssuer` |
| `oidc-setup` | [oidc.yml](oidc.yml) | oidc | publish JWKS → register IAM OIDC provider → web-identity role |
| `vault-setup` | [vault.yml](vault.yml) | vault | deploy Vault + injector → AWS secrets engine → Kubernetes auth |
| `app` | [app.yml](app.yml) | any | the demo workload |
| `validate` | [validate.yml](validate.yml) | any | proves it end to end from inside the pod |

`app`, `validate` and `destroy` act on one method's resources, so they **refuse
to run without being told which**:

```bash
./ansible-runner.sh validate -e auth_method=vault
```

Defaulting them would mean `validate` quietly reporting on `iamra` right after
you deployed `vault`, or `destroy` removing the wrong namespace.

### What "independent" actually means

| | `iamra` | `oidc` | `vault` |
|---|---|---|---|
| Namespace | `iamra-demo` | `oidc-demo` | `vault-demo` |
| IAM role | `ocp-iamra-app-s3` | `ocp-oidc-app-s3` | `ocp-vault-app-s3` |
| Also creates | Private CA, trust anchor, profiles, `ocp-iamra-issuer` | S3 bucket + IAM OIDC provider | IAM user for Vault |
| Cluster components | cert-manager operator, CSI driver, aws-privateca-issuer | none | Vault + agent injector |
| Sidecar image built | yes | **no** | **no** |

The only shared things are the demo app image (namespace `aws-auth-images`) and
the S3 bucket the app writes to. The credential sidecar image is built **only**
for `iamra` — a standalone `oidc` install has no reason to depend on
`aws_signing_helper` being downloadable.

The `oidc` step stops short of the disruptive part by default. It publishes
everything and registers the provider, then tells you what is still missing.
Nothing federates until you opt in:

```bash
./ansible-runner.sh oidc -e oidc_set_service_account_issuer=true
```

Everything is configured in
[inventory/group_vars/all.yml](inventory/group_vars/all.yml) and overridable per
run:

```bash
./ansible-runner.sh aws -e demo_bucket=my-unique-bucket -e aws_region=eu-west-1
./ansible-runner.sh app -e app_namespace=team-a -e app_service_account=payments
```

There is **no state file**. Every AWS object is looked up by name (or, for the
CA, by subject Common Name) on each run, so re-running is idempotent and nothing
has to be written back.

Teardown, per method:

```bash
# that method's cluster resources only
./ansible-runner.sh destroy --destroy -e auth_method=vault

# ...and its AWS resources. For iamra this is what stops the Private CA bill.
./ansible-runner.sh destroy --destroy -e auth_method=iamra -e destroy_aws=true

# all three namespaces at once
./ansible-runner.sh destroy --destroy -e auth_method=iamra -e destroy_all_methods=true
```

`destroy` refuses to run without `-e auth_method=` — it removes a namespace, and
guessing which one is not a risk worth taking.

## The demo application

[`app/`](app/) is a small Flask service that calls `sts:GetCallerIdentity` and
reads and writes an S3 bucket. The point of it is what is *not* there: no access
key, no `~/.aws/credentials`, no `credential_process`, no AWS-specific code at
all. [`app/app.py`](app/app.py) is ordinary boto3.

The only thing that makes it work is one environment variable in
[the deployment template](roles/demo_app/templates/deployment.yaml.j2):

```yaml
- name: AWS_EC2_METADATA_SERVICE_ENDPOINT
  value: "http://127.0.0.1:9911/"
```

The route shows the assumed role ARN, the certificate currently being presented
with a countdown to its expiry, and the bucket contents, with a button that
writes an object to prove the credentials carry write access. That certificate
panel is the useful part when something breaks: when `AssumeRole` starts failing,
the answer is nearly always that the CN shown there does not match the CN pinned
in the role's trust policy.

To port your own workload: add the sidecar as an `initContainer` with
`restartPolicy: Always`, mount the CSI volume, set that one environment variable,
and give the ServiceAccount the RBAC in
[rbac.yaml.j2](roles/demo_app/templates/rbac.yaml.j2). Nothing else changes.

## What this does differently from the blog post

### Bugs in the article

| Article | Here |
|---|---|
| Workload pod references `issuer-name: my-pca`, an issuer the walkthrough never creates | `iamra-pca`, the issuer that is actually created |
| Uses a namespaced `AWSPCAIssuer` in `cert-manager`, then references it from a pod in another namespace — impossible | `AWSPCAClusterIssuer`, which is what a multi-namespace setup needs |
| Requests `CN=${SERVICE_ACCOUNT_NAME}.${POD_NAMESPACE}` but only ever shows a trust policy pinning `CN=iamra-issuer`; the workload role's trust policy is never given | [Both](roles/aws_iam/templates/) trust policies, the workload one pinning the CN the CSI driver actually stamps |
| `CertificateRequestPolicy` RBAC binds only the `cert-manager` ServiceAccount, which never sees CSI-driven requests | CSI driver runs with `useTokenRequest: true`, so requests arrive as the *app's* ServiceAccount and RBAC binds that |
| Pins `subject.organizations`/`organizationalUnits` for CSI-issued certificates | The CSI driver cannot set O or OU — [only CN, DNS SANs, URI SANs](https://cert-manager.io/docs/usage/csi-driver/). Pinning them would deny every request, so the workload policy pins CN only |
| Sidecar is a plain container; the sample pod is `restartPolicy: Never` and can never complete | Native sidecar — ordered startup gated on a probe, ordered shutdown, works in Jobs |
| `alpine:3.17.0` (EOL), `aws_signing_helper` 1.3.0, unverified download, `ln -s libc.musl-x86_64.so.1 /lib/libresolv.so.2` | UBI 9, 1.7.0, SHA-256 verified at build. The binary is glibc-linked, so the musl symlink was never needed on UBI |
| `kubectl edit ClusterRole ...` to add permissions | Helm values and Ansible-managed resources, so nothing is silently reverted on upgrade |

### Certificate rotation

Nothing in the article addresses this. `aws_signing_helper serve` loads the
certificate and key once at startup, so when cert-manager renews them underneath
the process it keeps presenting the old ones — and roughly a week after a
successful deployment, every AWS call starts failing.

[`sidecar/healthcheck.sh`](sidecar/healthcheck.sh) is the liveness probe and
fails once the mounted certificate is inside `RELOAD_BEFORE` of expiry. kubelet
restarts that container, it re-reads the renewed files, and because it is a
native sidecar the application container is never disturbed. `RELOAD_BEFORE` is
set shorter than the certificate's `renewBefore` so the replacement is already on
disk when it fires.

Durations are `144h` (6 days), not the `168h` the article uses: 168h sits exactly
on the Private CA short-lived cap with no margin for clock skew.

### The failure mode you will actually hit

When the certificate exchange fails, `aws_signing_helper serve` does **not**
report an error to the SDK. It answers with HTTP 200 and an empty credential
document:

```json
{"AccessKeyId":"","SecretAccessKey":"","Token":"","Code":"Success",
 "Expiration":"0001-01-01T00:00:00Z"}
```

botocore then tries to parse that zero date and raises
`OverflowError: date value out of range`. Nothing in the traceback mentions AWS,
credentials, or certificates — so an application catching the obvious
`ClientError` / `NoCredentialsError` turns the most common failure of this
architecture into an unhandled 500. [`app/app.py`](app/app.py) catches it and
says where to look; [validate.yml](validate.yml) explains it too.

### Things that only show up when you actually run this

Every one of these cost a failed run, and none of them appear in the blog post.
They are called out at the point of use in the code as well.

**Roles Anywhere profiles reject a client-supplied session name by default.** The
sidecar sets one — the pod name, so CloudTrail can attribute an AWS call to an
individual pod (`assumed-role/ocp-iamra-app-s3/s3-demo-5c457f7ffc-57hc9`). Without
`--accept-role-session-name` on the profile, every `CreateSession` returns
`AccessDeniedException: Not authorized to set roleSessionName`. The article never
sets a session name, so it never hits this — and loses per-pod attribution.

**Probes on the sidecar must be `exec`.** `aws_signing_helper serve` binds
`127.0.0.1` only, which is correct — nothing outside the pod should be able to
ask it for AWS credentials. kubelet dials the *pod IP*, so a `tcpSocket` or
`httpGet` probe is refused every time and the sidecar restart-loops while its log
cheerfully reports that it is serving.

**Inline CSI volumes require `pod-security.kubernetes.io/enforce: privileged`.**
This is the real price of pod-lifecycle-bound certificates. It does not make the
pods privileged — SCC still governs and every container here is restricted-v2
compliant — but it does remove the PSA net for anything else in that namespace.
OpenShift's label-sync controller will also revert the label unless the namespace
carries `security.openshift.io/scc.podSecurityLabelSync: "false"`. See
[Avoiding the privileged PSA label](#avoiding-the-privileged-psa-label).

**`csi.cert-manager.io/pkcs12-enable` must be absent, not `"false"`.** The driver
validates it with `if enable := attr[KeyStorePKCS12EnableKey]; len(enable) > 0`,
so *any* value — including the string `"false"` — takes the enabled branch and
the mount then fails demanding a `pkcs12-password`. Omitting the key is the only
way to turn it off.

**Do not pre-set the namespace's SCC range annotations.** Declaring
`openshift.io/sa.scc.uid-range` yourself looks like the tidy way to make the pod's
`fsGroup` and `csi.cert-manager.io/fs-group` agree. It is a trap: OpenShift's
allocator treats a namespace carrying any of those annotations as already done
and never assigns `openshift.io/sa.scc.mcs`, after which *no pod in the namespace
can be admitted at all* — builds never start and the error names an annotation
you have never heard of. Create the namespace bare and read the range back, which
is what [ensure_app_namespace.yml](tasks/ensure_app_namespace.yml) does.

**The bootstrap certificate must outlive the sidecar's reload window.** A one-day
bootstrap certificate deadlocks against a 24h `RELOAD_BEFORE`: it is "about to
expire" from birth, the sidecar never becomes healthy, the issuer never becomes
Ready, and cert-manager therefore never gets to issue the long-lived replacement.
It is issued for 7 days, and the expiry check is excluded from the *startup*
probe for the same reason.

**Removing a field needs server-side apply.** `state: present` issues a
strategic-merge patch, which cannot delete a map key — take a line out of
`volumeAttributes` and the live object keeps it indefinitely. The demo_app
resources use `apply: true`. The same applies to the sidecar patch, where
changing a probe's handler type leaves both handlers in place and the API server
rejects it with "may not specify more than 1 handler type"; there the old handler
is deleted with an explicit `null`.

**`trim_blocks` bites twice.** Ansible templates with `trim_blocks` enabled. An
*indented* multi-line `{# … #}` comment leaves its leading whitespace glued to
the next line, silently doubling the indent and breaking the YAML mapping — so
in-YAML explanations here are `#` comments, not Jinja ones. The same rule eats
the newline after `{% endraw %}`, which welded a comment onto the end of the
Vault agent template until it became `{% endraw +%}`.

**Two methods want a role called `ocp-<method>-app-s3`, and both will configure
it.** `app.yml` originally ran `aws_iam` unconditionally, so deploying with
`auth_method=vault` rewrote that role's trust policy with the IAM Roles Anywhere
version and Vault instantly lost the ability to assume it. Method-specific roles
are now resolved *inside* `demo_app`, so only the owner of a resource ever
touches it.

**A play var defeats the `when` on its own `import_playbook`.** `site.yml` guards
each method with `when: auth_method == '<method>'`, but that conditional is
evaluated with the *imported play's* vars already in scope. `oidc.yml` and
`vault.yml` each set `vars: auth_method:` so they would work standalone — which
made every guard true for its own playbook, so a plain `install` silently
configured **all three methods**. Worse, the last play to run left its
`app_role_arn` fact behind, and `app.yml` deployed the iamra workload pointing at
the *vault* role; the only symptom was `AccessDeniedException: Requested Role
isn't one of those listed in the Profile` from a sidecar half an hour later.

Two defences now, because either alone is fragile:

- Those playbooks no longer set `auth_method` themselves; `ansible-runner.sh`
  passes `-e auth_method=…` for the standalone commands, which is extra-var
  precedence and composes correctly with `site.yml`.
- `demo_app` checks that the `app_role_arn` in scope actually *ends in the role
  name this method expects*, rather than merely being defined, and re-resolves if
  not. Facts persist across plays; "is it defined" is not the same question as
  "is it mine".

**The Vault chart's `openshift: true` breaks image pulls.** It gives you the
OpenShift-appropriate security contexts you want *and* repoints every image at
`registry.connect.redhat.com`, which does not carry the chart's default tags —
both pods go to `ImagePullBackOff` with "name unknown: Image not found". Keep the
flag, override the image repositories back.

**IAM rejects a trust policy naming a principal that does not exist yet.** The
Vault workload role trusts Vault's IAM user, so the user has to be created first
or you get `MalformedPolicyDocument: Invalid principal in policy`. IAM is also
eventually consistent about brand-new principals, hence the retry.

**`oc exec` needs `-i` to accept stdin.** Piping a Vault policy into
`vault policy write … -` without it produces "'policy' parameter not supplied or
empty", which reads like a Vault problem rather than a plumbing one.

**Ansible's `command` module shlex-splits its argument**, so a jsonpath
containing a space arrives at `oc` in pieces ("error parsing jsonpath {range,
unclosed action"). Query the API and pick fields in Jinja instead.

**The serviceAccountIssuer rollout has a split-brain window.** While the masters
roll one at a time they disagree about `spec.serviceAccountIssuer`, and kubelet
gets a token from whichever instance served it. Two pods of the same ReplicaSet,
created in the *same second*, came back with different `iss` claims — one
federated fine, the other was rejected with `InvalidIdentityToken`.

Watching the ClusterOperator's `Progressing` condition does not close this: it
flaps between nodes and reads `False` in the gaps. The playbook now waits until
every entry in `KubeAPIServer.status.nodeStatuses` is at
`latestAvailableRevision` with no `targetRevision` pending. Even then, **pods
created before the change keep their old-issuer token** until kubelet rotates it
(~80% of lifetime), so restart anything that needs AWS access rather than waiting
it out.

**A fact set inside a role does not exist in plays where that role did not run.**
`oidc_issuer_url` started life as a `set_fact` in `oidc_provider`; `validate` on
its own then died with "'oidc_issuer_url' is undefined". It and the bucket name
are now *derived vars* in `inventory/group_vars/all.yml` — lazy Jinja over
`aws_account_id` — so any playbook that ran `preflight` can use them. Anything
two playbooks both need is a var, not a fact.

**`:Z` on the workspace mount makes concurrent runs impossible.** `:Z` stamps a
private SELinux category on the source directory, so starting a second container
against the same repo — a quick `--syntax-check` while a deploy is running, say —
relabels it and the first one dies mid-task with
`PermissionError: [Errno 13] Permission denied: b'/workspace'`. The runner uses
`:z` (shared) for the workspace, the kubeconfig and `~/.aws`.

**`oc start-build --follow` returns before the Build resource is updated.** It
exits when the build *pod* finishes, but `status.phase` can still read `Running`
for a moment while the controller catches up — so reading the phase immediately
afterwards fails intermittently on a build that actually succeeded. Poll for a
terminal phase (`Complete`/`Failed`/`Error`/`Cancelled`) first, then assert which
one it is.

### OpenShift specifics

- **cert-manager comes from the Red Hat operator**
  (`openshift-cert-manager-operator`), resolved to the catalog's default channel
  at run time, not the Jetstack Helm chart. The CSI driver and
  `aws-privateca-issuer` have no Red Hat equivalent and come from their upstream
  charts — a deliberate mix of supported and community components. Decide that
  consciously before production.
- **SCC.** The chart's hard-coded `runAsUser: 65532` is removed; under
  `restricted-v2` OpenShift allocates a UID from the namespace range and a pod
  pinning one outside it is rejected. Every container declares
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `runAsNonRoot: true`, `drop: [ALL]`, `seccompProfile: RuntimeDefault`.
- **File ownership.** The CSI driver chowns what it writes to a fixed GID. If
  OpenShift also picks the namespace's GID range, the two disagree and `tls.key`
  ends up unreadable.
  [ensure_app_namespace.yml](tasks/ensure_app_namespace.yml) declares the ranges
  so the pod can pin `fsGroup: 2000` and the CSI attribute to the same value.
- **Secret mode `0440`, not `0400`,** so the arbitrary UID can read the issuer's
  key through its GID-0 primary group.
- **The CSI driver needs the `privileged` SCC** — hostPath mounts under
  `/var/lib/kubelet` with bidirectional propagation. The chart creates that
  binding itself; the values set it explicitly rather than relying on
  autodetection.
- **In-cluster builds.** Both images are built with OpenShift binary builds into
  ImageStreams, so there is no external registry and no push credentials. The
  issuer runs in `cert-manager` but pulls the sidecar from the app namespace, so
  a `system:image-puller` RoleBinding is created for it — without that the issuer
  pod sits in `ImagePullBackOff`. Set `build_images_in_cluster: false` and point
  `sidecar_image` / `app_image` elsewhere to opt out.

### Who can get a certificate

The trust anchor accepts any certificate chaining to the CA, so the real question
is who can obtain one bearing a privileged CN.

The control here is RBAC. With `useTokenRequest: true`, every
`CertificateRequest` is created as the mounting pod's ServiceAccount, so the
`Role`/`RoleBinding` in
[rbac.yaml.j2](roles/demo_app/templates/rbac.yaml.j2) decides which workloads can
get a certificate at all.

That bounds *who*, not *what CN they ask for*. Full content validation needs
cert-manager `approver-policy`, which requires disabling cert-manager's built-in
approver cluster-wide — that breaks unrelated certificates (Let's Encrypt ingress
certs, for instance) unless a catch-all policy is applied first, and the Red Hat
operator may not accept the required `--controllers` argument at all. The article
makes that change without comment. It is deliberately not automated here; check
first with:

```bash
oc explain certmanager.spec.controllerConfig.overrideArgs
```

## Inspecting a running pod

Every container has a shell (`bash`, `python3`, `openssl`, `curl`), so
`oc rsh deploy/s3-demo` works. Two purpose-built tools are on `PATH` because
decoding this stuff by hand is tedious and the interesting endpoint is
loopback-only.

### `token-info` — in the app container

Prints whatever *this* pod uses to prove its identity, which is a different
object under each method, plus the AWS environment and the default Kubernetes
token for comparison.

```bash
oc -n oidc-demo  exec deploy/s3-demo -c app -- token-info
oc -n iamra-demo exec deploy/s3-demo -c app -- token-info
oc -n vault-demo exec deploy/s3-demo -c app -- token-info

oc -n oidc-demo  exec deploy/s3-demo -c app -- token-info --raw    # full token / secret values
oc -n vault-demo exec deploy/s3-demo -c app -- token-info --json   # machine-readable
```

Secrets are redacted unless you pass `--raw`. JWT payloads are base64url and
unencrypted, so decoding one discloses nothing the verifier does not already
see — the signature is never printed.

Showing the default Kubernetes token alongside the AWS one is deliberate: under
`oidc` the fastest way to understand a rejected token is to compare their
audiences and lifetimes side by side.

```
--- projected token for AWS ---
  audience   ['sts.amazonaws.com']
  lifetime_seconds  3600

--- default token for the Kubernetes API ---
  audience   ['https://cluster-oidc-….amazonaws.com', 'https://kubernetes.default.svc']
  lifetime_seconds  31536000
```

### `imds-probe` — in the iamra sidecar

Queries the credential endpoint exactly the way an AWS SDK does — IMDSv2, token
first. It has to run *inside* the sidecar because the listener binds `127.0.0.1`.

```bash
oc -n iamra-demo exec deploy/s3-demo -c iamra-sidecar -- imds-probe
oc -n iamra-demo exec deploy/s3-demo -c iamra-sidecar -- imds-probe --raw
```

It prints the certificate being presented, the configured ARNs, the full IMDSv2
exchange, and the credentials returned. Critically it inspects the *fields*
rather than the status code, because a failed certificate exchange still returns
HTTP 200 with `Code: Success` and empty keys — so it flags that case explicitly
instead of looking like success.

## Troubleshooting

```bash
# What the sidecar is presenting — it logs the certificate subject at startup
oc -n iamra-demo logs deploy/s3-demo -c iamra-sidecar

# Whether the certificate was issued
oc -n iamra-demo get certificaterequest -o wide

# Whether the issuer itself can reach AWS
oc -n cert-manager logs deploy/iamra-aws-privateca-issuer -c aws-privateca-issuer
```

| Symptom | Cause |
|---|---|
| `AccessDenied` on `AssumeRole` | Certificate CN does not match `aws:PrincipalTag/x509Subject/CN` in the trust policy. Compare the two — by far the most common failure |
| `OverflowError: date value out of range` | The same thing, seen through botocore. See above |
| `Not authorized to set roleSessionName` | Profile lacks `--accept-role-session-name`; re-run `aws` |
| `Invalid or empty profile provided` | The Roles Anywhere profile no longer exists. Check `aws sts get-caller-identity` — on a sandbox/temporary account the whole account may have been recycled underneath you, in which case nothing from a previous run survives. See below |
| `Requested Role isn't one of those listed in the Profile` | The pod is presenting a role from a *different* auth method. Check `ROLE_ARN` on the sidecar against the profile's `roleArns` |
| ClusterIssuer stuck not Ready after an AWS-side fix | A running sidecar holds its startup session state. The playbook restarts it and retries once automatically |
| Startup probe `connection refused` on 9911 | A `tcpSocket` probe against a loopback-only listener. Must be `exec` |
| `uses an inline volume provided by CSIDriver ... lower than privileged` | Namespace needs `pod-security.kubernetes.io/enforce: privileged` |
| `pkcs12-password: Required value` | `pkcs12-enable` is present. Remove the key entirely — `"false"` does not work |
| `unable to find annotation openshift.io/sa.scc.mcs` | SCC range annotations were pre-set on the namespace. Delete and recreate it bare |
| A removed field keeps coming back | Patch instead of apply. Use `apply: true` |
| Sidecar: `tls.key` permission denied | Pod `fsGroup` and `csi.cert-manager.io/fs-group` disagree |
| Issuer pod `ImagePullBackOff` | The `system:image-puller` RoleBinding for the cert-manager namespace is missing; re-run `images` |
| Everything worked, then broke about a week later | Certificate rotation. Confirm the liveness probe is the `exec` one, not a bare TCP check |
| `NoCredentialsError` in the app | Sidecar not running, or `AWS_EC2_METADATA_SERVICE_ENDPOINT` unset |
| Sidecar: dial/timeout/TLS errors reaching `rolesanywhere` | No egress path from the pod network to `rolesanywhere.<region>.amazonaws.com`. Every session needs it — see [Network](#network). Check the pod's route to AWS, any egress firewall, and whether a proxy is required (`HTTPS_PROXY` is honoured by the signing helper) |
| Issuer works, workloads do not (or vice versa) | They use different endpoints: the issuer needs `acm-pca`, workload sidecars need `rolesanywhere`. A VPC endpoint or firewall rule covering one but not the other produces exactly this split |

### When the AWS account changes underneath you

On a sandbox or time-boxed account, everything can vanish at once — and because
this project keeps no state file, the symptom is confusing: the playbooks happily
create fresh resources in the *new* account while the cluster keeps presenting
credentials for the old one.

Check first:

```bash
aws sts get-caller-identity          # is this the account you deployed into?
```

If it changed, clear the one piece of cluster state that still points at the old
account and re-run. Everything else is rediscovered:

```bash
oc -n cert-manager delete certificate iamra-issuer secret/iamra-issuer --ignore-not-found
oc delete awspcaclusterissuer iamra-pca --ignore-not-found
./ansible-runner.sh install
```

Bucket names and the OIDC issuer URL both embed the account id, so they change
automatically. Vault's `aws/config/root` and role are overwritten by re-running
`vault`.

### Recovering from a fully expired issuer

If the cluster is down long enough for the issuer's own certificate to expire, it
cannot renew itself — it needs a valid certificate to obtain the credentials it
would use to issue one. Delete the Secret and re-run; the bootstrap path fires
again automatically because it is conditional on that Secret's absence:

```bash
oc -n cert-manager delete secret iamra-issuer
./ansible-runner.sh issuer
```

## Using your own CA instead of Private CA

To avoid the ~$400/month, back the trust anchor with a CA bundle you manage:

```bash
aws rolesanywhere create-trust-anchor --name onprem-selfmanaged --enabled \
  --source 'sourceType=CERTIFICATE_BUNDLE,sourceData={x509CertificateData="'"$(cat root-ca.pem)"'"}'
```

Then replace `AWSPCAClusterIssuer` with a cert-manager `ClusterIssuer` of kind
`CA` over that root and point `csi.cert-manager.io/issuer-*` at it. Everything
from the sidecar onwards — the trust policies, the CN pinning, the rotation
handling, the app — is unchanged, and the whole bootstrap problem disappears
along with `aws-privateca-issuer`. You take on running the CA, including
protecting its key.

## The OIDC path in detail

Four moving parts, in [roles/oidc_provider](roles/oidc_provider/):

1. **Publish** the cluster's OIDC discovery document and JWKS to an S3 bucket
   with anonymous read on exactly those two objects. They contain public keys and
   metadata only — nothing secret leaves the cluster — but AWS STS fetches them
   unauthenticated, so they must genuinely be reachable. The discovery document
   is hand-built rather than copied from the cluster, because the cluster's own
   copy advertises the in-cluster issuer.
2. **Register** that URL as an IAM OIDC identity provider.
3. **Reconfigure** `spec.serviceAccountIssuer` so the tokens the cluster mints
   actually claim that issuer. This rolls every kube-apiserver and is gated
   behind `oidc_set_service_account_issuer`.
4. **Create** a role whose trust policy pins the token's `sub` *and* `aud`.

The operational burden to plan for: **when the cluster rotates its ServiceAccount
signing keys, the published JWKS goes stale and every workload loses AWS access
at once.** Re-running `./ansible-runner.sh oidc` republishes it. Treat that as a
scheduled job, not a thing you remember to do.

Steps 1, 2 and 4 are safe to run and inspect at any time; the playbook reports
exactly what is still missing rather than silently half-working.

## The Vault path in detail

```
pod ──(ServiceAccount token)──► Vault kubernetes auth
     └──► policy allows read on aws/creds/s3-demo
          └──► Vault calls sts:AssumeRole with ITS OWN credential
               └──► STS credentials rendered by vault-agent to /vault/secrets/aws
```

The authorization boundary is `bound_service_account_names` /
`bound_service_account_namespaces` on the Vault role — the equivalent of the
certificate RBAC in `iamra` and the `sub` claim in `oidc`.

`credential_type=assumed_role`, not `iam_user`: the pod gets STS credentials that
expire on their own. `iam_user` would have Vault create a real IAM user and access
key per lease — a long-lived credential with a scheduled deletion, which is what
this whole exercise avoids. [validate.yml](validate.yml) asserts the rendered key
starts with `ASIA` for exactly this reason.

Vault's own IAM user is granted `sts:AssumeRole` on **one role and nothing else**.
The upstream docs commonly show `iam:*` on `*` so the engine can also create IAM
users; that credential type is not used here, so those permissions are not needed.

### Dev mode

`vault_deploy: true` + `vault_dev_mode: true` is the default only because dev mode
needs no PersistentVolume, which makes this demonstrable on a cluster with no
storage class. It is in-memory, starts unsealed, and the root token is `root`.
For anything real, set `vault_deploy: false` and point `vault_addr` /
`vault_token` at a properly initialised, sealed, HA Vault.

### Bootstrapping Vault without a static key

The one long-lived credential in this design is Vault's own AWS access key. You
can remove even that by giving **Vault itself** credentials via IAM Roles
Anywhere — the `iamra` path in this repo — and pointing the AWS secrets engine at
the sidecar rather than a static key:

```bash
# on the Vault pod, with an IAMRA sidecar alongside it
vault write aws/config/root region=us-east-2   # no access_key/secret_key
# the engine then falls back to the SDK default chain, which finds the sidecar
```

That composes the two mechanisms: certificates authenticate Vault, Vault brokers
everything else. It is more moving parts, and it is the right answer if a static
key is unacceptable but you still want Vault's policy engine and audit log.

## Avoiding the privileged PSA label

If relaxing Pod Security Admission on the workload namespace is not acceptable,
drop the CSI driver and issue an ordinary `Certificate` into a `Secret`:

```yaml
# instead of the csi: volume
- name: iamra-cert
  secret:
    secretName: s3-demo-tls
    defaultMode: 0440
```

with a `Certificate` whose `commonName` is `s3-demo.iamra-demo` and a matching
`secretName`. PSA stays `restricted` and no inline volume is involved.

What you give up: the certificate is no longer bound to the pod lifecycle. It
lives in a Secret that outlives the pod, anything able to read Secrets in that
namespace can assume the AWS role, and you are back to protecting a stored
credential — which is most of what this architecture exists to avoid. The
`useTokenRequest` RBAC boundary also disappears, since cert-manager creates the
request rather than the workload. Everything else — the sidecar, the trust
policies, the CN pinning, the rotation handling — is unchanged.

### Pinning the URI SAN

The CSI driver can set URI SANs and supports the same pod variables there, so a
SPIFFE-style identity is available as a second condition:

```yaml
csi.cert-manager.io/uri-sans: "spiffe://cluster.local/ns/${POD_NAMESPACE}/sa/${SERVICE_ACCOUNT_NAME}"
```

IAM Roles Anywhere surfaces SANs as session tags such as
`aws:PrincipalTag/x509SAN/URI`. Before adding a condition on it, issue one
certificate and confirm the tag actually appears on the `CreateSession` event in
CloudTrail — a condition on a tag that is not set denies every request.

## Layout

```
ansible-runner.sh      podman wrapper; the only entry point
Containerfile          runner image (ansible, oc, helm, aws cli)
site.yml               images -> ONE method -> app -> validate
inventory/group_vars/all.yml    all configuration, all three methods
tasks/                 reusable includes (channel resolution, namespace)
docs/                  environment capture + the script that regenerates it

  images.yml           shared by every method
  iamra.yml            = aws.yml + certmanager.yml + issuer.yml
  oidc.yml                               oidc
  vault.yml                              vault
  app.yml validate.yml destroy.yml       shared, require -e auth_method=

roles/
  preflight            cluster + AWS reachability, account discovery
  build_images         in-cluster binary builds
  demo_app             namespace, RBAC, deployment, service, route; resolves
                       whichever method-specific role it needs
  verify               end-to-end checks, with per-method identity assertions

  aws_pca              Private CA, self-signed and imported          ┐
  aws_trust_anchor     IAM Roles Anywhere trust anchor               │ iamra
  aws_iam              both roles, both profiles, policies, bucket   │
  cert_manager         Red Hat operator + CSI driver                 │
  privateca_issuer     bootstrap cert, chart, sidecar patch,         │
                       ClusterIssuer, restart-and-retry readiness    ┘

  oidc_provider        publish JWKS, register IAM provider,          ┐ oidc
                       optional apiserver reconfiguration, role      ┘

  vault_server         Vault + agent injector                        ┐ vault
  vault_aws_engine     AWS secrets engine, kubernetes auth, policy   ┘

sidecar/               UBI aws_signing_helper image (iamra only)
app/                   Flask demo application — identical for all three
```
