"""
Demo workload for on-prem OpenShift reaching AWS without long-lived credentials.

The same file runs unchanged under all three authentication methods -- IAM Roles
Anywhere, OIDC federation, and Vault's AWS secrets engine -- because none of them
require the application to know anything. There is deliberately no profile, no
key, no token handling, no call to a signing helper, no Vault client.

boto3's default credential chain does all of it:

    iamra   AWS_EC2_METADATA_SERVICE_ENDPOINT points at the sidecar, so the SDK
            believes it is on EC2 with an instance profile.
    oidc    AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE make the SDK call
            sts:AssumeRoleWithWebIdentity itself.
    vault   AWS_SHARED_CREDENTIALS_FILE points at the file the Vault agent
            renders, which is an ordinary credentials file.

That is the whole point: existing AWS code moves on-prem unchanged, and you can
switch mechanisms without touching the application.
"""

import datetime
import os
import socket

import boto3
import botocore
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from flask import Flask, jsonify, render_template, request

APP = Flask(__name__)

BUCKET = os.environ["DEMO_BUCKET"]
CERT_PATH = os.environ.get("CERT_PATH", "/iamra/tls.crt")
REGION = os.environ.get("AWS_REGION", "us-east-2")
AUTH_METHOD = os.environ.get("AUTH_METHOD", "iamra")

# What each method uses to prove identity, and where the UI should look for it.
METHOD_INFO = {
    "iamra": {
        "title": "IAM Roles Anywhere",
        "blurb": "A short-lived X.509 certificate, mounted by the cert-manager "
                 "CSI driver and exchanged for STS credentials by a sidecar "
                 "serving IMDSv2 on 127.0.0.1:9911.",
        "mechanism": "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    },
    "oidc": {
        "title": "OIDC federation (self-managed IRSA)",
        "blurb": "This pod's own projected ServiceAccount token, exchanged by "
                 "the AWS SDK via sts:AssumeRoleWithWebIdentity. No sidecar, no "
                 "certificate, no helper process.",
        "mechanism": "AWS_WEB_IDENTITY_TOKEN_FILE",
    },
    "vault": {
        "title": "HashiCorp Vault AWS secrets engine",
        "blurb": "The pod authenticated to Vault with its ServiceAccount token; "
                 "Vault assumed the role on its behalf and its agent rendered "
                 "the resulting STS credentials to a file.",
        "mechanism": "AWS_SHARED_CREDENTIALS_FILE",
    },
}


def _session():
    """A fresh session per request.

    Not an optimisation problem: botocore caches and refreshes the credentials
    it fetched from the metadata endpoint, so this does not re-hit the sidecar
    every time. Constructing it per request means a credential change (after the
    sidecar reloads a renewed certificate) is picked up without a process
    restart.
    """
    return boto3.session.Session(region_name=REGION)


def identity_material():
    """Whatever this deployment uses to prove who it is.

    Shown in the UI because when AWS starts refusing, comparing this against what
    the IAM role trusts is the first useful thing to do -- and what "this" means
    differs per method.
    """
    if AUTH_METHOD == "oidc":
        return _token_claims()
    if AUTH_METHOD == "vault":
        return _vault_credentials()
    return certificate_info()


def _token_claims():
    """Decode the projected ServiceAccount token's claims.

    A JWT payload is base64url and unencrypted; reading it is not a disclosure,
    it is exactly what AWS does after verifying the signature against the
    published JWKS. The signature itself is deliberately not shown.
    """
    import base64
    import json

    path = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
    try:
        with open(path) as handle:
            payload = handle.read().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (OSError, ValueError, IndexError) as exc:
        return {"error": f"cannot read token at {path or '<unset>'}: {exc}"}

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    aud = claims.get("aud")
    return {
        "kind": "ServiceAccount token (JWT)",
        "issuer": claims.get("iss", "?"),
        "subject": claims.get("sub", "?"),
        "audience": ", ".join(aud) if isinstance(aud, list) else str(aud),
        "not_after": datetime.datetime.fromtimestamp(
            claims["exp"], datetime.timezone.utc
        ).isoformat(),
        "remaining_hours": round((claims["exp"] - now) / 3600, 2),
        "total_validity_hours": round((claims["exp"] - claims["iat"]) / 3600, 2),
    }


def _vault_credentials():
    """Describe the credentials file the Vault agent rendered.

    Only the access key id is shown, and only its prefix matters: ASIA... is a
    temporary STS session, AKIA... would mean Vault is creating real IAM users.
    The secret and session token are never rendered into the page.
    """
    import configparser

    path = os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "")
    parser = configparser.ConfigParser()
    if not path or not parser.read(path):
        return {"error": f"vault agent has not rendered {path or '<unset>'} yet"}
    try:
        section = parser["default"]
        key_id = section["aws_access_key_id"]
    except KeyError as exc:
        return {"error": f"{path} is missing {exc}"}

    return {
        "kind": "STS session rendered by vault-agent",
        "access_key_id": key_id,
        "temporary": key_id.startswith("ASIA"),
        "session_token_present": bool(section.get("aws_session_token")),
        "path": path,
    }


