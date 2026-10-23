# platform-demo-workflows

<div class="pd-hero" markdown>
<span class="pd-kicker">platform · shared CI/CD</span>

Reusable GitHub Actions workflows shared by the platform-demo repositories. Services call these instead of carrying their own
copy of a 300-line pipeline, so a fix or a version bump lands once. Current release: **v3.5.0** (see the version history). Callers pin the release they use by commit SHA: the developer portal calls v3.0.3; the service repositories call `service-ci-<language>` and `service-publish` (v3.5.0 after the roll-out) or still pin an older release until they are re-pinned.
</div>

<div class="pd-facts" markdown>
<div><span>Kind</span><strong>Reusable workflows</strong><small>workflow_call</small></div>
<div><span>Pinned by</span><strong>commit SHA</strong><small>every caller</small></div>
<div><span>Runs on</span><strong>pull requests</strong><small>and v* tags</small></div>
<div><span>Secret scan</span><strong>gitleaks CLI</strong><small>free, checksum-pinned</small></div>
</div>

<div class="pd-cards" markdown>
<div><h4><a href="calling/">Calling the workflows</a></h4><p>The shape of ci.yaml (with the merge job) and publish.yaml in a service.</p></div>
<div><h4><a href="build-once/">Build once per tree</a></h4><p>One image per source tree, the merge button, the publish graph.</p></div>
<div><h4><a href="release-model/">Release model</a></h4><p>Trunk-based, semver tags, digest promotion.</p></div>
<div><h4><a href="changing/">Changing a workflow</a></h4><p>The procedure that worked, step by step.</p></div>
<div><h4><a href="things-to-know/">Things to know</a></h4><p>Constraints of the free plan and of Actions.</p></div>
</div>

