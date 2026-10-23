#!/usr/bin/env python3
"""Write the two caller files of an app repository: .github/workflows/ci.yaml (pull requests -> service-ci.yaml, check `ci / gate`)
and publish.yaml (push to main -> service-publish.yaml). Removes the old ci-*.yaml / release.yaml.
usage: gen-callers.py <repo-dir> <service> <wf-sha> <wf-ver>
<service> is the app name = repository name without the `app-` prefix = helm chart name = Docker Hub repository."""
import os, sys

repo_dir, svc, sha, ver = sys.argv[1:5]
WF = "platform-boutique/platform-demo-workflows/.github/workflows"
GO_CHECKOUT_ENV = ["PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550", "CART_SERVICE_ADDR=localhost:7070",
    "CURRENCY_SERVICE_ADDR=localhost:7000", "SHIPPING_SERVICE_ADDR=localhost:50051",
    "PAYMENT_SERVICE_ADDR=localhost:50051", "EMAIL_SERVICE_ADDR=localhost:8080"]

CFG = {
  "checkout-service": dict(lang="go", with_={"test-packages": "./internal/...", "e2e-packages": "./tests/e2e/...", "smoke-port": '"5050"', "smoke-env": ("|", GO_CHECKOUT_ENV)}, trivy=""),
  "shipping-service": dict(lang="go", with_={"test-packages": "./internal/...", "smoke-port": '"50051"'}, trivy=".trivyignore"),
  "frontend": dict(lang="go", with_={}, trivy=""),
  "product-catalog-service": dict(lang="go", with_={"test-packages": "./internal/..."}, trivy=""),
  "currency-service": dict(lang="node", with_={"smoke-port": '"7000"', "smoke-script": ".github/smoke.sh"}, trivy=".trivyignore"),
  "payment-service": dict(lang="node", with_={"smoke-port": '"50051"'}, trivy=".trivyignore"),
  "email-service": dict(lang="python", with_={}, trivy=".trivyignore"),
  "recommendation-service": dict(lang="python", with_={}, trivy=".trivyignore"),
  "ad-service": dict(lang="java", with_={}, trivy=""),
  "product-catalog-db": dict(lang="none", with_={}, trivy=""),
  "cart-service": dict(lang="dotnet", with_={"solution": "cartservice.sln", "project": "src/cartservice.csproj", "test-project": "tests/cartservice.tests.csproj"}, trivy=""),
}[svc]


# inputs of service-ci (see its `on.workflow_call.inputs`); the keys in CFG["with_"] are passed through unchanged
def with_block(d, indent="      "):
    out = "    with:\n"
    for k, v in d.items():
        if isinstance(v, tuple):
            out += f"{indent}{k}: {v[0]}\n" + "".join(f"{indent}  {l}\n" for l in v[1])
        else:
            out += f"{indent}{k}: {v}\n"
    return out

ci_inputs = {"language": CFG["lang"], **CFG["with_"]}
if CFG["lang"] == "none":
    ci_inputs["image"] = "false"  # no image pipeline for this repository
if CFG["trivy"]:
    ci_inputs["trivyignores"] = CFG["trivy"]

ci_perms = "      contents: read\n      packages: read # reads the registry build cache\n"
if CFG["lang"] == "dotnet":
    ci_perms += "      checks: write # test-reporter publishes a check run\n"
if CFG["lang"] == "none":
    ci_perms = "      contents: read\n"

CI = f"""name: CI

# Pull-request gate: secret scan, workflow lint and (when code changed) tests, SAST, SCA and an image build + CVE scan, all in
# parallel; nothing is pushed. The only required status check is `ci / gate`.
on:
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions: {{}}

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    permissions:
{ci_perms}    uses: {WF}/service-ci.yaml@{sha} # {ver}
{with_block(ci_inputs)}"""

pub_inputs = {"app": svc}
if CFG["trivy"]:
    pub_inputs["trivyignores"] = CFG["trivy"]

PUB = f"""name: Publish

# A merge to main publishes the image ONCE (ghcr.io staging, Trivy, Docker Hub as sha-<commit>, cosign, SBOM) and opens the
# pull request in platform-demo-helm that moves `dev` to the new digest. No tags, no rebuilds.
on:
  push:
    branches: [main]
  workflow_dispatch:

permissions: {{}}

concurrency:
  group: publish-${{{{ github.ref }}}}
  cancel-in-progress: false

jobs:
  publish:
    permissions:
      contents: read
      packages: write
      id-token: write # keyless cosign signing
    uses: {WF}/service-publish.yaml@{sha} # {ver}
{with_block(pub_inputs)}    secrets:
      DOCKERHUB_USERNAME: ${{{{ secrets.DOCKERHUB_USERNAME }}}}
      DOCKERHUB_TOKEN: ${{{{ secrets.DOCKERHUB_TOKEN }}}}
      PROMOTE_APP_ID: ${{{{ secrets.PROMOTE_APP_ID }}}}
      PROMOTE_APP_PRIVATE_KEY: ${{{{ secrets.PROMOTE_APP_PRIVATE_KEY }}}}
"""

d = os.path.join(repo_dir, ".github", "workflows")
os.makedirs(d, exist_ok=True)
for old in ("ci-feature.yaml", "ci-main.yaml", "release.yaml"):
    p = os.path.join(d, old)
    if os.path.exists(p): os.remove(p)
open(os.path.join(d, "ci.yaml"), "w").write(CI)
if CFG["lang"] != "none":  # product-catalog-db has no image pipeline: nothing to publish
    open(os.path.join(d, "publish.yaml"), "w").write(PUB)
elif os.path.exists(os.path.join(d, "publish.yaml")):
    os.remove(os.path.join(d, "publish.yaml"))
print("wrote callers for", svc)
