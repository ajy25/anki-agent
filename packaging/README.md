# Build and publish Mac downloads

This page is for maintainers. People installing the app should use the **[Mac setup guide](../docs/MAC_SETUP.md)**.

## Current handoff

- **Version:** 1.3.1, including the fix for the optional-shortcut crash after saving Setup.
- **Prepared locally:** `dist/Anki-Agent-1.3.1-macOS-arm64-preview.dmg`, its app ZIP, and its JSON manifest.
- **Architecture:** Apple silicon (`arm64`).
- **Signing:** the preview has an ad-hoc signature and is not notarized by Apple.
- **Publication:** no GitHub release has been published yet. The README and Mac setup guide state this explicitly; update their availability text when publishing.

**The preview can be shared as a GitHub Release asset.** An Apple Developer membership is not required to upload or share it. Recipients may need the per-app **Open Anyway** steps in the setup guide. Developer ID signing and notarization improve first-open behavior; they are an optional distribution improvement, not a condition for sharing the preview.

## Publish a download people can actually install

1. Test the installer on a separate Mac or clean user account, using the [user setup guide](../docs/MAC_SETUP.md). Check the first-open prompts, key entry and testing, profile/deck selection, saving Setup, asking a question, quitting, and reopening with saved settings. Record the actual macOS version and chip tested. Automated package checks do not replace this first-run check.
2. Open the repository's **[Releases page](https://github.com/ajy25/anki-agent/releases)** and choose **Draft a new release**. Choose the source revision used to build the app and a matching version tag. For this build, use a preview title such as **Anki Agent 1.3.1 — Mac preview** and mark it as a **pre-release**.
3. Attach these files from `dist/` to the release's assets:

   ```text
   Anki-Agent-1.3.1-macOS-arm64-preview.dmg
   Anki-Agent-1.3.1-macOS-arm64-preview.zip
   Anki-Agent-1.3.1-macOS-arm64-preview.json
   ```

4. In the release notes, identify **Apple silicon**, the macOS version actually tested, the preview's lack of Apple notarization, and the setup-save crash fix. Link the **[Mac setup guide](https://github.com/ajy25/anki-agent/blob/main/docs/MAC_SETUP.md)**. Check that this link exists on the published branch before using it.
5. Update the download status in the root README, `docs/MAC_SETUP.md`, and this page. Link to the actual release or asset. Publish those docs and the source revision for the built app, then publish the release.
6. Check the release while signed out, if the repository is intended to be public. Confirm that the `.dmg` is downloadable and that the setup-guide link works. Send users the release or setup-guide link, rather than GitHub's **Code → Download ZIP**.

`dist/` is ignored by Git. Pushing the source code does **not** upload an installer. Attach the `.dmg` to a Release; do not commit API keys, `.env`, collections, or personal settings. A public download contains the app, not the maintainer's cards or credentials. Each recipient supplies their own key and deck.

## Build a standalone preview

Build on a Mac of the target architecture with Python 3.12:

```sh
python3 -m venv .venv-build
source .venv-build/bin/activate
python -m pip install -r packaging/requirements-macos.txt
python packaging/build_macos.py
```

The output is a standalone `Anki Agent.app` inside a drag-to-Applications DMG, a ZIP of the app, and a JSON manifest containing the architecture, SHA-256 checksums, notarization status, and smoke-test results. Python, native WebKit support, UI assets, and inference dependencies are included. Anki collections, caches, `.env`, settings, and API keys are excluded.

Build the Apple silicon app on an arm64 Mac; do not rename it as universal. The manual **Build Mac downloads** GitHub workflow uploads preview artifacts. Workflow artifacts are build outputs, not a user-facing GitHub Release; inspect and test them, then attach the installers to a release.

The package smoke test runs the built executable with an empty data directory, checks setup routes and packaged assets, indexes a synthetic collection, changes the selected deck, and verifies the original collection is unchanged. It makes no model requests and uses an in-memory test credential rather than Keychain. Test actual first-open behavior and Keychain prompts separately on the intended Mac configurations.

## Optional: Developer ID signing and Apple notarization

The default build uses an ad-hoc signature and has `-preview` in its filename. Label this status clearly when sharing. Direct recipients to the setup guide's per-app security approval steps; do not tell them to disable Gatekeeper or strip quarantine.

For a signed and notarized build, a maintainer needs an Apple Developer ID Application certificate in their signing keychain and a saved `notarytool` credential profile. Configure those using Apple's [Developer ID](https://developer.apple.com/developer-id/) and [notarization documentation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution), then set the two names (these names themselves are not passwords):

```sh
export MACOS_CODESIGN_IDENTITY='Developer ID Application: Your Name (TEAMID)'
export MACOS_NOTARY_PROFILE='AnkiAgentNotary'
python packaging/build_macos.py --require-notarization
```

The builder signs the app, submits it to Apple, staples and validates the app's notarization ticket, creates the installer, signs and notarizes the DMG, and checks Gatekeeper assessment. `--require-notarization` fails before building if the signing configuration is absent. This flag selects the notarized build path; it does not prevent distributing a default preview build. Certificates and credentials are never copied into the app. The manifest explicitly records notarization status.

Test each supported architecture before publishing it, and state the macOS versions actually checked. The repository workflow builds previews; it does not publish releases or provision signing credentials.
