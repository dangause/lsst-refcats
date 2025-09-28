# src/nickel_refcats/export_rsp.py
from __future__ import annotations

import inspect
import os
import tarfile
from pathlib import Path
from typing import Iterable, Optional

def _butler_export_copy(butler, outdir: Path, refs: Iterable, repo_uri: Optional[str] = None):
    """
    Call Butler.export with transfer='copy', handling both signature variants:
      - export(repo_or_uri, outdir, refs, transfer=...)
      - export(outdir, refs, transfer=...)
    """
    fn = butler.export
    params = list(inspect.signature(fn).parameters)
    if len(params) >= 4 and params[0].name not in ("outdir", "directory"):
        # Newer signature: export(repo_or_uri, outdir, refs, transfer=...)
        if not repo_uri:
            raise TypeError("This Butler.export signature requires repo_uri (e.g. 'dp1').")
        return fn(repo_uri, str(outdir), refs, transfer="copy")
    # Older signature: export(outdir, refs, transfer=...)
    return fn(str(outdir), refs, transfer="copy")


def _ensure_tar(src_dir: Path, tar_path: Path) -> Path:
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src_dir, arcname=src_dir.name)
    return tar_path


def _read_htm7_csv(htm7_csv: str | None, htm7_file: str | None, use_stdin: bool) -> str:
    if htm7_csv:
        return htm7_csv.strip()
    if use_stdin:
        import sys
        return sys.stdin.read().strip()
    if htm7_file:
        return Path(htm7_file).read_text().strip()
    raise SystemExit("Provide one of --htm7-file, --htm7, or --stdin")


def _auto_find_monster_collection(b, dataset_type: str) -> str:
    """
    Try common DP1 collections; return the first that exposes dataset_type.
    """
    candidates = [
        "refcats/DM-49042/the_monster_20250219",
        "LSSTComCam/DP1",
    ]
    for coll in candidates:
        try:
            bb = b.__class__("dp1", collections=coll)  # re-scope
            dts = list(bb.registry.queryDatasetTypes(f"*{dataset_type.split('_')[0]}*"))
            if any(dt.name == dataset_type for dt in dts):
                return coll
        except Exception:
            pass
    # Fallback: scan a bit wider for 'refcat' collections and test them
    tried = set()
    for coll in b.registry.queryCollections("*refcat*"):
        name = getattr(coll, "name", str(coll))
        if name in tried:
            continue
        tried.add(name)
        try:
            bb = b.__class__("dp1", collections=name)
            dts = list(bb.registry.queryDatasetTypes(dataset_type))
            if dts:
                return name
        except Exception:
            continue
    raise SystemExit(f"Could not find a collection exposing dataset type '{dataset_type}' in DP1.")


def export_monster_htm7(
    repo: str,                # "dp1" for remote DP1 (RECOMMENDED on RSP)
    collections: str | None,  # if None → auto-discover
    htm7_csv: str | None = None,
    htm7_file: str | None = None,
    use_stdin: bool = False,
    dataset_type: str = "the_monster_20250219",
    out_dir: str = "monster_export",
    tar_path: str = "monster_bundle.tgz",
) -> str:
    """
    Export Monster shards for a set of HTM7 ids and make a tarball.

    Returns the absolute path to the tarball.
    """
    # 1) Resolve HTM7 list
    ids_csv = _read_htm7_csv(htm7_csv, htm7_file, use_stdin)
    ids = [int(x) for x in ids_csv.split(",") if x.strip()]
    if not ids:
        raise SystemExit("No HTM7 ids provided.")

    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    tar = Path(tar_path)

    # 2) Open Butler against DP1 (or local file repo for other use-cases)
    import lsst.daf.butler as dafButler
    b = dafButler.Butler(repo)

    # 3) Find a collection exposing the dataset type if not provided
    coll = collections or _auto_find_monster_collection(b, dataset_type)

    # 4) Re-scope Butler to that collection
    b = dafButler.Butler(repo, collections=coll)

    # 5) Query dataset refs for the selected shards
    where = "htm7 IN (" + ",".join(map(str, ids)) + ")"
    refs = list(b.registry.queryDatasets(dataset_type, where=where).expanded())
    if not refs:
        raise SystemExit(f"No {dataset_type} shards matched your HTM7 list (collection='{coll}').")
    print(f"[export] collection='{coll}' shards={len(refs)} → {out.resolve()}")

    # 6) Export with copy (handles signature variants)
    _butler_export_copy(b, out, refs, repo_uri=(repo if repo == "dp1" else None))

    # 7) Tar for download
    tar_abs = _ensure_tar(out, tar)
    print(f"[bundle] {tar_abs}")
    return str(tar_abs)
