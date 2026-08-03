#!/usr/bin/env python3.9
"""OpenShift console screenshots, redacted in the DOM before capture.

The kubeadmin password is read from the cluster at run time and passed via the
environment; it is never written to disk and never appears in a screenshot,
because the login form is filled and submitted before anything is captured.
"""
import os
import pathlib
import subprocess
import sys

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "shots/ocp")
OUT.mkdir(parents=True, exist_ok=True)

CONSOLE = subprocess.run(
    ["oc", "get", "route", "console", "-n", "openshift-console",
     "-o", "jsonpath={.spec.host}"], capture_output=True, text=True, check=True).stdout.strip()
USER = os.environ["OCP_USER"]
PASSWORD = os.environ["OCP_PASSWORD"]
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "")

REDACT_JS = """
(account) => {
  const subs = [
    [/\\bASIA[A-Z0-9]{12,20}\\b/g, 'ASIAEXAMPLEEXAMPLE00'],
    [/\\bAKIA[A-Z0-9]{12,20}\\b/g, 'AKIAEXAMPLEEXAMPLE00'],
    [/\\bAROA[A-Z0-9]{12,24}\\b/g, 'AROAEXAMPLEEXAMPLE00'],
    [/\\bAIDA[A-Z0-9]{12,24}\\b/g, 'AIDAEXAMPLEEXAMPLE00'],
    [/\\beyJ[A-Za-z0-9_\\-]{16,}/g, '<jwt-redacted>'],
    [/\\bhvs\\.[A-Za-z0-9_\\-]{10,}/g, '<vault-token-redacted>'],
  ];
  if (account) {
    subs.unshift([new RegExp(account, 'g'), '111122223333']);
  }
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walk.nextNode()) nodes.push(walk.currentNode);
  for (const n of nodes) {
    let t = n.nodeValue;
    for (const [re, rep] of subs) t = t.replace(re, rep);
    if (t !== n.nodeValue) n.nodeValue = t;
  }
  // Secret values are revealed only on click, but blank them defensively.
  document.querySelectorAll('textarea, input[type=password]').forEach(e => {
    if (e.value && e.value.length > 12) e.value = '<redacted>';
  });
}
"""

PAGES = [
    ("projects",
     "/k8s/cluster/projects?name=demo",
     "One namespace per method"),
    ("operators",
     "/k8s/ns/cert-manager-operator/operators.coreos.com~v1alpha1~ClusterServiceVersion",
     "The Red Hat cert-manager Operator, installed by the iamra path"),
    ("pods-iamra",
     "/k8s/ns/iamra-demo/pods",
     "iamra-demo: two containers per pod, app + credential sidecar"),
    ("pods-oidc",
     "/k8s/ns/oidc-demo/pods",
     "oidc-demo: one container. No sidecar at all"),
    ("pods-vault",
     "/k8s/ns/vault-demo/pods",
     "vault-demo: two containers, app + injected vault-agent"),
    ("clusterissuer",
     "/k8s/cluster/awspca.cert-manager.io~v1beta1~AWSPCAClusterIssuer",
     "The AWSPCAClusterIssuer, Ready"),
    ("certificates",
     "/k8s/ns/cert-manager/cert-manager.io~v1~Certificate",
     "The issuer's own certificate, renewed by cert-manager"),
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("CHROME"),
            args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  ignore_https_errors=True)
        page = ctx.new_page()

        page.goto(f"https://{CONSOLE}/", timeout=120000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        # OpenShift shows an identity-provider chooser when more than one exists.
        try:
            link = page.locator('a:has-text("kube:admin")').first
            if link.is_visible(timeout=4000):
                link.click()
                page.wait_for_timeout(2500)
        except Exception:
            pass

        try:
            page.fill('input#inputUsername', USER, timeout=20000)
            page.fill('input#inputPassword', PASSWORD, timeout=20000)
            page.click('button[type=submit]', timeout=20000)
            page.wait_for_timeout(9000)
        except Exception as exc:  # noqa: BLE001
            print(f"  login failed: {str(exc)[:160]}")
            page.screenshot(path=str(OUT / "_login-failure.png"))
            browser.close()
            return 1

        # Skip the first-run guided tour if it appears.
        for sel in ('button:has-text("Skip tour")', 'button:has-text("Skip")',
                    'button[aria-label="Close"]'):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2500):
                    el.click(timeout=3000)
                    page.wait_for_timeout(800)
            except Exception:
                pass

        for name, path, _caption in PAGES:
            try:
                page.goto(f"https://{CONSOLE}{path}", timeout=120000,
                          wait_until="domcontentloaded")
                # The console is a SPA: domcontentloaded fires long before any
                # data is on screen. Wait for real content, and for the loading
                # spinner to disappear, rather than guessing a duration.
                try:
                    page.wait_for_selector(
                        'table, [data-test="empty-box-body"], .co-m-pane__body, h1',
                        timeout=60000)
                except Exception:
                    pass
                for _ in range(30):
                    spinners = page.locator('.co-m-loader, .pf-v5-c-spinner, .pf-c-spinner')
                    try:
                        if spinners.count() == 0 or not spinners.first.is_visible(timeout=800):
                            break
                    except Exception:
                        break
                    page.wait_for_timeout(1000)
                page.wait_for_timeout(3500)
                page.evaluate(REDACT_JS, ACCOUNT)
                page.wait_for_timeout(400)
                page.screenshot(path=str(OUT / f"{name}.png"))
                print(f"  captured {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED  {name}: {str(exc)[:140]}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
