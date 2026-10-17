#!/usr/bin/env python3
"""Write .github/workflows/{ci,release}.yaml for any service. usage: gen_callers.py <repo-dir> <service> <wf-sha> <wf-ver>"""
import os, sys

repo_dir, svc, sha, ver = sys.argv[1:5]
WF = "mladenovskistefan111/platform-demo-workflows/.github/workflows"
SEMGREP = {"SEMGREP_APP_TOKEN": "${{ secrets.SEMGREP_APP_TOKEN }}"}
GO_CHECKOUT_ENV = ["PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550", "CART_SERVICE_ADDR=localhost:7070",
    "CURRENCY_SERVICE_ADDR=localhost:7000", "SHIPPING_SERVICE_ADDR=localhost:50051",
    "PAYMENT_SERVICE_ADDR=localhost:50051", "EMAIL_SERVICE_ADDR=localhost:8080"]

CFG = {
  "checkout-service": dict(lang="go", wf="go-ci.yaml", with_={"test-packages": "./internal/...", "e2e-packages": "./tests/e2e/...", "smoke-port": '"5050"', "smoke-env": ("|", GO_CHECKOUT_ENV)}, trivy=""),
  "shipping-service": dict(lang="go", wf="go-ci.yaml", with_={"test-packages": "./internal/...", "smoke-port": '"50051"'}, trivy=".trivyignore"),
  "frontend": dict(lang="go", wf="go-ci.yaml", with_={}, trivy=""),
  "product-catalog-service": dict(lang="go", wf="go-ci.yaml", with_={"test-packages": "./internal/..."}, trivy=""),
  "currency-service": dict(lang="node", wf="node-ci.yaml", with_={"smoke-port": '"7000"', "smoke-script": ".github/smoke.sh"}, trivy=".trivyignore"),
  "payment-service": dict(lang="node", wf="node-ci.yaml", with_={"smoke-port": '"50051"'}, trivy=".trivyignore"),
  "email-service": dict(lang="python", wf="python-ci.yaml", with_={}, trivy=".trivyignore"),
  "recommendation-service": dict(lang="python", wf="python-ci.yaml", with_={}, trivy=".trivyignore"),
  "ad-service": dict(lang="java", wf="java-gradle-ci.yaml", with_={}, trivy=""),
  "cart-service": dict(lang="dotnet", wf="dotnet-ci.yaml", with_={"solution": "cartservice.sln", "project": "src/cartservice.csproj", "test-project": "tests/cartservice.tests.csproj"}, trivy=""),
}[svc]

def with_block(d, indent="      "):
    if not d: return ""
    out = "    with:\n"
    for k, v in d.items():
        if isinstance(v, tuple):
            out += f"{indent}{k}: {v[0]}\n" + "".join(f"{indent}  {l}\n" for l in v[1])
        else:
            out += f"{indent}{k}: {v}\n"
    return out

lang_perms = "      contents: read\n      pull-requests: read # gitleaks lists the PR's commits through the API\n"
if CFG["lang"] == "dotnet":
    lang_perms += "      checks: write # test-reporter publishes a check run\n"
lang_secrets = ""  # semgrep no longer needs a token (workflows v2)

def jobs(release):
    out = f"""jobs:
  {CFG['lang']}:
    permissions:
{lang_perms}    uses: {WF}/{CFG['wf']}@{sha} # {ver}
{with_block(CFG['with_'])}{lang_secrets}
  image:
    needs: [{CFG['lang']}]
    permissions:
      contents: read
      packages: write
    uses: {WF}/docker-build.yaml@{sha} # {ver}
{('    with:' + chr(10) + '      trivyignores: ' + CFG['trivy'] + chr(10)) if CFG['trivy'] else ''}
  workflows:
    permissions:
      contents: read
    uses: {WF}/workflow-lint.yaml@{sha} # {ver}
"""
    if release:
        out += f"""
  promote:
    if: ${{{{ startsWith(github.ref, 'refs/tags/v') || inputs.publish }}}}
    needs: [image, workflows]
    permissions:
      id-token: write # keyless cosign signing
      contents: read
      packages: read
    uses: {WF}/docker-promote.yaml@{sha} # {ver}
    with:
      image-name: {svc}
      staged-image: ${{{{ needs.image.outputs.staged-image }}}}
      digest: ${{{{ needs.image.outputs.digest }}}}
    secrets:
      DOCKERHUB_USERNAME: ${{{{ secrets.DOCKERHUB_USERNAME }}}}
      DOCKERHUB_TOKEN: ${{{{ secrets.DOCKERHUB_TOKEN }}}}
"""
    return out

CI = """name: CI

# Pull-request gate. Nothing runs on a plain push; images are published by release.yaml.
on:
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions: {}

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

""" + jobs(False)

REL = """name: Release

# Runs when a semver tag is pushed (git tag -a v1.2.3 && git push origin v1.2.3) or by hand.
# Gates: same as CI. Then the scanned image digest is promoted to Docker Hub and signed.
on:
  push:
    tags: ['v*']
  workflow_dispatch:
    inputs:
      publish:
        description: Promote the scanned image to Docker Hub and sign it (commit must be on main)
        type: boolean
        default: false

permissions: {}

""" + jobs(True)

d = os.path.join(repo_dir, ".github", "workflows")
os.makedirs(d, exist_ok=True)
for old in ("ci-feature.yaml", "ci-main.yaml"):
    p = os.path.join(d, old)
    if os.path.exists(p): os.remove(p)
open(os.path.join(d, "ci.yaml"), "w").write(CI)
open(os.path.join(d, "release.yaml"), "w").write(REL)
print("wrote callers for", svc)
