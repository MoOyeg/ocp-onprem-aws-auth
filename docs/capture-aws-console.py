#!/usr/bin/env python3.9
"""AWS console screenshots, with account id and secrets masked in the DOM.

Signs in with a federation token minted from the CLI credentials rather than the
console password: no password is ever typed, stored, or capable of appearing in
a screenshot.

Redaction happens IN THE PAGE before the shot is taken, so what is captured is
already clean rather than edited afterwards.
"""
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "shots/aws")
OUT.mkdir(parents=True, exist_ok=True)
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")

ACCOUNT = subprocess.run(
    ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
    capture_output=True, text=True, check=True).stdout.strip()

ALIAS = subprocess.run(
    ["aws", "iam", "list-account-aliases", "--query", "AccountAliases[0]",
     "--output", "text"],
    capture_output=True, text=True).stdout.strip()
if ALIAS in ("None", ""):
    ALIAS = None

POLICY = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Action": ["acm-pca:List*", "acm-pca:Describe*", "acm-pca:Get*",
               "rolesanywhere:List*", "rolesanywhere:Get*",
               "iam:List*", "iam:Get*", "s3:List*", "s3:Get*",
               "sts:GetCallerIdentity", "tag:Get*",
               "cloudwatch:GetMetricData", "cloudwatch:ListMetrics"],
    "Resource": "*"}]}


def signin_url(destination):
    creds = json.loads(subprocess.run(
        ["aws", "sts", "get-federation-token", "--name", "console-capture",
         "--policy", json.dumps(POLICY)],
        capture_output=True, text=True, check=True).stdout)["Credentials"]
    session = json.dumps({"sessionId": creds["AccessKeyId"],
                          "sessionKey": creds["SecretAccessKey"],
                          "sessionToken": creds["SessionToken"]})
    token = json.loads(urllib.request.urlopen(
        "https://signin.aws.amazon.com/federation?Action=getSigninToken&Session="
        + urllib.parse.quote(session), timeout=45).read())["SigninToken"]
    return ("https://signin.aws.amazon.com/federation?Action=login"
            "&Issuer=ocp-onprem-aws-auth"
            "&Destination=" + urllib.parse.quote(destination)
            + "&SigninToken=" + token)


# Rewrites text nodes in place. Structure survives -- an ARN still reads as an
# ARN -- so the screenshot stays explanatory while disclosing nothing.
REDACT_JS = """
([account, alias]) => {
  // The nav bar renders the id grouped as 1234-5678-9012, which a plain
  // 12-digit match does not catch.
  const dashed = account.slice(0,4) + '-' + account.slice(4,8) + '-' + account.slice(8,12);
  const subs = [
    [new RegExp(account, 'g'), '111122223333'],
    [new RegExp(dashed, 'g'), '1111-2222-3333'],
    [/\\bASIA[A-Z0-9]{12,20}\\b/g, 'ASIAEXAMPLEEXAMPLE00'],
    [/\\bAKIA[A-Z0-9]{12,20}\\b/g, 'AKIAEXAMPLEEXAMPLE00'],
    [/\\bAROA[A-Z0-9]{12,24}\\b/g, 'AROAEXAMPLEEXAMPLE00'],
    [/\\bAIDA[A-Z0-9]{12,24}\\b/g, 'AIDAEXAMPLEEXAMPLE00'],
    [/\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b/g,
     'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'],
    [/\\b[0-9a-fA-F]{40,}\\b/g, '<redacted>'],
  ];
  // The account ALIAS identifies the account just as well as its id, and shows
  // up in switch-role links and the sign-in URL. Redact it too.
  if (alias) subs.push([new RegExp(alias, 'g'), 'example-account']);
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walk.nextNode()) nodes.push(walk.currentNode);
  for (const n of nodes) {
    let t = n.nodeValue;
    for (const [re, rep] of subs) t = t.replace(re, rep);
    if (t !== n.nodeValue) n.nodeValue = t;
  }
  // The account menu in the nav bar renders the id in an attribute too.
  document.querySelectorAll('[title],[aria-label],[href]').forEach(el => {
    for (const a of ['title', 'aria-label', 'href']) {
      let v = el.getAttribute(a);
      if (!v) continue;
      const before = v;
      v = v.split(account).join('111122223333');
      if (alias) v = v.split(alias).join('example-account');
      if (v !== before) el.setAttribute(a, v);
    }
  });
}
"""

PAGES = [
    ("acm-pca", f"https://{REGION}.console.aws.amazon.com/acm-pca/home?region={REGION}#/certificateAuthorities",
     "AWS Private CA — the certificate authority backing the trust anchor"),
    ("rolesanywhere-anchors", f"https://{REGION}.console.aws.amazon.com/rolesanywhere/home?region={REGION}#/trustAnchors",
     "IAM Roles Anywhere — trust anchors"),
    ("rolesanywhere-profiles", f"https://{REGION}.console.aws.amazon.com/rolesanywhere/home?region={REGION}#/profiles",
     "IAM Roles Anywhere — profiles"),
    ("iam-roles", "https://us-east-1.console.aws.amazon.com/iam/home#/roles",
     "IAM roles created by the three methods"),
    ("iam-oidc-provider", "https://us-east-1.console.aws.amazon.com/iam/home#/identity_providers",
     "The IAM OIDC identity provider registered for the cluster"),
    ("iam-role-vault-trust", "https://us-east-1.console.aws.amazon.com/iam/home#/roles/ocp-vault-app-s3?section=trust_relationships",
     "The Vault workload role trusts Vault's IAM user, not any pod identity"),
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("CHROME"),
            args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  ignore_https_errors=True)
        page = ctx.new_page()

        page.goto(signin_url(PAGES[0][1]), timeout=120000)
        page.wait_for_timeout(9000)

        for name, url, _caption in PAGES:
            try:
                page.goto(url, timeout=90000, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                # Dismiss first-run tooltips and dismissible banners so they do
                # not sit on top of the thing being documented.
                for sel in ('button:has-text("Done")',
                            'button:has-text("Next")',
                            'button[aria-label="Close"]',
                            'button[aria-label="Dismiss"]',
                            '[data-testid="close-button"]'):
                    for _ in range(3):
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=1200):
                                el.click(timeout=2500)
                                page.wait_for_timeout(400)
                            else:
                                break
                        except Exception:
                            break
                page.wait_for_timeout(1200)
                page.evaluate(REDACT_JS, [ACCOUNT, ALIAS])
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT / f"{name}.png"))
                print(f"  captured {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED  {name}: {str(exc)[:140]}")
        browser.close()


if __name__ == "__main__":
    main()
