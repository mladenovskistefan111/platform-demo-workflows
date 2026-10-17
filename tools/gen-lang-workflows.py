#!/usr/bin/env python3
"""Generate node-ci, python-ci, java-gradle-ci, dotnet-ci reusable workflows into <workflows-repo>/.github/workflows"""
import os, sys, textwrap
out = os.path.join(sys.argv[1], ".github", "workflows")

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
SHA = dict(
    node="actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0",
    py="actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0",
    java="actions/setup-java@de7274f081f381c8f8158605e0321c36c376e2e6 # v6.0.1",
    gradle="gradle/actions/setup-gradle@0723195856401067f7a2779048b490ace7a47d7c # v5.0.2",
    dotnet="actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0",
    gitleaks="gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e # v3.0.0",
    semgrep="semgrep/semgrep-action@713efdd345f3035192eaa63f56867b88e63e4e5d # v1",
    trivy="aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0",
    upload="actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2",
    reporter="dorny/test-reporter@a43b3a5f7366b97d083190328d2c652e1a8b6aa2 # v3.0.0",
)

def steps(*blocks):
    return "".join(textwrap.indent(textwrap.dedent(b).strip("\n") + "\n\n", "      ") for b in blocks).rstrip("\n") + "\n"

def co(fetch_all=False):
    return f"""- uses: {CHECKOUT}
  with:
    persist-credentials: false""" if not fetch_all else f"""- uses: {CHECKOUT}
  with:
    fetch-depth: 0 # full history so gitleaks scans every commit
    persist-credentials: false"""

def job(key, name, body, needs=None, perms="contents: read", extra="", cond=""):
    n = f"    needs: [{', '.join(needs)}]\n" if needs else ""
    c = f"    if: {cond}\n" if cond else ""
    p = "".join(f"      {l.strip()}\n" for l in perms.split(";"))
    return f"""
  {key}:
    name: {name}
{c}    runs-on: ubuntu-latest
{n}    permissions:
{p}{extra}    steps:
{body}"""

def secret_scan():
    return job("secret-scan", "Secret Scan", steps(co(True), f"""
- name: Run Gitleaks
  uses: {SHA['gitleaks']}
  env:
    GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
    GITLEAKS_ENABLE_COMMENTS: "false"
"""), perms="contents: read;pull-requests: read # gitleaks lists the PR's commits through the API")

def semgrep(inputname="semgrep-config"):
    return job("sast", "SAST - Static Security Analysis", steps(co(), f"""
- uses: {SHA['py']}
  with:
    python-version: "3.13"

- name: Install Semgrep
  env:
    VERSION: ${{{{ inputs.semgrep-version }}}}
  run: pip install "semgrep==${{VERSION}}"

- name: Run Semgrep
  env:
    SEMGREP_CONFIG: ${{{{ inputs.{inputname} }}}}
  run: |
    read -ra rules <<< "$SEMGREP_CONFIG"
    args=()
    for rule in "${{rules[@]}}"; do
      args+=(--config "$rule")
    done
    semgrep scan "${{args[@]}}" --error --metrics=off
"""), needs=STAGE1)

STAGE1 = ["lint-format", "secret-scan", "build-check", "unit-tests"]

def header(name, desc, inputs, secrets=False):
    sec = """    secrets:
      SEMGREP_APP_TOKEN:
        required: false
""" if secrets else ""
    return f"""name: {name}

# {desc}
# Call it with:  uses: mladenovskistefan111/platform-demo-workflows/.github/workflows/{{FILE}}@<sha> # <tag>

on:
  workflow_call:
    inputs:
{inputs}{sec}
# Least privilege by default; every job below declares what it needs.
permissions: {{}}

jobs:"""

def write(fname, text):
    open(os.path.join(out, fname), "w").write(text.replace("{FILE}", fname).rstrip("\n") + "\n")
    print("wrote", fname)

SMOKE_INPUTS = """      smoke-port:
        description: gRPC port for a start-the-server health smoke test; empty skips the job
        type: string
        default: ""
      smoke-command:
        description: Command that starts the built server (run after the build)
        type: string
        default: node dist/server.js
      smoke-health-args:
        description: Extra grpcurl arguments for the health check (no reflection, so point at the proto)
        type: string
        default: -import-path ./proto -proto health.proto
      smoke-script:
        description: Optional script in the caller repo for service-specific checks (gets GRPCURL and SMOKE_PORT)
        type: string
        default: ""
"""

