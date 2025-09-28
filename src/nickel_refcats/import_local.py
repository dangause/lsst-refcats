# src/nickel_refcats/import_local.py
from __future__ import annotations
import subprocess
from pathlib import Path

def _find_export_dir(unpacked_root: Path) -> Path:
    # Look for a directory with a 'registry' folder (typical butler export structure)
    for d in unpacked_root.iterdir():
        if d.is_dir() and (d / "registry").exists():
            return d
    # Fallback: find first directory containing 'datasets.yaml' or similar
    for d in unpacked_root.iterdir():
        if d.is_dir() and any(p.name.startswith("datasets") for p in d.glob("*")):
            return d
    raise RuntimeError("Could not locate export directory after untar.")

def import_bundle(repo_local: str, bundle_tgz: str, chain_to: str | None = None) -> str | None:
    """
    Import a monster_bundle.tgz into a local Butler repo and optionally chain a collection.
    Returns the RUN collection chained (if any).
    """
    repo = Path(repo_local); repo.mkdir(parents=True, exist_ok=True)
    bundle = Path(bundle_tgz).resolve()
    unpack_root = bundle.parent

    subprocess.check_call(["tar", "-xzf", str(bundle), "-C", str(unpack_root)])
    export_dir = _find_export_dir(unpack_root)

    # Import registry and (if present) files referenced inside export_dir
    subprocess.check_call(["butler", "import", str(repo), str(export_dir)])

    # Some exports include a manifest requiring 'ingest-files'. If it exists, run it.
    file_manifest = export_dir / "file_manifest.yaml"
    if file_manifest.exists():
        subprocess.check_call(["butler", "ingest-files", str(repo), str(export_dir)])

    run_to_chain = None
    if chain_to:
        out = subprocess.check_output(["butler", "query-collections", str(repo)]).decode()
        runs = [ln.split()[0] for ln in out.splitlines() if ln.startswith("import/")]
        if runs:
            run_to_chain = runs[-1]
            subprocess.check_call([
                "butler", "collection-chain", str(repo),
                chain_to, "--mode=REPLACE", run_to_chain
            ])
            print(f"[chain] {run_to_chain} → {chain_to}")
    return run_to_chain
