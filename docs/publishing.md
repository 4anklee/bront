# Publishing Bront

Publishing a GitHub Release triggers `.github/workflows/publish.yml`. The release
tag must be `v` followed by the exact version in `pyproject.toml`, for example
`v0.1.0`. Editing the version, pushing a commit or tag, and saving a draft release
do not publish to PyPI. Publishing either a stable release or a prerelease does.

The workflow checks the version and lockfile, runs the complete existing CI
(including Python 3.12–3.14, Compose, and proxy scenarios), then builds the wheel
and source archive. It installs the release wheel in an isolated environment and
runs the external endpoint smoke checks. Only after these succeed does a separate
job download the built distributions and upload them to PyPI. Build artifacts are
retained for 30 days. Only the publishing job has OIDC token permission.

## One-time setup

1. Push the workflow and documentation to the GitHub repository's default branch.
2. In GitHub **Settings → Environments**, create an environment named `pypi`.
   If you restrict deployment branches/tags, allow release tags such as `v*`.
3. Sign in to PyPI with a verified email and two-factor authentication enabled.
   If Bront is not published yet, open
   [account publishing settings](https://pypi.org/manage/account/publishing/) and
   add a pending publisher. If it already exists under your account, use the
   project's **Publishing** settings to add a trusted publisher instead.
4. Select GitHub and enter:

   | Field | Value |
   | --- | --- |
   | PyPI project name (pending publisher only) | `bront` |
   | GitHub owner | `4anklee` |
   | Repository | `bront` |
   | Workflow filename | `publish.yml` |
   | Environment | `pypi` |

Use the current repository name on GitHub, even if the local directory or remote
URL still uses the old name. The workflow filename is just `publish.yml`, not its
full path. No PyPI API token or GitHub repository secret is needed.

A pending publisher creates the project on the first successful upload; it does
not reserve the package name. See PyPI's instructions for
[new projects](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [existing projects](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Release a version

For the first unpublished release, keep the current `0.1.0`. For a later patch:

```sh
uv version --bump patch
git add pyproject.toml uv.lock
git commit -m "chore: bump release version"
git push
```

In GitHub **Releases → Draft a new release**, create a tag matching the version
(`v0.1.0` for the first release, `v0.1.1` for the next patch). Target the commit
containing that version and the publishing workflow, write release notes, and
select **Publish release**. Watch **Actions → Publish to PyPI** for the result.
The workflow reruns CI on the release commit, even if branch CI already passed.

For a prerelease, use a Python prerelease version such as `0.2.0rc1` and the exact
tag `v0.2.0rc1`; marking a GitHub release as a prerelease alone does not change the
package version or how PyPI treats it.

After successful publication, install from PyPI:

```sh
uv tool install bront
bront --help
```

To replace an existing local installation with the published first release:

```sh
uv tool install --force --refresh bront==0.1.0
```

## Failures and retries

- A version/tag mismatch stops before CI or publishing. Create the release with
  the matching tag and version.
- If tests or build checks fail, nothing is uploaded. Fix the code and release the
  corrected commit.
- If authentication fails, check the owner, repository, filename, and environment
  against the PyPI publisher configuration, then rerun the failed publishing job.
- If an upload partially succeeds, rerun the failed publishing job so it uses the
  same retained build artifact. uv skips already uploaded identical files.
  Changed files require a new version; PyPI does not allow overwriting a release.

See the [uv publishing documentation](https://docs.astral.sh/uv/guides/package/)
for upload and retry behavior.
