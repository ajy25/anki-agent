# GitHub Pages website

The public site is **https://ajy25.github.io/anki-agent/**. It introduces the Mac app, provides an interactive sample using illustrative cards, and links to setup instructions, downloads, and support.

## Edit the site

- `site/public/index.html` — page content and layout.
- `site/public/style.css` — responsive styling.
- `site/public/app.js` — sample Search/Tutor tabs, sample-card searches, and citations. The website does not run inference or accept API keys.
- `site/public/icon.svg` — app logo.
- `site/build.py` — dependency-free Python builder and internal-link checks.
- `site/test_build.py` — tests for public download selection and build validation.

Run locally from the repository root:

```sh
python3 -m unittest discover -s site -p 'test_*.py' -v
python3 site/build.py
python3 -m http.server 8000 --directory build/site
```

Open **http://localhost:8000**. A local build without release metadata shows the upcoming-download state. To preview an available download, save the public releases API response to a JSON file and pass `--releases /path/to/releases.json` to the builder.

## Deployment and downloads

The **Website** workflow builds and deploys changes to `site/`, `VERSION`, or its workflow on `main`. It also rebuilds when a release is published, edited, or deleted, and can be started manually from **Actions → Website → Run workflow**. It always builds the current `main` site, including when triggered by a release from an older tag.

GitHub Pages must use **GitHub Actions** as its publishing source. The workflow uploads only `build/site`; app code, credentials, Anki data, and local installers are not part of the site. Pages deployment uses GitHub's temporary deployment token, without a personal token or custom domain.

The builder reads GitHub's release metadata during deployment. It selects the newest published release with a matching Apple silicon DMG, including public pre-releases and excluding drafts. Before a public installer exists, it says the first download is coming soon and links to the Releases page. Once an installer is published, the site links directly to that asset. No GitHub API request or token is needed in a visitor's browser.

Website checks also run in CI for pull requests and pushes to `main`. They verify internal anchors, relative asset paths, release selection, and JavaScript syntax. The page uses relative paths so it works under GitHub Pages' `/anki-agent/` project path.