# ───────────────────────────── node ─────────────────────────────
node_in = """      node-version-file:
        type: string
        default: .nvmrc
      semgrep-config:
        type: string
        default: p/typescript p/nodejs p/secrets
      semgrep-version:
        type: string
        default: 1.180.0
""" + SMOKE_INPUTS
NODE_SETUP = f"""- uses: {SHA['node']}
  with:
    node-version-file: ${{{{ inputs.node-version-file }}}}
    cache: npm

- name: Install dependencies
  run: npm ci"""
node = header("Node CI", "Reusable pipeline for the Node/TypeScript services (currency, payment): format/typecheck/lint, secret scan, build, tests, SCA (npm audit), SAST (Semgrep), optional gRPC smoke test.", node_in)
node += job("lint-format", "Lint & Format", steps(co(), NODE_SETUP, """
- name: Check formatting
  run: npm run format:check

- name: Typecheck
  run: npm run typecheck

- name: Lint
  run: npm run lint
"""))
node += secret_scan()
node += job("build-check", "Build Check", steps(co(), NODE_SETUP, "- name: Build\n  run: npm run build"))
node += job("unit-tests", "Unit Tests", steps(co(), NODE_SETUP, "- name: Run unit tests\n  run: npm run test:ci"))
node += "\n  # ─── STAGE 2 (parallel) ───────────────────────────────────────────────────\n"
node += job("sca", "SCA - Dependency Vulnerabilities", steps(co(), NODE_SETUP, """
- name: Run npm audit
  # audit-level=high: fail only on high/critical findings
  run: npm audit --audit-level=high --omit=dev
"""), needs=STAGE1)
node += semgrep()
node += job("smoke-test", "Smoke Test (gRPC health)", steps(co(), NODE_SETUP, "- name: Build server\n  run: npm run build", """
- name: Install grpcurl
  env:
    VERSION: 1.9.4
    SHA256: 97e13d58d2733a0e62cd2571d1d5f0c02823f0d25282f08bddedf1ad9c5d1736
  run: |
    curl -fsSL -o grpcurl.tgz "https://github.com/fullstorydev/grpcurl/releases/download/v${VERSION}/grpcurl_${VERSION}_linux_x86_64.tar.gz"
    echo "${SHA256}  grpcurl.tgz" | sha256sum -c -
    tar -xzf grpcurl.tgz -C "$RUNNER_TEMP" grpcurl

- name: Start server and check gRPC health
  env:
    SMOKE_PORT: ${{ inputs.smoke-port }}
    SMOKE_COMMAND: ${{ inputs.smoke-command }}
    HEALTH_ARGS: ${{ inputs.smoke-health-args }}
    SMOKE_SCRIPT: ${{ inputs.smoke-script }}
  run: |
    export GRPCURL="$RUNNER_TEMP/grpcurl"
    read -ra server_cmd <<< "$SMOKE_COMMAND"
    read -ra health_args <<< "$HEALTH_ARGS"
    PORT="$SMOKE_PORT" "${server_cmd[@]}" &
    server_pid=$!
    trap 'kill $server_pid 2>/dev/null || true' EXIT
    sleep 2
    response=$("$GRPCURL" -plaintext "${health_args[@]}" "localhost:${SMOKE_PORT}" grpc.health.v1.Health/Check)
    status=$(echo "$response" | grep -o '"status": "[^"]*"' | cut -d'"' -f4)
    if [ "$status" != "SERVING" ]; then
      echo "FAIL: expected SERVING, got '$status'"
      exit 1
    fi
    echo "PASS: service is SERVING"
    if [ -n "$SMOKE_SCRIPT" ]; then
      bash "$SMOKE_SCRIPT"
    fi
"""), needs=STAGE1, cond="inputs.smoke-port != ''")
write("node-ci.yaml", node)

