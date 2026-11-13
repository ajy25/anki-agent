# GitHub Pages website

The public site is **https://ajy25.github.io/anki-agent/**. It keeps a simple demo-and-download layout, with release status, a short feature list, expandable search guidance, and a combined pricing table for both AI services. The demo is labeled as version 1.3.1.

## Edit the site

- `site/public/index.html` — page content and layout.
- `site/public/style.css` — responsive styling.
- `site/public/demo.mp4` — a short recording of the actual app using a sample deck.
- `site/public/demo-poster.webp` and `demo.vtt` — a real app screenshot and walkthrough captions.
- `site/DEMO.md` — recording provenance and update notes.
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

The **Website** workflow builds and deploys changes to `site/`, `VERSION`, or its workflow on `main`. After publishing, editing, or deleting a release, refresh its download link using **Actions → Website → Run workflow** on **main**. Deployments run from `main`, as required by the Pages environment; release tags do not deploy directly.

GitHub Pages must use **GitHub Actions** as its publishing source. The workflow uploads only `build/site`; app code, credentials, Anki data, and local installers are not part of the site. Pages deployment uses GitHub's temporary deployment token, without a personal token or custom domain.

The builder reads GitHub's release metadata during deployment. It selects the newest published release with a matching Apple silicon DMG, including public pre-releases and excluding drafts. Before a public installer exists, the button links to the Releases page. Once an installer is published, it links directly to that asset. The release status, download version, and setup text follow the published installer metadata. Rebuild the site after publishing a new release to refresh them. No GitHub API request or token is needed in a visitor's browser.

Website checks also run in CI for pull requests and pushes to `main`. They verify internal anchors, relative asset paths, demo media, and release selection. The site needs no client-side JavaScript; the demo uses the browser's standard video controls. The page uses relative paths so it works under GitHub Pages' `/anki-agent/` project path.
