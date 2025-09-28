# src/nickel_refcats/export_rsp.py
from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Iterable, Optional

def _butler_export_copy(butler, outdir: Path, refs: Iterable, repo_uri: str | None = None, transfer: str = "copy"):
    """
    Robustly call Butler.export with transfer support across Butler/RemoteButler variants.
    Always use keyword args; try multiple param-name combos.

    Attempts (in order):
      1) repo_uri + (outdir|directory) + (refs|datasets|datasetRefs)
      2)        -  (outdir|directory) + (refs|datasets|datasetRefs)
    """
    out = str(outdir)
    last_err: Exception | None = None

    # helper
    def _try(**kwargs):
        return butler.export(**kwargs)

    # with repo_uri (newer RemoteButler often expects this)
    if repo_uri is not None:
        for outkey in ("outdir", "directory"):
            for refkey in ("refs", "datasets", "datasetRefs"):
                try:
                    return _try(repo_uri=repo_uri, **{outkey: out, refkey: refs}, transfer=transfer)
                except TypeError as e:
                    last_err = e

    # without repo_uri (older local Butler or older RemoteButler)
    for outkey in ("outdir", "directory"):
        for refkey in ("refs", "datasets", "datasetRefs"):
            try:
                return _try(**{outkey: out, refkey: refs}, transfer=transfer)
            except TypeError as e:
                last_err = e

    if last_err:
        raise last_err
    raise RuntimeError("Butler.export() failed: no compatible signature found.")

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
    candidates = [
        "refcats/DM-49042/the_monster_20250219",
        "LSSTComCam/DP1",
    ]
    for coll in candidates:
        try:
            bb = b.__class__("dp1", collections=coll)
            dts = list(bb.registry.queryDatasetTypes(dataset_type))
            if dts:
                return coll
        except Exception:
            pass
    # broader scan
    tried = set()
    for coll in b.registry.queryCollections("*refcat*"):
        name = getattr(coll, "name", str(coll))
        if name in tried:
            continue
        tried.add(name)
        try:
            bb = b.__class__("dp1", collections=name)
            if list(bb.registry.queryDatasetTypes(dataset_type)):
                return name
        except Exception:
            continue
    raise SystemExit(f"Could not find a collection with dataset type '{dataset_type}' in DP1.")

def export_monster_htm7(
    repo: str,                # "dp1" on the RSP (recommended)
    collections: str | None,  # if None → auto-detect
    htm7_csv: str | None = None,
    htm7_file: str | None = None,
    use_stdin: bool = False,
    dataset_type: str = "the_monster_20250219",
    out_dir: str = "monster_export",
    tar_path: str = "monster_bundle.tgz",
) -> str:
    # 1) read HTM7 list
    ids_csv = _read_htm7_csv(htm7_csv, htm7_file, use_stdin)
    ids = [int(x) for x in ids_csv.split(",") if x.strip()]
    if not ids:
        raise SystemExit("No HTM7 ids provided.")

    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    tar = Path(tar_path)

    # 2) open Butler
    import lsst.daf.butler as dafButler
    b = dafButler.Butler(repo)

    # 3) collection resolution
    coll = collections or _auto_find_monster_collection(b, dataset_type)
    b = dafButler.Butler(repo, collections=coll)

    # 4) refs for those shards
    where = "htm7 IN (" + ",".join(map(str, ids)) + ")"
    refs = list(b.registry.queryDatasets(dataset_type, where=where).expanded())
    if not refs:
        raise SystemExit(f"No {dataset_type} shards matched your HTM7 list (collection='{coll}').")

    print(f"[export] collection='{coll}' shards={len(refs)} → {out.resolve()}")

    # 5) export with copy (keyword-only, multiple signature support)
    try:
        _butler_export_copy(b, out, refs, repo_uri=(repo if repo == "dp1" else None), transfer="copy")
    except TypeError:
        # fallback if 'copy' unsupported in this build; try 'auto', then 'none'
        try:
            _butler_export_copy(b, out, refs, repo_uri=(repo if repo == "dp1" else None), transfer="auto")
        except TypeError:
            _butler_export_copy(b, out, refs, repo_uri=(repo if repo == "dp1" else None), transfer="none")

    # 6) tar bundle
    tar_abs = _ensure_tar(out, tar)
    print(f"[bundle] {tar_abs}")
    return str(tar_abs)