# ───────────────────────────── python ─────────────────────────────
py_in = """      python-version-file:
        type: string
        default: .python-version
      source-dir:
        description: Directory scanned by bandit
        type: string
        default: src/
      build-check-file:
        description: Entry-point file that must parse
        type: string
        default: src/server.py
      tests-path:
        type: string
        default: tests/
      bandit-config:
        type: string
        default: pyproject.toml
      ruff-version:
        type: string
        default: 0.17.0
      pip-audit-version:
        type: string
        default: 2.10.1
      bandit-version:
        type: string
        default: 1.9.4
"""
PY_SETUP = f"""- uses: {SHA['py']}
  with:
    python-version-file: ${{{{ inputs.python-version-file }}}}
    cache: pip"""
py = header("Python CI", "Reusable pipeline for the Python services (email, recommendation): ruff, secret scan, import check, pytest, SCA (pip-audit), SAST (bandit).", py_in)
py += job("lint-format", "Lint & Format", steps(co(), PY_SETUP, """
- name: Install lint tools
  env:
    VERSION: ${{ inputs.ruff-version }}
  run: pip install "ruff==${VERSION}"

- name: Check formatting (ruff format)
  run: ruff format --check .

- name: Run linter (ruff check)
  run: ruff check .
"""))
py += secret_scan()
py += job("build-check", "Build Check", steps(co(), PY_SETUP, """
- name: Install dependencies
  run: pip install -r requirements.txt

- name: Verify entry point parses (build smoke)
  env:
    BUILD_FILE: ${{ inputs.build-check-file }}
  run: python -c "import ast, os; ast.parse(open(os.environ['BUILD_FILE']).read()); print('Parse OK')"
"""))
py += job("unit-tests", "Unit Tests", steps(co(), PY_SETUP, """
- name: Install dependencies
  run: |
    pip install -r requirements.txt
    pip install -r requirements-dev.txt

- name: Run unit tests
  env:
    TESTS_PATH: ${{ inputs.tests-path }}
  run: pytest "$TESTS_PATH" -v --tb=short
"""))
py += "\n  # ─── STAGE 2 (parallel) ───────────────────────────────────────────────────\n"
py += job("sca", "SCA - Dependency Vulnerabilities", steps(co(), PY_SETUP, """
- name: Install pip-audit
  env:
    VERSION: ${{ inputs.pip-audit-version }}
  run: pip install "pip-audit==${VERSION}"

- name: Run pip-audit
  run: pip-audit -r requirements.txt
"""), needs=STAGE1)
py += job("sast", "SAST - Static Security Analysis", steps(co(), PY_SETUP, """
- name: Install bandit
  env:
    VERSION: ${{ inputs.bandit-version }}
  run: pip install "bandit==${VERSION}"

- name: Run bandit
  env:
    SOURCE_DIR: ${{ inputs.source-dir }}
    BANDIT_CONFIG: ${{ inputs.bandit-config }}
  run: bandit -r "$SOURCE_DIR" -c "$BANDIT_CONFIG"
"""), needs=STAGE1)
write("python-ci.yaml", py)

# ───────────────────────────── java (gradle) ─────────────────────────────
java_in = """      java-version:
        type: string
        default: "21"
      java-distribution:
        type: string
        default: temurin
      semgrep-config:
        type: string
        default: p/java p/owasp-top-ten
      semgrep-version:
        type: string
        default: 1.180.0
"""
JAVA_SETUP = f"""- uses: {SHA['java']}
  with:
    java-version: ${{{{ inputs.java-version }}}}
    distribution: ${{{{ inputs.java-distribution }}}}

- name: Setup Gradle
  uses: {SHA['gradle']}"""
