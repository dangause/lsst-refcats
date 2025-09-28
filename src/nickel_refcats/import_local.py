from __future__ import annotations
import subprocess
from pathlib import Path

def import_bundle(repo_local: str, bundle_tgz: str, chain_to: str | None = None) -> str | None:
    """
    Import a monster_bundle.tgz into a local Butler repo and optionally chain a collection.
    Returns the RUN collection chained (if any).
    """
    repo = Path(repo_local); repo.mkdir(parents=True, exist_ok=True)
    bundle = Path(bundle_tgz)
    subprocess.check_call(["tar", "-xzf", str(bundle), "-C", str(bundle.parent)])

    # Find export dir (commonly 'monster_export') and files dir
    export_dir = None
    for d in bundle.parent.iterdir():
        if d.is_dir() and (d/"registry").exists():
            export_dir = d; break
    files_dir = bundle.parent/"monster_files"

    if not export_dir:
        raise RuntimeError("Could not locate export directory after untar.")

    subprocess.check_call(["butler", "import", str(repo), str(export_dir)])
    if files_dir.exists():
        subprocess.check_call(["butler", "ingest-files", str(repo), str(files_dir)])

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
    return run_to_chain
