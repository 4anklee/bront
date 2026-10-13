# Migrating to Bront

The project is now named **Bront** throughout its distribution metadata, import
package, CLI, reference configuration, and deployment examples. This is a breaking
cleanup of the unreleased MVP; compatibility aliases were intentionally removed.

| Previous | Current |
| --- | --- |
| Python distribution `inference-guard` | `bront` |
| Python package `inference_guard` | `bront` |
| `inferenceguard` executable | `bront` |
| Gateway modules at the package root | `bront.reference` |
| `IG_*` reference environment variables | `BRONT_*` |
| `inferenceguard_*` reference metrics | `bront_*` |
| `inference-guard` kind cluster / namespace / image prefix | `bront` |
| `compose.benchmark.yaml` | `compose.scenarios.yaml` |

Run `uv sync --frozen` in the checkout to replace the old editable distribution and
entry point. Update custom imports and scripts. The canonical gateway entry point
is `bront.reference.gateway:app`; the mock is `bront.reference.mock_backend:app`.
Core wheel installs require only the runner dependencies. Install `bront[reference]`
from your locally built wheel when launching the reference services; source-checkout
dev dependencies already include them.

The old `benchmark`, `status`, and persistent `fault` commands were specific to the
gateway demonstration. Use TOML suites with `bront run`, then `compare` and `report`.
Reusable fault scenarios own their cleanup instead of requiring a separate reset.
Inspect the optional gateway's `/status` endpoint or dashboard for observed circuit
state. Its mock `/faults` API remains only as a fixture for integration tests.

The old video, generated demonstration reports, recording script, and bespoke
rolling-update script were removed with the duplicate workflow. Their originals
remain in Git history. Current Kubernetes suites and the in-cluster run Job use
the generic runner; the old compact benchmark-v2 JSON is not silently converted.
Existing Bront schema-3 runs and schema-1 series/comparisons can still be rendered.
Source/package fingerprints change, so collect both comparison groups with the
same renamed version before making a new release comparison.

## Local deployment migration

Rename reference settings in `.env` from `IG_` to `BRONT_`, preserving your chosen
values and ports. Compose's default project name is now `bront`. Stop an older
project using its original project name before starting the new one on the same
ports; renaming source files does not rename or stop running containers.

Likewise, existing Kubernetes resources are not migrated automatically. The new
manifests target namespace `bront`, context `kind-bront`, and image `bront:local`.
Create the new disposable cluster with the Kubernetes guide, or explicitly adapt
suite context/namespace fields to an existing cluster. Do not apply new manifests
assuming they will replace resources in the old namespace. Existing fault controls
and running experiments must finish or be restored before switching configuration.

The GitHub repository name and local checkout directory can be renamed separately.
The package works regardless of the checkout directory's name. No Git remote,
GitHub repository, running stack, or registry publication is changed by this commit.