java = header("Java Gradle CI", "Reusable pipeline for Gradle services (ad-service): compile, secret scan, build, tests, SCA (Trivy fs), SAST (Semgrep).", java_in)
java += job("lint-format", "Lint & Format", steps(co(), JAVA_SETUP, "- name: Compile (lint check)\n  run: ./gradlew compileJava --warning-mode all"))
java += secret_scan()
java += job("build-check", "Build Check", steps(co(), JAVA_SETUP, "- name: Build (skip tests)\n  run: ./gradlew build -x test --no-daemon"))
java += job("unit-tests", "Unit Tests", steps(co(), JAVA_SETUP, "- name: Run unit tests\n  run: ./gradlew test --no-daemon", f"""
- name: Publish test report
  uses: {SHA['upload']}
  if: always()
  with:
    name: test-results
    path: build/reports/tests/test/
"""))
java += "\n  # ─── STAGE 2 (parallel) ───────────────────────────────────────────────────\n"
java += job("sca", "SCA - Dependency Vulnerabilities", steps(co(), f"""
- name: Run Trivy filesystem vulnerability scan
  uses: {SHA['trivy']}
  with:
    scan-type: fs
    scan-ref: .
    scanners: vuln
    severity: CRITICAL,HIGH
    exit-code: 1
"""), needs=STAGE1)
java += semgrep()
write("java-gradle-ci.yaml", java)

# ───────────────────────────── dotnet ─────────────────────────────
dn_in = """      dotnet-version-file:
        type: string
        default: global.json
      solution:
        type: string
        required: true
      project:
        description: Main project (build + vulnerability audit)
        type: string
        required: true
      test-project:
        type: string
        required: true
      test-filter:
        type: string
        default: FullyQualifiedName!~Integration&FullyQualifiedName!~E2E
      semgrep-config:
        type: string
        default: p/csharp p/owasp-top-ten
      semgrep-version:
        type: string
        default: 1.180.0
"""
DN_SETUP = f"""- uses: {SHA['dotnet']}
  with:
    dotnet-version-file: ${{{{ inputs.dotnet-version-file }}}}"""
dn = header(".NET CI", "Reusable pipeline for .NET services (cart-service): format + analyzers, secret scan, build, tests, SCA (dotnet list --vulnerable), SAST (Semgrep).", dn_in)
dn += job("lint-format", "Lint & Format", steps(co(), DN_SETUP, """
- name: Restore tools
  run: dotnet tool restore

- name: Check formatting
  run: dotnet format --verify-no-changes --verbosity diagnostic

- name: Lint (Roslyn Analyzers)
  env:
    SOLUTION: ${{ inputs.solution }}
  run: dotnet build "$SOLUTION" --configuration Release /warnaserror
"""))
dn += secret_scan()
dn += job("build-check", "Build Check", steps(co(), DN_SETUP, """
- name: Build
  env:
    PROJECT: ${{ inputs.project }}
  run: dotnet build "$PROJECT" --configuration Release
"""))
dn += job("unit-tests", "Unit Tests", steps(co(), DN_SETUP, """
- name: Restore dependencies
  env:
    TEST_PROJECT: ${{ inputs.test-project }}
  run: dotnet restore "$TEST_PROJECT"

- name: Run unit tests
  env:
    TEST_PROJECT: ${{ inputs.test-project }}
    TEST_FILTER: ${{ inputs.test-filter }}
  run: |
    dotnet test "$TEST_PROJECT" \\
      --configuration Release \\
      --no-restore \\
      --filter "$TEST_FILTER" \\
      --logger "trx;LogFileName=unit-test-results.trx" \\
      --results-directory ./test-results
""", f"""
- name: Publish test results
  uses: {SHA['reporter']}
  if: always()
  with:
    name: Unit Test Results
    path: test-results/unit-test-results.trx
    reporter: dotnet-trx
"""), perms="contents: read;checks: write # test-reporter publishes a check run")
dn += "\n  # ─── STAGE 2 (parallel) ───────────────────────────────────────────────────\n"
dn += job("sca", "SCA - Dependency Vulnerabilities", steps(co(), DN_SETUP, """
- name: Restore dependencies
  env:
    PROJECT: ${{ inputs.project }}
  run: dotnet restore "$PROJECT"

- name: Check for vulnerable packages
  env:
    PROJECT: ${{ inputs.project }}
  run: |
    dotnet list "$PROJECT" package --vulnerable --include-transitive 2>&1 | tee vuln-report.txt
    if grep -q "has the following vulnerable packages" vuln-report.txt; then
      echo "FAIL: vulnerable packages found"
      cat vuln-report.txt
      exit 1
    fi
    echo "PASS: no vulnerable packages found"
"""), needs=STAGE1)
dn += semgrep()
write("dotnet-ci.yaml", dn)
