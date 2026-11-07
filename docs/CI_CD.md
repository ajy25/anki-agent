# Automated checks and releases

## What runs automatically

Every pull request and push to `main` runs **CI**:

The independent **Website checks** job also tests public download selection, builds the static site, checks internal links, and validates the website's JavaScript. The separate **Website** workflow deploys the site to GitHub Pages; see [website maintenance](WEBSITE.md).

1. **Tests (macOS):** installs the pinned Mac environment, checks dependencies and the version file, runs the Python suite, checks JavaScript syntax, and runs the browser tests.
2. **Mac installer:** runs only after the tests pass. Builds the Apple silicon app, verifies its signature, checks that private files are excluded, and smoke-tests setup, indexing, deck changes, and read-only collection access with synthetic data. It then verifies the installer checksums and source revision.
3. The verified `.dmg`, app `.zip`, and JSON manifest are saved together as **Anki-Agent-macOS-arm64-preview** in the run's **Artifacts** for 14 days.

Open **[Actions → CI](https://github.com/ajy25/anki-agent/actions/workflows/ci.yml)** to inspect a run. Failed tests prevent the packaging job from running. A packaging or verification failure fails CI; it does not produce a release.

The checks need no Groq key, Anki profile, or signing credentials. Model calls are mocked and sample collections are generated in temporary folders. Python 3.12 and Node 24 are selected explicitly; Mac dependencies are pinned in `packaging/requirements-macos.txt`. The optional source-only OpenAI provider is outside this packaged Groq test environment.

Older CI runs on the same branch or pull request are canceled when a newer run starts. Jobs have time limits and cache downloaded Python packages. The workflows run on GitHub-hosted machines, not a developer's Mac.

## Build on demand

Use **[Actions → Build Mac downloads](https://github.com/ajy25/anki-agent/actions/workflows/macos-build.yml) → Run workflow** to test and package a selected branch without making a release. It calls the same checks as CI, so the manual and automatic builds stay aligned.

Workflow artifacts require GitHub access and expire. Share a published **Release asset** with app users, rather than a workflow artifact or the source-code ZIP.

## Prepare a release

`VERSION` in the repository root is the single source for the app's version, installer filename, and release validation. It contains a number such as `1.3.1`. The PyInstaller spec and build script both read it.

1. Finish the changes on `main`, set `VERSION` to the intended release number, and update `packaging/RELEASE_NOTES.md` with the changes and macOS version actually tested. Update download-availability wording in the README and Mac setup guide as appropriate for publication.
2. Commit and push those changes. Confirm that **CI** is green for that commit.
3. Create and push a matching version tag on that commit. For example, if `VERSION` is `1.3.2`:

   ```sh
   git tag -a v1.3.2 -m "Anki Agent 1.3.2"
   git push origin v1.3.2
   ```

4. **[Mac release draft](https://github.com/ajy25/anki-agent/actions/workflows/release.yml)** checks that the tag matches `VERSION`, reruns the same tests and package build at that tag, and downloads the resulting artifacts. Before attaching them, it checks their version, architecture, source commit, smoke-test results, byte sizes, and SHA-256 checksums again.
5. If all jobs pass, the workflow creates a **draft pre-release** with the DMG, ZIP, manifest, and release notes. Open **[Releases](https://github.com/ajy25/anki-agent/releases)**, check the notes, and test the attached installer on the intended Mac configuration. Then click **Publish release** when ready to make it downloadable.

Pushing normal commits does not publish an app. No release tag is created automatically. A mismatched tag or failed check stops the release workflow. It does not overwrite an existing release; use a new version for a replacement build. A rerun that encounters an existing draft needs maintainer attention rather than silently replacing its downloads.

The current automated workflow produces an Apple silicon preview with an ad-hoc signature. Apple notarization is not configured or required to create that preview. For the separate signed/notarized build path, see the [packaging instructions](../packaging/README.md#optional-developer-id-signing-and-apple-notarization).

## Permissions and maintenance

- Test and build jobs have read-only repository access. Only the final release-draft job receives `contents: write`, and its token is supplied only to the release command. Checkout does not persist Git credentials.
- Pull requests run through `pull_request`, with no inherited application or signing secrets. There is no privileged `pull_request_target` workflow.
- Actions are pinned to exact commit revisions. Dependabot proposes monthly updates for Actions and the pinned Mac dependencies; those proposals run through the same CI checks.
- To require green checks before merging, configure a GitHub rule for `main` requiring **Tests (macOS)**, **Mac installer**, and **Website checks**. Workflow files do not enable branch protection themselves; choose the repository's merge policy in **Settings → Rules → Rulesets** after the checks have appeared.
- CI smoke tests do not exercise real inference, a real user's Keychain, or every first-open security prompt. Test those user-facing steps before publishing a release.

## Run the checks locally

On an Apple silicon Mac, create a Python 3.12 environment, install `packaging/requirements-macos.txt`, and use Node 24 for the browser checks:

```sh
python -m pip check
python packaging/release_checks.py
python -m unittest discover -s tests -v
node --check ankiagent/static/app.js
node --check ankiagent/static/setup.js
node --test tests/test_tutor_ui.cjs
python packaging/build_macos.py --output-dir /tmp/anki-agent-ci-dist
python packaging/release_checks.py --artifacts /tmp/anki-agent-ci-dist
```

Use a fresh output directory for each build; the release verifier rejects unexpected files, including older installers. To validate workflow edits locally, run [actionlint](https://github.com/rhysd/actionlint) against `.github/workflows/*.yml`.