def certificate_info():
    """Read the certificate the sidecar is presenting to AWS.

    Shown in the UI because when AssumeRole starts failing, the CN on this
    certificate versus the CN in the IAM trust policy is the first thing worth
    comparing.
    """
    try:
        with open(CERT_PATH, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
    except (OSError, ValueError) as exc:
        return {"error": f"cannot read {CERT_PATH}: {exc}"}

    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    now = datetime.datetime.now(datetime.timezone.utc)

    return {
        "kind": "X.509 certificate",
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial": format(cert.serial_number, "x"),
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "total_validity_hours": round((not_after - not_before).total_seconds() / 3600, 1),
        "remaining_hours": round((not_after - now).total_seconds() / 3600, 1),
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
    }


ENDPOINT = os.environ.get("AWS_EC2_METADATA_SERVICE_ENDPOINT", "<unset>")

# Worth knowing before you ever have to debug this.
#
# When `aws_signing_helper serve` cannot exchange the certificate for
# credentials -- expired certificate, a CN that does not satisfy the trust
# policy, a disabled profile -- it does NOT report an error to the SDK. It
# answers the metadata request with HTTP 200 and this body:
#
#   {"AccessKeyId":"","SecretAccessKey":"","Token":"","Code":"Success",
#    "Expiration":"0001-01-01T00:00:00Z", ...}
#
# botocore then tries to parse that zero date and raises, of all things,
# `OverflowError: date value out of range`. Nothing in the traceback mentions
# AWS, credentials, or certificates. Catching only ClientError and
# NoCredentialsError -- the obvious choice -- turns the single most common
# failure of this architecture into an unhandled 500.
#
# The real error is in the sidecar's log, which is why the message says so.
CREDENTIAL_EXCHANGE_FAILED = (
    "The sidecar is reachable but AWS refused to issue credentials, so it "
    "returned an empty credential document. Almost always the certificate CN "
    "does not satisfy the aws:PrincipalTag/x509Subject/CN condition on the "
    "role's trust policy, or the certificate has expired. The actual AWS error "
    "is in the sidecar log: oc logs <pod> -c iamra-sidecar"
)


def _aws_error(exc):
    """Turn a botocore failure into something a human can act on."""
    if isinstance(exc, OverflowError):
        return CREDENTIAL_EXCHANGE_FAILED
    if isinstance(exc, botocore.exceptions.NoCredentialsError):
        return f"No credentials -- nothing is serving on {ENDPOINT}."
    return f"{type(exc).__name__}: {exc}"


def caller_identity():
    try:
        return {"ok": True, **_session().client("sts").get_caller_identity()}
    except Exception as exc:  # noqa: BLE001 -- see _aws_error
        return {"ok": False, "error": _aws_error(exc)}


def list_objects(limit=25):
    try:
        response = _session().client("s3").list_objects_v2(Bucket=BUCKET, MaxKeys=limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "bucket": BUCKET, "error": _aws_error(exc)}

    return {
        "ok": True,
        "bucket": BUCKET,
        "objects": [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "modified": obj["LastModified"].isoformat(),
            }
            for obj in response.get("Contents", [])
        ],
    }


@APP.get("/healthz")
def healthz():
    # Liveness only. Deliberately does not touch AWS: a transient STS problem
    # should not get the pod killed.
    return "ok", 200


@APP.get("/readyz")
def readyz():
    return ("ok", 200) if caller_identity()["ok"] else ("no aws credentials", 503)


@APP.get("/api/identity")
def api_identity():
    return jsonify(caller_identity())


@APP.get("/api/certificate")
def api_certificate():
    return jsonify(certificate_info())


@APP.get("/api/identity-material")
def api_identity_material():
    """Whatever proves identity under the configured auth method."""
    return jsonify({"auth_method": AUTH_METHOD, **identity_material()})


@APP.get("/api/objects")
def api_objects():
    return jsonify(list_objects())


@APP.post("/api/objects")
def api_put_object():
    """Prove write access, not just read access."""
    body = request.get_json(silent=True) or {}
    text = body.get("content", f"written by {socket.gethostname()}")
    key = body.get("key", f"iamra-demo/{datetime.datetime.utcnow():%Y%m%dT%H%M%SZ}.txt")
    try:
        _session().client("s3").put_object(
            Bucket=BUCKET, Key=key, Body=text.encode(), ContentType="text/plain"
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _aws_error(exc)}), 502
    return jsonify({"ok": True, "bucket": BUCKET, "key": key})


@APP.get("/")
def index():
    info = METHOD_INFO.get(AUTH_METHOD, METHOD_INFO["iamra"])
    return render_template(
        "index.html",
        pod=socket.gethostname(),
        namespace=os.environ.get("POD_NAMESPACE", "?"),
        service_account=os.environ.get("SERVICE_ACCOUNT_NAME", "?"),
        region=REGION,
        bucket=BUCKET,
        auth_method=AUTH_METHOD,
        method=info,
        mechanism_value=os.environ.get(info["mechanism"], "<unset>"),
        identity=caller_identity(),
        material=identity_material(),
        listing=list_objects(),
    )


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=8080)
